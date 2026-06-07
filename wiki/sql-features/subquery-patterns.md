---
title: "Subquery Patterns"
aliases:
  - "subquery usage"
  - "correlated subquery"
  - "scalar subquery"
  - "derived table"
source_files:
  - src/backend/optimizer/plan/subselect.c
  - src/backend/executor/nodeSubplan.c
  - src/backend/optimizer/prep/prepjointree.c
  - src/include/nodes/primnodes.h
symbols:
  - make_subplan
  - build_subplan
  - convert_EXISTS_sublink_to_join
  - convert_ANY_sublink_to_join
  - SS_process_sublinks
  - SS_process_ctes
  - SubLink
  - SubPlan
---

# Subquery Patterns

Subqueries let you express multi-step logic inside a single SQL statement: filter by a computed set, produce a per-row value inline, or build a virtual table without materialising a CTE. For how PostgreSQL classifies and transforms subqueries internally — flattening, InitPlan vs. SubPlan, semi-join conversion — see [[subsystems/planner/subqueries]].

## Subqueries by Position

**Scalar subquery** — a subquery in the `SELECT` list or in a `WHERE` condition that must return exactly one column and at most one row. If it returns more than one row PostgreSQL raises a runtime error. If it returns zero rows, the result is `NULL`.

```sql
-- Per-row lookup in the SELECT list
SELECT o.id,
       (SELECT name FROM customers WHERE id = o.customer_id) AS customer_name
FROM orders o;
```

A correlated scalar subquery (one that references outer columns) executes once per outer row. For large result sets this becomes an O(N) sequence of index lookups — acceptable when the outer set is small, a potential performance trap otherwise. An uncorrelated scalar subquery executes once. Its result is then reused for every outer row; see [[subsystems/planner/subqueries]] for how the planner handles each case.

**Subquery in FROM (derived table)** — a subquery wrapped in parentheses and given an alias in the `FROM` clause. The planner treats its output as a virtual table.

```sql
SELECT region, total
FROM (
    SELECT region, sum(amount) AS total
    FROM sales
    GROUP BY region
) regional_totals
WHERE total > 10000;
```

Derived tables are scoped to the enclosing query. They cannot be referenced elsewhere. Unlike a CTE, a simple derived table lets the planner optimise the whole query as a unit. Subqueries with aggregation, `DISTINCT`, `LIMIT`, or window functions always execute independently.

**Subquery in WHERE with IN / EXISTS / ANY / ALL** — tests whether each outer row satisfies a condition against a set produced by the subquery. These are the forms most aggressively transformed by the planner; see [[subsystems/planner/in-vs-exists-vs-join]] for the full transformation logic.

## EXISTS vs IN vs JOIN — the Practical Choice

All three can express "rows from A that have a related row in B," but they differ in NULL handling, duplicate semantics, and how readily the planner optimises them.

**EXISTS** is the clearest semi-join expression. It returns true as soon as one matching row is found; no rows from the subquery appear in the output. The `SELECT` list inside the subquery is irrelevant — `SELECT 1` is idiomatic.

```sql
SELECT o.id, o.total
FROM orders o
WHERE EXISTS (
    SELECT 1 FROM order_items i WHERE i.order_id = o.id
);
```

EXISTS handles NULLs safely: it tests row existence, not value equality. The planner converts correlated `EXISTS` to a `JOIN_SEMI` when it can. This gives efficient "stop on first match" execution.

**IN with a subquery** is rewritten to a semi-join in most cases, making it equivalent in performance to `EXISTS`. The difference emerges with `NOT IN`.

**JOIN with DISTINCT** is explicit and readable, but risks row multiplication if the inner side can return multiple rows per outer row. You then need `DISTINCT` or `GROUP BY` to collapse them. That adds a sort or hash step. Prefer `EXISTS` or `IN` unless you actually need columns from the joined table.

### NOT IN and the NULL trap

This is the most consequential correctness difference. `NOT IN` uses three-valued SQL logic: for each outer row, PostgreSQL checks whether the value is not equal to every value returned by the subquery. If the subquery returns any `NULL`, the comparison `x <> NULL` yields `UNKNOWN`. PostgreSQL then suppresses the outer row.

```sql
-- Dangerous: if any order_id is NULL in cancelled_orders,
-- this returns zero rows even for orders that were never cancelled.
SELECT id FROM orders
WHERE id NOT IN (SELECT order_id FROM cancelled_orders);
```

```sql
-- Safe: NOT EXISTS tests row existence, not value equality.
SELECT id FROM orders o
WHERE NOT EXISTS (
    SELECT 1 FROM cancelled_orders c WHERE c.order_id = o.id
);
```

```sql
-- Also safe: make the NULL exclusion explicit.
SELECT id FROM orders
WHERE id NOT IN (
    SELECT order_id FROM cancelled_orders
    WHERE order_id IS NOT NULL
);
```

The `NOT EXISTS` form is almost always the right default. It expresses intent clearly and handles NULLs correctly. The planner also converts it to an anti-join (`JOIN_ANTI`) for efficient execution.

## Correlated Subqueries

A correlated subquery references columns from the outer query. That reference forces re-evaluation: the subquery runs once per outer row, using the current row's values as parameters.

```sql
-- Correlated: runs once per order row
SELECT o.id
FROM orders o
WHERE (SELECT count(*) FROM order_items i WHERE i.order_id = o.id) > 5;
```

Correlated subqueries are perfectly fine when the outer result set is small. They become a problem at scale because total cost grows as O(outer rows × inner cost). Two rewrites address this:

- Pre-aggregate the inner table and JOIN the result (eliminates the correlation entirely).
- Use `LATERAL` to express the correlation in the `FROM` clause, where the planner can apply index-based access paths per outer row (see [[sql-features/lateral]]).

When the planner cannot flatten a correlated subquery it becomes a SubPlan node — visible in `EXPLAIN` as `SubPlan N` with a `loops=` count equal to the number of outer rows. That `loops=` number is the clearest signal that a rewrite is worth considering.

## ANY and ALL

`ANY` and `ALL` generalise `IN` to arbitrary comparison operators.

```sql
-- True if the salary is above at least one department average
WHERE salary > ANY (SELECT avg(salary) FROM dept_salaries GROUP BY dept_id)

-- True if the salary is above every department average
WHERE salary > ALL (SELECT avg(salary) FROM dept_salaries GROUP BY dept_id)
```

`= ANY(subquery)` is exactly equivalent to `IN (subquery)`, including the same planner transformation to a semi-join. `<> ALL(subquery)` is exactly equivalent to `NOT IN (subquery)`, including the same NULL semantics — if the subquery can return NULLs, `<> ALL` silently suppresses outer rows.

For non-equality operators (`>`, `<`, `>=`, `<=`) the planner cannot always convert to a join; the subquery may execute as an InitPlan (uncorrelated) or SubPlan (correlated).

## Subquery in FROM vs CTE vs LATERAL

These three forms look similar but have different scoping and optimisation properties.

**Derived table** (`FROM (SELECT ...) alias`): scoped to the enclosing query, evaluated inline. The planner can see through it and may flatten it into the outer join tree. No reuse.

**CTE** (`WITH name AS (SELECT ...)`): named and reusable within the query. In PostgreSQL 12 and later the planner inlines a non-recursive CTE that is referenced exactly once. It does not inline the CTE if the CTE is marked `MATERIALIZED`. A CTE marked `MATERIALIZED` (or inferred to require it) creates an optimisation fence: the subquery executes once and its result is buffered. This prevents predicate pushdown from the outer query. See [[sql-features/ctes]].

**LATERAL**: allows a subquery in the `FROM` clause to reference columns from earlier `FROM` items. LATERAL is the right tool when you need a per-row operation more complex than a simple lookup — for example, returning the top-N related rows per outer row. The planner can apply index paths on the inner side of a nested-loop LATERAL. This makes it more flexible than a correlated scalar subquery. See [[sql-features/lateral]].

```sql
-- LATERAL: the inner query references o.id from the outer FROM clause
SELECT o.id, recent.amount
FROM orders o,
LATERAL (
    SELECT amount FROM order_items
    WHERE order_id = o.id
    ORDER BY created_at DESC
    LIMIT 1
) recent;
```

## Subqueries in UPDATE and DELETE

Subqueries appear frequently in data modification statements. Two patterns are worth distinguishing.

**Correlated subquery in SET**: assigns a computed value from a related table.

```sql
UPDATE orders o
SET total = (
    SELECT sum(price * qty) FROM order_items WHERE order_id = o.id
);
```

This works but executes the scalar subquery once per updated row. For bulk updates it is often slower than the UPDATE with FROM pattern below.

**UPDATE with FROM** (PostgreSQL extension): joins the target table to a source table or subquery in the `FROM` clause and uses the join result to drive both the filter and the assignment. This is the preferred pattern for bulk updates from another table.

```sql
-- Pre-aggregate in a subquery, then join and assign in one pass.
UPDATE orders o
SET total = s.computed_total
FROM (
    SELECT order_id, sum(price * qty) AS computed_total
    FROM order_items
    GROUP BY order_id
) s
WHERE o.id = s.order_id;
```

The aggregation runs once; the update then applies a single join rather than one subquery execution per row. For large tables the performance difference is significant.

`DELETE` supports a similar `USING` clause for the same pattern:

```sql
DELETE FROM order_items oi
USING cancelled_orders c
WHERE oi.order_id = c.id;
```

## Common Anti-Patterns

**NOT IN against a nullable column.** The NULL trap described above is easy to miss because the query runs without error — it just returns fewer rows than expected, sometimes zero. Whenever the subquery touches a column without a `NOT NULL` constraint, use `NOT EXISTS`.

**Scalar subquery in SELECT that can return multiple rows.** PostgreSQL raises `ERROR: more than one row returned by a subquery used as an expression` at runtime, not at parse time. Defensive options: add `LIMIT 1` (make sure the ordering is deterministic), add a `WHERE` clause that guarantees uniqueness, or rewrite as a `LEFT JOIN`.

```sql
-- Fragile: errors if a customer has more than one active contract
SELECT c.name, (SELECT contract_id FROM contracts WHERE customer_id = c.id) AS cid
FROM customers c;

-- Safer as a LEFT JOIN (also avoids per-row execution)
SELECT c.name, ct.contract_id
FROM customers c
LEFT JOIN contracts ct ON ct.customer_id = c.id AND ct.status = 'active';
```

**Correlated subquery in SELECT list on a large table.** Each row in the outer result triggers a separate execution of the inner query. A window function, a pre-aggregated `LEFT JOIN`, or a `LATERAL` subquery will usually replace this pattern with a single pass over the inner table.

```sql
-- Anti-pattern: subquery runs once per customer row
SELECT c.id,
       (SELECT count(*) FROM orders WHERE customer_id = c.id) AS order_count
FROM customers c;

-- Preferred: aggregate once, join once
SELECT c.id, coalesce(oc.order_count, 0) AS order_count
FROM customers c
LEFT JOIN (
    SELECT customer_id, count(*) AS order_count
    FROM orders
    GROUP BY customer_id
) oc ON oc.customer_id = c.id;
```

## Related Topics

- [[subsystems/planner/subqueries|Planner: Subqueries]] — covers how the planner classifies and executes subqueries as InitPlan, SubPlan, or flattened joins
- [[subsystems/planner/in-vs-exists-vs-join|IN vs EXISTS vs JOIN]] — documents the semijoin and antijoin transformations that determine execution strategy for set-membership tests
- [[sql-features/ctes|CTEs]] — explains CTE inlining, materialisation fences, and when a WITH clause behaves differently from a derived table
- [[sql-features/lateral|LATERAL]] — details the LATERAL join form that replaces correlated scalar subqueries in the SELECT list with indexed per-row lookups
- [[subsystems/executor/subplan-nodes|Subplan Nodes]] — describes how the executor runs SubPlan and InitPlan nodes and how to read their cost in EXPLAIN
- [[subsystems/planner/anti-patterns|Planner Anti-Patterns]] — broader catalogue of query shapes that defeat optimisation, including correlated subquery pitfalls
- [[code-paths/simple-select|Simple SELECT Code Path]] — traces the full pipeline from parse through plan to execution for SELECT statements containing subqueries

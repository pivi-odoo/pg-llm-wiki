---
title: "Common Table Expressions (WITH clauses)"
aliases:
  - "CTEs"
  - "WITH queries"
  - "WITH RECURSIVE"
source_files:
  - src/backend/parser/parse_cte.c
symbols:
  - transformWithClause
  - analyzeCTE
  - analyzeCTETargetList
  - checkWellFormedRecursion
  - TopologicalSort
  - CommonTableExpr
  - CTEMaterialize
---

# Common Table Expressions (WITH clauses)

A Common Table Expression (CTE) is a named subquery in a `WITH` clause, placed before the main `SELECT`, `INSERT`, `UPDATE`, or `DELETE`. It lets you name an intermediate result and reference it one or more times within the same statement. For how PostgreSQL decides whether to inline or materialize a CTE — including the `CTEMaterialize` enum and the planner's decision criteria — see [[subsystems/planner/ctes]].

## Basic syntax

```sql
WITH
  active_users AS (
    SELECT id, name FROM users WHERE status = 'active'
  ),
  recent_orders AS (
    SELECT user_id, count(*) AS order_count
    FROM orders
    WHERE created_at > now() - interval '30 days'
    GROUP BY user_id
  )
SELECT u.name, coalesce(o.order_count, 0) AS orders
FROM active_users u
LEFT JOIN recent_orders o ON o.user_id = u.id;
```

Multiple CTEs are separated by commas. A CTE may reference any CTE that appears earlier in the same `WITH` clause, but not later ones — the definitions are ordered. The main query (and any CTE that comes after) can reference any previously defined CTE by name.

## The materialization question

Since PostgreSQL 12, the default is:

- **Inline** if the CTE is non-recursive, contains no volatile functions, contains no DML, and is referenced exactly once.
- **Materialize** if the CTE is referenced more than once (to avoid re-evaluating the body at each site), is recursive, contains volatile functions, or contains DML.

### Overriding the default

PostgreSQL 12 added explicit keywords to override this logic:

```sql
-- Force materialization even for a singly-referenced CTE:
WITH summary AS MATERIALIZED (
  SELECT region, sum(revenue) FROM sales GROUP BY region
)
SELECT * FROM summary WHERE region = 'west';
-- The WHERE is applied after the full aggregation runs; no predicate pushdown.

-- Force inlining even when referenced twice:
WITH filtered AS NOT MATERIALIZED (
  SELECT * FROM events WHERE user_id = $1
)
SELECT * FROM filtered WHERE type = 'login'
UNION ALL
SELECT * FROM filtered WHERE type = 'logout';
-- Each branch can use an index on (user_id, type) independently.
```

**Use `MATERIALIZED` when:**
- The CTE contains a volatile function (e.g., `random()`, `now()` in a context where you want a stable value) and you need it evaluated exactly once.
- You want a planning fence to force a stable intermediate result — for example, when statistics on a complex subquery are unreliable and the planner is making bad cardinality estimates downstream.
- You intentionally want to compute an expensive aggregation once and scan it twice, rather than running the aggregation twice.

**Use `NOT MATERIALIZED` when:**
- The CTE is referenced multiple times but each reference applies a highly selective filter — inlining lets each site use its own index.
- The outer query's predicates would eliminate most of the CTE's rows; materializing first means computing those rows for nothing.

**Avoid relying on the implicit fence behavior** (pre-PG 12 style). If you need a fence, say so explicitly with `MATERIALIZED`. Silent fences cause surprises during upgrades and code review.

You can confirm what the planner did with `EXPLAIN`. A materialized CTE appears as an `InitPlan` block and a `CTE Scan` node. An inlined CTE leaves no trace under its name — look for a `Subquery Scan` or notice that the CTE name is absent.

For the planner's internal decision logic and the `CTEMaterialize` enum, see [[subsystems/planner/ctes]].

## Recursive CTEs

`WITH RECURSIVE` allows a CTE to reference itself, making it the standard tool for tree and graph traversal in SQL. The body must have exactly two terms combined with `UNION` or `UNION ALL`:

- **Anchor term** (non-recursive): provides the starting rows. Runs once.
- **Recursive term**: references the CTE by name, processing the rows produced in the previous iteration. Runs repeatedly until it produces no new rows.

```sql
-- Traverse an org chart from a given employee downward
WITH RECURSIVE subordinates AS (
  -- anchor: start at the target employee
  SELECT id, name, manager_id, 1 AS depth
  FROM employees
  WHERE id = $1

  UNION ALL

  -- recursive term: find direct reports of the previous generation
  SELECT e.id, e.name, e.manager_id, s.depth + 1
  FROM employees e
  JOIN subordinates s ON e.manager_id = s.id
)
SELECT id, name, depth
FROM subordinates
ORDER BY depth, name;
```

### UNION ALL vs UNION

Prefer `UNION ALL` unless you specifically need duplicate elimination. `UNION` tracks every row ever emitted across all iterations in a hash table and discards any row already seen. This deduplication is expensive and memory-intensive. For tree traversals where the data structure guarantees no node is visited twice, `UNION ALL` is both correct and much faster.

Use `UNION` only when the graph may naturally produce the same row via multiple paths and you want exactly one occurrence in the output.

### Cycle detection

For graphs that may contain cycles, PostgreSQL 14 introduced the `CYCLE` clause:

```sql
WITH RECURSIVE graph_walk AS (
  SELECT src, dest, 1 AS depth FROM edges WHERE src = $1
  UNION ALL
  SELECT e.src, e.dest, w.depth + 1
  FROM edges e
  JOIN graph_walk w ON e.src = w.dest
)
CYCLE dest SET is_cycle USING path
SELECT * FROM graph_walk WHERE NOT is_cycle;
```

`CYCLE dest SET is_cycle USING path` instructs PostgreSQL to track visited values of `dest`. When a repeated value is encountered, PostgreSQL sets `is_cycle = true` and stops that branch. `path` records the traversal path as an array.

On PostgreSQL 13 and earlier, add a depth limit manually:

```sql
WHERE w.depth < 50
```

Without a guard on cyclic graphs, the recursive term keeps producing rows indefinitely until the query is cancelled or the server exhausts resources.

### LIMIT is safe with recursive CTEs

PostgreSQL stops iterating as soon as the outer query's `LIMIT` is satisfied. A query like `SELECT ... FROM subordinates LIMIT 10` will not compute all reachable nodes before returning.

## Data-modifying CTEs

CTEs can contain `INSERT`, `UPDATE`, and `DELETE` statements. `RETURNING` makes the changes visible to the rest of the query.

```sql
-- Atomic move: delete from queue and insert into archive in one statement
WITH dequeued AS (
  DELETE FROM job_queue
  WHERE id = (SELECT id FROM job_queue ORDER BY priority DESC LIMIT 1)
  RETURNING *
)
INSERT INTO job_archive
SELECT *, now() AS archived_at FROM dequeued;
```

Important semantics:

- **All DML in a `WITH` clause runs at the same command snapshot.** A `DELETE` in one CTE does not affect what a `SELECT` in another CTE within the same statement sees. The entire `WITH` block reads a consistent snapshot from the start of the statement.
- **PostgreSQL commits side effects together** with the outer statement. If the outer statement is inside a transaction that rolls back, all CTE DML rolls back too.
- **DML CTEs are always materialized.** They cannot be inlined. The `RETURNING` rows are the only way to pass their output into the rest of the query.

A common pattern is the conditional upsert helper or the "select-then-act" sequence where you need the affected rows for a subsequent join:

```sql
WITH updated AS (
  UPDATE accounts
  SET balance = balance - $1
  WHERE id = $2 AND balance >= $1
  RETURNING id, balance
)
INSERT INTO transactions (account_id, amount, new_balance, ts)
SELECT id, $1, balance, now()
FROM updated;
-- If the UPDATE matched nothing (balance too low), the INSERT also inserts nothing.
```

## CTEs vs subqueries vs temporary tables

These three constructs overlap in capability. Choose based on your actual needs:

| Situation | Reach for |
|---|---|
| Single-use intermediate result, simple case | Inline subquery — least overhead, cleanest optimizer visibility |
| Named intermediate result for readability, referenced once | CTE (will be inlined by default in PG 12+, equivalent to subquery) |
| Intermediate result referenced multiple times | CTE with default or explicit `MATERIALIZED` |
| Very large intermediate result needing custom indexing or statistics | Temporary table — lets you `CREATE INDEX` and `ANALYZE` before the main query |
| Recursive traversal | `WITH RECURSIVE` — no equivalent in plain subqueries |
| Atomic multi-step DML | Data-modifying CTE — cleaner than chained statements |

Temporary tables add session overhead and require explicit DDL. They are the right tool when the intermediate result is large enough to need its own physical layout and statistics, independent from the query that produces it. See [[subsystems/planner/temp-tables-vs-ctes|Temp Tables vs CTEs]] for a deeper comparison of statistics availability, catalog overhead, and `ON COMMIT` cleanup semantics.

## Practical notes

- **EXPLAIN is your friend.** Always verify whether a CTE was inlined or materialized. Unexpected materialization is a common cause of query slowdowns, especially in code migrated from PostgreSQL 11 or earlier where CTEs were intentionally used as fences.
- **Reference count determines the default.** Adding a second reference to a previously inlined CTE switches it to materialized automatically. This can be a surprising performance change if the CTE is large.
- **Volatile functions block inlining.** A CTE containing `random()`, `clock_timestamp()`, `nextval()`, or any other volatile function is never inlined by default (and cannot be forced inline with `NOT MATERIALIZED` either). The planner does not attempt predicate pushdown into such CTEs.

For how the planner implements inlining and materialization internally, including the `CteScan`, `RecursiveUnion`, and `WorkTableScan` plan nodes, see [[subsystems/planner/ctes]].

## Related Topics

- [[subsystems/planner/ctes|Planner: CTEs]] — internal implementation of CTE inlining, materialization decisions, and the CteScan, RecursiveUnion, and WorkTableScan plan nodes.
- [[subsystems/planner/temp-tables-vs-ctes|Temp Tables vs CTEs]] — statistics, catalog overhead, and ON COMMIT comparison between CTEs and temporary tables.
- [[subsystems/planner/recursive-queries|Planner: Recursive Queries]] — how the planner handles WITH RECURSIVE, iteration control, and work-table scan mechanics.
- [[subsystems/planner/subqueries|Planner: Subqueries]] — covers subquery flattening and how the planner treats inlined CTEs identically to plain subqueries.
- [[subsystems/planner/optimization-fences|Planner: Optimization Fences]] — explains how materialized CTEs act as planning barriers and when to use them deliberately.
- [[subsystems/executor/subquery-values-worktable-scan|Executor: Subquery, Values, and WorkTable Scans]] — runtime execution of CTE scan nodes including the tuplestore-backed WorkTableScan used in recursive queries.
- [[subsystems/executor/tuplestore|Executor: Tuplestore]] — the tuplestore abstraction that backs materialized CTEs and stores intermediate rows during recursive iteration.
- [[sql-features/subquery-patterns|Subquery Patterns]] — contrasts CTEs with correlated and uncorrelated subqueries, covering when each form is preferable.

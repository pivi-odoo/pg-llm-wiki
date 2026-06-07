---
title: "LATERAL"
aliases:
  - "LATERAL subquery"
  - "LATERAL join"
  - "top-N per group"
  - "lateral SRF"
source_files:
  - src/backend/parser/parse_clause.c
  - src/backend/parser/parse_relation.c
  - src/backend/optimizer/plan/initsplan.c
  - src/backend/optimizer/path/joinpath.c
  - src/backend/optimizer/path/joinrels.c
  - src/include/nodes/pathnodes.h
  - src/include/nodes/parsenodes.h
symbols:
  - find_lateral_references
  - create_lateral_join_info
  - extract_lateral_references
  - check_lateral_ref_ok
  - setNamespaceLateralState
  - RelOptInfo
  - RangeTblEntry
---

`LATERAL` marks a subquery or function in the `FROM` clause as allowed to reference columns from earlier items in the same `FROM` list. This turns it into a correlated table expression whose inputs change per outer row. For how the planner models these dependencies and why `LATERAL` always produces a nested-loop join, see [[subsystems/planner/lateral-joins]].

## Syntax

The `LATERAL` keyword appears immediately before the subquery or function in the `FROM` clause:

```sql
SELECT u.name, recent.title
FROM users u,
     LATERAL (
         SELECT title
         FROM posts
         WHERE user_id = u.id
         ORDER BY created_at DESC
         LIMIT 3
     ) AS recent;
```

You can also use explicit `JOIN` syntax, which is cleaner when you want `LEFT JOIN` semantics:

```sql
SELECT u.name, recent.title
FROM users u
JOIN LATERAL (
    SELECT title
    FROM posts
    WHERE user_id = u.id
    ORDER BY created_at DESC
    LIMIT 3
) AS recent ON true;
```

PostgreSQL infers `LATERAL` on a subquery that already references an earlier `FROM` item, even without the keyword. The keyword is required, though, before function calls that take arguments from earlier `FROM` items. Writing it explicitly is clearer and avoids surprises.

## Top-N Per Group

The canonical reason to reach for `LATERAL`. Fetching the three most recent posts per user using a plain join or window function materialises every candidate row before filtering. `LATERAL + LIMIT` lets the planner seek into an index and stop after N rows:

```sql
SELECT u.id, u.name, recent.title, recent.created_at
FROM users u
JOIN LATERAL (
    SELECT title, created_at
    FROM posts
    WHERE user_id = u.id
    ORDER BY created_at DESC
    LIMIT 3
) AS recent ON true;
```

For this to be fast, the `posts` table needs a composite index leading with `user_id` and including `created_at DESC`. That lets the planner satisfy the `ORDER BY … LIMIT` without a sort node:

```sql
CREATE INDEX ON posts (user_id, created_at DESC);
```

With that index the planner seeks to each user's slice of the index and reads at most three entries — regardless of how many total posts that user has. Without the index, every outer row drives a sequential scan of the entire `posts` table.

A correlated scalar subquery in the `SELECT` list cannot do this: it returns a single value. `LATERAL` is the right tool when you need multiple columns or multiple rows per outer row.

## Set-Returning Functions with Row Context

Functions that expand a value into multiple rows — `unnest`, `jsonb_each`, `string_to_table`, custom SRFs — become useful in `FROM` with `LATERAL` when the value to expand comes from a column in the same row:

```sql
-- Flatten a tags array column into one row per tag
SELECT t.id, t.name, tag.value
FROM topics t,
     LATERAL unnest(t.tags) AS tag(value);
```

```sql
-- Expand a JSONB metadata column into key-value pairs
SELECT doc.id, kv.key, kv.value
FROM documents doc,
     LATERAL jsonb_each_text(doc.metadata) AS kv;
```

Each invocation of the SRF receives the column value from the current outer row. The `LATERAL` keyword is technically optional for SRFs that reference an earlier `FROM` item (PostgreSQL infers it), but writing it makes the dependency explicit.

`unnest` with `WITH ORDINALITY` preserves the original position of each element:

```sql
SELECT d.id, t.tag, t.pos
FROM data d,
     LATERAL unnest(d.tags) WITH ORDINALITY AS t(tag, pos);
```

## Reusing a Computed Expression

If the outer `SELECT` needs to reference the same complex expression multiple times, wrapping it in a `LATERAL` subquery names it once. This avoids repeating the computation or relying on the planner to recognise the duplicate:

```sql
SELECT p.id,
       calc.score,
       calc.score * p.weight AS weighted_score,
       calc.score > 100      AS above_threshold
FROM products p,
     LATERAL (
         SELECT some_expensive_function(p.attributes) AS score
     ) AS calc;
```

Without `LATERAL`, you would have to repeat `some_expensive_function(p.attributes)` in every place it appears, or push it into a CTE. That may introduce a materialisation fence.

## LATERAL vs. Correlated Subqueries in SELECT

A correlated subquery in the `SELECT` list and a `LATERAL` subquery in `FROM` both reference outer columns, but they serve different purposes:

| | Correlated SELECT subquery | LATERAL subquery |
|---|---|---|
| Position | `SELECT` list, `WHERE`, `HAVING` | `FROM` clause |
| Returns | One scalar value | A table: zero-to-many rows, multiple columns |
| Can carry `LIMIT` | No | Yes |
| Multiple output columns | No | Yes |

Use `LATERAL` when you need more than one output column, more than one output row, or a `LIMIT` applied per outer row. A correlated scalar subquery is fine for a single lookup value where you are certain exactly one row matches.

## LEFT JOIN LATERAL for Optional Results

When the lateral subquery might return zero rows and you still want the outer row to appear in the result — with `NULL` for the lateral columns — use `LEFT JOIN LATERAL … ON true`:

```sql
SELECT o.id, o.total, last_pay.amount, last_pay.paid_at
FROM orders o
LEFT JOIN LATERAL (
    SELECT amount, paid_at
    FROM payments
    WHERE order_id = o.id
    ORDER BY paid_at DESC
    LIMIT 1
) AS last_pay ON true;
```

`ON true` is required. There is no natural join condition because the correlation is already expressed inside the subquery body. Omitting it causes a syntax error. Use `CROSS JOIN LATERAL` (or the comma shorthand) when the subquery is guaranteed to return at least one row and dropping outer rows with no matches is acceptable.

## Performance Characteristics

PostgreSQL always executes `LATERAL` as a nested loop: the lateral subquery runs once per row of the driving table. This is not a planner heuristic — it is structurally required, since Hash Join and Merge Join build their inner side once and cannot re-execute it per outer row. Practical consequences for query design:

- Keep the driving table small, or ensure it is well-filtered before the lateral join, since cost is O(outer_rows × inner_cost_per_row).
- The lateral subquery must use an index on its correlated column. A sequential inner scan is almost always a performance defect for a non-trivial outer table. Confirm with `EXPLAIN (ANALYZE, BUFFERS)` that the inner side shows an Index Scan or Index Only Scan.
- For the top-N pattern, a composite index `(join_column, order_column DESC)` lets the planner satisfy the `ORDER BY … LIMIT` directly from the index without a sort node.
- PostgreSQL 14 introduced the `Memoize` node. It caches recent inner results when the lateral parameters repeat across outer rows. Check `EXPLAIN ANALYZE` for hit/miss ratios when outer rows carry repetitive parameter values.
- Avoid lateral subqueries inside a CTE that is referenced multiple times. The CTE fence prevents the planner from pulling the lateral reference up. This can add repeated materialisation on top of the per-row nested loop cost.

For how the planner represents these dependencies internally — `RelOptInfo.lateral_relids`, `LateralJoinInfo`, and the join-ordering constraints they impose — see [[subsystems/planner/lateral-joins]].

## Related Topics

- [[subsystems/planner/lateral-joins|Lateral Joins (Planner)]] — internal planner data structures, join ordering constraints, and `LateralJoinInfo` that back the SQL-level `LATERAL` feature
- [[sql-features/subquery-patterns|Subquery Patterns]] — correlated subqueries, `EXISTS`, scalar subqueries, and when to prefer them over `LATERAL`
- [[subsystems/executor/joins|Joins]] — executor-level join strategies; explains why `LATERAL` is always a nested loop and how that shapes its cost profile
- [[subsystems/executor/memoize|Memoize]] — the Memoize node introduced in PostgreSQL 14 that caches lateral subquery results when outer-row parameters repeat
- [[sql-features/window-functions|Window Functions]] — alternative to `LATERAL` for top-N and ranked-row queries; understand the trade-offs in materialisation and index usage
- [[subsystems/executor/set-returning-functions|Set-Returning Functions]] — how SRFs like `unnest` and `jsonb_each` are executed, relevant to `LATERAL` usage that expands array or JSONB columns
- [[subsystems/planner/join-method-selection|Join Method Selection]] — how the planner picks nested loop, hash join, or merge join, and why lateral dependencies force nested loop

---
title: SQL Anti-Patterns for Performance
aliases:
  - SQL Performance Anti-Patterns
  - Query Anti-Patterns
tags:
  - symptom/slow-query
  - theme/query-optimization
source_files:
  - src/backend/optimizer/path/indxpath.c
  - src/backend/optimizer/prep/prepjointree.c
  - src/backend/optimizer/plan/subselect.c
symbols:
  - predicate_implied_by
  - convert_ANY_sublink_to_join
  - match_index_to_operand
---

# SQL Anti-Patterns for Performance

Ten recurring query patterns that silently destroy PostgreSQL performance, with mechanistic explanations and verified fixes. Each pattern is described from the planner's perspective so you understand not just what to change but why the original form fails.

---

## SELECT *

**Why it hurts:** `SELECT *` expands to every column in the relation at parse time. The planner cannot use index-only scans (IOS) because IOS requires that all projected columns be covered by the index. Even if a perfect covering index exists, a star projection forces a heap fetch for every row. Wide rows also inflate shared buffer consumption — a 200-byte row tuple in an 8 kB page holds 40 rows; if you only need two 4-byte integers, you waste 98 % of every page read. Network bandwidth and client-side memory follow the same amplification.

**Fix:** Name columns explicitly.

```sql
-- bad
SELECT * FROM orders WHERE customer_id = 42;

-- good: enables index-only scan on (customer_id, status, total)
SELECT status, total FROM orders WHERE customer_id = 42;
```

---

## Function Call on Indexed Column in WHERE

**Why it hurts:** The planner's `match_index_to_operand` checks whether the WHERE clause operand matches an index attribute directly or via an expression index. `WHERE date_trunc('day', created_at) = '2024-01-01'` wraps the indexed column inside a function, so the operand is the *result* of `date_trunc`, not `created_at` itself. No plain btree index on `created_at` satisfies this; the planner falls back to a sequential scan with the function applied to every row.

**Fix option A — range rewrite** (zero DDL cost):

```sql
-- bad
WHERE date_trunc('day', created_at) = '2024-01-01'

-- good
WHERE created_at >= '2024-01-01' AND created_at < '2024-01-02'
```

**Fix option B — expression index** (if the exact function call must remain):

```sql
CREATE INDEX ON events (date_trunc('day', created_at));
```

The expression index stores the pre-computed value. `match_index_to_operand` recognises it as an exact match. The planner can then seek into it.

---

## Implicit Type Cast on Indexed Column

**Why it hurts:** PostgreSQL's type-coercion rules resolve `varchar_col = 123` by casting the *column* side to integer (or vice versa depending on operator resolution). PostgreSQL applies the cast per-row, making the column effectively wrapped in an implicit function — same mechanism as anti-pattern 2. The btree index on the raw `varchar` values is useless.

**Fix:** Cast the literal to match the column type.

```sql
-- bad: implicit cast may break index use
WHERE account_code = 9900

-- good
WHERE account_code = '9900'

-- or explicit, self-documenting
WHERE account_code = 9900::text
```

Always verify with `EXPLAIN` that an `Index Scan` or `Index Only Scan` appears.

---

## OFFSET for Pagination

**Why it hurts:** `LIMIT 20 OFFSET 10000` tells the executor to materialise and discard 10 000 rows before returning 20. The cost is O(offset), so page 500 of a result set is 500× more expensive than page 1. On large tables this manifests as linear scan times that grow with user scroll depth.

**Fix:** Keyset (cursor) pagination anchors the scan at the last-seen primary key.

```sql
-- bad
SELECT id, title FROM posts ORDER BY id LIMIT 20 OFFSET 10000;

-- good: constant cost regardless of page number
SELECT id, title FROM posts
WHERE id > $last_seen_id
ORDER BY id
LIMIT 20;
```

The `WHERE id > $last_seen_id` predicate drives an index seek to exactly the right position; only 20 rows are fetched. For multi-column sort keys, use a row-value comparison: `WHERE (created_at, id) > ($last_ts, $last_id)`.

---

## NOT IN with Nullable Subquery

**Why it hurts:** Three-valued logic. `x NOT IN (a, b, NULL)` reduces to `x <> a AND x <> b AND x <> NULL`. The last conjunct is always `NULL` (not `TRUE`), so the entire predicate is `NULL` — falsy — for every outer row. The result set is empty even though logically some rows should match. The `convert_ANY_sublink_to_join` transformation in `subselect.c` cannot safely pull a nullable subquery into an anti-join without adding a null guard, so it may also inhibit join planning.

**Fix A — NOT EXISTS** (semantically correct, anti-join plan):

```sql
-- bad
WHERE id NOT IN (SELECT manager_id FROM employees)

-- good
WHERE NOT EXISTS (
    SELECT 1 FROM employees WHERE manager_id = e.id
)
```

**Fix B — explicit IS NOT NULL filter in subquery:**

```sql
WHERE id NOT IN (
    SELECT manager_id FROM employees WHERE manager_id IS NOT NULL
)
```

`NOT EXISTS` is preferred because it expresses intent clearly and the planner reliably chooses a hash anti-join.

---

## Leading Wildcard LIKE

**Why it hurts:** Btree indexes store values in sorted order. A prefix scan (`name LIKE 'foo%'`) can seek to the first matching key. A leading wildcard (`name LIKE '%foo'`) has no usable prefix — the planner cannot bound the scan and must read every index entry. That is slower than a sequential scan on wide tables. `predicate_implied_by` cannot derive useful index conditions from a leading-wildcard clause.

**Fix options:**

```sql
-- bad
WHERE name LIKE '%foo'

-- option 1: trigram GIN index (handles arbitrary substrings)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ON products USING gin (name gin_trgm_ops);
-- original query now uses the GIN index

-- option 2: reverse-string suffix index for pure suffix searches
CREATE INDEX ON products (reverse(name));
WHERE reverse(name) LIKE reverse('%foo')  -- becomes 'oof%', prefix-safe

-- option 3: full-text search for natural language
CREATE INDEX ON articles USING gin (to_tsvector('english', body));
WHERE to_tsvector('english', body) @@ plainto_tsquery('foo')
```

---

## OR Across Different Indexed Columns

**Why it hurts:** `WHERE col_a = 1 OR col_b = 2` involves two independent predicates on columns that may each have an index. A single btree index can satisfy one predicate at a time. The planner may attempt a BitmapOr plan (two index scans whose TID bitmaps are OR-ed before heap fetch), but if row estimates are poor or the table is small, it may fall back to a sequential scan. Complex OR trees defeat `predicate_implied_by`'s ability to prune partitions.

**Fix:** `UNION ALL` with explicit predicates allows each branch to use its own index independently. The planner can then choose the cheapest path per branch.

```sql
-- potentially sub-optimal
SELECT * FROM events WHERE category_id = 5 OR user_id = 99;

-- explicit union: guarantees separate index scans
SELECT * FROM events WHERE category_id = 5
UNION ALL
SELECT * FROM events WHERE user_id = 99 AND category_id <> 5;
```

Use `EXPLAIN` first. BitmapOr is often adequate. The `UNION ALL` rewrite introduces duplication risk if the exclusion predicate is wrong.

---

## Unnecessary DISTINCT

**Why it hurts:** `DISTINCT` forces a sort or hash-aggregate deduplication pass over the entire result set. When it appears because a query happens to return duplicates rather than because the domain requires uniqueness, it is masking a deeper problem — usually a missing join condition or a one-to-many join producing fan-out. The dedup sort is O(N log N). It prevents the planner from using cheaper streaming aggregation.

**Fix:** Diagnose and fix the join.

```sql
-- bad: DISTINCT hiding implicit cross-join fan-out
SELECT DISTINCT u.id, u.name
FROM users u
JOIN orders o ON u.id = o.customer_id;

-- good option 1: use EXISTS to check existence without fan-out
SELECT u.id, u.name
FROM users u
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = u.id);

-- good option 2: aggregate explicitly if you need order data
SELECT u.id, u.name, count(o.id) AS order_count
FROM users u
LEFT JOIN orders o ON u.id = o.customer_id
GROUP BY u.id, u.name;
```

---

## Functions in JOIN Conditions

**Why it hurts:** `ON f(a.col) = f(b.col)` means neither side of the join condition is a bare column reference. The planner cannot use existing btree indexes on `a.col` or `b.col` for a nested-loop index join. Merge join requires sorted input on the join key — it cannot sort on the raw column and satisfy the transformed predicate. The executor is forced into a hash join after applying `f` to every row of both inputs, O(N+M) with no index acceleration.

**Fix:** Create expression indexes that match the join expression, or precompute the value in a generated column.

```sql
-- bad
ON lower(a.email) = lower(b.email)

-- fix: expression indexes on both sides
CREATE INDEX ON users (lower(email));
CREATE INDEX ON invites (lower(email));
-- the planner can now use index-based joins

-- alternative: generated column (PG12+)
ALTER TABLE users ADD COLUMN email_lower text GENERATED ALWAYS AS (lower(email)) STORED;
CREATE INDEX ON users (email_lower);
```

---

## Large CTE Used Multiple Times (Optimization Fences)

**Why it hurts:** Before PostgreSQL 12, every CTE was always materialised — computed once, stored in a tuplestore, and scanned from there on each reference. This was an intentional optimization fence that prevented the planner from pushing predicates into the CTE. Post-PG12, the planner inlines non-recursive, non-volatile CTEs by default (`NOT MATERIALIZED`). However, if the CTE is referenced multiple times and is expensive, inlining causes it to be re-evaluated on each reference. That can be worse than the old materialise-once behaviour.

```sql
-- pre-PG12 fence pattern that now inlines (may re-execute N times)
WITH expensive AS (
    SELECT ... FROM large_table WHERE complex_condition
)
SELECT * FROM expensive e1 JOIN expensive e2 ON ...;

-- explicit materialisation: compute once, reuse
WITH expensive AS MATERIALIZED (
    SELECT ... FROM large_table WHERE complex_condition
)
SELECT * FROM expensive e1 JOIN expensive e2 ON ...;

-- if used only once and selectivity is high, let it inline (default)
WITH recent AS (
    SELECT id FROM orders WHERE created_at > now() - interval '1 day'
)
SELECT * FROM shipments WHERE order_id IN (SELECT id FROM recent);
```

The decision rule: use `MATERIALIZED` when the CTE is referenced more than once and the scan cost dominates predicate pushdown benefit. Use `NOT MATERIALIZED` (or let the planner decide) when used once and push-down of outer predicates improves row estimates.

---

## Practical Guidance

- **Always run EXPLAIN (ANALYZE, BUFFERS)** after any query rewrite. Row estimates, actual rows, and buffer hit counts reveal whether the fix had the intended effect.
- **Check for implicit casts** with `\d table_name` in psql: compare column types against your WHERE literal types. A mismatch almost always breaks index use.
- **Pagination strategy**: keyset pagination requires a stable, unique sort key. For compound sort orders expose a tiebreaker (usually `id`) and use row-value comparisons.
- **NOT IN vs NOT EXISTS**: treat `NOT IN` with subqueries as a code smell. The only safe use is against a statically known list of non-null constants.
- **Trigram indexes** (`pg_trgm`) are effective for LIKE/ILIKE patterns of 3+ characters but have high write amplification on large text columns. Benchmark both directions.
- **CTE materialisation** is a tuning knob, not a default optimisation. Measure; do not assume either direction is universally better.
- Use [[subsystems/observability/pg-stat-statements|pg_stat_statements]] to surface the highest `total_exec_time` queries — anti-patterns cluster in the top 10.

---

## Related Topics

- [[subsystems/planner/selectivity-estimation|Selectivity Estimation]] — understanding how the planner estimates row counts explains why bad estimates lead to the sequential scans and hash joins these anti-patterns produce.
- [[subsystems/planner/in-vs-exists-vs-join|IN vs EXISTS vs JOIN]] — deep dive into the semantic and planning differences between `IN`, `NOT IN`, `EXISTS`, and anti-joins that underlie the nullable-subquery anti-pattern.
- [[subsystems/planner/ctes|CTEs in the Planner]] — covers the materialisation barrier mechanics and when the planner inlines or fences CTEs, complementing the optimization-fence anti-pattern.
- [[subsystems/indexes/index-only-scans|Index-Only Scans]] — explains exactly when IOS is eligible, linking directly to the `SELECT *` and implicit-cast anti-patterns that prevent it.
- [[subsystems/planner/or-clauses|OR Clauses]] — details how the planner handles OR predicates and BitmapOr paths, extending the OR-across-columns anti-pattern discussion.
- [[subsystems/planner/subqueries|Subqueries]] — covers how correlated and uncorrelated subqueries are transformed, relevant to the `NOT IN` / `NOT EXISTS` and CTE anti-patterns.
- [[troubleshooting/slow-queries|Slow Queries]] — practical diagnostic guide for identifying which anti-patterns are active in a running system using `EXPLAIN`, `pg_stat_statements`, and wait events.
- [[subsystems/planner/index-selection|Index Selection]] — covers how the planner generates and costs index paths, the mechanism that the function-wrapping and implicit-cast anti-patterns above silently disable.
- [[subsystems/planner/predicate-pushdown|Predicate Pushdown]] — explains how outer filters move into subqueries and views, background for why materializing or fencing a CTE changes what can be pushed down.
- [[subsystems/planner/scan-selection|Scan Type Selection]] — describes how the planner chooses between sequential, index, bitmap, and index-only scans, the decision most of these anti-patterns end up defeating.
- [[subsystems/planner/optimization-fences|Optimization Fences]] — the general concept of planner boundaries, of which the CTE materialisation barrier discussed above is one instance.
- [[subsystems/indexes/expression-indexes|Expression (Functional) Indexes]] — the fix for the function-call-on-indexed-column anti-pattern, storing precomputed expression results so they can be matched by an index scan.
- [[subsystems/indexes/partial-indexes|Partial Indexes]] — a complementary indexing technique for shrinking index size when only a subset of rows is ever queried, useful alongside the fixes above.

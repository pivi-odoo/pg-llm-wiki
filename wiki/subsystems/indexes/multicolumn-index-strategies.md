---
title: Multicolumn Index Strategies
aliases:
  - Composite Index Strategies
  - Multi-Column Index Design
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/path/indxpath.c
  - src/backend/optimizer/path/costsize.c
  - src/backend/access/nbtree/nbtutils.c
  - src/include/access/nbtree.h
symbols:
  - get_index_paths
  - build_index_pathkeys
  - _bt_preprocess_keys
  - BTScanInsertData
---

A multicolumn (composite) B-tree index stores entries sorted by the first key column, then by the second key column within ties of the first, and so on. This ordering governs every aspect of access efficiency: which predicates can be used for index scans, how far into the key space the scan can skip, and whether the index satisfies an `ORDER BY` without a sort step. Understanding the implications of column ordering is the single most important skill in PostgreSQL index design.

## The Prefix-Matching Rule

The planner in `get_index_paths` (indxpath.c) walks index columns left to right and matches WHERE clauses to index columns via `match_clause_to_index_col`. An index column is *usable* only if all preceding columns are constrained by equality predicates or the scan has reached a range-bounded column that terminates prefix applicability.

Consider `CREATE INDEX idx ON t (a, b, c)`:

| Query predicate | Columns used | Reason |
|---|---|---|
| `WHERE a = 1 AND b = 2 AND c = 3` | a, b, c | Full key equality |
| `WHERE a = 1 AND b = 2` | a, b | Trailing column unconstrained |
| `WHERE a = 1 AND c = 3` | a only | b not constrained; c is not reachable |
| `WHERE b = 2 AND c = 3` | none (pre-PG17) | Leading column a absent |
| `WHERE a > 10` | a (range scan) | Range on leading col terminates prefix |
| `WHERE a = 1 AND b > 10 AND c = 5` | a, b | Range on b terminates prefix; c unused |

`_bt_preprocess_keys` in nbtutils.c consolidates redundant key entries. It also establishes the scan boundaries that `_bt_first` uses to position the scan. Once a range key is encountered, the scan cannot skip to a precise position for subsequent columns, because those columns' values vary across the range.

## Column Ordering Guidelines

**Equality columns before range columns.** A range predicate breaks the prefix chain. Place all equality-constrained columns first so range predicates apply only to the last key column actually used.

**Most-selective equality column first — with caveats.** When multiple equality columns exist, placing the most selective column first reduces the number of index entries examined after the first key descent. The planner consults per-column statistics from `pg_stats` (the `n_distinct` and `most_common_vals`/`most_common_freqs` entries) when estimating selectivity. Columns with high cardinality (low `n_distinct` magnitude relative to row count) prune the search space fastest.

However, if two columns always appear together in queries, selectivity ordering matters less than prefix-matching completeness. A column that appears in some queries without the other should lead, ensuring the index serves both query shapes.

**Range columns last.** A column with range predicates (`>`, `<`, `BETWEEN`, `LIKE 'prefix%'`) should be the last key column in use. Any key columns after it cannot be used to constrain the scan.

```sql
-- Poor: range on a breaks access to b even though b is selective
CREATE INDEX ON orders (ordered_at, customer_id);
-- Query: WHERE ordered_at > now() - interval '7 days' AND customer_id = 42
-- Only ordered_at is used as a scan key; customer_id filtered post-scan.

-- Better: equality column first
CREATE INDEX ON orders (customer_id, ordered_at);
-- Query above now uses both key columns.
```

## Composite Index vs. Two Single-Column Indexes

### When composite wins

A single composite index (`(a, b)`) performs a single B-tree descent to a precise key range. Two separate indexes on `a` and `b` require two index scans and a `BitmapAnd` to intersect the result sets. The overhead includes:

- Two separate index root-to-leaf traversals.
- Two sets of TID bitmap pages allocated in [[subsystems/executor/work-mem-and-spill|work_mem]].
- A bitwise AND pass over those bitmaps.
- Potential multiple heap fetches if the bitmaps are lossy (when they exceed `work_mem`, they degrade to page-granularity lossy bitmaps).

`cost_bitmap_and_node` in costsize.c models this overhead. For highly selective combined predicates, the BitmapAnd plan is measurably more expensive than a single composite scan, particularly in I/O terms because composite index pages covering the joint predicate are fewer and more cache-friendly than two independent index subtrees.

### When two single-column indexes win

Separate indexes serve *different* query patterns independently. If some queries filter only on `a` and others filter only on `b`, a composite `(a, b)` index only serves the first pattern efficiently (leading column). A composite `(b, a)` serves the second. Two separate indexes serve both without duplication. For low-cardinality columns combined via OR rather than AND, the planner may prefer a BitmapOr of two single-column scans over any composite index.

## Index Skip Scan (PostgreSQL 17)

Prior to PostgreSQL 17, if the leading column was unconstrained the index was entirely unusable for inner columns. PostgreSQL 17 introduces **index skip scan** for B-tree indexes: the executor can advance through distinct values of the leading column without a predicate on it, making inner column predicates usable.

```sql
CREATE INDEX ON t (a, b);
-- PG16: full sequential scan
-- PG17: skip scan iterates distinct values of a, using b predicate at each step
SELECT * FROM t WHERE b = 42;
```

Skip scan is efficient only when the leading column has low cardinality (few distinct values). With many distinct values of `a`, the overhead of probing each value of `a` for `b = 42` exceeds a sequential scan. The planner estimates cost using the `n_distinct` statistic for the leading column. The implementation adds synthetic equality keys for the skipped leading column in `_bt_preprocess_keys`, driving multiple sequential sub-scans.

## INCLUDE Columns vs. Key Columns

`INCLUDE (col)` appends columns to B-tree leaf pages without including them in the key tree structure. This enables index-only scans (IOS) for columns that are not needed for ordering or pruning:

```sql
CREATE INDEX ON orders (customer_id, ordered_at) INCLUDE (total_amount);
-- total_amount is on leaf pages; not part of the sort key.
-- A query filtering on (customer_id, ordered_at) and projecting total_amount
-- can be satisfied entirely from the index without heap access.
```

Key differences:

| Aspect | Key column | INCLUDE column |
|---|---|---|
| Participates in prefix matching | Yes | No |
| Widens non-leaf (interior) pages | Yes | No |
| Satisfies `ORDER BY` | Yes | No |
| Enables IOS projection | Yes | Yes |
| Index size growth | All levels | Leaf level only |

Use `INCLUDE` for columns needed only in projections (`SELECT` list), not in predicates or ordering. Adding such columns as key columns unnecessarily widens interior pages, reducing fan-out and increasing tree depth.

## Composite Index and Sort Avoidance

The planner calls `build_index_pathkeys` to determine whether an index scan order matches a required `ORDER BY`. An index satisfies a sort if:

1. The `ORDER BY` columns appear as a *prefix* of the index key columns.
2. Each column's sort direction (ASC/DESC) matches the index definition or its reverse (indexes can be scanned backward).
3. No unordered columns appear between the matched key columns and the sort columns.

```sql
CREATE INDEX ON t (a ASC, b ASC, c ASC);

-- Sort avoidance: yes
SELECT * FROM t WHERE a = 1 ORDER BY b, c;
-- a is equality-constrained; (b, c) prefix of remaining index key.

-- Sort avoidance: yes (backward scan)
SELECT * FROM t ORDER BY a DESC, b DESC, c DESC;

-- Sort avoidance: no
SELECT * FROM t ORDER BY b, c;
-- a is not constrained; cannot use index for sort without skip scan.

-- Sort avoidance: no
SELECT * FROM t WHERE a = 1 ORDER BY c, b;
-- Order of (c, b) does not match index key order (b, c).
```

To handle mixed direction mismatches (`a ASC, b DESC`), define the index with mixed `DESC` modifiers: `CREATE INDEX ON t (a ASC, b DESC)`.

## Inspecting pg_stats to Guide Column Ordering

Before committing to a column order, examine per-column statistics:

```sql
-- Cardinality and common values for candidate index columns
SELECT
    attname,
    n_distinct,
    array_length(most_common_vals::text::text[], 1) AS num_mcv,
    most_common_freqs[1] AS top_freq
FROM pg_stats
WHERE tablename = 'orders'
  AND attname IN ('customer_id', 'status', 'ordered_at')
ORDER BY n_distinct DESC;
```

High `n_distinct` (many distinct values, negative meaning fraction of rows) indicates high selectivity — good leading equality columns. Low `n_distinct` with a high `top_freq` indicates a skewed low-cardinality column. Range predicates on such columns offer poor pruning as leading columns.

Combine cardinality analysis with actual query patterns. A column with moderate selectivity that appears in every query is a better leading column than a highly selective column used in only 10% of queries.

## Practical Guidance

- Start with the access patterns: list all queries the index must serve. Identify which columns appear in equality predicates vs. range predicates vs. projections only.
- Place equality columns before range columns. Among equality columns, lead with those that appear in the most queries, breaking ties by selectivity (higher cardinality first).
- Use `INCLUDE` for projection-only columns (those appearing only in `SELECT`, not `WHERE` or `ORDER BY`) to enable IOS without widening the key tree.
- Do not add a third or fourth key column if queries rarely constrain all of them. Each added key column widens every index page and slows inserts.
- For queries that match different single-column patterns, prefer two narrow indexes over one wide composite index; let the planner choose or combine via BitmapOr.
- After creating an index, verify it is actually used: `EXPLAIN (ANALYZE, BUFFERS)` and check for `Index Scan` vs. `Bitmap Index Scan` vs. `Seq Scan`. Check `Index Cond` to confirm which key columns are constraining the scan vs. which appear under `Filter`.
- On PostgreSQL 17+, reconsider indexes that were previously unusable due to unconstrained leading columns; skip scan may now make them viable for inner-column queries on low-cardinality leading columns.

## Related Topics

- [[subsystems/indexes/btree|B-tree Index Internals]] — the underlying B-tree access method whose prefix-matching semantics and key preprocessing drive all multicolumn scan behavior.
- [[subsystems/planner/index-selection|Index Selection]] — how the planner evaluates and chooses among candidate indexes, including composite index paths, for a given query.
- [[subsystems/planner/bitmap-scans|Bitmap Scans]] — covers BitmapAnd/BitmapOr plans that arise when two single-column indexes are combined instead of a composite index.
- [[subsystems/planner/sort-avoidance|Sort Avoidance]] — explains how the planner matches index key order to ORDER BY requirements, directly relevant to composite index column ordering.
- [[subsystems/indexes/index-only-scans|Index-Only Scans]] — how INCLUDE columns on composite indexes enable projection without heap access.
- [[subsystems/indexes/partial-indexes|Partial Indexes]] — complementary index design technique that can be combined with multicolumn strategies to further narrow scan scope.
- [[subsystems/planner/cost-model|Planner Cost Model]] — the cost functions (including `cost_bitmap_and_node`) that determine when composite indexes beat separate single-column indexes.

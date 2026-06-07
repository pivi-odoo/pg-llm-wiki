---
title: "Index Selection and Index Path Costing"
aliases:
  - "Index Selection"
  - "Index Paths"
  - "btcostestimate"
  - "create_index_paths"
tags:
  - theme/query-optimization
  - symptom/slow-query
source_files:
  - src/backend/optimizer/path/indxpath.c
  - src/backend/utils/adt/selfuncs.c
  - src/backend/access/index/indexam.c
  - src/include/nodes/pathnodes.h
symbols:
  - create_index_paths
  - build_index_paths
  - match_clauses_to_index
  - btcostestimate
  - IndexPath
  - BitmapHeapPath
---

# Index Selection and Index Path Costing

For each base relation, the planner generates one or more `IndexPath` alternatives alongside the sequential scan path. The cost model then picks the cheapest complete plan. Understanding how paths are generated and costed explains why the planner chooses (or ignores) a given index.

## create_index_paths

The planner calls `create_index_paths` (`optimizer/path/indxpath.c`) for each base relation during `add_paths_to_joinrel` / `set_plain_rel_pathlist`. It:

1. Iterates all indexes on the relation (from `RelationGetIndexList` / `IndexOptInfo` array).
2. For each index, calls `build_index_paths` to generate possible index scan paths.
3. Calls `generate_bitmap_or_paths` to consider combining multiple indexes with `BitmapOrPath`.
4. Adds each viable path to the relation's `pathlist`.

## IndexOptInfo

The planner's representation of an index, populated from `pg_index` and `pg_am`:

| Field | Source | Purpose |
|---|---|---|
| `indexoid` | `pg_index.indexrelid` | Index OID |
| `rel` | — | Back-pointer to the relation |
| `ncolumns` | `pg_index.indnatts` | Number of index columns |
| `indexkeys` | `pg_index.indkey` | Attribute numbers of indexed columns |
| `indexcollations` | `pg_index.indcollation` | Collations per column |
| `opfamily` | `pg_index.indclass` | Operator family OID per column |
| `sortopfamily` | — | Sort operator family (for ordered scans) |
| `indpred` | `pg_index.indpred` | Partial index predicate (as Expr list) |
| `amcostestimate` | `pg_am.amcostestimate` | AM-specific cost function |
| `amcanorderbyop` | — | Whether the AM supports ORDER BY operator |
| `amcanbackward` | — | Whether the AM can scan backward |
| `amcanreturn` | — | Whether columns can be returned without heap fetch |

## Matching quals to index columns

`match_clauses_to_index` tests each `RestrictInfo` (WHERE clause predicate) against each index column:

1. For each clause, `match_clause_to_index_col` checks whether the clause's operator is a member of the index column's operator family (`pg_amop`).
2. The clause must reference the index column on one side and a non-index expression on the other.
3. Matched clauses become `IndexClause` entries with `indexcol` (which column) and `lossy` (whether recheck is needed) fields.
4. Unmatched clauses become filter conditions applied after the index scan.

### Multi-column indexes

PostgreSQL can use a multi-column index even if only the leading columns have matching quals. Trailing columns without matching quals contribute no selectivity reduction but the index is still usable. An index on `(a, b, c)` is usable for `WHERE a = 1` or `WHERE a = 1 AND b > 5` but not for `WHERE b = 2` alone (for B-tree; depends on AM).

## Partial index matching

If `IndexOptInfo.indpred` is non-empty, the planner calls `predicate_implied_by(indpred, baserestrictinfo)`. The index is usable only when the query's WHERE clause logically implies the partial index predicate. This avoids adding the partial index path when the predicate is not satisfied.

Example: an index `ON orders (status) WHERE status = 'pending'` is only used when the query has `WHERE status = 'pending'` (or a condition that implies it).

## Index-only scans

An index-only scan avoids touching the heap entirely if all needed columns are available in the index. Two conditions:

1. `amcanreturn` covers every column in the SELECT list and WHERE clause for that index column.
2. The visibility map indicates the target page is all-visible (avoiding a heap fetch to check tuple visibility).

The planner generates a separate `IndexOnlyScan` path when these conditions hold. At runtime, the executor calls `heap_fetch` only for pages where the visibility map bit is clear.

## btcostestimate

The B-tree AM provides `btcostestimate` as its `amcostestimate` function. It computes:

| Output | How computed |
|---|---|
| `indexStartupCost` | Cost to position at the first matching entry (~1 page read per tree level) |
| `indexTotalCost` | `indexStartupCost` + cost to scan the matching leaf pages |
| `indexSelectivity` | Fraction of heap tuples matched; computed by `clauselist_selectivity` using column statistics |
| `indexCorrelation` | From `pg_statistic` `STATISTIC_KIND_CORRELATION`; 1.0 = perfectly sequential, 0.0 = random |

`indexCorrelation` feeds into the cost estimate for heap access. High correlation → sequential I/O pattern → lower effective `random_page_cost`. Low correlation → random I/O → higher cost.

The total cost of an index scan on the heap is:

```
total_cost = indexTotalCost
           + heap_pages_fetched * effective_page_cost
```

where `effective_page_cost` blends `random_page_cost` and `seq_page_cost` based on `indexCorrelation`.

## Bitmap index scans

When an index scan would produce many scattered heap fetches, a **bitmap index scan** may be cheaper:

1. `BitmapIndexPath`: scan the index, build a TID bitmap of matching heap locations.
2. `BitmapHeapPath`: sort the TIDs, then fetch heap pages in order (sequential-ish access pattern).

The planner can combine multiple `BitmapIndexPath` nodes with `BitmapOrPath` or `BitmapAndPath` to union or intersect multiple indexes:

```sql
-- May use bitmap OR of two indexes
SELECT * FROM t WHERE a = 1 OR b = 2;
```

`BitmapAnd`/`BitmapOr` nodes appear in EXPLAIN output as children of `Bitmap Heap Scan`.

## Index ordering and pathkeys

An index scan on `(a ASC, b ASC)` produces tuples in `(a, b)` order. The planner represents this as a **pathkey list** on the `IndexPath`. If the query has `ORDER BY a, b` or a merge join needs tuples in that order, the index scan path avoids a sort step. Its cost advantage then includes the saved sort.

`build_index_pathkeys` constructs the pathkey list from the index's sort operators and collations.

## Reasons the planner skips an expected index

The planner adds an `IndexPath` to the candidate set only when two conditions hold. The index must be *structurally usable* — the WHERE clause matches the index definition. The resulting path must also be *cheaper* than the alternatives. Most surprising index-skip cases fall into one of the categories below.

### Low selectivity

The most common reason. When a predicate matches a large fraction of the table, reading the index first and then chasing each matching TID to its heap page costs more than reading the whole table sequentially. The `random_page_cost / seq_page_cost` ratio governs the break-even point, since every heap fetch during an index scan is a random I/O. With the default values (`random_page_cost = 4.0`, `seq_page_cost = 1.0`), an index scan that must visit more than roughly 25% of heap pages typically loses to a sequential scan.

Concretely, `cost_index` (`costsize.c`) computes two extremes: `max_IO_cost` assuming fully random access, and `min_IO_cost` assuming fully sequential access. It then blends them using `csquared = indexCorrelation²`:

```
run_cost += max_IO_cost + csquared * (min_IO_cost - max_IO_cost)
```

A predicate that selects 40% of a million-row table produces a large `tuples_fetched`, a correspondingly large `pages_fetched`, and a run cost that will almost always exceed the sequential scan's `seq_page_cost * baserel->pages`.

To confirm this is the reason, temporarily set `enable_seqscan = off` in the session. The planner will use the index even when it is cheaper not to. If the plan changes and the query becomes slower, selectivity is the culprit. Reducing `random_page_cost` has the same diagnostic effect. It also reveals what threshold the planner would need to reach a different decision.

### Function wrapping on the column

`WHERE lower(name) = 'alice'` cannot use a plain index on `name`. The index stores the raw column value. The planner cannot invert `lower()` to find a corresponding range in the index. From the planner's perspective, `lower(name)` is a `FuncExpr` node. `match_index_to_operand` (`indxpath.c`) requires the operand to be either a bare `Var` matching the indexed attribute, or an expression tree that is structurally equal (via `equal()`) to a stored index expression. A plain column index satisfies neither test when the column is wrapped in a function.

The fix is an expression index: `CREATE INDEX ON t (lower(name))`. The planner stores the expression tree in `IndexOptInfo.indexprs`. During matching, `match_index_to_operand` walks `indexprs`. It calls `equal()` on the stored expression tree against the query's expression tree. A match occurs only when the exact same expression appears in the WHERE clause. The comparison is structural equality of expression tree nodes, not textual comparison. `LOWER(name)` and `lower(name)` match because the parser normalizes function names, but `lower(name || '')` does not.

### Implicit cast on the column side

If a WHERE clause involves a type mismatch between the column and the constant, the planner must resolve it. The critical question is which side gets the implicit cast. When the planner applies the cast to the constant — `'alice'::varchar` cast to `text` — the indexed column remains a bare `Var`. The index is then usable. When the planner applies the cast to the column — wrapping it in a `CoerceViaIO` or a `FuncExpr` — the column is no longer a bare `Var`. The index match then fails, for the same reason as explicit function wrapping above.

A common instance: `WHERE created_at = '2024-01-01'::date` on a `timestamp` column. Depending on available implicit casts and operator definitions, the planner may resolve this by casting `created_at` to `date`. That defeats any index on `created_at`. The safe pattern is to cast the constant to match the column's type: `WHERE created_at >= '2024-01-01'::timestamp AND created_at < '2024-01-02'::timestamp`.

### Operator family mismatch

The planner associates every index column with an operator family (`IndexOptInfo.opfamily`, drawn from `pg_opclass`). The clause-matching code in `match_opclause_to_indexcol` checks whether the query's operator is a member of that operator family via `op_in_opfamily`. If it is not, the planner cannot use the clause as an index qual.

The practical consequence: a default B-tree index on a `text` column uses `text_ops`, whose operator family covers the standard comparison operators (`=`, `<`, `>`, etc.) under the database collation. `LIKE 'foo%'` uses a different operator (`~~`) that belongs to the `text_pattern_ops` operator family, not `text_ops`. The planner correctly refuses to use a `text_ops` index for a pattern-match predicate. Creating the index with the correct operator class resolves this:

```sql
CREATE INDEX ON t (col text_pattern_ops);
```

`ILIKE` and case-insensitive matching require either a `citext` column or a `citext`-aware operator class. The general principle is the same: the operator in the WHERE clause must belong to the operator family of the index column.

### Partial index predicate not satisfied

The planner considers a partial index defined with `WHERE status = 'active'` only when it can prove that every row the query might scan satisfies `status = 'active'`. The check is `predicate_implied_by(index->indpred, baserestrictinfo, false)` in `indxpath.c`. If the query has no restriction on `status`, or restricts it to a different value, implication fails. The planner does not add the index to the candidate set at all — it is not merely costed out, but entirely excluded.

This is correct behaviour. A query for inactive orders must not use an index that only covers active ones. The consequence for developers is that partial indexes are precise instruments. They accelerate exactly the queries whose WHERE clause implies the index predicate. They are invisible to everything else.

### Physical correlation and random I/O cost

Even when an index is highly selective, the heap fetch cost depends heavily on `indexCorrelation` — the Pearson correlation between the physical order of rows in the heap and the sort order of the indexed values (stored in `pg_statistic` under `STATISTIC_KIND_CORRELATION`). A correlation near 1.0 means matching rows are clustered together on a few heap pages. Adjacent index entries then point to adjacent heap pages, so access is nearly sequential. A correlation near 0.0 means matching rows are scattered randomly across the heap. Each TID fetch is then an independent random I/O.

`cost_index` models this with the blending formula above. When `indexCorrelation` is near zero and `random_page_cost` is high (4.0, the default tuned for spinning disks), even a selective index scan can be more expensive than a sequential scan. The sequential scan pays only `seq_page_cost` per page regardless of row order.

On solid-state storage, random reads are far cheaper — typically 5–10x faster relative to sequential, compared to 50–100x on spinning disks. The default `random_page_cost = 4.0` reflects spinning-disk ratios. It systematically over-penalises index scans on SSDs. Adjusting to a value in the 1.1–2.0 range (depending on hardware) brings the model in line with actual latencies. Administrators can apply this setting per tablespace with `ALTER TABLESPACE ... SET (random_page_cost = ...)`. This allows different settings for indexes on SSD versus archival data on HDD.

### Stale statistics

`clauselist_selectivity` (`selfuncs.c`) computes `indexSelectivity`. It queries `pg_statistic` for column histograms, MCV lists, and null fractions. The total number of rows in the table — `baserel->tuples`, loaded from `pg_class.reltuples` via `plancat.c` — is the denominator for selectivity fractions. If `pg_class.reltuples` diverges significantly from the actual live row count, selectivity estimates are wrong.

The two failure modes are opposite in effect. If the table has grown since the last `ANALYZE` — say, from 1,000 rows to 10 million — `reltuples` is still 1,000. The planner estimates the table as tiny. It concludes the absolute number of matching rows is negligible. It may then either trivially choose a sequential scan (because it appears cheap on a small table) or generate badly miscalibrated plans. If the table was recently truncated or bulk-deleted and has shrunk, `reltuples` is inflated. The planner then overestimates selectivity. It may over-prefer index scans that return only a few actual rows.

Monitor the gap with:

```sql
SELECT reltuples, n_live_tup, n_dead_tup
FROM pg_class
JOIN pg_stat_user_tables ON relid = pg_class.oid
WHERE relname = 'your_table';
```

A large discrepancy between `reltuples` and `n_live_tup` is a reliable signal that `ANALYZE` is overdue. [[subsystems/background/autovacuum|Autovacuum]] handles this automatically for steady-state tables, but bulk loads, bulk deletes, and initial data imports can outpace the autovacuum schedule.

## Diagnosing a missing index

When the planner does not use an expected index, the following sequence typically identifies the cause quickly.

First, `EXPLAIN (ANALYZE, BUFFERS)` the query. Check the estimated row count in the plan against the actual row count reported at runtime. A large discrepancy (especially a much lower estimate) points to stale statistics. Run `ANALYZE` and re-check. A large estimate (predicate matches a substantial fraction of the table) points to low selectivity.

Second, set `enable_seqscan = off` in the session. Then re-run `EXPLAIN`. If the planner now uses the index and the cost is only moderately higher than without the index, the break-even calculation is on the margin. Adjusting `random_page_cost` may tip it. If the index still does not appear, the index is structurally unusable for this query.

Third, if the index is structurally absent from the plan even with `enable_seqscan = off`, verify the structural match:

- Check that the WHERE clause operator is in the index's operator family: `SELECT * FROM pg_amop WHERE amopfamily = (SELECT opcfamily FROM pg_opclass WHERE opcname = 'text_ops')` to list its operators, then compare against what the query uses.
- In `psql`, use `\d indexname` to inspect the operator class on each column. Confirm it matches the query's pattern (e.g. `text_pattern_ops` for LIKE).
- If the column is wrapped in a function in the WHERE clause, check whether an expression index exists with the exact same expression.
- For partial indexes, verify that the query's WHERE clause implies the index predicate — remember, implication is logical, not syntactic.

Fourth, check `pg_stats` for the relevant column:

```sql
SELECT tablename, attname, correlation, n_distinct, null_frac
FROM pg_stats
WHERE tablename = 'your_table' AND attname = 'your_column';
```

A `correlation` near 0 combined with a high `random_page_cost` explains why a selective index is still losing to a sequential scan.

## See also

- [[subsystems/planner/cost-model]] — how startup and total costs drive path selection
- [[subsystems/planner/statistics]] — column statistics used by selectivity estimation
- [[subsystems/indexes/btree]] — B-tree index internals and leaf-page layout
- [[subsystems/indexes/index-am]] — the index AM API including amcostestimate
- [[subsystems/storage/visibility-map]] — role of the visibility map in index-only scans

---
title: "Planner Statistics and Selectivity"
aliases:
  - "Planner Statistics"
  - "Selectivity Estimation"
  - "pg_statistic"
tags:
  - theme/query-optimization
source_files:
  - src/backend/utils/adt/selfuncs.c
  - src/backend/utils/adt/like_support.c
  - src/backend/optimizer/path/clausesel.c
  - src/backend/commands/analyze.c
  - src/backend/statistics/extended_stats.c
  - src/include/catalog/pg_statistic.h
symbols:
  - clause_selectivity
  - eqsel
  - scalarltsel
  - nulltestsel
  - eqjoinsel
  - patternsel
  - pattern_fixed_prefix
  - compute_scalar_stats
  - GetSnapshotData
  - examine_variable
---

# Planner Statistics and Selectivity

Cost estimates are only useful if the planner knows how many rows a WHERE clause will pass. PostgreSQL collects per-column statistics during `ANALYZE` and stores them in `pg_statistic`. At planning time, selectivity functions read those statistics to estimate what fraction of rows survive each filter. This drives both row-count estimates and join order decisions.

## What ANALYZE collects

`ANALYZE` samples the table (default 30,000 rows, controlled by `default_statistics_target = 100`) and builds type-specific statistics for each column. For scalar columns, `compute_scalar_stats()` (`analyze.c`) computes three complementary structures:

**Most-common values (MCV list)** — up to `statistics_target` values that appear most frequently, stored with their exact frequencies. This allows precise selectivity for common values like `status = 'active'`, where treating the value as part of a uniform distribution would be wildly inaccurate.

**Histogram** — up to `statistics_target` boundaries dividing the non-MCV values into bins of approximately equal population, used for range queries. The histogram deliberately excludes MCV values so that the two sources can be combined without double-counting: a query can sum MCV contributions and histogram contributions independently.

**Correlation** — Pearson's correlation coefficient between column order and physical heap order. Values near ±1 mean the column is nearly sorted on disk. The planner uses this to estimate index scan I/O cost, since a correlated index scan reads pages nearly sequentially and incurs far fewer buffer misses than an uncorrelated one.

**stadistinct** — estimated number of distinct values. A positive value is an exact count. A negative value is a fraction of rows instead (e.g. −1.0 means every row is unique). For high-cardinality columns where many values appear only once in the sample, the Haas-Stokes estimator produces a more accurate projection than a simple count (`compute_scalar_stats()`, line 2625).

**stanullfrac** — fraction of NULLs.

`pg_statistic` stores these values in five generic slots per column. Each slot has a kind code, an operator OID, a `float4[]` of numeric statistics (frequencies), and an `anyarray` of data values:

| Kind | Contents |
|---|---|
| 1 `STATISTIC_KIND_MCV` | `stavalues`: MCV values; `stanumbers`: their frequencies |
| 2 `STATISTIC_KIND_HISTOGRAM` | `stavalues`: bin boundary values (min … max) |
| 3 `STATISTIC_KIND_CORRELATION` | `stanumbers[0]`: correlation coefficient |
| 4 `STATISTIC_KIND_MCELEM` | Most-common elements (for array/tsvector columns) |
| 5 `STATISTIC_KIND_DECHIST` | Distinct-element count histogram (arrays) |

## Extended statistics

Single-column statistics assume independence between columns. This assumption causes the planner to multiply per-column selectivities together when combining filters. For correlated columns — city and zip code, for instance — this produces grossly underestimated row counts. `CREATE STATISTICS` creates a `pg_statistic_ext` entry that triggers collection of cross-column statistics during ANALYZE:

- **ndistinct** — distinct value counts for combinations of columns; corrects `GROUP BY` cardinality estimates when columns are correlated.
- **dependencies** — functional dependencies (e.g. city → zip code); allows the planner to avoid multiplying independent selectivities when columns are functionally related.
- **MCV** — multi-column most-common value lists; the most powerful form, allowing precise estimates for queries filtering on multiple correlated columns at once.

The planner consults extended stats before falling back to single-column estimates (`statext_clauselist_selectivity()`, `extended_stats.c`).

## Dispatching selectivity by clause type

Every filter expression in a query reaches a single entry point, `clause_selectivity()` (`clausesel.c`). This function examines the clause structure and routes it to the appropriate estimator. For operator clauses, the operator's `oprrest` function registered in `pg_operator` resolves to one of the built-in selectivity functions. `clauselist_selectivity()` handles AND lists. It tries extended statistics first before falling back to per-clause estimates. OR lists use inclusion-exclusion across sub-clauses.

| Clause form | Handler |
|---|---|
| `col = constant` | `eqsel()` → `var_eq_const()` |
| `col < constant` / `col > constant` | `scalarltsel()` / `scalargtsel()` → `scalarineqsel()` |
| `col IS NULL` / `col IS NOT NULL` | `nulltestsel()` |
| `NOT clause` | 1.0 − selectivity of inner clause |
| `AND` list | `clauselist_selectivity()`, tries extended stats first |
| `OR` list | inclusion-exclusion over sub-clauses |

## Equality selectivity

Equality estimation exploits the MCV list when available, since the most common values in a column are precisely the ones that benefit most from exact frequency data (`eqsel()`, `var_eq_const()`, `selfuncs.c`). For a column with a unique index, the selectivity is simply `1.0 / num_tuples`. Otherwise, if the constant matches an entry in the MCV list, the estimator returns that entry's recorded frequency directly. For a value that does not appear in the MCV list, the estimator computes its expected frequency as the average across the non-MCV portion of the distribution:

```
(1.0 - sum(MCV_freqs) - stanullfrac) / (stadistinct - num_mcv_values)
```

This correctly accounts for the fact that the MCV list and null fraction together consume part of the row population.

## Range query selectivity

`scalarineqsel()` (`selfuncs.c`) handles range predicates (`<`, `<=`, `>`, `>=`) by combining contributions from both the MCV list and the histogram, because the two cover disjoint portions of the value distribution.

The MCV contribution sums the frequencies of all MCV values satisfying the inequality (`mcv_selectivity()`). The histogram contribution locates the comparison constant's position among the bin boundaries via binary search. It then linearly interpolates within the found bin to estimate what fraction of non-MCV rows qualify (`ineq_histogram_selectivity()`). The estimator combines the two as:

```
selec = mcv_selec + (1.0 - nullfrac - total_mcv_freq) × hist_selec
```

The linear interpolation within histogram bins assumes values are uniformly distributed within each bin. This works well for most distributions but underestimates selectivity for highly skewed data not captured by the MCV list. The intended mitigation is to raise the statistics target. Raising it increases both MCV list length and histogram resolution.

## NULL selectivity

Because `stanullfrac` is collected directly during ANALYZE, null fraction queries require no estimation: `IS NULL` returns `stanullfrac` and `IS NOT NULL` returns `1.0 - stanullfrac` (`nulltestsel()`, `selfuncs.c`). If no statistics exist for the column, the estimator assumes a default of 10% nulls.

## LIKE and pattern matching selectivity

`patternsel()` in `like_support.c` handles LIKE predicates. It dispatches through `patternsel_common()`. The central operation is extracting a fixed prefix from the pattern via `pattern_fixed_prefix()`. When the pattern starts with literal characters — `'abc%'`, for example — `pattern_fixed_prefix()` returns `'abc'`. The planner then converts the predicate into an equivalent range scan: `col >= 'abc' AND col < 'abd'`. The planner hands this synthetic range query to the same histogram-based range selectivity machinery used for `<` and `>` predicates, so the estimate is grounded in actual data distribution.

When the pattern has no useful prefix — `'%abc'`, `'%abc%'`, or any pattern beginning with a wildcard — `pattern_fixed_prefix()` returns no prefix. The fallback is then a hardcoded constant: `DEFAULT_MATCH_SEL = 0.005` (0.5%), defined in `like_support.c`. This constant has no connection to actual column data. It is the same regardless of whether the substring appears in 0.01% or 50% of rows. ILIKE follows the same code path but with case-insensitive prefix extraction. It inherits the same behavior: a constant prefix gets real range-based estimation, and a leading wildcard gets the 0.5% fallback.

The practical consequence is asymmetric estimation quality. `LIKE 'London%'` on a `city` column gets a selectivity estimate derived from the histogram. `LIKE '%ondon'` always receives 0.5% instead. When a trailing-wildcard query is rare, 0.5% is a tolerable approximation. When a leading-wildcard query matches a large fraction of a table — or almost none of it — the fixed default can mislead the planner into choosing a sequential scan over an index scan, or vice versa. There is no workaround through statistics targets. No amount of ANALYZE data can improve estimates for patterns that start with `%`.

## Join selectivity

Estimating the output size of an equality join requires knowing how many rows from one side match rows on the other. The estimator for this is `eqjoinsel()` in `selfuncs.c`. It delegates to `eqjoinsel_inner()` for standard inner and outer joins.

When both sides have MCV lists, the estimator iterates over all cross-pairs to find values that appear in both lists, accumulating `freq_A[v] * freq_B[v]` for each matching value `v`. This sum, `matchprodfreq`, is the exact join selectivity contribution from the MCV-covered population on both sides. The estimator assumes the remaining population — rows whose values fall outside the MCV lists — is uniformly distributed across the remaining distinct values. It then estimates that population's contribution as the product of the two non-MCV frequencies divided by the number of remaining distinct values. The estimator computes this residual contribution from both perspectives (treating each side's `nd` as the denominator in turn). It takes the smaller of the two totals as the final selectivity. The join output row count is then this selectivity multiplied by `outer_rows * inner_rows`.

When one or both sides lack MCV data, the estimator falls back to assuming a uniform distribution entirely:

```
selec = (1.0 - nullfrac1) * (1.0 - nullfrac2) / max(nd1, nd2)
```

This is the classic "each row joins to `N / ndistinct` partners" formula. It is a reasonable upper bound when the join column is roughly uniformly distributed on both sides. It can be severely wrong, though, when the distributions are mismatched. A common failure mode is a join on a user ID where one table has a handful of extremely frequent IDs (e.g. a bot or system account) and the other has a uniform distribution. The MCV lists for the two sides have no overlap, so the cross-MCV contribution is zero. The non-MCV residual formula then underestimates the join output, because the heavy-hitter rows on one side do not register in the other side's statistics. The result is an underestimated join size. This can cause the planner to choose a nested-loop strategy that proves catastrophically expensive at runtime.

Raising the statistics target helps when the heavy-hitter values on one side would appear in a longer MCV list, giving the overlap computation real data to work with. In extreme cases where the skew is structural and the two tables' value distributions are genuinely non-overlapping, extended statistics on the join key (if they can be defined) or manual plan hints are the only mitigations.

## Loading statistics at planning time

Before any selectivity function can read from `pg_statistic`, the planner must locate and load the stats tuple for the relevant column. `examine_variable()` (`selfuncs.c`) populates a `VariableStatData` struct for each column variable encountered in a clause, holding the `HeapTuple` for that column's stats row. Selectivity functions then call `get_attstatsslot()` to extract a specific kind of statistics from the tuple's slot array.

The planner fetches statistics once per variable per planning operation and does not re-read them. If `ANALYZE` has never been run, the stats tuple is NULL. All selectivity functions then fall back to hard-coded defaults — typically 0.5% for equality and 33% for range predicates.

## Statistics target

The statistics target, set globally via `default_statistics_target` or per-column via `ALTER TABLE t ALTER COLUMN c SET STATISTICS n`, controls both the depth of collection and the cost of maintaining it. A higher target increases MCV list length, increases histogram bin count, extends ANALYZE runtime, and grows `pg_statistic` storage. The sample size scales with the target at approximately `300 × target` rows. Raising the target on a selective, high-cardinality column can dramatically improve plan quality for range and equality queries. The cost is paid at ANALYZE time, not at query time.

## See also

- [[subsystems/planner/overview]] — how selectivity estimates feed into path costs
- [[architecture/overview]] — ANALYZE in the maintenance context

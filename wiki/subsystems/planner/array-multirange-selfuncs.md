---
title: Array and Multirange Selectivity Estimation
aliases:
  - array selfuncs
  - multirange selfuncs
  - array containment selectivity
  - multirange selectivity
tags:
  - theme/query-optimization
source_files:
  - src/backend/utils/adt/array_selfuncs.c
  - src/backend/utils/adt/multirangetypes_selfuncs.c
  - src/include/utils/selfuncs.h
symbols:
  - scalararraysel
  - scalararraysel_containment
  - arraycontsel
  - mcelem_array_contain_overlap_selec
  - mcelem_array_contained_selec
  - multirangesel
  - calc_multirangesel
  - calc_hist_selectivity
  - calc_hist_selectivity_contains
  - calc_hist_selectivity_contained
  - calc_distr
  - calc_hist
---

The planner calls type-specific selectivity estimator functions ("selfuncs") to predict what fraction of rows survive a predicate. For complex types like arrays and multiranges, those estimates are substantially harder than scalar comparisons because a single column value is itself a collection. Getting these numbers wrong is one of the most common causes of bad index selection on array columns, JSONB paths, and range/multirange columns.

## Array selectivity: scalar op ANY/ALL

When the planner sees `scalar_val = ANY(array_col)` or `scalar_val = ALL(array_col)`, it generates a `ScalarArrayOpExpr` node. It calls `scalararraysel()` (selfuncs.c). For equality and inequality operators on a column-side array, `scalararraysel()` first tries a smarter path: it delegates to `scalararraysel_containment()` (array_selfuncs.c). This function reframes the problem as an array containment test — `= ANY(col)` becomes `col @> ARRAY[const]` and `= ALL(col)` becomes `col <@ ARRAY[const]` — and then uses the MCELEM statistics directly. This is significantly more accurate than treating each element independently when the column has been analyzed.

If the array is a literal constant rather than a column, or if `scalararraysel_containment()` returns `-1` (cannot estimate), `scalararraysel()` falls back to deconstructing the constant array and calling the underlying scalar operator's own selectivity function once per element. It then combines the per-element estimates using independent-probability arithmetic: for ANY (OR semantics) it applies `s = s + s2 - s*s2`; for ALL (AND semantics) it multiplies `s = s * s2`. For `= ANY` with distinct elements it also computes an additive "disjoint" estimate and uses it if the result falls in `[0,1]`.

## MCELEM statistics and the containment estimators

For columns that hold arrays (or tsvectors), `ANALYZE` stores `STATISTIC_KIND_MCELEM` (slot 4 in [[subsystems/planner/statistics]]) — a sorted list of the most-common individual elements together with their per-element frequencies. It also stores `STATISTIC_KIND_DECHIST` (slot 5), a histogram of how many distinct elements each row's array contains.

### @> and && (contain / overlap)

`arraycontsel()` handles the `@>`, `<@`, and `&&` operators. For `@>` and `&&`, it calls `mcelem_array_contain_overlap_selec()`, which merges the sorted MCELEM array with the sorted constant-array elements in a single linear scan (or binary-search scan when `nitems * log2(nmcelem) < nmcelem + nitems`).

For each element of the constant array that is found in MCELEM, the function uses that element's stored frequency directly. For elements not found in MCELEM the function falls back to `MIN(DEFAULT_CONTAIN_SEL, minfreq/2)` — where `DEFAULT_CONTAIN_SEL = 0.005` and `minfreq` is the lowest frequency stored in the MCELEM slot. This ensures that rare elements not promoted into MCELEM still generate a conservative, non-zero estimate.

The combination rule assumes **element occurrence independence**: for containment (`@>`), estimates multiply (`selec *= elem_selec`); for overlap (`&&`), estimates accumulate using the inclusion-exclusion formula (`selec = selec + elem_selec - selec * elem_selec`). Independence is a known approximation — real data frequently violates it — but it is the standard tradeoff made throughout the planner for composite predicates.

### <@ (contained-by) and the DECHIST correction

`mcelem_array_contained_selec()` handles `col <@ ARRAY[const]` — the predicate that asks whether every element of the column array is a member of the constant. This case is harder because the fraction of rows that match depends heavily on how many distinct elements the typical row contains, not just whether individual elements appear.

The function therefore uses both MCELEM and the DECHIST histogram. If either is absent it returns the default constant `DEFAULT_CONTAIN_SEL = 0.005`. When both are available, the algorithm:

1. Computes per-element frequencies for the constant's elements from MCELEM, falling back to `MIN(DEFAULT_CONTAIN_SEL, minfreq/2)` for elements not in MCELEM.
2. Accumulates `rest` — the sum of expected frequencies for all elements not covered by MCELEM — using the average distinct element count stored as the last entry of DECHIST. Rare elements are modelled via `exp(-rest)` (Poisson approximation).
3. Calls `calc_distr()` to compute the probability distribution of exactly *k* of the constant's elements being present in a column row, using the DP recurrence `M[i,j] = M[i-1,j] * (1 - p[i]) + M[i-1,j-1] * p[i]`. This is O(unique_nitems²). It is bounded by an `EFFORT = 100` cap that trims the constant array down to the most-frequent elements to protect planning time.
4. Calls `calc_hist()` to convert the DECHIST histogram into a probability mass function over distinct-element-count values up to `unique_nitems`.
5. Sums `hist_part[i] * mult * dist[i] / mcelem_dist[i]` over all `i` to get the final estimate — effectively re-weighting the independence-assumption probability by the empirical histogram of distinct element counts.

`mcelem_array_contained_selec()` then multiplies the result by `(1 - nullelem_freq)` to account for rows containing null elements, which cause containment to fail. The MCELEM numbers array carries the null-element frequency as its last-but-one extra slot.

### NULL handling

`mcelem_array_selec()` strips null elements from the constant array before any comparison. A null in the constant causes `@>` to return selectivity 0.0 immediately (no row can contain a null element). For `&&` and `<@`, null elements in the constant are silently ignored. Null rows (the whole column value is NULL) are handled separately: both `calc_arraycontsel()` and `scalararraysel_containment()` multiply the final result by `(1 - stats->stanullfrac)`.

## Multirange selectivity (PG 14+)

A multirange is an ordered, non-overlapping sequence of ranges; a single `int4multirange` value like `{[1,5],[10,20]}` is a union of gaps. The `multirangesel()` function (multirangetypes_selfuncs.c) is the registered restriction estimator for all multirange operators introduced in PostgreSQL 14.

### Operator dispatch and constant promotion

`multirangesel()` accepts more than a dozen operator OIDs. When the constant is an element value (`@> elem`), it builds a degenerate single-point range, then wraps that in a single-range multirange. This lets the rest of the logic treat everything uniformly. When the constant is a plain range (`@> range`, `&& range`) it similarly promotes it to a single-range multirange. `multirangesel()` currently punts on operators where the variable is the range side (e.g., `range @> multirange`), using the default constant, because the estimation logic is not yet implemented for that direction.

### Empty multirange short-circuits

`calc_multirangesel()` handles an empty constant multirange as a special case before touching any histogram. An empty multirange is contained by every non-empty value, contains nothing, overlaps nothing, and is less than or equal to every empty value. These rules are directly encoded in a switch statement that returns exact fractions (`0.0`, `empty_frac`, `1.0`, `1.0 - empty_frac`) without consulting statistics. This avoids nonsense estimates for `col @> '{}'::int4multirange` style predicates that would otherwise get the generic fallback.

### Histogram-based estimation

For non-empty constants, `calc_hist_selectivity()` reads two statistics from `pg_statistic`:

- `STATISTIC_KIND_BOUNDS_HISTOGRAM` — a histogram of representative range values, stored as the range type. The function deserialises each entry into a `(lower, upper)` `RangeBound` pair, yielding parallel arrays `hist_lower[]` and `hist_upper[]`.
- `STATISTIC_KIND_RANGE_LENGTH_HISTOGRAM` — a histogram of range lengths (as `float8`), required only for containment operators.

The key insight is that `calc_hist_selectivity()` treats the multirange constant by its **outer span** — it extracts `const_lower` as the lower bound of the first component range and `const_upper` as the upper bound of the last component range (via `multirange_get_bounds()`). All subsequent computation ignores the internal gaps of the multirange. This is the central approximation: a multirange with gaps is estimated as if it were a single contiguous range of the same outer width.

Operator families map to histogram comparisons as follows:

| Operator family | Histogram query |
|---|---|
| `<`, `<=`, `>`, `>=` | fraction of lower bounds less/greater than `const_lower` |
| `<<` (strictly left of) | upper bound histogram: fraction < `const_lower` |
| `>>` (strictly right of) | lower bound histogram: fraction > `const_upper` |
| `&&` (overlaps) | complement of (strictly left OR strictly right) |
| `@>` (contains) | `calc_hist_selectivity_contains()` |
| `<@` (contained by) | `calc_hist_selectivity_contained()` |

`calc_hist_selectivity_scalar()` performs the core bound-histogram lookup: binary search to find the enclosing bin, then linear interpolation using the range subtype's `subdiff` function when available (falling back to 0.5 within a bin if `subdiff` is absent).

### Containment and the length histogram

For `@>` and `<@`, a bound-only histogram is insufficient — containment depends on the length of the value relative to the query range. `calc_hist_selectivity_contains()` walks the lower-bound histogram bins from right to left. For each bin it asks: what fraction of values in this bin are long enough to span from the bin's lower bound all the way to `const_upper`? That fraction comes from `calc_length_hist_frac()`, which integrates the empirical CDF of lengths over a range using a piecewise-trapezoid method.

`calc_hist_selectivity_contained()` does the symmetric operation for `<@`: for each bin it asks what fraction of values are short enough that they fit inside the `[const_lower, const_upper]` window.

Both functions assume **independence between lower bounds and range lengths** — a simplification that can fail for data with strong structural correlations (e.g., time-series ranges that get longer as they get older).

### Wrapping up: NULLs and empty fractions

`calc_multirangesel()` adjusts the histogram selectivity for the fraction of empty multiranges (`empty_frac`) and NULL values (`null_frac`). For containment operators, empty column values match a non-empty query constant; for all others they do not. `calc_multirangesel()` then multiplies the final result by `(1 - null_frac)` because all multirange operators are strict.

## When estimates degrade and how to respond

Both estimators have systematic failure modes:

**Array columns**: When MCELEM is absent (no `ANALYZE`, column not yet analysed, or `default_statistics_target = 0`) every array operator falls back to `DEFAULT_CONTAIN_SEL = 0.005` for `@>` / `<@` and `DEFAULT_OVERLAP_SEL = 0.01` for `&&`. These numbers may be off by orders of magnitude for highly selective or highly non-selective predicates, causing the planner to pick sequential scans over GIN indexes or vice versa. JSONB GIN indexes are particularly affected because JSONB path operators like `@>` route through `arraycontsel` logic internally.

**Multirange columns**: Because `calc_hist_selectivity()` collapses a multirange to its outer span, a query like `col && '{[1,2],[1000,1001]}'::int4multirange` will receive an overlap estimate as if the constant were `[1,1001]` — a much larger span — resulting in a much higher (optimistic) overlap estimate and a tendency to favour index scans that may not be justified.

**Joint array predicates** (`col && '{A}' AND col && '{B}'`): the estimators treat each clause independently and multiply their selectivities. This assumes the elements are uncorrelated. If elements A and B typically co-occur, the resulting estimate is far too low. `CREATE STATISTICS ... (dependencies)` does not help here because it targets inter-column correlation, not intra-column element co-occurrence.

The most effective remedy in current PostgreSQL is `CREATE STATISTICS ... (mcv) ON expression` or increasing `ALTER TABLE ... SET (statistics = N)` to give `ANALYZE` a larger MCELEM budget. This helps the containment estimators reach more element frequencies rather than falling back to defaults.

## Related Topics

- [[subsystems/planner/selectivity-estimation]]
- [[subsystems/planner/statistics]]
- [[subsystems/planner/extended-statistics]]
- [[subsystems/planner/stale-statistics-and-bad-plans]]
- [[subsystems/planner/index-selection]]

---
title: "CREATE STATISTICS and Extended Statistics"
aliases:
  - "Extended Statistics"
  - "CREATE STATISTICS"
  - "multivariate statistics"
  - "pg_statistic_ext"
tags:
  - theme/query-optimization
source_files:
  - src/backend/commands/statscmds.c
  - src/backend/statistics/extended_stats.c
  - src/backend/statistics/mvdistinct.c
  - src/backend/statistics/dependencies.c
  - src/backend/statistics/mcv.c
  - src/include/catalog/pg_statistic_ext.h
  - src/include/catalog/pg_statistic_ext_data.h
symbols:
  - CreateStatistics
  - AlterStatistics
  - BuildRelationExtStatistics
  - statext_clauselist_selectivity
  - statext_mcv_clauselist_selectivity
  - dependencies_clauselist_selectivity
  - statext_ndistinct_build
  - statext_dependencies_build
  - statext_mcv_build
  - StatExtEntry
  - MVNDistinct
  - MVDependencies
  - MCVList
  - STATS_EXT_NDISTINCT
  - STATS_EXT_DEPENDENCIES
  - STATS_EXT_MCV
  - STATS_EXT_EXPRESSIONS
---

# CREATE STATISTICS and Extended Statistics

The default [[subsystems/planner/statistics|planner statistics]] model treats column values as statistically independent. When computing the selectivity of `WHERE city = 'London' AND zip = 'EC1A'`, the planner multiplies the two per-column selectivities together. This implicitly assumes that knowing the city tells you nothing about the zip code. For correlated columns, this independence assumption produces severely underestimated row counts. That leads the planner to choose plans that appear cheap but are actually expensive. `CREATE STATISTICS` addresses this by teaching the planner about multi-column correlations discovered during [[code-paths/analyze|ANALYZE]].

## The independence assumption problem

PostgreSQL collects single-column statistics into `pg_statistic`, one row per column per table. When a WHERE clause touches two columns simultaneously, `clauselist_selectivity()` in `clausesel.c` multiplies the individual selectivities. If city = 'London' is present in 10% of rows and zip = 'EC1A' in 0.5%, the planner estimates 0.1 × 0.005 = 0.05%. But if city fully determines zip, the real selectivity is simply 0.5%. The underestimate grows exponentially with the number of correlated predicates. This causes the planner to believe result sets are tiny. It then favours nested-loop joins and index scans. Those plans perform poorly when the actual cardinality is much higher.

## Syntax

```sql
CREATE STATISTICS [ IF NOT EXISTS ] name
    [ ( statistics_kind [, ...] ) ]
    ON { column_name | ( expression ) } [, ...]
    FROM table_name;
```

The `statistics_kind` list may contain `ndistinct`, `dependencies`, and `mcv`. Omitting the list causes PostgreSQL to build all applicable kinds. Starting from PostgreSQL 14, a statistics object may also include expressions — arbitrary expressions wrapped in parentheses — not just simple column references.

The `name` is optional; if omitted, PostgreSQL auto-generates one via `ChooseExtendedStatisticName()` in `statscmds.c`. The maximum number of columns or expressions in a single statistics object is `STATS_MAX_DIMENSIONS = 8`.

## Catalog layout

`CREATE STATISTICS` calls `CreateStatistics()` in `statscmds.c` and inserts a row into `pg_statistic_ext`, the definition catalog. ANALYZE later fills in `pg_statistic_ext_data` with the computed coefficients. The split mirrors `pg_class`/`pg_statistic`: the definition persists across ANALYZE cycles, while ANALYZE replaces the data row wholesale each run. For the full field layout of both catalogs, see [[subsystems/planner/extended-statistics|Extended Statistics]].

## Statistics kinds

### ndistinct

The ndistinct kind stores estimated distinct-value counts for every subset of the covered columns. For a statistics object on columns `(a, b, c)`, it stores coefficients for `(a,b)`, `(a,c)`, `(b,c)`, and `(a,b,c)` — the per-column estimates are already in `pg_statistic`. The coefficients follow the same sign convention as `stadistinct`: positive means an absolute count, negative means a fraction of total rows.

The planner uses these coefficients when estimating GROUP BY cardinality on multiple columns. Without them, the planner multiplies per-column ndistinct values and then applies a heuristic correction. The multivariate coefficients let it use a direct measurement instead.

The build function is `statext_ndistinct_build()` in `mvdistinct.c`. It iterates over all column subsets using a combination generator. For each subset, it calls `ndistinct_for_combination()`, which applies the same Haas-Stokes estimator used for per-column statistics.

### dependencies

Functional dependency statistics record, for every pair and higher-order combination of columns, the *degree* to which knowing the values of one subset determines the values of another. A degree of 1.0 is a perfect functional dependency (knowing `zip` always gives you `city`). A degree of 0.0 means the columns are uncorrelated.

This is a "soft" formulation. Real-world data contains measurement errors and exceptions, so the degree represents the fraction of rows consistent with the dependency rather than requiring strictness. The build algorithm in `dependencies.c` (`statext_dependencies_build()`) sorts rows by each candidate determinant set and counts how many distinct values appear in the dependent column for each determinant value group. When only one distinct dependent value exists per group, the rows support the dependency.

At planning time, `dependencies_clauselist_selectivity()` in `dependencies.c` applies the formula:

```
P(a=x, b=y) = P(a=x) * (d + (1-d) * P(b=y))
```

where `d` is the degree of `a → b`. With `d = 1`, this reduces to `P(a=x)`, correctly ignoring the b predicate once a is already constrained. With `d = 0`, it reduces to the independent product. The formula generalises recursively to multi-column dependencies.

Functional dependencies only work for equality clauses joined by AND. They do not apply to range predicates, IS NULL, or OR conditions.

### MCV list

The multi-column MCV (most-common values) list, added in PostgreSQL 12, is the most powerful form of extended statistics. It stores the most frequent tuples of values across the covered columns, each with its exact frequency (`frequency`) and the frequency that would be predicted by treating the columns as independent (`base_frequency`). The ratio between the two quantifies the correlation strength for that specific value combination.

The serialised format in `mcv.c` stores values in a deduplicated per-column array and represents each item as a compact set of `uint16` indexes into that array, null flags, the actual frequency, and the base frequency:

```c
/* each item: ndim*(uint16+bool) + 2*double */
#define ITEM_SIZE(ndims) \
    ((ndims) * (sizeof(uint16) + sizeof(bool)) + 2 * sizeof(double))
```

At planning time, `statext_mcv_clauselist_selectivity()` in `extended_stats.c` iterates the MCV list and sums the frequencies of items matching all supplied clauses. For rows not covered by the MCV list, it combines the MCV-derived estimate with the per-column statistics estimate using `mcv_combine_selectivities()`. That function blends the MCV-based and simple estimates in proportion to the fraction of the distribution each explains. This extrapolation handles the tail of the distribution that the MCV list does not cover.

Unlike functional dependencies, MCV lists support equality, inequality, IS NULL / IS NOT NULL, and OR clauses.

### Expression statistics (PG 14+)

Before PostgreSQL 14, extended statistics could only be defined on plain columns. PostgreSQL 14 added support for expressions. The syntax wraps expressions in parentheses:

```sql
CREATE STATISTICS s ON (lower(email)), country FROM users;
```

PostgreSQL builds expression statistics as ordinary single-column per-expression statistics, stored in `stxdexpr` of `pg_statistic_ext_data` using the same `pg_statistic` format as regular columns. This lets the planner use real frequency data for a frequently-filtered expression rather than treating it as an opaque computation. `compute_expr_stats()` within `extended_stats.c` handles expression statistics of type `'e'` (`STATS_EXT_EXPRESSIONS`).

## How ANALYZE builds extended statistics

When [[code-paths/analyze|ANALYZE]] runs, it collects the per-column sample and then calls `BuildRelationExtStatistics()`. That function builds each requested kind (ndistinct, dependencies, MCV, expressions) from the same sample rows already gathered for per-column statistics, so no additional table scan is needed. See [[subsystems/planner/extended-statistics|Extended Statistics]] for the full build pipeline, including how the effective statistics target is computed and how each kind's build function works.

## ALTER STATISTICS

The statistics target for an extended statistics object is adjusted with:

```sql
ALTER STATISTICS name SET STATISTICS target;
```

This calls `AlterStatistics()` in `statscmds.c`, which updates `stxstattarget` in `pg_statistic_ext`. Setting the target to `DEFAULT` (or `-1` in older versions) clears the per-object override and falls back to the column and global defaults. The maximum target is `MAX_STATISTICS_TARGET`. The new target takes effect at the next ANALYZE.

## How the planner uses extended statistics

The planner consults `statext_clauselist_selectivity()` from `clauselist_selectivity()` whenever the relation has a usable extended statistics object. It runs an MCV pass first and falls back to functional dependencies for whatever AND clauses remain unestimated. See [[subsystems/planner/extended-statistics|Extended Statistics]] for the two-pass algorithm, the dependency-combination formula, and how `estimatedclauses` prevents double-counting.

## Limitations

Extended statistics have several important restrictions:

- **OR conditions** — functional dependencies (`'f'`) cannot be applied to OR clause lists at all. MCV lists can handle OR, but only when all columns in the OR are covered by the same statistics object.
- **Cross-table correlations** — statistics objects can only span columns from a single base table. There is no mechanism to describe correlations between columns in different tables; join selectivity estimation does not use extended statistics.
- **Subquery expressions** — statistics are not applied to predicates involving subqueries or volatile functions.
- **Column coverage** — a statistics object is skipped entirely if any of its columns was not included in the current ANALYZE run (e.g., when ANALYZE is called with a column list).
- **Consistency requirement for dependencies** — if the equality clauses provided are inconsistent with the actual functional dependency (e.g., `zip = '10001' AND city = 'London'` where that zip belongs to New York), the dependency formula produces an overestimate. MCV lists handle this correctly because they store actual observed value combinations.

## Related Topics

- [[subsystems/planner/statistics|Planner Statistics and Selectivity]] — per-column statistics, `pg_statistic`, and single-column selectivity estimators
- [[subsystems/planner/overview|Planner Overview]] — how row estimates feed into path cost calculations
- [[code-paths/analyze|ANALYZE]] — how both per-column and extended statistics are collected

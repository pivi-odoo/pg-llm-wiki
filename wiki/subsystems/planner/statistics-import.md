---
title: "Statistics Import API"
aliases:
  - pg_restore_attribute_stats
  - pg_restore_relation_stats
  - pg_clear_attribute_stats
  - pg_clear_relation_stats
  - statistics import
  - direct statistics injection
tags:
  - theme/query-optimization
source_files:
  - src/backend/statistics/attribute_stats.c
  - src/backend/statistics/relation_stats.c
  - src/backend/statistics/stat_utils.c
  - src/include/statistics/stat_utils.h
symbols:
  - pg_restore_attribute_stats
  - pg_restore_relation_stats
  - pg_clear_attribute_stats
  - pg_clear_relation_stats
  - attribute_statistics_update
  - relation_statistics_update
  - RangeVarCallbackForStats
  - stats_fill_fcinfo_from_arg_pairs
  - stats_check_required_arg
  - stats_check_arg_pair
  - StatsArgInfo
---

The statistics import API, introduced in PostgreSQL 18, lets a superuser or a role with `MAINTAIN` privilege write planner statistics directly into `pg_statistic` and `pg_class` without running `ANALYZE`. It is the mechanism behind `pg_dump --include-statistics` and `pg_restore`'s corresponding option. This lets a restored database have accurate planner estimates immediately, rather than requiring a full `ANALYZE` pass after the data is loaded.

The API exposes four SQL functions: `pg_restore_attribute_stats()` and `pg_restore_relation_stats()` for setting statistics, and `pg_clear_attribute_stats()` and `pg_clear_relation_stats()` for resetting them to freshly-created-table values. Three source files implement it: `attribute_stats.c` (per-column statistics in `pg_statistic`), `relation_stats.c` (relation-level statistics in `pg_class`), and `stat_utils.c` (shared argument-handling utilities).

## Variadic Name/Value Interface

Both `pg_restore_*` functions accept arguments as variadic name/value pairs — alternating text names and typed values — rather than as positional or regular named parameters. For example:

```sql
SELECT pg_restore_attribute_stats(
    'relname', 'orders',
    'schemaname', 'public',
    'attname', 'status',
    'inherited', false,
    'null_frac', 0.02::float4,
    'n_distinct', 5.0::float4,
    'most_common_vals', '{''pending'',''shipped'',''cancelled''}'::text,
    'most_common_freqs', ARRAY[0.5, 0.4, 0.1]::float4[]
);
```

The developers chose this pattern deliberately over a fixed 18-argument signature: a function with that many parameters would be fragile to call and impossible to evolve without breaking existing callers. The variadic interface allows future versions to add new parameters without changing the function signature.

The translation from name/value pairs to internal positional arguments happens in `stats_fill_fcinfo_from_arg_pairs()` (`stat_utils.c`). It processes the pairs in order. It locates each name in a `StatsArgInfo` table that maps argument names to their expected OIDs, and builds a positional `FunctionCallInfo`. The inner worker functions (`attribute_statistics_update()`, `relation_statistics_update()`) consume this `FunctionCallInfo` as if they had been called with fixed parameters. Unknown argument names produce a `WARNING`. The function skips them. It accepts the special `version` argument and silently ignores it (reserved for future use when interpreting older dumps).

## Relation Statistics

`pg_restore_relation_stats()` updates the four planner-visible row counts in `pg_class`:

| Parameter | pg_class column | Meaning |
|---|---|---|
| `relpages` | `relpages` | Estimated disk pages |
| `reltuples` | `reltuples` | Estimated live rows (`-1.0` = unknown) |
| `relallvisible` | `relallvisible` | Pages covered by the [[subsystems/storage/visibility-map|visibility map]] |
| `relallfrozen` | `relallfrozen` | Pages where all tuples are frozen (PG18+) |

`pg_clear_relation_stats()` is a convenience wrapper that calls the same internal `relation_statistics_update()` with `relpages=0`, `reltuples=-1.0`, `relallvisible=0`, `relallfrozen=0`, restoring the appearance of a freshly-created table.

The function takes a `ShareUpdateExclusiveLock` on the relation before touching `pg_class`, mirroring the lock discipline used by `vac_update_relstats()` in VACUUM.

## Attribute Statistics

`pg_restore_attribute_stats()` writes one row of `pg_statistic` for a specified column and inheritance flag. Callers can identify the column by name (`attname`) or number (`attnum`), but not both. Required parameters are `schemaname`, `relname`, and `inherited`. At least one of `attname` or `attnum` is also required.

The function supports all standard `pg_statistic` slot kinds:

| Parameters | Slot kind | Notes |
|---|---|---|
| `null_frac`, `avg_width`, `n_distinct` | scalar fields | Always in the tuple header |
| `most_common_vals`, `most_common_freqs` | `STATISTIC_KIND_MCV` | Must be supplied together |
| `histogram_bounds` | `STATISTIC_KIND_HISTOGRAM` | Requires a less-than operator for the column type |
| `correlation` | `STATISTIC_KIND_CORRELATION` | Requires a less-than operator |
| `most_common_elems`, `most_common_elem_freqs` | `STATISTIC_KIND_MCELEM` | Arrays and tsvector columns only |
| `elem_count_histogram` | `STATISTIC_KIND_DECHIST` | Arrays and tsvector columns only |
| `range_bounds_histogram` | `STATISTIC_KIND_BOUNDS_HISTOGRAM` | Range types only |
| `range_length_histogram`, `range_empty_frac` | `STATISTIC_KIND_RANGE_LENGTH_HISTOGRAM` | Range types only; must be supplied together |

Array-valued statistics (`most_common_vals`, `histogram_bounds`, etc.) arrive as text literals that `array_in()` parses. The function downgrades any parse error from `ERROR` to `WARNING` and skips the affected slot while other slots proceed.

The operation has upsert semantics: if a `pg_statistic` row already exists for the given `(reloid, attnum, inherited)` triple, the function updates it in place. Otherwise, it inserts a new row. The function also supports partial updates — omitting a parameter or passing `NULL` leaves that field unchanged in an existing row.

`pg_clear_attribute_stats()` deletes the `pg_statistic` row entirely, making the column appear as if it has never been analyzed.

## Error Handling

The functions follow a lenient error policy designed for use in restore scripts: major errors (relation or column does not exist, permission denied, recovery in progress) raise `ERROR` and abort the call. The function downgrades conversion or validation errors for individual statistic slots to `WARNING` instead. It sets its boolean return value to `false` but continues processing other slots. This means a restore can still set the statistics it can, and the caller can detect partial failure from the return value.

`pg_restore` uses this leniency to restore as many statistics as possible even if a dump from a newer server contains statistic kinds unknown to the restore target.

## Permissions

A caller may modify statistics for a relation if either of the following holds:

- The caller owns the current database **and** the relation is not a shared catalog.
- The caller has the `MAINTAIN` privilege on the relation.

`RangeVarCallbackForStats()` in `stat_utils.c` enforces these checks. PostgreSQL invokes it as the lock callback during `RangeVarGetRelidExtended()`. The function excludes shared catalogs unconditionally, because their statistics span all databases and must not be altered per-database.

## Related Topics

- [[subsystems/planner/statistics]] — how the planner reads and uses `pg_statistic`
- [[subsystems/planner/extended-statistics]] — multi-column extended statistics (`CREATE STATISTICS`)
- [[subsystems/replication/base-backup]] — `pg_basebackup` and full backups
- [[code-paths/analyze]] — how `ANALYZE` populates `pg_statistic` normally

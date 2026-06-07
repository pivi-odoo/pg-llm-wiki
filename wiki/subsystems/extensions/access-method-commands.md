---
title: "Access Method DDL and SQL Inspection API"
aliases:
  - "CREATE ACCESS METHOD"
  - "DROP ACCESS METHOD"
  - "pg_indexam_has_property"
  - "pg_index_has_property"
  - "pg_index_column_has_property"
  - "amcmds"
  - "amutils"
source_files:
  - src/backend/commands/amcmds.c
  - src/backend/utils/adt/amutils.c
symbols:
  - CreateAccessMethod
  - lookup_am_handler_func
  - indexam_property
  - pg_indexam_has_property
  - pg_index_has_property
  - pg_index_column_has_property
  - IndexAmRoutine
  - IndexAMProperty
---

`CREATE ACCESS METHOD` and `DROP ACCESS METHOD` are the DDL commands that register and remove index access methods (AMs) in the PostgreSQL catalog. Together with the SQL inspection functions in `amutils.c`, they form the complete public interface for managing the AM registry. Registration goes through `amcmds.c`. Runtime capability discovery goes through the `pg_index*_has_property` family of functions.

## Registering a New Access Method

`CREATE ACCESS METHOD name TYPE INDEX HANDLER handler_func` inserts one row into `pg_am` (`amcmds.c`, `CreateAccessMethod()`). The row stores only the AM name, its type character (`i` for index, `t` for table), and the OID of the handler function. The catalog stores no capability flags. Every behavioral detail lives inside the `IndexAmRoutine` struct that the handler returns at runtime.

Before writing the catalog row, `CreateAccessMethod()` validates the handler function via `lookup_am_handler_func()`. Validation requires two conditions. The function must accept exactly one argument of type `internal`. Its return type must be `index_am_handler` for index AMs (or `table_am_handler` for table AMs). If either condition fails, `CreateAccessMethod()` rejects registration with an error before any catalog mutation occurs. The function signature check exists because PostgreSQL calls the handler in a low-level context. A handler that accepts wrong arguments or returns an unexpected type would be undetectable at call time.

After insertion, `CreateAccessMethod()` records a `DEPENDENCY_NORMAL` dependency from the `pg_am` row to the handler function (`recordDependencyOn()`, `amcmds.c`). This means `DROP FUNCTION` on the handler fails unless you drop the AM first or specify `CASCADE`. If an extension install script executes the `CREATE ACCESS METHOD` statement, `recordDependencyOnCurrentExtension()` also ties the AM's lifetime to the extension. `DROP EXTENSION` then removes both.

Only superusers can execute `CREATE ACCESS METHOD`. This restriction exists because a malformed handler can crash the backend. The system calls the handler function in performance-critical paths. There is no isolation boundary between the handler and the core.

## Dropping an Access Method

`DROP ACCESS METHOD` removes the `pg_am` row and follows the standard dependency-tracking path. If any indexes of that AM type still exist, the drop fails unless you specify `CASCADE`. `CASCADE` then drops all dependent indexes. That, in turn, may cascade to views, constraints, or other objects that relied on those indexes. PostgreSQL does not drop the handler function itself unless it has no other dependents. The dependency runs from the AM to the function, not the reverse.

## The Dynamic Capability Model

`pg_am` is intentionally narrow. The only AM-level metadata stored in the catalog is the handler OID. PostgreSQL discovers all capabilities — whether the AM supports unique indexes, multi-column indexes, ordering, bitmap scans, and so on — at runtime, by calling the handler and examining the returned `IndexAmRoutine` struct (defined in `src/include/access/amapi.h`).

This design means that no `ALTER ACCESS METHOD` command exists to update capability flags. Capabilities are immutable properties of the handler implementation. It also means that the planner and DDL commands must call the handler (or use a cached copy) whenever they need to know what an AM supports. The [[subsystems/extensions/custom-index-am|custom index AM]] page covers the full `IndexAmRoutine` struct and its callback table.

## SQL Property Inspection

`amutils.c` provides three SQL functions that expose AM capabilities to client tooling, query planners, and extension developers without requiring direct access to C headers or catalog internals.

**`pg_indexam_has_property(amoid, property)`** queries AM-wide capabilities — properties that are fixed for the AM regardless of which specific index you look at. The meaningful property names at this level are `can_order`, `can_unique`, `can_multi_col`, `can_exclude`, and `can_include`. These correspond directly to fields in `IndexAmRoutine` (`amcanorder`, `amcanunique`, `amcanmulticol`, and so on).

**`pg_index_has_property(indexrelid, property)`** queries properties of a specific index. The relevant names here are `clusterable`, `index_scan`, `bitmap_scan`, and `backward_scan`. Although these currently depend only on the AM (not on anything unique to the individual index), the function takes an index OID rather than an AM OID to keep the door open for per-index variation in the future.

**`pg_index_column_has_property(indexrelid, attno, property)`** queries per-column properties. The set of meaningful names includes `asc`, `desc`, `nulls_first`, `nulls_last`, `orderable`, `distance_orderable`, `returnable`, `search_array`, and `search_nulls`. `test_indoption()` reads some of these (`asc`, `desc`, `nulls_first`, `nulls_last`) from the `indoption` vector in `pg_index`. They are only meaningful when the AM supports ordering. Others (`returnable`, `distance_orderable`) require calling the AM's `amproperty` handler because the answer depends on index-level state that only the AM knows.

All three functions route through the shared `indexam_property()` helper in `amutils.c`. That helper first calls the AM's `amproperty()` callback if one is registered. This gives the AM first refusal to answer the query. If `amproperty()` declines (returns false), the generic logic in `indexam_property()` answers from the `IndexAmRoutine` fields and `pg_index` catalog data. This two-phase dispatch is what allows extension AMs to declare novel property names. The AM's `amproperty()` implementation can recognise and answer custom names that the generic code would return NULL for.

Property names are strings rather than enums in the SQL API precisely for this reason. The internal `IndexAMProperty` enum (`AMPROP_ASC`, `AMPROP_CAN_UNIQUE`, etc.) is a performance shortcut. `lookup_prop_name()` converts the string to an enum value before the dispatch, so the generic switch statements avoid repeated string comparisons. A property name that `lookup_prop_name()` does not recognise returns `AMPROP_UNKNOWN`. The AM's `amproperty()` handler can still match `AMPROP_UNKNOWN` against the raw string argument.

```sql
-- Does btree support unique indexes?
SELECT pg_indexam_has_property(am.oid, 'can_unique')
FROM pg_am am WHERE amname = 'btree';

-- Can a specific index be used for a bitmap scan?
SELECT pg_index_has_property('my_idx'::regclass, 'bitmap_scan');

-- Is column 1 of an index returned in ascending order?
SELECT pg_index_column_has_property('my_idx'::regclass, 1, 'asc');
```

## Relevance for Extension Authors

Extension authors implementing a custom index AM (such as a vector similarity index or a full-text structure) need `CREATE ACCESS METHOD` in their install script and `DROP ACCESS METHOD` in their uninstall script. The author must create the handler function before the `CREATE ACCESS METHOD` statement that references it. Both should be wrapped in the same extension, so `DROP EXTENSION` removes both cleanly.

The `pg_index*_has_property` functions are useful for any tooling — query builders, ORMs, schema introspection utilities — that needs to understand what operations a given index supports without hardcoding AM names. An extension AM that supports novel properties (for example, an approximate-match AM exposing `'approximate'` as a property) implements `amproperty()` in its handler to answer those queries. This makes them visible to standard SQL tooling without any core changes.

## See also

- [[subsystems/extensions/custom-index-am|Custom Index Access Methods]] — the `IndexAmRoutine` struct, callback table, and capability flags
- [[subsystems/extensions/overview|Extension System Overview]] — how extension install scripts interact with DDL commands
- [[subsystems/catalog/core-catalogs|Core Catalogs]] — `pg_am` schema and the dependency system
- [[subsystems/extensions/define-objects|Defining Extension Objects]] — registration patterns for SQL-callable C functions

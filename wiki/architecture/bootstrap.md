---
title: "Bootstrap Mode and Cluster Initialization"
aliases:
  - bootstrap mode
  - initdb internals
  - BKI
  - Backend Interface
  - catalog bootstrap
source_files:
  - src/backend/bootstrap/bootstrap.c
  - src/include/bootstrap/bootstrap.h
symbols:
  - BootstrapModeMain
  - CheckerModeMain
  - boot_openrel
  - DefineAttr
  - InsertOneTuple
  - InsertOneValue
  - build_indices
  - index_register
  - boot_get_type_io_data
  - TypInfo
  - IndexList
  - boot_reldesc
---

Bootstrap mode is the mechanism by which `initdb` creates a fresh PostgreSQL cluster from nothing. The bootstrap backend reads a sequence of low-level commands from a BKI (Backend Interface) script. At this point no SQL can be parsed, no system catalogs exist yet, and no working syscache is available. It uses these commands to physically create the first catalog tables, populate their initial rows, and build the indexes on them. Every PostgreSQL installation ultimately rests on what bootstrap mode wrote. A running backend depends on the system catalogs for almost everything: type information, function signatures, operator definitions, index metadata. But PostgreSQL stores those catalogs in tables. Reading a table's contents requires the type's I/O functions. Finding those I/O functions itself requires a catalog lookup. This circular dependency means the normal query engine cannot bootstrap itself from nothing. Bootstrap mode breaks the circularity by bypassing most of the normal infrastructure: it does not use the SQL parser, the planner, the executor, or the syscache. Instead, it provides a stripped-down command language and a hardwired table of type information (`TypInfo[]` in `bootstrap.c`). This table covers only the small set of primitive types needed to describe catalog columns: `bool`, `bytea`, `int2`, `int4`, `oid`, `text`, `name`, and a handful of others. This static table is the only type knowledge available during bootstrap. It never changes and requires no catalog lookup.

## The BKI input format

The `initdb` program does not actually create catalog rows itself. It passes a BKI script to a postgres process running in `--boot` mode. The `genbki.pl` script generates the BKI script at build time from the `pg_*.h` catalog header files. The BKI script encodes the complete definition of every core system catalog and every initial row those catalogs should contain.

The BKI command language is intentionally minimal. Its vocabulary covers four operations:

- `create` — declare a new heap relation and its attribute list
- `insert` — insert one row of literal values into the current relation
- `open` / `close` — set or clear the current relation
- `build indices` — flush and build all previously registered indexes

No DDL dialect, no expressions, no subqueries. The bootstrap parser (`boot_yyparse()`, `bootstrap.c`) processes this language directly. A full SQL parser is never involved.

## How catalog rows are inserted

When the bootstrap backend opens a relation (`boot_openrel()`), it stores the open descriptor in the global `boot_reldesc`. Subsequent `insert` commands call `DefineAttr()` for each column value and `InsertOneTuple()` to commit the assembled row. These functions write directly to the heap via the access method layer — the same `heap_insert()` path used by normal backends — but with the syscache and catalog-cache layers entirely absent.

Because there is no syscache during bootstrap, `boot_get_type_io_data()` resolves type I/O for each column value. It first looks up type information from the static `TypInfo[]` array. Once bootstrap has populated `pg_type` itself, it looks up from a secondary in-memory list called `Typ` (a `List` of `typmap` structs) instead. This two-phase lookup avoids the catch-22 of needing `pg_type` in order to insert into `pg_type`.

The BKI script also handles nullability semantics through explicit annotations (`BOOTCOL_NULL_FORCE_NULL`, `BOOTCOL_NULL_FORCE_NOT_NULL`, `BOOTCOL_NULL_AUTO`) rather than through constraint enforcement, because constraint machinery is unavailable.

Bootstrap inserts all rows under a single transaction that encompasses the entire bootstrap run. The [[subsystems/memory/contexts|memory context]] used for per-row attribute storage (`nogc`, a special non-garbage-collected context in `bootstrap.c`) persists for the duration of the bootstrap session to avoid premature deallocation of attribute data referenced from open heap pages.

## Index registration and deferred building

Bootstrap cannot create an index on a table until it has populated the table. Index creation requires reading the existing heap pages. Bootstrap mode handles this with a two-phase approach: during the catalog creation pass, `index_register()` records each index to be built in a linked list (`ILHead`, of type `IndexList`). After bootstrap has inserted all catalog rows, a single `build indices` BKI command triggers `build_indices()`. This function walks the list and calls `index_build()` on each registered index. At that point the heap pages exist, the index access methods are available, and the build proceeds normally.

## The relmapper and catalog self-reference

Some of the earliest catalog tables — most importantly `pg_class` and `pg_attribute` — have a property that makes them impossible to handle with the normal relation-lookup path: their own metadata is stored in themselves. To look up `pg_class` through normal catalog machinery, a backend would need to first look up `pg_class` to find where `pg_class` lives.

PostgreSQL resolves this with the relation mapper (`relmapper.c`). The mapper maintains a small flat file (`pg_filenode.map`) in the data directory and in each database directory. This file records the filenode numbers for a fixed set of relations whose OIDs cannot be determined from `pg_class` in the normal way. During bootstrap, PostgreSQL marks every catalog whose relfilenode is recorded in the map with `RELKIND_TOASTVALUE = false` and `mapped_tables = true`. Subsequent access to those tables bypasses `pg_class` and consults the map file directly. This is why `pg_class` and `pg_attribute` themselves, along with `pg_type` and a few others, survive without needing to look themselves up.

## Checker mode

In addition to `--boot`, the postgres binary accepts `--check`, which runs `CheckerModeMain()`. This mode starts up just far enough to allocate shared memory and semaphores. It then exits immediately. Its purpose is to validate that the GUC settings governing shared memory sizing are sane before `initdb` proceeds further. The actual shared memory allocation happens via `CreateSharedMemoryAndSemaphores()` before `CheckerModeMain()` is ever reached. If that call succeeds, the check passes.

## Transition to normal operation

When the bootstrap session finishes, the postgres process exits. `initdb` then takes over again. It then runs a series of standalone postgres invocations in single-user mode to execute SQL scripts that fill out the rest of the catalog — the `information_schema`, default privileges, procedural languages, and built-in functions. The cluster is not ready for normal operation until those scripts complete. Only then does `initdb` create `pg_hba.conf` and `postgresql.conf`. It also marks the cluster complete at this point.

The shared memory allocation during bootstrap uses the same `CreateSharedMemoryAndSemaphores()` path as a normal postmaster startup (`bootstrap.c`). This means bootstrap processes any `shared_preload_libraries` entries specified on the bootstrap command line. In practice `initdb` does not pass any preload libraries. The path is shared regardless. As a result, extensions that register shared memory can in principle participate in the bootstrap environment.

## See also

- [[architecture/startup-sequence|Startup Sequence]]
- [[architecture/shared-memory|Shared Memory]]
- [[subsystems/catalog/core-catalogs|System Catalogs]]
- [[subsystems/memory/contexts|Memory Contexts]]

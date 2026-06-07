---
title: "Relation Mapper"
aliases:
  - relmapper
  - pg_filenode.map
  - RelationMapOidToFilenumber
  - mapped catalog
  - mapped relation
tags:
  - theme/durability
source_files:
  - src/backend/utils/cache/relmapper.c
  - src/include/utils/relmapper.h
symbols:
  - RelMapFile
  - RelMapping
  - RelationMapOidToFilenumber
  - RelationMapFilenumberToOid
  - RelationMapUpdateMap
  - RelationMapInvalidate
  - AtEOXact_RelationMap
  - CheckPointRelationMap
  - RELMAPPER_FILENAME
---

For most tables, `pg_class.relfilenode` identifies the physical file that stores the table's data. That approach breaks down for a small set of "mapped" catalogs — catalogs that must be accessible before the system can read `pg_class` at all, or catalogs that are shared across databases. The **relation mapper** (`relmapper.c`) solves this problem. It maintains a separate `pg_filenode.map` file that records the OID-to-filenumber mappings for these special catalogs, bypassing `pg_class` entirely.

## The bootstrap problem

`pg_class` itself is the prototypical mapped catalog. To read `pg_class.relfilenode` for any table, the system must first open `pg_class`. But to open `pg_class`, the system needs its own file number. `initdb` breaks this circular dependency: it hardcodes the initial file numbers in the map file it creates. Other catalogs are also mapped: `pg_attribute`, `pg_proc`, `pg_type`, and a handful of others that are "nailed" into the relcache for fast access. These catalogs must be accessible during the early phases of startup, before `pg_class` entries can be trusted.

**Shared catalogs** form a second category of mapped relations. Shared catalogs like `pg_authid`, `pg_database`, and `pg_tablespace` are physically stored once in the global `pg_global` tablespace and shared by all databases in the cluster. Relocating a shared catalog's file (e.g. after `VACUUM FULL`) would require updating `pg_class.relfilenode` in every database — which is impractical. Instead, shared catalog file numbers live in a separate shared map file.

A catalog is mapped if `pg_class.relfilenode` is zero (i.e., `InvalidRelFileNumber`). When the relcache opens such a relation (`relcache.c`), it calls `RelationMapOidToFilenumber` to find the actual file number instead of reading `relfilenode` from the tuple.

## The map file format

The map file is a flat binary file named `pg_filenode.map` in the database directory. For shared catalogs, it lives in the `global/` directory under the data directory. Its layout is:

```c
typedef struct RelMapFile {
    int32       magic;              /* RELMAPPER_FILEMAGIC */
    int32       num_mappings;
    RelMapping  mappings[MAX_MAPPINGS]; /* up to 64 entries */
    pg_crc32c   crc;
} RelMapFile;
```

Each `RelMapping` entry is a pair of `{Oid mapoid, RelFileNumber mapfilenumber}`. The entries are unsorted; lookups scan linearly. With at most a few dozen mapped catalogs, linear search is fast enough to be irrelevant.

A CRC covers the entire file to detect corruption. Because the file may span multiple disk sectors, overwriting it in place is unsafe — a partial write could leave a corrupt file. Instead, an update writes a new file to `pg_filenode.map.tmp`, then renames it over the live file. The OS performs this rename atomically at the filesystem level.

## In-memory state and transaction integration

The relmapper loads two in-memory copies of the map at startup and keeps them in static global variables: one for shared catalogs (`shared_map`) and one for the current database's local catalogs (`local_map`). Lookups (`RelationMapOidToFilenumber`) read from these in-memory copies without any lock.

A transaction batches updates to the map in separate `pending_` and `active_` update structs, following a two-stage protocol:

1. **Pending updates** accumulate during the transaction in `pending_shared_updates` / `pending_local_updates`. These reflect changes that should take effect only if the transaction commits.
2. **At command completion** (`AtCCI_RelationMap`), the transaction merges pending updates into `active_*` updates, which take effect for subsequent commands in the same transaction.
3. **At transaction commit** (`AtEOXact_RelationMap`), `perform_relmap_update` writes the active updates to disk. It writes a WAL record and renames the new file into place.
4. **On abort**, the transaction discards the pending and active update structs, with no disk changes.

The restriction against map updates inside subtransactions simplifies this design considerably: it removes the need for a savepoint-aware undo mechanism.

## WAL logging and crash recovery

A WAL record (resource manager `RM_RELMAP_ID`) precedes every map file write. The WAL record contains the full new `RelMapFile` contents, so recovery can reconstruct the map file even if the rename was not durable. `CheckPointRelationMap` flushes the shared and local maps to disk during checkpoint, ensuring that recovery does not need to replay indefinitely far back.

## Cache invalidation

When one backend updates the map file, it sends a shared-invalidation message (via `CacheInvalidateRelmap`). Other backends receive the sinval message in their invalidation processing loop and call `RelationMapInvalidate`. This sets a flag that causes the backend to reload the in-memory map from disk before the next lookup. This ensures all backends agree on the current mapping even after a `VACUUM FULL` on a mapped catalog.

Parallel workers receive the active map state at worker launch via `SerializeRelationMap` / `RestoreRelationMap`. This copies the currently active in-memory map into shared memory, so workers do not need to reload from disk.

## Related Topics

- [[subsystems/catalog/relcache|Relation Cache]]
- [[subsystems/catalog/core-catalogs|Core System Catalogs]]
- [[subsystems/wal/overview|WAL Overview]]
- [[subsystems/catalog/cache-invalidation|Cache Invalidation]]

---
title: "Auxiliary Catalog Caches"
aliases:
  - attoptcache
  - partcache
  - relfilenumbermap
  - spccache
  - ts_cache
  - attribute options cache
  - tablespace cache
  - text search cache
tags:
  - theme/caching
source_files:
  - src/backend/utils/cache/attoptcache.c
  - src/backend/utils/cache/partcache.c
  - src/backend/utils/cache/relfilenumbermap.c
  - src/backend/utils/cache/spccache.c
  - src/backend/utils/cache/ts_cache.c
symbols:
  - get_attribute_options
  - AttoptCacheHash
  - AttoptCacheEntry
  - RelationGetPartitionKey
  - RelationBuildPartitionKey
  - RelationGetPartitionQual
  - generate_partition_qual
  - RelidByRelfilenumber
  - RelfilenumberMapHash
  - RelfilenumberMapEntry
  - get_tablespace
  - get_tablespace_page_costs
  - get_tablespace_io_concurrency
  - TableSpaceCacheHash
  - lookup_ts_parser_cache
  - lookup_ts_dictionary_cache
  - lookup_ts_config_cache
  - TSParserCacheEntry
  - TSDictionaryCacheEntry
  - TSConfigCacheEntry
  - InvalidateAttoptCacheCallback
  - InvalidateTableSpaceCacheCallback
  - RelfilenumberMapInvalidateCallback
  - InvalidateTSCacheCallBack
---

PostgreSQL's main [[subsystems/catalog/relcache|relcache]] and [[subsystems/catalog/syscache|syscache]] cover the most-used catalog data, but several categories of derived or infrequently-accessed information are expensive enough to recompute per lookup that they deserve their own backend-local caches. Five modules in `src/backend/utils/cache/` each maintain a small hash table for one such category, register a [[subsystems/catalog/cache-invalidation|cache invalidation]] callback to stay consistent, and load lazily on first use.

## Common structure

Each of the five caches follows the same pattern. A static `HTAB *` pointer, initially `NULL`, is the cache's backing store. The first call to the public lookup function initializes the hash table in `CacheMemoryContext`, registers a syscache or relcache invalidation callback, and then performs the lookup. Subsequent calls check the hash first and only fall through to a catalog scan on a miss.

On invalidation, most of these caches flush all entries rather than the single changed entry. The comment in `attoptcache.c` explains the reasoning directly: the tables they front are rarely modified and usually small. A full flush is therefore cheap and simpler to implement correctly. The text-search cache (`ts_cache.c`) takes a slightly softer approach — it marks entries `isvalid = false` rather than removing them, so the hash slot can be reused without reallocating on revalidation.

## Attribute options cache

`pg_attribute` stores column storage options — `n_distinct`, [[subsystems/storage/fillfactor|fillfactor]], and compression settings — in the variable-length `attoptions` field. Parsing a `bytea` reloptions array into a structured `AttributeOpts` on every planner or ANALYZE access would be wasteful.

`attoptcache.c` maintains `AttoptCacheHash`, keyed by `(attrelid, attnum)`. The public entry point `get_attribute_options()` looks up the key, and on a miss reads `pg_attribute` via `SearchSysCache2(ATTNUM, ...)`, calls `attribute_reloptions()` to parse the binary, and copies the result into `CacheMemoryContext`. The returned pointer is always a `palloc` copy in the caller's context; the cached copy stays in `CacheMemoryContext` and is freed only when the entire cache is flushed.

`attoptcache.c` registers invalidation against the `ATTNUM` syscache (`CacheRegisterSyscacheCallback`), so any `pg_attribute` update triggers a complete flush of all entries. Because attribute options are not consulted during tight execution loops — the planner and ANALYZE are the primary callers — the coarse invalidation strategy is acceptable.

## Partition descriptor cache

A partitioned relation's `PartitionKey` (column list, strategy, operator class information, comparison support functions) requires reading `pg_partitioned_table` and resolving operator class entries from `pg_opclass`. This work is expensive enough that PostgreSQL caches the result directly on the `RelationData` struct in the relcache, in a dedicated child [[subsystems/memory/contexts|memory context]] (`rd_partkeycxt`).

`partcache.c` provides `RelationGetPartitionKey()`, which checks `rd_partkey` and calls `RelationBuildPartitionKey()` on the first access. `RelationBuildPartitionKey()` allocates a private memory context initially parented to `CurTransactionContext`, fills in the `PartitionKey` struct (strategy, per-column attribute numbers, opfamily OIDs, collation OIDs, and resolved `FmgrInfo` support function pointers), then reparents the context to `CacheMemoryContext` only after all allocation succeeds. This two-phase context ownership prevents memory leaks if the function errors partway through.

PostgreSQL caches the partition constraint (the `CHECK`-like predicate a partition must satisfy) separately on `rd_partcheck` / `rd_partcheckcxt`. `generate_partition_qual()` builds it recursively up the partition hierarchy — a leaf partition's constraint is its own bound combined with its parent's. Once built, `rd_partcheckvalid = true` lets subsequent calls return a copy without redoing the catalog reads.

Because partition metadata lives on the `RelationData` struct, invalidation rides the ordinary relcache invalidation mechanism. When the relcache entry is rebuilt, PostgreSQL frees the partition key and constraint contexts and resets the fields to `NULL`/`false`, triggering a rebuild on next access.

## Relfilenode-to-OID reverse map

WAL replay and recovery code encounters page images identified by `(tablespace OID, relfilenode)`. It then needs to find the corresponding relation OID to open the relation. PostgreSQL stores the forward direction — OID to relfilenode — in `pg_class.relfilenode` and caches it in the relcache. The reverse direction has no direct index, so `relfilenumbermap.c` provides it via `RelidByRelfilenumber()`.

On a miss, `RelidByRelfilenumber()` scans `pg_class` using `ClassTblspcRelfilenodeIndexId` (an index on `(reltablespace, relfilenode)`), skipping temporary relations. Temporary relations may share relfilenumbers across backends. For shared-catalog relations (global tablespace) it calls `RelationMapFilenumberToOid()` from [[subsystems/catalog/relmapper|relmapper]] instead. `RelidByRelfilenumber()` stores results — including negative results (no matching relation) — in `RelfilenumberMapHash`, keyed by `(reltablespace, relfilenumber)`.

This cache registers a relcache invalidation callback (`CacheRegisterRelcacheCallback`) rather than a syscache callback, since changes to `pg_class` that affect the mapping arrive as relcache invalidation events. On a full reset (`relid == InvalidOid`) or when the specific relation's entry is invalidated, the cache removes the corresponding hash entries. The cache always flushes negative entries on any invalidation, since a previously missing relation might now exist.

## Tablespace options cache

The planner adjusts I/O cost estimates for relations stored on non-default tablespaces using per-tablespace overrides for `random_page_cost`, `seq_page_cost`, `effective_io_concurrency`, and `maintenance_io_concurrency`. `pg_tablespace` stores these as a parsed `spcoptions` reloptions string.

`spccache.c` maintains `TableSpaceCacheHash`, keyed by tablespace OID. The internal function `get_tablespace()` drives lookups; the public functions `get_tablespace_page_costs()`, `get_tablespace_io_concurrency()`, and `get_tablespace_maintenance_io_concurrency()` call it and apply GUC defaults when the tablespace has no override. `spccache.c` transparently remaps `InvalidOid` to `MyDatabaseTableSpace`, so callers do not need to handle the default tablespace specially.

Invalidation flushes the entire hash on any `pg_tablespace` change, registered via `CacheRegisterSyscacheCallback(TABLESPACEOID, ...)`. This is safe because there are typically very few tablespaces per database cluster and they change rarely.

## Text-search configuration cache

Full-text search is latency-sensitive: every call to `to_tsvector()` or `to_tsquery()` must locate the right parser, walk the token-to-dictionary mapping in `pg_ts_config_map`, and invoke the `lexize` function of each applicable dictionary. Performing catalog lookups for all of this on every document would be prohibitive during bulk indexing.

`ts_cache.c` maintains three independent hash tables — `TSParserCacheHash`, `TSDictionaryCacheHash`, and `TSConfigCacheHash` — for parsers, dictionaries, and configurations respectively.

A `TSParserCacheEntry` stores the `FmgrInfo` structs for the five parser methods (`prsstart`, `prstoken`, `prsend`, `prsheadline`, `prslextype`), resolved at cache fill time via `fmgr_info_cxt()`. A `TSDictionaryCacheEntry` is heavier: it allocates a private child context (`dictCtx`) in `CacheMemoryContext`, runs the dictionary's template `init` function (if any) in that context, and stores the resulting `dictData` pointer along with the resolved `lexize` `FmgrInfo`. A `TSConfigCacheEntry` holds the parser OID and the full token-to-dictionary mapping built by scanning `pg_ts_config_map`.

Each hash table also maintains a `lastUsed*` pointer for a fast single-entry "last-used" check before hitting the hash — useful because `to_tsvector` calls typically use the same configuration repeatedly within a session.

Invalidation uses `InvalidateTSCacheCallBack`, registered against the relevant syscache IDs (`TSPARSEROID`, `TSDICTOID`, `TSTEMPLATEOID`, `TSCONFIGOID`, `TSCONFIGMAP`). Unlike the other auxiliary caches, invalidation marks entries `isvalid = false` rather than removing them. On next lookup, the cache finds the stale entry in the hash and refills it in place, reusing the existing hash slot. Dictionary entries reuse the existing `dictCtx` by resetting and re-identifying it. `ts_cache.c` also clears the `default_text_search_config` GUC's cached OID (`TSCurrentConfigCache`) whenever the config hash is invalidated.

## See also

- [[subsystems/catalog/relcache|Relation Cache (relcache)]]
- [[subsystems/catalog/syscache|Catalog Caches (syscache/catcache)]]
- [[subsystems/catalog/cache-invalidation|Catalog Cache Invalidation]]
- [[subsystems/catalog/relmapper|Relation Mapper]]
- [[subsystems/partitioning/overview|Partitioning Overview]]
- [[subsystems/planner/cost-model|Planner Cost Model]]

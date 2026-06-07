---
title: Catalog Caches
aliases:
  - catcache
  - syscache
  - relcache
  - catalog cache
tags:
  - theme/caching
source_files:
  - src/backend/utils/cache/syscache.c
  - src/backend/utils/cache/catcache.c
  - src/backend/utils/cache/relcache.c
  - src/include/utils/syscache.h
  - src/include/utils/catcache.h
  - src/backend/utils/time/snapmgr.c
  - src/backend/storage/ipc/sinvaladt.c
symbols:
  - SearchSysCache
  - SearchSysCache1
  - ReleaseSysCache
  - SearchCatCacheInternal
  - SearchCatCacheMiss
  - CatCacheInvalidate
  - ResetCatalogCaches
  - CatalogCacheInitializeCache
  - RelationBuildDesc
  - RelationIdGetRelation
  - GetCatalogSnapshot
  - InvalidateCatalogSnapshot
  - SICleanupQueue
  - CatCache
  - CatCTup
  - CatCList
  - RelIdCacheEnt
---

# Catalog Caches

Every query PostgreSQL executes must consult the system catalogs: to resolve a table name to an OID, to look up a function's argument types, to find the operator that implements `=` for a given type, to check whether a column has a default value. Without caching, each of these lookups would be a heap scan or index scan against `pg_class`, `pg_proc`, `pg_operator`, and dozens of other system tables. Even the simplest `SELECT 1 + 1` touches multiple catalog entries. Caching catalog data is therefore not an optimisation — it is a prerequisite for acceptable performance.

PostgreSQL maintains two separate per-backend caches for catalog data: the *catcache* (catalog cache), which stores individual catalog tuples keyed by one or more attributes, and the *relcache* (relation cache), which stores fully assembled relation descriptors. A shared-memory invalidation protocol backs both, keeping them coherent across concurrent DDL.

## The Catcache: Tuple-Level Caching

The catcache is a set of per-backend hash tables, one for each catalog/key combination that the planner, parser, and executor need to look up frequently. A `CatCache` struct describes each table; the `cacheinfo[]` array (`syscache.c`) pre-declares it at compile time. At PostgreSQL 16 there are over 80 such caches, covering everything from `PROCOID` (function lookup by OID) to `CASTSOURCETARGET` (cast lookup by source and target type OID) to `AMOPSTRATEGY` (access method operator lookup by family, input types, and strategy number).

Every catcache entry maps a specific key combination to exactly one catalog tuple. The cache hashes the key to a bucket in its open-addressed hash table; within a bucket, it keeps entries in LRU order, so that the most frequently re-used entries float to the front. Each entry is a `CatCTup`:

| Field | Type | Purpose |
|---|---|---|
| `hash_value` | `uint32` | precomputed hash of the key values |
| `keys[]` | `Datum[4]` | copy of the key attribute values |
| `refcount` | `int` | number of callers currently holding this entry |
| `dead` | `bool` | marked for eviction but still referenced |
| `negative` | `bool` | asserts that no matching tuple exists |
| `tuple` | `HeapTupleData` | the cached tuple (positive entries only) |
| `c_list` | `CatCList *` | owning list entry, if any |

The `CatCache` itself tracks the hash table geometry and the catalog backing each cache:

| Field | Purpose |
|---|---|
| `cc_reloid` | OID of the system catalog being cached |
| `cc_indexoid` | OID of the unique index used for miss lookups |
| `cc_nkeys` | number of key columns (1–4) |
| `cc_keyno[]` | attribute numbers of the key columns |
| `cc_nbuckets` | current number of hash buckets (always a power of two) |
| `cc_ntup` | live tuple count in this cache |
| `cc_hashfunc[]` | per-key hash functions (hard-coded fast paths for OID, name, int2, int4, text) |
| `cc_fastequal[]` | per-key equality functions |

The hash uses XOR-with-rotation across the per-key hashes, so a two-key lookup like `(relid, attname)` for `ATTNAME` produces a single 32-bit value, which the cache masks to a bucket index with a cheap bitmask.

### The Lookup Path

Callers never interact with `CatCache` directly. The public surface is `SearchSysCache()` and its arity-specific variants (`SearchSysCache1()` through `SearchSysCache4()`), which take a `SysCacheIdentifier` constant and up to four key datums. These are thin wrappers that index into a fixed array of `CatCache` pointers and delegate to `SearchCatCacheInternal()` (`catcache.c`).

The fast path computes the hash, finds the bucket, and scans the LRU chain looking for a non-dead entry whose hash and key values match. On a hit — including a negative hit — no I/O occurs and the bucket scan typically terminates after one or two comparisons. On a miss, `SearchCatCacheMiss()` opens the underlying catalog relation, executes a syscatalog index scan via `systable_beginscan()`, and inserts the resulting tuple into the cache before returning it.

`SearchSysCache` increments the `refcount` of every positive entry it returns and registers the entry with the current [[subsystems/memory/resource-owner|ResourceOwner]]. Callers must call `ReleaseSysCache()` when done, which decrements the refcount. If an entry with a non-zero refcount receives an invalidation signal, the cache marks it `dead` rather than freeing it immediately. It frees the entry when the last caller releases it.

### Negative Caching

When a syscache miss finds no matching tuple in the catalog, the cache inserts a *negative entry* — a `CatCTup` with `negative = true` and no real tuple. Future lookups for the same key hit this entry and return `NULL` without touching the heap. The cache treats negative entries identically to positive entries for invalidation purposes: if a DDL statement creates the previously-missing object, it invalidates and discards the negative entry. This is particularly valuable for type resolution and operator lookup, where the parser probes many combinations that do not exist.

### Partial-Key List Caching

Beyond single-tuple lookups, the catcache supports *list searches* through `SearchSysCacheList()`. A list search uses only the first K of an N-key cache's keys; it collects all tuples matching the partial key into a `CatCList` and caches them together. A `CatCList` holds an array of `CatCTup` pointers, a refcount, and its own hash value. Any invalidation of the underlying catalog flushes all `CatCList` entries for that cache wholesale, because it is too expensive to determine which partial-key sets are still valid.

## The Relcache: Relation-Level Caching

The relcache stores fully assembled `RelationData` descriptors. Where a catcache entry is a single catalog tuple, a relcache entry synthesises data from several catalogs: `pg_class` (the base relation row), `pg_attribute` (all column definitions via `RelationBuildTupleDesc()`), `pg_constraint` (check constraints), `pg_attrdef` (default expressions), `pg_rewrite` (rewrite rules), and index metadata. Building one entry requires multiple catalog scans, which is why PostgreSQL keeps the relcache and catcache separate — the cost profiles differ by orders of magnitude.

The relcache is a backend-local hash table, `RelationIdCache`, keyed by relation OID. Each entry (`RelIdCacheEnt`) maps an OID to a `Relation` pointer, which is the live `RelationData` structure. Backends open relations with `RelationIdGetRelation()` and close them with `RelationClose()`. While open, the relation's reference count prevents the descriptor from being destroyed.

Because relcache entries are expensive to build, PostgreSQL serialises a subset of them — the critical shared-catalog descriptors and the per-database catalog descriptors — into binary files (`pg_global/pg_internal.init` and `$PGDATA/base/<oid>/pg_internal.init`) at the end of each transaction that modifies those relations. On backend startup, `RelationCacheInitializePhase2()` and `RelationCacheInitializePhase3()` attempt to load these files rather than scanning the catalogs from scratch, which dramatically reduces startup overhead in deployments with many backends.

## Cache Invalidation

Caching catalog data creates a coherence problem: when one backend executes `ALTER TABLE` and modifies `pg_attribute`, all other backends holding cached entries from that table must discard them. PostgreSQL solves this with a *shared invalidation queue* managed by `sinvaladt.c`.

When `CatalogTupleInsert()` or `CatalogTupleUpdate()` modifies a catalog tuple, the write path queues a `SharedInvalidationMessage` into shared memory before the transaction commits. The message carries the cache ID and the hash value of the affected key (not the TID, to survive VACUUM FULL on system catalogs). At the start of each new command and at transaction boundaries, each backend calls `AcceptInvalidationMessages()`, which drains any pending messages from its read pointer in the queue and calls `CatCacheInvalidate()` for each catcache message, plus the relcache equivalent for relcache messages.

`CatCacheInvalidate()` walks the hash bucket for the given hash value and marks matching entries dead. It also flushes all `CatCList` entries for that cache unconditionally, because a partial-key list could include or exclude the changed tuple. `CatCacheInvalidate()` marks entries with non-zero refcounts dead but does not free them; it removes them when their last reference is dropped.

### The Sinval Queue and Overflow

The shared invalidation queue is a fixed-size circular buffer of `MAXNUMMESSAGES` (4096) entries in shared memory. Each backend maintains a `nextMsgNum` read pointer; the queue advances `maxMsgNum` as new messages arrive. Under normal conditions, backends drain the queue frequently enough that it never fills.

If a backend falls far behind — because it is idle inside a long transaction — and the queue would overflow, `SICleanupQueue()` sets that backend's `resetState` flag to `true`. When the lagging backend next calls `AcceptInvalidationMessages()` and finds `resetState` set, it calls `ResetCatalogCaches()`, which discards every entry in every catcache and relcache. This is a correctness mechanism, not merely a performance concern: PostgreSQL cannot permit a backend that missed invalidation messages to use stale catalog data.

The consequence is that holding a long idle transaction while other backends execute DDL can force a full cache flush on the idle backend's next command. After a flush, the first query re-populates the caches from the catalogs, incurring the normal miss costs.

### Invalidation During Entry Construction

A race exists between processing an invalidation message and inserting a new cache entry: invalidation could arrive while a cache miss is actively scanning the catalog and building the entry. To handle this, catcache maintains a `CatCInProgress` stack of in-flight entry constructions. `CatCacheInvalidate()` checks this stack and marks any matching in-progress entry as `dead`. `SearchCatCacheMiss()` checks whether construction marked the freshly-built entry dead — this can happen because `table_open()` calls `AcceptInvalidationMessages()` — and restarts the catalog scan if so.

## Catalog Snapshots

Catalog scans use a special snapshot managed by `GetCatalogSnapshot()` (`snapmgr.c`). Unlike regular MVCC snapshots, PostgreSQL shares the catalog snapshot across all catalog accesses within a single command and deliberately invalidates it more aggressively. Whenever the backend processes an invalidation message, `InvalidateCatalogSnapshot()` discards the current catalog snapshot, so the next catalog scan acquires a fresh one. This narrows the window during which a backend might scan a catalog with a snapshot old enough to miss a recently committed row. That risk applies specifically to rows for which an invalidation message has already been processed.

For relations that have no syscache coverage, `GetNonHistoricCatalogSnapshot()` checks whether catcache or snapshot invalidation messages are sent for the target relation. If neither is, the function refreshes the snapshot unconditionally on every scan, accepting the cost to ensure correctness.

During logical decoding, `GetCatalogSnapshot()` returns a *historic snapshot* pinned to the LSN being decoded. This ensures the decoder sees catalog state consistent with the replication stream it is processing, rather than the current catalog state.

## Interaction with DDL

The combined effect of these mechanisms is that catalog changes made by DDL are visible to other backends only after their caches have been invalidated. A backend executing a query never sees a partially-updated catalog because:

1. DDL writes are transactional — the catalog rows are not visible until the DDL transaction commits.
2. The system sends invalidation messages only at commit time, so no backend discards a valid cache entry for a change that might roll back.
3. Each backend processes pending invalidations before starting each new query, so a query always begins with caches that reflect all committed catalog changes known to the invalidation queue.

This design deliberately tolerates a backend seeing a stale snapshot within a single command — the catalog snapshot is not refreshed mid-command — in exchange for avoiding the overhead of re-acquiring a snapshot on every catalog access.

## Bootstrap Constraints

During `initdb` and very early backend startup, the catcache and relcache are not yet fully operational. Several special cases handle this:

- The catcache always fetches entries for `pg_am` with heap scans, never index scans, because `pg_am` is tiny and its indexes must not be consulted before the relcache is ready.
- The system disables index scans on `pg_index` until `criticalRelcachesBuilt` is set.
- Shared-catalog entries (`AUTHNAME`, `AUTHOID`, `AUTHMEMMEMROLE`, `DATABASEOID`) fall back to heap scans until `criticalSharedRelcachesBuilt` is set.
- The catcache does not insert negative entries in bootstrap mode, because the invalidation mechanism that would later clear them is not running.

These guards ensure that the bootstrapping process can read the catalogs before the caches themselves depend on catalog data.

## Related Topics

- [[subsystems/catalog/cache-invalidation|Cache Invalidation]] — the shared invalidation queue and sinval protocol that keeps catcache and relcache coherent across backends.
- [[subsystems/catalog/relcache|Relcache]] — the relation descriptor cache that layers on top of catcache, storing fully assembled `RelationData` structs.
- [[subsystems/catalog/lsyscache|lsyscache]] — convenience wrappers over `SearchSysCache` that provide the most common catalog lookups to the rest of the backend.
- [[subsystems/catalog/auxiliary-caches|Auxiliary Caches]] — supplementary per-backend caches (type cache, operator cache, planner info caches) that sit alongside catcache.
- [[subsystems/memory/resource-owner|ResourceOwner]] — tracks live syscache references so that cache entries are released when a query or transaction ends.
- [[subsystems/transactions/snapshot|Snapshot]] — MVCC snapshots underlie catalog scans; the catalog snapshot is a specialised variant invalidated more aggressively than regular snapshots.
- [[architecture/bootstrap|Bootstrap]] — the early-startup sequence during which catcache and relcache are partially unavailable and must fall back to heap scans.
- [[subsystems/transactions/mvcc|MVCC]] — MVCC snapshots underpin catalog scans generally; the catalog snapshot used by syscache is a specialised case of the same mechanism.
- [[subsystems/locking/overview|Locking Overview]] — catalog modification requires heavyweight locks, while catcache lookups themselves take only pin-level protection.
- [[subsystems/storage/buffer-manager|Buffer Manager]] — catalog heap pages flow through the buffer manager like any other heap page.
- [[subsystems/planner/overview|Planner Overview]] — the planner is the heaviest user of syscache lookups, touching type, operator, and index caches on every query.
- [[architecture/overview|Architecture Overview]] — the per-backend nature of the caches is part of the broader shared-nothing-between-backends design.
- [[code-paths/insert|INSERT]] — `CatalogTupleInsert()` in the DDL path is where invalidation messages that keep catcache coherent are enqueued.

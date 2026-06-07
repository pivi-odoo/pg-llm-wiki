---
title: Hash Index
aliases:
  - hash indexes
  - hash access method
tags:
  - theme/storage-format
  - theme/durability
  - theme/vacuum-and-maintenance
source_files:
  - src/backend/access/hash/hash.c
  - src/backend/access/hash/hashfunc.c
  - src/backend/access/hash/hashpage.c
  - src/backend/access/hash/hashsearch.c
  - src/backend/access/hash/hashutil.c
  - src/include/access/hash.h
symbols:
  - HashMetaPageData
  - HashPageOpaqueData
  - HashScanOpaqueData
  - _hash_expandtable
  - _hash_splitbucket
  - _hash_first
  - _hash_readpage
  - _hash_doinsert
  - hashbuild
  - hashgettuple
  - _hash_hashkey2bucket
  - _hash_datum2hashkey
  - BUCKET_TO_BLKNO
---

# Hash Index

A hash index answers a single question efficiently: does a column equal a given value? It hashes the indexed column down to a 32-bit integer and uses that code to route every lookup directly to one bucket, skipping every row that cannot possibly match. When the access pattern is purely equality — user lookup by ID, session token, or UUID — a hash index can be faster than a B-tree. It eliminates the multi-level page traversal a B-tree requires, collapsing the lookup to a direct address calculation.

The trade-off is narrow. Hash indexes do not support range queries, ordering, prefix patterns like `LIKE 'foo%'`, or multi-column index scans. The [[subsystems/indexes/btree]] handles all of those; hash is the specialist.

## Physical Structure

A hash index file is divided into four kinds of pages, each identified by flag bits in the page-opaque area.

**Metapage** (block 0) is the control center. It holds the entire state needed to interpret the rest of the file: the current bucket count, the split-point counters used to locate any bucket by number, the fill-factor target, and an array of block numbers pointing to the bitmap pages. There is exactly one metapage, and it is always at block zero.

**Bucket pages** hold the actual index tuples. Each bucket is a chain that begins with a primary bucket page and may extend into overflow pages. The primary page records in its opaque area the value of `hashm_maxbucket` at the time the bucket was last split — a detail that allows readers to detect a stale cached metapage without re-acquiring its lock.

**Overflow pages** extend a bucket when its primary page is full. They are chained as a doubly-linked list using `hasho_prevblkno` and `hasho_nextblkno` in `HashPageOpaqueData`. When vacuum empties an overflow page, it can be returned to the free list and reused by a different bucket.

**Bitmap pages** track which overflow pages are free. Each bitmap page is a dense bit array: a 1-bit means the corresponding overflow page is in use, a 0-bit means it is available for reuse. The metapage holds the block numbers of all existing bitmap pages in `hashm_mapp[]`. At an 8 kB block size, a single bitmap page tracks thousands of overflow pages; up to 1024 bitmap pages can exist before the metapage runs out of room in its array.

Every non-metapage carries `HashPageOpaqueData` at the end of the page:

| Field | Purpose |
|---|---|
| `hasho_prevblkno` | On overflow pages: previous page in bucket chain. On bucket pages: `hashm_maxbucket` at last split. |
| `hasho_nextblkno` | Next page in bucket chain, or `InvalidBlockNumber` at end. |
| `hasho_bucket` | Bucket number this page belongs to. |
| `hasho_flag` | Page type bits plus transient state flags (see below). |
| `hasho_page_id` | Fixed sentinel `0xFF80` for external tools like `pg_filedump`. |

The `hasho_flag` field encodes both the page type and in-progress state. The type bits — `LH_OVERFLOW_PAGE`, `LH_BUCKET_PAGE`, `LH_BITMAP_PAGE`, `LH_META_PAGE` — are stable. The state flags are transient:

| Flag | Meaning |
|---|---|
| `LH_BUCKET_BEING_POPULATED` | A split is filling this new bucket; tuples are still arriving. |
| `LH_BUCKET_BEING_SPLIT` | This bucket is currently being split. |
| `LH_BUCKET_NEEDS_SPLIT_CLEANUP` | Tuples moved during a split have not yet been removed by vacuum. |
| `LH_PAGE_HAS_DEAD_TUPLES` | The page contains index entries pointing to deleted heap tuples. |

## The Two-Level Hashing Scheme

PostgreSQL's hash index uses a classic linear-hashing approach where the bucket count grows in a controlled, deterministic sequence. Every key is reduced to a 32-bit hash code by the operator class's hash function (registered as `HASHSTANDARD_PROC`). That code is then mapped to a bucket number using two masks stored in the metapage: `hashm_highmask` and `hashm_lowmask`.

The mapping (in `_hash_hashkey2bucket()`, hashutil.c) is:

```c
bucket = hashkey & highmask;
if (bucket > maxbucket)
    bucket = bucket & lowmask;
```

Before the first split, `highmask` and `lowmask` both select the same low-order bits, so the address space is half the next power of two. After each split round, `highmask` gains one more bit. The two-mask design means that as the table doubles, roughly half the keys in each old bucket map to the same bucket, and half map to the new one. No rehashing of the entire index is needed.

The location of a bucket's primary page given only a bucket number is computable from the metapage's `hashm_spares[]` array via the `BUCKET_TO_BLKNO` macro. This array records how many overflow pages have been allocated before each split point, allowing the correct block offset to be derived arithmetically. This is what makes splits crash-safe: a recovery process can recompute where any bucket lives without scanning pages.

## Hash Function Layer

Every hash index operator class must supply a hash function registered under support function number `HASHSTANDARD_PROC` in `pg_amproc`. This function receives a single value and returns a 32-bit integer that must distribute as uniformly as possible across the full output range. A second optional support function (`HASHEXTENDED_PROC`) accepts an additional 64-bit seed; the seeded variant is required when the index participates in hash joins that need cross-type compatibility.

The two primitive routines that most type-specific hash functions delegate to are `hash_any` and `hash_uint32` (from `common/hashfn.h`). `hash_any` takes an arbitrary byte sequence and length; it is used for variable-length types like `text`, `bytea`, and `name`, as well as fixed-width types whose representation is not a plain integer. `hash_uint32` is a cheaper mixing function optimised for 32-bit integer values; small integer types (`int2`, `int4`, `oid`, `enum`) all route through it after trivial widening or narrowing.

Several type-specific functions in `hashfunc.c` encode cross-type constraints that are not obvious from their signatures:

- Integer types of different widths (`int2`, `int4`, `int8`) must hash equal values to the same code so that hash joins can match across type boundaries. The 64-bit function folds the high and low 32-bit halves together in a sign-aware way before passing the result to `hash_uint32`.
- Floating-point types must treat `−0.0` and `+0.0` as equal (as IEEE 754 comparison does), so both are normalised to zero before hashing. All NaN values, regardless of bit pattern, are normalised to a canonical NaN. `float4` is widened to `float8` before hashing so that equal float4 and float8 values produce the same code.
- Text hashing is collation-aware. For deterministic collations the raw UTF-8 bytes are hashed directly with `hash_any`. For non-deterministic collations (ICU with strength-insensitive comparison), the key is first transformed with `pg_strnxfrm` to produce a collation key. That byte sequence is then hashed instead. This ensures that strings that compare equal under the collation also hash equal — a prerequisite for correctness.

When the access method needs to hash an index key, `_hash_datum2hashkey` (hashutil.c) looks up the operator class's support function via `index_getprocinfo` and calls it with the column's collation. For cross-type situations — for example when a query compares a stored `int4` column against an `int8` literal — `_hash_datum2hashkey_type` looks up the appropriate function from the operator family via `get_opfamily_proc`, making cross-type equality semantics consistent between the index and the executor.

NULL values are never stored in a hash index. The tuple conversion path in `_hash_convert_tuple` (hashutil.c) returns false immediately if the input value is null. The caller then skips the insertion. Because the only supported operator is strict equality, a null can never match anything. Omitting nulls from the index is therefore safe.

Once a hash code is available, it is stored directly in the index tuple rather than the original key value. The stored code is later retrieved by `_hash_get_indextuple_hashkey`, which reads the first attribute of the index tuple as a raw `uint32`. This design means the index is genuinely lossy: two different values that happen to produce the same hash code are indistinguishable at the index level. This is why every scan result requires a heap recheck.

## Bucket Splits

A split is triggered during insertion when the number of stored tuples divided by the current bucket count exceeds the fill factor (`hashm_ffactor`). The target fill factor is calculated at index creation based on page size and the [[subsystems/storage/fillfactor|fillfactor]] storage parameter (default 75%), and represents how many tuples per bucket the index aims to hold.

When `_hash_expandtable()` (hashpage.c) fires, it allocates a new bucket at position `maxbucket + 1` and then calls `_hash_splitbucket()` to redistribute entries from the old bucket. Every tuple whose hash code maps to the new bucket under the updated masks is moved there; tuples that still belong to the old bucket stay put. The old bucket page gets the `LH_BUCKET_NEEDS_SPLIT_CLEANUP` flag set on it, telling vacuum that lingering copies of moved tuples need removal.

The redistribution is idempotent. Because the split sequence follows a fixed mathematical progression — bucket 0 splits first, then 1, etc., with masks advancing in lockstep — a crash mid-split does not corrupt the index. On recovery, WAL replays the split operations. The state flags ensure that vacuum will clean up any tuples that were copied but not yet deleted. Before PostgreSQL 10, this determinism existed but was not exploited with WAL logging (see below).

A bucket being split cannot itself be split again until the cleanup is complete. `_hash_expandtable()` checks for `LH_BUCKET_NEEDS_SPLIT_CLEANUP` and skips the candidate bucket if garbage remains, so split pressure propagates to the next eligible bucket instead.

## WAL Logging and Crash Safety

Before PostgreSQL 10, hash indexes were not WAL-logged. A crash or immediate shutdown left the index in an indeterminate state. `REINDEX` was required after recovery. The risk was real enough that some installations avoided hash indexes entirely.

From PostgreSQL 10 onward, every structural change — metapage updates, bucket initialization, split operations, overflow page allocation and release, tuple deletion — emits WAL records under resource manager `RM_HASH_ID`. The `_hash_init()` function (hashpage.c) explicitly checks `RelationNeedsWAL(rel)` and logs each new page. Vacuuming records deletions with `XLOG_HASH_DELETE` and marks split-cleanup completion with `XLOG_HASH_SPLIT_CLEANUP`. Hash indexes are now fully crash-safe and replicated to standbys.

Unlogged relations still skip WAL for the main fork, but they write an init fork (via `hashbuildempty()`, hash.c) that is WAL-logged, allowing the index to be reset to an empty state on recovery.

## Index Build

Building a hash index from scratch involves a potential performance trap. If tuples are inserted in heap order, the hash codes will be scattered across buckets at random. Once the index exceeds available buffer pool, every insert will thrash between different bucket pages. To avoid this, `hashbuild()` (hash.c) compares the estimated number of initial buckets against `maintenance_work_mem` and `NBuffers`. If the index will not fit in memory, it spools all tuples through a sorter (`_h_spoolinit()`, hashsort.c) and sorts them by expected bucket number. It then inserts them in bucket order, so each bucket is filled contiguously before the next is touched.

## Scanning

Every hash index scan requires an equality key on the indexed column. Attempting a scan without one — a whole-index scan — is an error: `_hash_first()` (hashsearch.c) rejects it explicitly, because there is no practical way to lock all buckets against concurrent splits during an unkeyed traversal.

For a normal equality scan, the sequence is:

1. Hash the query value to a 32-bit code using `_hash_datum2hashkey()`.
2. Derive the target bucket number from the code using `_hash_hashkey2bucket()`.
3. Locate the bucket's primary page with `_hash_getbucketbuf_from_hashkey()`, which pins it for the duration of the scan.
4. Binary-search within each page using `_hash_binsearch()` — tuples on a page are stored in hash-code order — to find the first candidate offset.
5. Walk all matching items on the page and all overflow pages in the chain.

The critical subtlety is that the index stores hash codes, not original key values. Two different keys can hash to the same code (a hash collision). Because of this, `hashgettuple()` (hash.c) always sets `scan->xs_recheck = true`, instructing the executor to re-evaluate the original predicate against the heap tuple before returning it. The scan is inherently lossy at the index level; correctness is guaranteed by the recheck.

During a split, a scan that arrives at a bucket flagged `LH_BUCKET_BEING_POPULATED` must also check the source bucket. The `HashScanOpaqueData` tracks both buffers (`hashso_bucket_buf` for the new bucket, `hashso_split_bucket_buf` for the old one) and the flags `hashso_buc_populated` and `hashso_buc_split` to navigate both chains without missing or double-counting tuples that are mid-migration.

## Vacuum and Tuple Cleanup

Vacuum on a hash index walks every bucket from 0 to `hashm_maxbucket`. For each bucket, it acquires a cleanup lock on the primary bucket page (which blocks concurrent scans from entering), then scans the full chain identifying dead tuples via the callback from `hashbulkdelete()` (hash.c). It also removes tuples flagged `INDEX_MOVED_BY_SPLIT_MASK` from buckets that have the `LH_BUCKET_NEEDS_SPLIT_CLEANUP` flag — these are the original copies of entries that were migrated to a new bucket during a split.

After deletion, if the cleanup lock can still be obtained, `_hash_squeezebucket()` (hashovfl.c) compacts the chain by moving tuples from later overflow pages into earlier ones, then freeing the now-empty pages back to the bitmap free list. Lock chaining between pages prevents concurrent scans from seeing inconsistency during the squeeze.

If a new split occurs while vacuum is iterating, the loop detects the change by comparing `hashm_maxbucket` before and after acquiring the metapage write lock, and re-runs from the top to cover the new bucket.

## Limitations and When Not to Use a Hash Index

Hash indexes trade generality for equality speed. Several important capabilities are absent:

- **No range queries.** Hash codes are not monotone; `WHERE col > x` cannot be answered from a hash index.
- **No ordering.** Results from a hash index scan arrive in arbitrary hash-code order. `ORDER BY` queries cannot use the index to avoid a sort.
- **No prefix or pattern matching.** `LIKE 'foo%'` cannot exploit a hash index.
- **Single-column only.** The access method sets `amcanmulticol = false`. Multi-column equality conditions cannot benefit from a single hash index covering multiple columns.
- **No index-only scans.** Hash indexes do not support returning the original key value; the index stores only the hash code and the heap TID.
- **Not clustererable.** Because there is no meaningful physical ordering, `CLUSTER` cannot use a hash index.

Size is also a consideration. For low-cardinality columns or workloads with many hash collisions, overflow pages can accumulate. The effective size may then exceed an equivalent B-tree. For high-cardinality equality-only access patterns on large tables, hash indexes often outperform B-trees, but measurement on real data is advisable before committing.

For all other index access patterns — ranges, ordering, multi-column, full-text, geometric — consider [[subsystems/indexes/btree]] or the appropriate specialized index type.

## Related Topics

- [[subsystems/indexes/hash-build-overflow|Hash Build and Overflow]] — detailed coverage of overflow page allocation, the free-list bitmap, and how `_hash_squeezebucket` reclaims pages during vacuum
- [[subsystems/wal/hash-index-wal|Hash Index WAL]] — the specific WAL record types emitted by hash index operations and their redo handlers
- [[subsystems/indexes/index-am|Index Access Method Interface]] — the generic AM API that hash implements, including `aminsert`, `amgettuple`, and `ambulkdelete` entry points
- [[subsystems/indexes/btree|B-tree Index]] — the general-purpose alternative that covers range queries, ordering, and multi-column scans that hash cannot serve
- [[subsystems/executor/hash-join-spill|Hash Join Spill]] — the executor's hash join uses the same seeded hash functions (`HASHEXTENDED_PROC`) that hash indexes register, enabling cross-type join correctness
- [[subsystems/memory/dynahash|Dynamic Hash Tables]] — in-memory hash table implementation used elsewhere in the backend; shares conceptual design with the on-disk hash index bucket structure
- [[subsystems/indexes/generic-index-access|Generic Index Access]] — the index scan machinery that invokes `hashgettuple` and enforces the `xs_recheck` lossy-scan contract
- [[subsystems/wal/overview|WAL Overview]] — WAL infrastructure that makes hash indexes crash-safe since PG 10
- [[subsystems/storage/buffer-manager|Buffer Manager]] — buffer pinning and locking that hash page access relies on
- [[subsystems/locking/overview|Locking Overview]] — cleanup locks used during vacuum and split
- [[code-paths/vacuum|Vacuum]] — how [[subsystems/background/autovacuum|autovacuum]] drives `hashbulkdelete` and cleanup
- [[code-paths/insert|Insert]] — how `hashinsert` fits into the insert path

---
title: "Hash Index Build, Insertion, and Overflow Management"
aliases:
  - hash overflow pages
  - hash index build
  - hash bucket overflow
  - hash index insertion
tags:
  - theme/vacuum-and-maintenance
  - theme/concurrency-control
source_files:
  - src/backend/access/hash/hashinsert.c
  - src/backend/access/hash/hashovfl.c
  - src/backend/access/hash/hashsort.c
  - src/backend/access/hash/hashvalidate.c
  - src/include/access/hash.h
symbols:
  - _hash_doinsert
  - _hash_pgaddtup
  - _hash_addovflpage
  - _hash_freeovflpage
  - _hash_squeezebucket
  - _hash_initbitmapbuffer
  - HSpool
  - _h_spoolinit
  - _h_spool
  - _h_indexbuild
  - hashvalidate
  - HashMetaPageData
  - HashPageOpaqueData
---

A [[subsystems/indexes/hash|hash index]] stores index entries in fixed buckets, each of which starts with one primary page and can grow by chaining overflow pages when that page fills up. This page covers the four complementary mechanisms that keep this structure functional: single-tuple insertion, overflow page allocation and reclamation, the sort-based bulk build path used by `CREATE INDEX`, and the operator-class validation logic that enforces correctness before a type can be indexed.

## Single-Tuple Insertion

Every insertion into a hash index passes through `_hash_doinsert()` (`hashinsert.c`). The hash code is already baked into the index tuple — it was computed upstream in `hashinsert()` and stored in the tuple's first attribute. `_hash_doinsert()` then begins by reading it back with `_hash_get_indextuple_hashkey()` and uses it to locate the primary bucket page via `_hash_getbucketbuf_from_hashkey()`.

The write lock on the primary bucket page is the key concurrency primitive for insertion. Once that lock is held, the insertion can walk the entire bucket chain — primary page and any overflow pages — without acquiring additional page-level locks. Overflow pages are treated as implicitly protected by the bucket lock, so the lock is held for the full duration of the insert traversal.

Before inserting, `_hash_doinsert()` checks whether the bucket is flagged `LH_BUCKET_BEING_SPLIT`. If a split is in progress and the current backend can obtain a cleanup lock on the bucket (`IsBufferCleanupOK(buf)`), it will first call `_hash_finish_split()` to complete the split, then restart the insert from scratch. This cooperative split-completion avoids accumulating work for future vacuums and may create enough free space that no overflow page is needed.

If the primary bucket page lacks space for the new tuple, the inserter walks forward through the overflow chain using `hasho_nextblkno`. When it reaches a page with dead tuples (`LH_PAGE_HAS_DEAD_TUPLES`), it opportunistically cleans them with `_hash_vacuum_one_page()` before checking whether space is now available. This is a lightweight in-line vacuum that avoids allocating a new overflow page when existing space can be recovered. Only when all existing pages in the chain are exhausted does the inserter call `_hash_addovflpage()` to extend the chain.

Once a destination page is found, the tuple is placed using `_hash_pgaddtup()`. Tuples on each hash page are kept in hash-code order. `_hash_pgaddtup()` uses `_hash_binsearch()` to locate the correct insertion point, unless the caller passes `appendtup = true`. This is safe when tuples arrive in non-decreasing hash-code order, as they do during bulk build. Maintaining sort order within a page enables binary search during scans.

After writing the tuple, the metapage is write-locked momentarily to increment `hashm_ntuples`. The updated count is then compared against the split threshold: if the total tuple count exceeds `hashm_ffactor * (hashm_maxbucket + 1)`, `_hash_expandtable()` is called after releasing all other locks to initiate a bucket split. This deferred check means split pressure is detected on every insert. However, the split itself runs without holding the bucket lock.

## Overflow Chain Management

When a bucket's pages are all full and no free space can be recovered from dead tuples, `_hash_addovflpage()` (`hashovfl.c`) allocates a new overflow page and chains it to the end of the bucket.

Overflow pages are tracked by a set of bitmap pages, whose block numbers are stored in `hashm_mapp[]` in the metapage. Each bitmap page is a packed bit array where a 1-bit means "in use" and a 0-bit means "free". The metapage field `hashm_firstfree` caches the bit number of the lowest-numbered free overflow page to avoid scanning the bitmap from the beginning each time.

When allocating, `_hash_addovflpage()` scans the bitmap pages starting from `hashm_firstfree`. If it finds a free bit, it takes that overflow page (marking the bit as in use), initializes it, and links it into the bucket chain by updating `hasho_nextblkno` on the former tail page and `hasho_prevblkno` on the new page. If no free bits exist, the function extends the index file by adding a new overflow page at the end. When the current bitmap page itself is full, a new bitmap page is allocated first; new bitmap pages are initialized with all bits set to 1 (all "in use"), relying on the convention that pages beyond the current end of the file are considered pre-allocated and in use.

Bitmap pages are physically distinct from overflow pages. They carry the page type flag `LH_BITMAP_PAGE` in `hasho_flag` and contain no index tuples — their payload is entirely the packed bit array returned by `HashPageGetBitmap()`. The maximum number of bitmap pages is `HASH_MAX_BITMAPS` (up to 1024 at 8 kB block size), which bounds the total overflow space for a single index.

The locking order in `_hash_addovflpage()` is carefully structured to avoid deadlock with concurrent inserters: the tail page of the bucket chain is write-locked first, then the metapage is locked to identify and lock the bitmap page. Once a free bit is found and the bitmap page is locked, the metapage lock is released before the new overflow buffer is fetched. This ordering ensures that an inserter always acquires the metapage after any bucket-level lock, matching the pattern in `_hash_doinsert()`.

### Vacuum and the Squeeze Operation

[[code-paths/vacuum|Vacuum]] on a hash index removes dead tuples and then tries to compact the overflow chain so that empty overflow pages can be returned to the free pool. This compaction is called "squeezing" and is performed by `_hash_squeezebucket()` (`hashovfl.c`).

The squeeze algorithm maintains two cursors through the bucket chain: a "write" cursor starting at the primary page (the first page in the chain) and a "read" cursor starting at the last overflow page. The read cursor scans backward through the chain, collecting live tuples; the write cursor scans forward looking for space to place them. Tuples are moved from later overflow pages to earlier ones until the two cursors meet. Any overflow page that becomes empty during this process is freed by `_hash_freeovflpage()`, which clears its bitmap bit and updates the doubly-linked `hasho_prevblkno`/`hasho_nextblkno` pointers in the adjacent pages to splice it out of the chain.

The squeeze requires a cleanup lock on the primary bucket page for its entire duration. This cleanup lock blocks any concurrent scan from entering the bucket. This prevents a scan from visiting tuples in an inconsistent intermediate state, where a tuple exists on both its old and new location simultaneously. To avoid holding the cleanup lock longer than necessary as the read and write cursors advance, the implementation uses lock chaining. A lock on the next page is acquired before the lock on the current page is released, so the chain is never entirely unlocked between page transitions.

After freeing an overflow page, `_hash_freeovflpage()` checks whether the released bit number is lower than `hashm_firstfree` and, if so, updates the metapage cache to point to the newly freed page. This keeps future allocations efficient.

## Bulk Build via Sorting

When building a hash index from scratch with `CREATE INDEX`, inserting tuples one at a time in heap order would be pathological. Hash codes distribute uniformly across buckets, so each insertion would target a different page. This thrashes the buffer pool once the index exceeds available buffers.

The build path in `hashbuild()` (`hash.c`) detects this condition by comparing the estimated number of initial buckets against available memory. When the index is too large to fit in memory, it spools all tuples through a sort before inserting them.

The spooling state is represented by `HSpool` (`hashsort.c`), which wraps a `Tuplesortstate` configured to sort index tuples by bucket number. The sort comparator maps each tuple's hash code to a bucket number, using the same two-mask computation as `_hash_hashkey2bucket()`. This ensures tuples that belong to the same bucket sort together, regardless of whether a split might later redistribute them. The masks (`high_mask`, `low_mask`, `max_buckets`) are captured at spool-creation time based on the number of buckets initially allocated.

During the build scan, each tuple is handed to `_h_spool()` (`hashsort.c`), which passes it to `tuplesort_putindextuplevalues()`. Once all heap tuples have been spooled, `_h_indexbuild()` calls `tuplesort_performsort()` to materialize the sorted order, then feeds tuples one by one to `_hash_doinsert()` with the `sorted` flag set to `true`. The `sorted` flag tells `_hash_pgaddtup()` to use the fast append path — placing each tuple at the end of the page rather than binary-searching for its position — because within a bucket, tuples arrive in hash-code order by construction.

The sort uses `maintenance_work_mem` rather than `work_mem` for its memory budget, consistent with other index-building operations that are expected to run in isolation and benefit from larger sort buffers.

If the row-count estimate was too low and bucket splits occur during the build, tuples that were sorted into one bucket may end up belonging to two after the split. The sort order is still beneficial in this case: each original bucket and its new sibling are processed together before moving on. I/O locality is largely preserved as a result, even if the sort-to-bucket mapping is no longer exact.

## Operator Class Validation

`hashvalidate()` (`hashvalidate.c`) is called by `ALTER OPERATOR FAMILY` and related DDL to verify that an operator class is self-consistent before it can be used to build an index. For hash indexes, the constraints are minimal but meaningful.

The primary check is the signature of the hash support functions. A hash operator class must define support function number `HASHSTANDARD_PROC` (1), which must take exactly one argument of a type binary-coercible to the opclass's input type and return `int4`. The optional extended hash function `HASHEXTENDED_PROC` (2) must return `int8` and accept a second `int8` argument for the salt. The `check_hash_func_signature()` helper enforces both the return type and argument count, plus a short allowlist of built-in functions that are accepted despite a technically non-coercible argument type. For example, `hashint4()` is accepted for `date`, `xid`, and `cid`, whose internal representations are physically compatible with `int4`, even though no SQL cast exists.

Beyond function signatures, `hashvalidate()` checks that every operator registered in the opfamily has strategy number `HTEqualStrategyNumber` (which maps to the `=` operator) and is a search operator rather than an `ORDER BY` operator. Hash indexes cannot support ordering operators. Any attempt to register one is flagged as an invalid definition.

Cross-type completeness is also verified: if a hash opfamily supports types A and B, it must register hash functions and equality operators for all four combinations (A=A, B=B, A=B, B=A). The validator counts all operator groups and compares against the square of the number of hashable types, emitting an informational message if any cross-type combinations are missing.

## Related Topics

- [[subsystems/indexes/hash|Hash index]] — physical structure, two-level hashing scheme, scanning, and WAL logging
- [[subsystems/indexes/index-maintenance|Index maintenance]] — how VACUUM drives `hashbulkdelete` and bucket cleanup

---
title: "SharedFileSet, copydir, Index FSM, and ItemPointer Utilities"
aliases:
  - SharedFileSet
  - shared temp file set
  - copydir
  - index free space map
  - index FSM
  - ItemPointer
  - TID utilities
tags:
  - theme/parallelism
  - theme/storage-format
source_files:
  - src/backend/storage/file/sharedfileset.c
  - src/include/storage/sharedfileset.h
  - src/backend/storage/file/copydir.c
  - src/backend/storage/freespace/indexfsm.c
  - src/backend/storage/page/itemptr.c
  - src/include/storage/itemptr.h
symbols:
  - SharedFileSet
  - SharedFileSetInit
  - SharedFileSetAttach
  - SharedFileSetDetach
  - SharedFileSetDeleteAll
  - SharedFileSetOnDetach
  - copydir
  - copy_file
  - clone_file
  - GetFreeIndexPage
  - RecordFreeIndexPage
  - RecordUsedIndexPage
  - IndexFreeSpaceMapVacuum
  - ItemPointerData
  - ItemPointerCompare
  - ItemPointerEquals
  - ItemPointerIndicatesMovedPartitions
  - ItemPointerInc
  - ItemPointerDec
---

This page covers four small storage utilities that appear throughout PostgreSQL's internals: `SharedFileSet`, a DSM-backed multi-owner extension of [[subsystems/storage/fileset|FileSet]]; `copydir`, a portable recursive directory copy; the index [[subsystems/storage/fsm|free space map]]; and the `ItemPointer` (TID) utility functions used in MVCC and index scans. Each is a focused mechanism with a narrow contract. Understanding them clarifies how these utilities underpin larger subsystems — parallel query, tablespace cloning, index vacuum, and tuple identity.

## SharedFileSet: shared ownership over a named temp namespace

A [[subsystems/storage/fileset|FileSet]] gives one backend a named namespace for temporary files. `SharedFileSet` extends this with multi-backend shared ownership: multiple processes each hold a reference. The underlying files survive until the last participant releases its reference. Parallel hash join and parallel sort depend on this layer. Worker processes write intermediate data here. The leader reads it back.

The `SharedFileSet` struct (`sharedfileset.h`) is a thin wrapper around a `FileSet` with two additions: a spinlock (`mutex`) and a reference count (`refcnt`). The struct lives in a [[subsystems/storage/dsm-impl|dynamic shared memory]] (DSM) segment so that all parallel workers can access it without any IPC beyond ordinary shared-memory reads.

`SharedFileSetInit()` initialises the struct and sets `refcnt` to 1. It delegates directory setup to `FileSetInit()`. It registers `SharedFileSetOnDetach()` as a DSM detach callback. Every subsequent worker calls `SharedFileSetAttach()` instead. This function acquires the spinlock and increments `refcnt`. It registers the same callback for its own DSM handle. If `refcnt` is already zero when a worker tries to attach — meaning the fileset was destroyed before the worker connected — the attach fails with an error.

Cleanup is entirely event-driven through DSM lifecycle. When any participant releases its DSM handle (whether by normal exit or error cleanup), `SharedFileSetOnDetach()` fires. It decrements the reference count. If the count reaches zero, it calls `FileSetDeleteAll()` to remove every file in every configured tablespace. Because the callback runs in error-recovery paths, it cannot raise errors. This design means no backend needs to perform explicit teardown. The last backend to exit triggers the cleanup automatically. This makes the mechanism safe against parallel worker crashes.

## copydir: recursive directory copy with durability guarantees

`copydir()` (`copydir.c`) implements a portable recursive directory copy. Its primary callers are tablespace creation (populating a new tablespace's per-database subdirectory from a template) and `pg_basebackup` (replicating tablespace directories to a standby or backup). The function accepts source and destination paths and a `recurse` flag. When `recurse` is true, it calls itself for each subdirectory.

For each regular file, `copydir()` dispatches to either `copy_file()` or `clone_file()` depending on the `file_copy_method` GUC. The default, `copy`, uses `copy_file()`. `copy_file()` reads in 8×`BLCKSZ` chunks. It flushes dirty data to the OS every 1 MB (32 MB on macOS/APFS, which handles small `msync` requests poorly). It relies on an explicit fsync pass after the copy loop completes. The `clone` method uses `clone_file()`, which delegates to the platform's copy-on-write clone syscall (`copyfile(COPYFILE_CLONE_FORCE)` on macOS, `copy_file_range()` on Linux). Cloning is faster for large tablespaces because the kernel shares physical blocks between source and destination until one side writes.

After copying all files, `copydir()` performs a second pass over the destination: it calls `fsync_fname()` on each regular file and then on the destination directory itself. The directory fsync is necessary because individual file fsyncs do not guarantee that the directory entry pointing to the file is durable — a well-known property of ext3 and similar filesystems.

## Index FSM: binary free-space tracking for index pages

The heap [[subsystems/storage/fsm|free space map]] tracks fractional free space per page so that `INSERT` and `UPDATE` can find pages with enough room. Index pages do not work this way: a page is either available for recycling (completely empty after a vacuum) or in use. `indexfsm.c` implements a simplified FSM that captures this binary distinction.

The implementation reuses the heap FSM machinery from `freespace.c` verbatim. Rather than inventing a separate data structure, `indexfsm.c` encodes the binary state as a numeric free-space value: `BLCKSZ - 1` means the page is free, `0` means it is in use. `GetFreeIndexPage()` calls `GetPageWithFreeSpace()` with a threshold of `BLCKSZ / 2` (anything above this threshold is considered free). It then immediately marks the returned page as used with `RecordUsedIndexPage()`, to prevent two concurrent callers from claiming the same page. `RecordFreeIndexPage()` and `RecordUsedIndexPage()` are thin wrappers that pass the appropriate constant to `RecordPageWithFreeSpace()`.

When vacuum reclaims empty index pages, it calls `RecordFreeIndexPage()` for each one. `IndexFreeSpaceMapVacuum()` then triggers a compaction pass over the FSM tree (`FreeSpaceMapVacuum()`) to propagate updates upward. The next `GetFreeIndexPage()` call then finds the recycled page instead of extending the relation.

## ItemPointer: the physical tuple address

An `ItemPointer` (also called a TID, tuple identifier) encodes the physical location of a tuple on disk: a 32-bit block number and a 16-bit offset within the page's line-pointer array. The struct `ItemPointerData` (`itemptr.h`) is exactly six bytes. A `StaticAssertDecl` in `itemptr.c` enforces this size. Compiler annotations pack the struct to avoid padding. This matters because PostgreSQL embeds `ItemPointerData` in every heap tuple header and every index entry on disk. Wasting two bytes to alignment would inflate every relation in the system.

Most access to `ItemPointerData` is through inline functions in `itemptr.h`. The non-inline functions in `itemptr.c` cover the cases that need to be callable from non-C code or that have meaningful semantics beyond a field read:

- `ItemPointerCompare()` provides a total order suitable for btree sorting — block number major, offset number minor. It deliberately uses the `NoCheck` variants of the accessor macros to avoid asserting `ip_posid != 0`, because callers may pass user-supplied TIDs that are technically invalid.
- `ItemPointerEquals()` tests equality between two valid pointers.
- `ItemPointerInc()` and `ItemPointerDec()` increment or decrement the address treating the full 48-bit value as a counter, wrapping the offset into the next or previous block. They respect only the type's numeric range limits, not `MaxOffsetNumber` or `FirstOffsetNumber`, so results may land on `!OffsetNumberIsValid` offsets.

Two magic values encoded in the offset field carry out-of-band semantics. `SpecTokenOffsetNumber` (`0xfffe`) marks a speculative insertion: the tuple has been written but not yet confirmed. In this state, `ip_blkid` holds an opaque token rather than a real block number. `MovedPartitionsOffsetNumber` (`0xfffd`, with block number `InvalidBlockNumber`) marks a tuple that an `UPDATE` moved to a different partition. `ItemPointerIndicatesMovedPartitions()` detects this sentinel. Both values are above `MaxOffsetNumber`. This is how PostgreSQL distinguishes them from a valid page offset.

## See also

- [[subsystems/storage/fileset|FileSet: named temp file namespaces]]
- [[subsystems/storage/fsm|Free space map (heap FSM)]]
- [[subsystems/storage/dsm-impl|Dynamic shared memory]]
- [[subsystems/storage/tablespaces|Tablespaces]]
- [[subsystems/storage/page-layout|Page layout and line pointers]]
- [[subsystems/executor/parallel|Parallel query execution]]
- [[subsystems/executor/work-mem-and-spill|work_mem and spill to disk]]

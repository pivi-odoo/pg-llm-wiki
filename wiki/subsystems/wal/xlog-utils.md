---
title: "WAL Recovery Utilities"
aliases:
  - "xlogutils"
  - "XLogReadBufferForRedo"
  - "invalid page tracking"
tags:
  - theme/durability
source_files:
  - src/backend/access/transam/xlogutils.c
  - src/include/access/xlogutils.h
symbols:
  - XLogReadBufferForRedo
  - XLogReadBufferForRedoExtended
  - XLogReadBufferExtended
  - XLogInitBufferForRedo
  - XLogRedoAction
  - XLogHaveInvalidPages
  - XLogCheckInvalidPages
  - XLogDropRelation
  - XLogDropDatabase
  - XLogTruncateRelation
  - XLogReadDetermineTimeline
  - CreateFakeRelcacheEntry
  - FreeFakeRelcacheEntry
  - read_local_xlog_page
  - read_local_xlog_page_no_wait
  - wal_segment_open
  - wal_segment_close
  - WALReadRaiseError
  - InRecovery
  - HotStandbyState
---

The code in `xlogutils.c` provides the infrastructure that individual resource manager redo functions depend on during [[subsystems/wal/overview|WAL]] replay. It covers three distinct concerns: deciding what to do when a redo function tries to access a page that no longer exists or was never initialized, reading the correct buffer from disk while handling recovery-specific edge cases, and supplying the page-read callbacks that feed raw WAL bytes to the `XLogReader` infrastructure. None of this code runs during normal operation — it is entirely recovery-time machinery.

## Invalid page tracking

When `full_page_writes` is off, WAL records carry only deltas, not full page images. If a relation is later dropped or truncated, some earlier WAL records may reference pages. By the time recovery processes those records, the pages may no longer exist. Silently ignoring those references would be dangerous: if the relation was *not* actually dropped later in the stream, recovery would produce a corrupt database without any warning.

The design tracks such references in a hash table (`invalid_page_tab`) keyed by `(RelFileLocator, ForkNumber, BlockNumber)`. Each entry also records whether the page *existed but was uninitialized* (the `present` flag) or simply did not exist on disk at all — two conditions with different diagnostic implications.

Entries accumulate freely before the database reaches a consistent recovery state. Once `reachedConsistency` becomes true, the WAL stream should be self-consistent. From that point, any new reference to a missing or zeroed page immediately triggers a PANIC rather than being deferred. The `ignore_invalid_pages` GUC downgrades the terminal check from PANIC to WARNING — an escape hatch for administrators recovering from known-corrupt WAL, not something to use routinely.

Drop and truncate WAL records call `XLogDropRelation`, `XLogDropDatabase`, or `XLogTruncateRelation` to purge matching entries from the table. At the end of recovery, `XLogCheckInvalidPages` emits a WARNING for every remaining entry. If it finds any, it PANICs. It dumps all diagnostics before aborting, so the operator can see every affected page at once.

## Buffer acquisition for redo

The primary interface for redo functions is `XLogReadBufferForRedo`. It takes an `XLogReaderState` (the decoded WAL record) and a block ID, reads the relevant page into the shared buffer cache, compares the page's LSN to the record's end LSN, and returns one of four outcomes:

| Result | Meaning |
|---|---|
| `BLK_NEEDS_REDO` | Page is older than this record; redo function must apply the changes |
| `BLK_DONE` | Page LSN is already at or past the record LSN; record has been replayed |
| `BLK_RESTORED` | Record carried a full-page image that was restored; redo function need not do anything |
| `BLK_NOTFOUND` | Page does not exist (relation was later dropped or truncated) |

The LSN comparison (`lsn <= PageGetLSN(page)`) is what makes replay idempotent. If the system crashes partway through applying a batch of records and some pages were flushed to disk with their new LSN before others, re-running recovery from the checkpoint will silently skip the already-applied pages.

`XLogReadBufferForRedoExtended` exposes additional control. The `mode` argument accepts `RBM_ZERO_AND_LOCK` or `RBM_ZERO_AND_CLEANUP_LOCK` when a redo function is about to initialize a page from scratch rather than modify an existing one. In those modes, the function zeros the buffer. The result is always `BLK_NEEDS_REDO`. A WILL_INIT flag in the WAL record's block descriptor must match the caller's choice of mode. A mismatch causes an immediate PANIC, enforcing the invariant that the caller declares initialization intent at WAL-write time.

When a full-page image is present and marked for application (`BKPIMAGE_APPLY`), the redo path always restores the image. It does so even if the page on disk appears newer. Recovery trusts the WAL image over the on-disk page because it has a verified CRC. The on-disk page might instead be a torn-write artifact from before the crash. After restoring, if the fork is an init fork (`INIT_FORKNUM`), the redo path calls `FlushOneBuffer` immediately to keep the on-disk init fork in sync with shared memory. This is a special case: crash recovery copies init forks directly to disk at the end, bypassing the buffer manager.

## Low-level buffer reading

`XLogReadBufferExtended` sits below the redo-action layer and handles the mechanics of getting a page into the buffer cache when there is no live relcache. In `RBM_NORMAL` mode, if the page does not exist on disk or is all-zeroes, it calls `log_invalid_page` and returns `InvalidBuffer` — the signal to redo functions to skip the record. In `RBM_ZERO_*` modes, it extends the relation via `ExtendBufferedRelTo` without acquiring a relation-extension lock. This is safe because recovery runs single-threaded in the startup process.

One notable detail: the function always calls `smgrcreate(..., true)` (the `true` meaning "create if not exists") before checking the block count. This ensures a relation file exists even if the WAL stream contains writes to a relation that is later deleted. Suppressing the writes would be simpler. But writing the data and deleting the file later is safer. A filesystem that loses an inode during a crash might not record the deletion. Having the data present prevents silent data loss.

## Fake relcache entries

Redo functions often call utility routines that expect a `Relation` pointer, but during recovery the relcache is not operational. `CreateFakeRelcacheEntry` allocates a minimal `RelationData` structure populated only with the fields needed for physical I/O: `rd_locator`, `rd_smgr`, persistence, and a dummy lock ID. `CreateFakeRelcacheEntry` sets the name field to the relation file number as a string since the catalog name is not available. Callers must free the entry with `FreeFakeRelcacheEntry` when done; it is a plain `palloc` allocation with no cleanup side effects.

## Local WAL page reading and timeline determination

The `read_local_xlog_page` and `read_local_xlog_page_no_wait` functions implement the `XLogReaderRoutine->page_read` callback for reading from the local `pg_wal` directory. They are public because logical decoding and other consumers outside walsender need the same callback without building their own.

Both functions share a core loop that determines how far the WAL stream can currently be read. During recovery the limit is `GetXLogReplayRecPtr`; on a live primary it is `GetFlushRecPtr`. The wait-variant polls with a 1 ms sleep until the requested data is available; the no-wait variant sets a flag in its private data structure and returns immediately when it reaches the end of available WAL.

`XLogReadDetermineTimeline` handles timeline switching during this read loop. A cascading standby can become a promoted primary while a logical decoding session is still running. Because of this, the current timeline can change at any time. The function uses `readTimeLineHistory` to determine which timeline owns the end of the WAL segment containing the target page, updates `state->currTLI` and `state->currTLIValidUntil`, and avoids redundant timeline lookups for sequential reads. Three fast-path conditions skip the full history scan: the requested page is already in the read buffer, the reader is on the current timeline reading forward, or the current timeline remains valid through the end of the target segment.

The `wal_segment_open` and `wal_segment_close` callbacks open and close segment files by constructing the path with `XLogFilePath`. If the segment has been removed (archived and cleaned up), `wal_segment_open` produces an error message that identifies the file by name to assist diagnosis.

`WALReadRaiseError` translates the structured `WALReadError` type into a human-readable PostgreSQL error with the segment filename and byte offset, so operators can locate the exact position of a read failure in the WAL stream. `WALReadError` captures errno, file descriptor state, and I/O metrics from `WALRead`.

## Recovery process flags

This file declares two module-level variables and exports them for use throughout the backend. `InRecovery` is true only in the startup process while it is replaying WAL records. It differs from `RecoveryInProgress()`, which reads a shared memory flag visible to all processes. Functions use `InRecovery` when they need to know whether the current process is a WAL redo function, not whether the cluster is in recovery mode. `standbyState` tracks hot standby readiness through four stages (disabled, initialized, snapshot pending, snapshot ready). The `InHotStandby` macro returns true once the state reaches snapshot-pending.

## Related Topics

- [[subsystems/wal/overview|WAL]] — write-ahead logging architecture and crash recovery
- [[subsystems/wal/wal-records|WAL Records]] — record format, full-page images, and resource managers
- [[subsystems/storage/buffer-manager|buffer manager]] — shared buffer cache and LSN-based page management

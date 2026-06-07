---
title: "Deferred Fsync and the Storage Sync Queue"
aliases:
  - "storage sync"
  - "sync.c"
  - "deferred fsync"
  - "pending fsync"
  - "ProcessSyncRequests"
  - "RegisterSyncRequest"
tags:
  - theme/durability
  - symptom/high-io
source_files:
  - src/backend/storage/sync/sync.c
  - src/include/storage/sync.h
symbols:
  - FileTag
  - SyncRequestType
  - SyncRequestHandler
  - PendingFsyncEntry
  - PendingUnlinkEntry
  - InitSync
  - SyncPreCheckpoint
  - SyncPostCheckpoint
  - ProcessSyncRequests
  - RememberSyncRequest
  - RegisterSyncRequest
---

PostgreSQL never calls `fsync()` immediately when it writes a dirty buffer to disk. Instead, every write registers a deferred sync request. PostgreSQL batches all pending fsyncs and executes them together at the end of the next [[subsystems/wal/checkpoint|checkpoint]]. The `sync.c` module manages this deferred-sync queue. It tracks which relation file segments need to be fsynced, deduplicates redundant requests, and drives the actual fsync calls when the checkpoint is ready to complete.

## The pending-fsync hash table

The core data structure in `sync.c` is a hash table, `pendingOps`, whose keys are `FileTag` values. A `FileTag` (`sync.h`) identifies a specific file segment:

```c
typedef struct FileTag
{
    int16  handler;   /* SyncRequestHandler: which storage type */
    int16  forknum;   /* which fork of the relation */
    RelFileLocator rlocator;
    uint32 segno;     /* which 1 GB segment */
} FileTag;
```

The `handler` field selects which set of sync functions to use. The five handlers currently registered cover heap/index files (`SYNC_HANDLER_MD`), `pg_xact` (`SYNC_HANDLER_CLOG`), `pg_commit_ts`, and the two `pg_multixact` files (`SYNC_HANDLER_MULTIXACT_OFFSET`, `SYNC_HANDLER_MULTIXACT_MEMBER`). Each handler provides three callbacks in the `SyncOps` table (`sync.c`): a sync function that actually calls `fsync()`, an unlink function, and a tag-matching predicate used when bulk-canceling requests for a dropped relation.

Using `FileTag` as the hash key is what gives the queue its deduplication property. A relation segment that is dirtied ten times between checkpoints produces ten calls to `RegisterSyncRequest()`, but only one hash table entry — and therefore only one `fsync()` call at checkpoint time. On a spinning disk, where `fsync()` serializes I/O through the disk cache, this batching can make the difference between a checkpoint that completes in seconds and one that takes minutes.

The hash table lives in its own [[subsystems/memory/contexts|memory context]], `pendingOpsCxt`. PostgreSQL marks this context as allowed to allocate inside a critical section (`MemoryContextAllowInCriticalSection`). This is an intentional exception to the usual rule: the checkpointer must be able to absorb incoming sync requests even while holding the critical state that makes checkpoints crash-safe.

## How writes register sync requests

When `mdwrite()` or `mdextend()` writes a block, it calls `register_dirty_segment()`, which in turn calls `RegisterSyncRequest()` (`sync.c`) with a `SYNC_REQUEST` for the affected segment. Regular backends do not own a `pendingOps` table — they forward requests to the checkpointer process via `ForwardSyncRequest()`, which posts the `FileTag` into a shared-memory queue. The checkpointer drains that queue periodically by calling `AbsorbSyncRequests()`, which calls `RememberSyncRequest()` to insert or update entries in `pendingOps`.

If the shared-memory queue overflows (a rare but possible condition under heavy write load), `CompactCheckpointerRequestQueue()` scans the queue. It deduplicates the queue in place. If the queue is still full after compaction, the writing backend falls back to performing its own `fsync()` inline — a rare slow path counted in `pg_stat_bgwriter.buffers_backend_fsync`.

The [[subsystems/background/bgwriter|bgwriter]] evicts dirty buffers proactively between checkpoints. Each evicted buffer goes through the same `smgrwrite()` path. It registers a sync request exactly as the checkpointer's `BufferSync()` does. This means the bgwriter's writes contribute to the pending-fsync queue. The checkpointer will fsync them at the next checkpoint, without needing a second write pass.

## ProcessSyncRequests at checkpoint time

At the end of `CheckPointGuts()`, after `BufferSync()` has written all dirty shared buffers to the OS page cache, the checkpointer calls `ProcessSyncRequests()` (`sync.c`). This is the step that makes all those written pages durable.

`ProcessSyncRequests()` begins with `AbsorbSyncRequests()` to pull in any last-minute requests from backends. It then increments an internal `sync_cycle_ctr` counter. Any entry in `pendingOps` whose `cycle_ctr` equals the new counter value arrived after `ProcessSyncRequests()` started. `ProcessSyncRequests()` skips those entries; the next checkpoint will handle them. This cycle counter prevents unbounded checkpoint loops: it does not chase new writes that arrive during a slow checkpoint indefinitely.

The function then iterates all remaining entries via `hash_seq_search`. It calls the handler's `sync_syncfiletag` callback for each one (e.g., `mdsyncfiletag()` for heap and index files, `clogsyncfiletag()` for `pg_xact`). Each callback opens the file and calls `pg_fsync()`. On success, the function removes the entry from the hash table. On failure with `ENOENT`, the function absorbs pending requests to check whether a cancellation arrived (which would happen if the relation was dropped between the write and the fsync) before deciding whether to report an error.

The two-phase write-then-sync design — buffer writes in `BufferSync()`, fsyncs in `ProcessSyncRequests()` — lets the kernel scheduler reorder the actual disk writes for optimal throughput before the checkpoint commits to durability. It also means the checkpoint's sync phase is the only moment when PostgreSQL waits for disk I/O to complete, making it easier to reason about checkpoint latency.

## Unlink requests and the post-checkpoint cleanup

Dropping a relation cannot simply unlink its files immediately. A checkpoint may be in progress that still needs to read those pages. WAL replay on a standby may also need to apply writes to the file before the drop takes effect. The [[subsystems/storage/smgr|storage manager]] therefore defers file deletion by posting a `SYNC_UNLINK_REQUEST` to the sync queue instead of calling `unlink()` directly.

`sync.c` tracks unlink requests in a separate linked list, `pendingUnlinks`, rather than the fsync hash table. There is no need to deduplicate unlinks. The order matters for correctness. `SyncPreCheckpoint()` increments a `checkpoint_cycle_ctr` (distinct from `sync_cycle_ctr`) at the start of each checkpoint. Unlink entries carry the `checkpoint_cycle_ctr` value at the time they were registered, which lets `SyncPostCheckpoint()` distinguish requests that arrived before the checkpoint from those that arrived during it.

`SyncPostCheckpoint()` runs after PostgreSQL has flushed the checkpoint WAL record and updated `pg_control`. It iterates `pendingUnlinks`. It calls the handler's `sync_unlinkfiletag` for each entry whose cycle counter predates the current checkpoint. Only at this point — after the checkpoint has durably recorded that the drop happened — is it safe to remove the file from disk. PostgreSQL reports errors as `WARNING` rather than `ERROR`, because by the time the unlink runs, the transaction has already committed and there is no way to roll back the drop.

This post-checkpoint timing also handles a subtle correctness requirement: if PostgreSQL reuses a relation file number before it unlinks the old file, WAL replay on a crash-recovered instance could confuse the old and new files. Deferring the unlink until after the checkpoint ensures the file number cannot be recycled until the checkpoint has recorded the deletion.

## Canceling sync requests

When PostgreSQL drops a relation, any pending fsync requests for that relation become irrelevant — there is no point fsyncing a file that is about to be deleted. `RegisterSyncRequest()` with `SYNC_FORGET_REQUEST` cancels a single specific entry in `pendingOps` by setting its `canceled` flag. `SYNC_FILTER_REQUEST` cancels all entries matching a given handler and tag-matching predicate. PostgreSQL uses this when dropping an entire relation or database at once.

`sync.c` does not remove canceled entries from the hash table immediately. It leaves them in place; `ProcessSyncRequests()` skips (or removes) them when it iterates the table. This lazy cleanup avoids modifying the hash table while a scan might be in progress.

## Checkpoint completion target and I/O spreading

The deferred-fsync design concentrates all disk-synchronization work at the end of `CheckPointGuts()`. On systems with slow persistent storage this can produce a visible I/O spike at checkpoint time. `checkpoint_completion_target` (GUC, default 0.9) mitigates this by spreading the write phase of `BufferSync()` over a fraction of the checkpoint interval via `CheckpointWriteDelay()`, so that the final `ProcessSyncRequests()` call encounters pages that are largely already on disk and need only `fdatasync()` to confirm durability.

The `checkpoint_flush_after` GUC (default 2 MB) controls how often the write phase calls `sync_file_range(SYNC_FILE_RANGE_WRITE)` to nudge the OS into starting writeback early. This reduces the latency of the final fsync phase by bounding how much dirty data can accumulate in the OS page cache between writeback hints.

## Related Topics

- [[subsystems/wal/checkpoint|checkpointing]]
- [[subsystems/storage/smgr|storage manager]]
- [[subsystems/storage/buffer-manager|buffer manager]]
- [[subsystems/background/bgwriter|bgwriter]]

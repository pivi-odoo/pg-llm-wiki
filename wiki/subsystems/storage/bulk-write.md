---
title: "Bulk Write API"
aliases:
  - bulk write
  - smgr_bulk_write
  - BulkWriteState
  - relation bulk loading
tags:
  - theme/durability
source_files:
  - src/backend/storage/smgr/bulk_write.c
  - src/include/storage/bulk_write.h
symbols:
  - BulkWriteState
  - PendingWrite
  - smgr_bulk_start_rel
  - smgr_bulk_start_smgr
  - smgr_bulk_finish
  - smgr_bulk_write
  - smgr_bulk_get_buf
  - smgr_bulk_flush
---

The bulk write API, introduced in PostgreSQL 17 as a formalized subsystem, provides an efficient path for writing many pages to a new or freshly truncated relation without going through the normal [[subsystems/storage/buffer-manager|shared buffer manager]]. Operations that need to populate a relation quickly, in the absence of concurrent access, use it: `CREATE INDEX`, `CLUSTER`, `COPY` in certain modes, and CREATE TABLE AS. Bypassing the buffer manager eliminates lock acquisition overhead and avoids polluting shared buffers with data that will not be read back immediately.

The tradeoff is that pages written through the bulk API are not in shared buffers and must be read from storage on first access after the operation completes. For large relations this is an acceptable cost; for small relations the overhead of going through the buffer manager would have been comparable anyway.

## How It Works

The caller creates a `BulkWriteState` for one relation fork via `smgr_bulk_start_rel()` or `smgr_bulk_start_smgr()`. The state records the SMgrRelation, the fork number, and whether WAL logging is needed. It also captures the current redo pointer (`GetRedoRecPtr()`) so that a concurrent checkpoint can be detected later.

`smgr_bulk_get_buf()` allocates an aligned page buffer for the caller to fill. Once filled, the caller queues the page with `smgr_bulk_write()`. Up to `MAX_PENDING_WRITES` (the maximum WAL block ID count) pages can be pending at once; when the queue is full, the bulk writer calls `smgr_bulk_flush()` automatically.

`smgr_bulk_flush()` processes all queued writes in block-number order (sorted by `qsort`). If WAL logging is enabled, `log_newpages()` logs all pending pages as a single `XLOG_FPI` record, which amortizes WAL record header overhead across many pages. `smgr_bulk_flush()` then computes each page's checksum in-place before writing it to storage via `smgrextend()` or `smgrwrite()`. It frees the page buffer after the write. It writes pages that extend the relation in sequence; it fills any gap in block numbers with zero-filled pages to avoid filesystem holes.

## Fsync Handling

At the end of the operation, `smgr_bulk_finish()` ensures the relation reaches durable storage through one of three paths:

- **Temporary relations** — no fsync needed; temporary data is not crash-safe by design.
- **Unlogged relations or WAL-skipped relations (`wal_level=minimal`)** — `smgrregistersync()` registers the relation with the checkpointer so it is fsynced at the next checkpoint. This is safe for unlogged relations; for permanent relations with minimal WAL, `smgrDoPendingSyncs()` at commit handles the fsync or WAL emission.
- **WAL-logged permanent relations** — this is the common case for index builds. The pages have already been WAL-logged, but the caller passed `skipFsync=true` to each write to avoid per-page fsync registration overhead. At finish time, the bulk writer checks whether a checkpoint occurred during the operation (by comparing `start_RedoRecPtr` with the current redo pointer). If a checkpoint ran and therefore missed registering the pages for fsync, `smgr_bulk_finish()` fsyncs the relation immediately (`smgrimmedsync()`). Otherwise, `smgrregistersync()` defers the fsync to the next checkpoint. `DELAY_CHKPT_START` protects the checkpoint detection window, to prevent a new checkpoint from starting between the redo pointer read and the `smgrregistersync()` call.

## Relationship to the Buffer Manager

Callers must not mix bulk write operations with normal buffer manager reads or writes to the same fork during the bulk operation. The buffer manager maintains its own view of what is in shared buffers; pages written through the bulk path will not appear there until they are read back. Mixing the two interfaces would produce inconsistent results.

## Related Topics

- [[subsystems/storage/smgr]] — the SMgrRelation layer that bulk write calls into
- [[subsystems/storage/buffer-manager]] — the standard I/O path that bulk write bypasses
- [[code-paths/create-index]] — index builds, a primary user of the bulk write API
- [[subsystems/wal/checkpoint]] — how checkpoint interacts with the deferred fsync mechanism

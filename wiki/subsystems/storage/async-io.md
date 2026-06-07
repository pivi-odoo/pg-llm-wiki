---
title: "Asynchronous I/O"
aliases:
  - "AIO"
  - "io_method"
  - "io_uring"
  - "async I/O"
source_files:
  - src/backend/storage/aio/aio.c
  - src/include/storage/aio.h
  - src/backend/storage/aio/aio_io.c
  - src/backend/storage/buffer/bufmgr.c
  - src/backend/storage/aio/method_io_uring.c
  - src/backend/storage/aio/method_worker.c
  - src/backend/storage/aio/read_stream.c
  - src/include/storage/aio_types.h
  - src/backend/storage/aio/aio_init.c
  - src/backend/storage/aio/aio_callback.c
  - src/backend/storage/aio/aio_target.c
  - src/backend/storage/aio/aio_funcs.c
  - src/backend/storage/aio/method_sync.c
symbols:
  - PgAioHandle
  - PgAioReturn
  - pgaio_io_start_readv
  - pgaio_io_start_writev
  - ReadStream
  - read_stream_begin_relation
  - read_stream_next_buffer
  - AioShmemSize
  - AioShmemInit
  - pgaio_init_backend
  - PgAioHandleCallbacks
  - PgAioHandleCallbackID
  - pgaio_io_register_callbacks
  - pgaio_io_set_handle_data_64
  - pgaio_io_call_complete_shared
  - pgaio_io_call_complete_local
  - PgAioTargetInfo
  - PgAioTargetID
  - pgaio_io_set_target
  - pgaio_io_reopen
  - pg_get_aios
  - IoMethodOps
  - pgaio_sync_ops
---

PostgreSQL 18 introduced a native asynchronous I/O (AIO) subsystem that decouples I/O submission from I/O completion, allowing the backend to overlap CPU work with disk activity. Before 18, all I/O was synchronous: every `ReadBuffer()` call blocked until the OS returned the data. The new subsystem keeps the same high-level buffer manager API visible to most of the codebase while changing how I/O is actually issued.

## Subsystem Initialisation

The AIO subsystem allocates all of its state in shared memory during postmaster startup (`aio_init.c`). The entry points follow the standard PostgreSQL IPCI pattern:

- `AioShmemSize()` — computes how much shared memory is required. It also pins the value of `io_max_concurrency`: if the GUC was left at its default of `-1`, `AioChooseMaxConcurrency()` derives a suitable value based on `NBuffers` and `MaxBackends` (capped at 64). The method-specific ops table (`pgaio_method_ops`) may add extra space via `IoMethodOps->shmem_size`.
- `AioShmemInit()` — allocates five shared-memory regions: `AioCtl` (the top-level control struct `PgAioCtl`), `AioBackend` (one `PgAioBackend` per backend slot), `AioHandle` (the flat pool of `PgAioHandle` objects), `AioHandleIOV` (the `iovec` arrays backing each handle), and `AioHandleData` (64-bit per-buffer metadata arrays). `AioShmemInit()` links each backend's slice of handles onto an idle list at init time. It then calls the method-specific `shmem_init` hook at the end.
- `pgaio_init_backend()` — runs once per backend after `MyProc` is assigned. It wires `pgaio_my_backend` to the right `PgAioBackend` slot, calls the method-specific `init_backend` hook, and registers `pgaio_shutdown` as an `on_shmem_exit` callback so handles are reclaimed on exit.

The number of handle slots per backend is `io_max_concurrency`; the total pool has `(MaxBackends + NUM_AUXILIARY_PROCS) * io_max_concurrency` entries.

## I/O Methods

The `io_method` GUC selects the I/O backend at server startup. A constant `IoMethodOps` struct (defined in `aio_internal.h`) describes each method, supplying hooks for shared-memory sizing, backend initialisation, submission, waiting, and FD-close semantics.

| Value | Mechanism | Availability |
|-------|-----------|-------------|
| `sync` | Traditional blocking `pread()`/`pwrite()` — identical to pre-18 behavior | All platforms |
| `worker` | Async I/O dispatched to a pool of background worker processes | All platforms |
| `io_uring` | Async I/O via Linux `io_uring` system calls | Linux 5.1+ |

`sync` is the default, providing a conservative upgrade path. `worker` mode submits I/O requests to dedicated worker processes that perform blocking I/O and signal completion back to the requesting backend. `io_uring` submits requests directly to the kernel's submission queue and polls the completion queue, avoiding context switches for I/O completion.

### The Synchronous Method (`method_sync.c`)

`pgaio_sync_ops` is the simplest `IoMethodOps` implementation. Its `needs_synchronous_execution` hook always returns `true`, which causes the AIO core to execute every I/O inline before `pgaio_io_stage()` returns rather than batching it for later submission. Its `submit` hook is therefore unreachable and calls `elog(ERROR)` as a safety guard.

This method exists for two purposes: as the safe default on all platforms, and as a regression baseline — any performance difference between `io_method=sync` and pre-18 code is attributable to AIO overhead rather than to the kernel interface.

Other methods (e.g. `io_uring`) may also fall back to synchronous execution for individual handles by setting the `PGAIO_HF_SYNCHRONOUS` flag, which causes the same inline-execution path to trigger.

## Read Streams

The primary user-visible benefit in PostgreSQL 18 is **read streams**: a high-level API (`src/backend/storage/aio/read_stream.c`) that wraps the buffer manager and issues prefetch or async read requests for a sequence of blocks ahead of the current position.

Sequential scans, bitmap heap scans, and VACUUM use the read stream API rather than calling `ReadBuffer()` one block at a time. The caller initializes a read stream with a callback that supplies the next block number to read. The stream then prefetches up to `io_combine_limit` blocks (controlled by the `io_combine_limit` GUC, with `io_max_combine_limit` as an upper bound) as a single vectored I/O operation. By the time the consumer asks for a block, it is often already in the buffer pool.

```c
/* Typical pattern */
ReadStream *stream = read_stream_begin_relation(flags, buffer_access_strategy,
                                                relation, MAIN_FORKNUM,
                                                my_block_callback, &state, 0);
while ((buf = read_stream_next_buffer(stream, &private)) != InvalidBuffer) {
    LockBuffer(buf, BUFFER_LOCK_SHARE);
    /* process page */
    UnlockReleaseBuffer(buf);
}
read_stream_end(stream);
```

## AIO Handle Lifecycle

Lower-level code that needs direct async I/O uses `PgAioHandle` — a handle representing a single pending I/O operation. The lifecycle is:

1. **Acquire** a handle via `pgaio_io_acquire()` from a per-backend pool.
2. **Assign a target** via `pgaio_io_set_target()` (see I/O Targets below).
3. **Register callbacks** via `pgaio_io_register_callbacks()` before starting the I/O.
4. **Start** the I/O with `pgaio_io_start_readv()` or `pgaio_io_start_writev()`, specifying the file descriptor, buffer, and offset.
5. **Wait** for completion with `pgaio_io_wait()`, or let the read stream API batch and wait automatically.
6. **Release** the handle after inspecting the result.

Handles are lightweight shared-memory structures. Each handle carries a monotonically increasing `generation` counter so that stale references can be detected even after a handle is recycled.

## I/O Targets (`aio_target.c`)

Every `PgAioHandle` has an associated **target** that identifies the storage abstraction the I/O is directed at. The `PgAioTargetID` enum identifies targets; the only currently defined non-invalid target is `PGAIO_TID_SMGR`, representing storage-manager (relation file) I/O.

Each target registers a `PgAioTargetInfo` descriptor containing:

- `name` — a short string used in log messages and the `pg_aios` view.
- `describe_identity` — formats a human-readable description of the specific file/block being accessed.
- `reopen` — reopens the file descriptor in a different process. This is required for the `worker` method: worker processes cannot inherit FDs from the issuing backend, so the target's `reopen` hook must re-derive and install the FD before the worker executes the I/O.

The caller must call `pgaio_io_set_target()` exactly once on a newly acquired handle before `pgaio_io_start_*()`. `pgaio_io_can_reopen()` and `pgaio_io_reopen()` are internal helpers used by the submission path when the I/O needs to execute in a foreign process.

## Completion Callbacks (`aio_callback.c`)

Callers register what should happen when an I/O finishes by attaching one or more callbacks to the handle before starting the I/O. `PgAioHandleCallbackID` — a small integer enum — identifies callbacks, rather than a function pointer. Using stable numeric IDs means the callback table survives process boundaries (worker processes, crash recovery) and makes the set of possible callbacks enumerable for debugging.

The three currently defined callback IDs are:

| ID | Callback object | Purpose |
|----|----------------|---------|
| `PGAIO_HCB_MD_READV` | `aio_md_readv_cb` | Lower-level md.c read completion |
| `PGAIO_HCB_SHARED_BUFFER_READV` | `aio_shared_buffer_readv_cb` | Shared buffer pool read completion |
| `PGAIO_HCB_LOCAL_BUFFER_READV` | `aio_local_buffer_readv_cb` | Local (temp table) buffer read completion |

Each `PgAioHandleCallbacks` struct provides up to three hooks:

- `stage` — called when the I/O transitions from `HANDED_OUT` to `STAGED`. Used to transfer ownership of buffer pins to the AIO subsystem so they survive across process boundaries.
- `complete_shared` — called in a critical section by whichever backend (or worker) actually completes the I/O. It may be the issuing backend or an unrelated one. Only shared-memory state may be modified here. The AIO subsystem invokes callbacks innermost-first (last registered, first called); each receives and returns a `PgAioResult` that may be further transformed.
- `complete_local` — called in a critical section in the issuing backend only, after `complete_shared`. It may modify process-local state (e.g. error reporting). The AIO subsystem does not store its return value back into the handle, since it should not affect other waiters.

A callback can also supply a `report` hook that formats a user-visible error message from a `PgAioResult` when an I/O fails.

Callers can register up to `PGAIO_HANDLE_MAX_CALLBACKS` callbacks per handle. Each callback also accepts a small `uint8` data value (e.g. a flag indicating whether to zero a buffer on invalid content) and an optional array of 64-bit per-iovec data words set via `pgaio_io_set_handle_data_64()` / `pgaio_io_set_handle_data_32()`.

## I/O Combining

The `io_combine_limit` GUC (default: `min(16, max_worker_processes)`) controls how many adjacent blocks are merged into a single vectored read. When the read stream has prefetched block N and the next callback returns block N+1, it submits them as a single `preadv()` call (or equivalent). This reduces the number of system calls. On NVMe devices, it also takes advantage of the drive's internal parallelism.

`io_max_combine_limit` sets the per-session upper bound; `io_combine_limit` can be lowered at session scope within that limit. For tablespaces on slow rotating disks, lowering `io_combine_limit` prevents excessive prefetch latency.

## Effect on Existing Code

Most existing backend code is unaffected by the AIO subsystem. Code that calls `ReadBuffer()` directly still works exactly as before: `ReadBuffer()` issues a synchronous read if the block is not in the shared buffer pool. The performance improvement comes from code paths that have been converted to use read streams — sequential scans, VACUUM heap scan, bitmap heap scan. Over time, more operations will likely migrate to the read stream API.

## Monitoring

The `pg_get_aios()` function (exposed as the `pg_aios` view, implemented in `aio_funcs.c`) iterates over the entire shared handle pool and returns one row per non-idle handle. Because there is no lock protecting handle state, the function uses a generation-and-state snapshot protocol: it records the generation before copying handle data, then verifies that neither the generation nor the state changed after the copy. If the generation changed, the function skips the row; if only the state changed, it retries the copy.

Each row exposes:

- owning PID, handle ID, generation counter, and state name
- operation type (`readv` / `writev`), file offset, and total byte length
- target name and a human-readable target description
- raw syscall result and distilled result status
- per-handle flags: `synchronous`, `references_local`, `buffered`

```sql
-- Open AIO file handles
SELECT * FROM pg_aios;

-- Per-backend I/O statistics (PG 18)
SELECT * FROM pg_stat_get_backend_io(pg_backend_pid());
```

The `pg_stat_io` view gains `read_bytes`, `write_bytes`, and `extend_bytes` columns in PG 18, replacing the single `op_bytes` column.

## Related Topics

- [[subsystems/storage/buffer-manager|Buffer Manager]] — how buffers are managed and how read streams interact with shared_buffers
- [[subsystems/storage/smgr|Storage Manager (smgr)]] — the file descriptor layer that AIO calls into
- [[code-paths/vacuum|VACUUM]] — one of the first major code paths converted to use read streams

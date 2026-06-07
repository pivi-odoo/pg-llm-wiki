---
title: "Parallel Logical Apply"
aliases:
  - parallel apply
  - parallel apply worker
  - logical replication parallel apply
tags:
  - theme/parallelism
  - symptom/replication-lag
source_files:
  - src/backend/replication/logical/applyparallelworker.c
  - src/include/replication/worker_internal.h
symbols:
  - ParallelApplyWorkerInfo
  - ParallelApplyWorkerShared
  - ParallelTransState
  - PartialFileSetState
  - pa_allocate_worker
  - pa_send_data
  - pa_switch_to_partial_serialize
  - pa_xact_finish
  - pa_lock_stream
  - pa_lock_transaction
  - LogicalParallelApplyLoop
  - ParallelApplyWorkerMain
---

Parallel logical apply is a feature introduced in PostgreSQL 16 that allows a logical replication subscriber to apply multiple streaming transactions concurrently, each in a dedicated background worker, instead of serializing all work through a single apply worker. PostgreSQL activates it when a subscription sets `streaming = parallel` and the publisher has `wal_level = logical` with streaming support enabled. The practical payoff is reduced apply lag for workloads where many large transactions arrive in rapid succession.

## Architecture: Leader and Parallel Apply Workers

The subscription's main process is the **leader apply worker** (LA). It receives the decoded change stream from the walsender, interprets protocol messages, and either applies changes directly (for non-streaming transactions) or hands them off to **parallel apply workers** (PA). Each PA is a separate background worker process that applies exactly one streaming transaction at a time.

LA assigns a PA to a transaction the moment the first `STREAM_START` message arrives for that transaction (`pa_allocate_worker()`, `applyparallelworker.c`). LA tracks the assignment in `ParallelApplyTxnHash`, a hash table keyed by remote transaction ID. LA caches a pointer to the worker assigned to the currently active stream block in `stream_apply_worker`, to avoid hash lookups on every message within a `STREAM_START`/`STREAM_STOP` pair.

LA does not start PA processes fresh for each transaction. Instead, LA maintains them in `ParallelApplyWorkerPool`, a list of `ParallelApplyWorkerInfo` structs. When a transaction ends, LA checks whether the pool already holds more than half of `max_parallel_apply_workers_per_subscription` idle workers. If so, it stops the worker process. Otherwise, LA marks the worker available for reuse (`pa_free_worker()`, `applyparallelworker.c`). This pool avoids the overhead of launching a new process for every transaction, while bounding idle memory consumption.

LA does not spawn workers when any of the following conditions hold (`pa_can_start()`): the subscription is not in parallel streaming mode, a `skiplsn` has been configured (skipping requires knowing the final LSN of the transaction before applying starts), or not all table sync operations have reached the `READY` state (because PAs cannot evaluate the `remote_final_lsn` check needed for partially-synced tables).

## Shared Memory Layout

Each PA gets its own dynamic shared memory (DSM) segment, created when the worker is launched (`pa_setup_dsm()`, `applyparallelworker.c`). `pa_setup_dsm()` divides the segment into three regions via a table-of-contents (`shm_toc`):

| Key | Contents | Size |
|---|---|---|
| `PARALLEL_APPLY_KEY_SHARED` | `ParallelApplyWorkerShared` control struct | Fixed |
| `PARALLEL_APPLY_KEY_MQ` | `shm_mq` for changes LA→PA | 16 MB |
| `PARALLEL_APPLY_KEY_ERROR_QUEUE` | `shm_mq` for errors PA→LA | 16 KB |

One segment per worker — rather than a single large shared segment — means PostgreSQL only allocates memory when a worker is actually needed.

`ParallelApplyWorkerShared` is the coordination hub between LA and each PA. Its fields govern commit ordering, deadlock detection, and the fallback serialization path:

| Field | Purpose |
|---|---|
| `xid` | Remote transaction ID being applied |
| `xact_state` (`ParallelTransState`) | `UNKNOWN` → `STARTED` → `FINISHED` lifecycle |
| `pending_stream_count` | Atomic counter of stream blocks not yet fully consumed |
| `last_commit_end` | PA's `XactLastCommitEnd`, reported back to LA for LSN tracking |
| `fileset_state` (`PartialFileSetState`) | State of the overflow file path |
| `fileset` | `FileSet` for serialized overflow changes |

## Commit Ordering via Session Locks

Allowing independent transactions to apply in parallel risks two failure modes: transaction dependency violations (a row inserted by TX-1 that TX-2 updates, if TX-2 commits first) and deadlocks. PostgreSQL preserves commit order by having LA wait for each PA to finish its transaction before advancing past the corresponding commit message. But naive waiting — LA blocks, PA needs more data from LA — creates a deadlock that the lock manager cannot see.

The solution is two distinct session-level lmgr locks, both keyed by `(subid, xid)`:

**Stream lock** (`PARALLEL_APPLY_LOCK_STREAM`, `pa_lock_stream()`): LA acquires this lock in `AccessExclusiveLock` mode before sending `STREAM_STOP`. It releases the lock after sending `STREAM_START`, `STREAM_COMMIT`, `STREAM_PREPARE`, or `STREAM_ABORT`. PA acquires the same lock in `AccessShareLock` mode after processing `STREAM_STOP`. It then immediately releases the lock. This creates a wait edge from PA to LA in the lock graph when PA is blocked waiting for the next stream block.

**Transaction lock** (`PARALLEL_APPLY_LOCK_XACT`, `pa_lock_transaction()`): PA holds this lock in `AccessExclusiveLock` mode for the entire duration of the transaction, releasing it only at commit or abort. LA acquires `AccessShareLock` at the transaction finish command. It releases the lock immediately. This creates a wait edge from LA to PA that completes the cycle. This makes three-process deadlocks (LA → PA-2 → PA-1 → LA) visible to the deadlock detector.

PostgreSQL uses session-level locks rather than transaction-level locks because they must persist across the transaction boundaries of the streaming protocol itself (`XactLockTableWait()` would not work for prepared transactions, which remain "in progress" from its perspective).

## Overflow Serialization

The LA→PA message queue is 16 MB. If LA cannot write to the queue within approximately 9 seconds (10-second timeout minus one retry interval, `pa_send_data()`, `applyparallelworker.c`), it concludes that PA is blocked — most likely waiting on a row-level lock held by another PA. It then switches to **partial serialization mode** (`pa_switch_to_partial_serialize()`).

In this mode, LA stops trying to send directly and instead writes the remaining transaction changes to a file via `stream_start_internal()`. The `PartialFileSetState` enum tracks the handoff:

| State | Meaning |
|---|---|
| `FS_EMPTY` | No overflow; direct queue path in use |
| `FS_SERIALIZE_IN_PROGRESS` | LA is writing to file |
| `FS_SERIALIZE_DONE` | LA has finished writing; fileset handle copied to shared memory |
| `FS_READY` | PA has acknowledged and is about to read |

PA polls for spooled messages in `pa_process_spooled_messages_if_required()` each time the queue would block. It transitions through `FS_SERIALIZE_DONE` → `FS_READY` → `FS_EMPTY` as it reads and applies the overflow file. Because the partial serialization path corrupts the in-queue state, LA always stops the PA associated with a serialized transaction rather than returning it to the pool when the transaction ends.

## Parallel Apply Worker Lifecycle

`ParallelApplyWorkerMain()` is the entry point. The worker attaches to its DSM segment, sets up the message queue and error queue, initialises the apply subsystem, and then loops in `LogicalParallelApplyLoop()`. The loop calls `shm_mq_receive()` in non-blocking mode. When no message is available, it checks for spooled overflow messages and then waits on its latch with a 1-second timeout.

Messages from LA are ordinary logical replication protocol messages framed with a `'w'` byte prefix. PA strips a 24-byte statistics header (two `XLogRecPtr` values plus a `TimestampTz`) that LA has already accounted for. It then calls `apply_dispatch()` — the same dispatcher used by the non-parallel apply path.

PA handles subtransactions with named savepoints. When PA receives a change whose XID differs from the top-level XID, it defines a savepoint with a name derived from `(suboid, xid)` (`pa_savepoint_name()`). A `STREAM_ABORT` for a subtransaction rolls back to the appropriate savepoint rather than aborting the top-level transaction.

On shutdown, the `pa_shutdown()` on-exit callback signals LA via `PROCSIG_PARALLEL_APPLY_MESSAGE` before detaching the DSM segment. This ensures LA reads any final error messages from the error queue before the segment disappears.

## Error Propagation

When a PA encounters an error, it writes an ErrorResponse into the error queue shm_mq. LA polls all active workers' error queues via `HandleParallelApplyMessages()`. `ProcessInterrupts()` invokes this function. If it finds an error message, LA re-raises it in its own context with an annotation identifying the source as a parallel apply worker. This surfaces errors to the subscription's monitoring infrastructure (and ultimately to `pg_stat_subscription`) as if the error occurred in LA directly.

## Key Structures

| Symbol | File | Role |
|---|---|---|
| `ParallelApplyWorkerInfo` | `worker_internal.h` | Per-worker handle: queue handles, DSM segment, shared pointer |
| `ParallelApplyWorkerShared` | `worker_internal.h` | In-DSM coordination state: XID, transaction state, fileset state |
| `ParallelTransState` | `worker_internal.h` | `UNKNOWN` / `STARTED` / `FINISHED` commit-order state machine |
| `PartialFileSetState` | `worker_internal.h` | `FS_EMPTY` / `FS_SERIALIZE_IN_PROGRESS` / `FS_SERIALIZE_DONE` / `FS_READY` |
| `ParallelApplyWorkerPool` | `applyparallelworker.c` | `List*` of all active/idle `ParallelApplyWorkerInfo` entries |
| `ParallelApplyTxnHash` | `applyparallelworker.c` | Hash map from remote XID to worker info |

## Related Topics

- [[subsystems/replication/logical|Logical Replication]] — the broader apply pipeline that parallel apply extends

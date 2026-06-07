---
title: "Postmaster Child Slot Management"
aliases:
  - PMChild
  - PMChildPool
  - postmaster child slots
  - pmchild
  - ActiveChildList
source_files:
  - src/backend/postmaster/pmchild.c
symbols:
  - PMChild
  - PMChildPool
  - ActiveChildList
  - InitPostmasterChildSlots
  - AssignPostmasterChildSlot
  - ReleasePostmasterChildSlot
  - AllocDeadEndChild
  - FindPostmasterChildByPid
  - MaxLivePostmasterChildren
---

The table that manages postmaster child slots was introduced in PostgreSQL 18. It is a centralised data structure that tracks every process spawned by the postmaster. Before PG18, the postmaster tracked each child type in its own ad-hoc array. `pmchild.c` consolidates that into a single fixed-size pool of `PMChild` structs, one pool per backend type, plus an `ActiveChildList` doubly-linked list spanning all active children. When a child exits, the postmaster's `SIGCHLD` handler calls `FindPostmasterChildByPid()` to identify the dead process by PID and take the appropriate cleanup action.

## Slot Pools

`InitPostmasterChildSlots()` pre-allocates child slots at startup and divides them into type-specific pools (`PMChildPool`). Each pool has a size, a starting slot number, and a freelist of unused `PMChild` entries. The relevant GUCs determine pool sizes:

| Pool | Size |
|---|---|
| `B_BACKEND` | `2 × (MaxConnections + max_wal_senders)` |
| `B_AUTOVAC_WORKER` | `autovacuum_worker_slots` |
| `B_BG_WORKER` | `max_worker_processes` |
| `B_IO_WORKER` | `MAX_IO_WORKERS` |
| Single-instance processes | 1 each |

The backend pool is sized at twice the connection limit. Connections in authentication still occupy a slot before the postmaster decides whether to admit them. WAL senders start life as regular backends and share the `B_BACKEND` pool.

Each `PMChild` struct records:

- `pid` — the child's OS process ID (0 until the process is actually forked)
- `child_slot` — a unique 1-based integer within the pool, used as an index into the shared-memory `PMChildFlags` array managed by `pmsignal.c`
- `bkend_type` — the `BackendType` enum value
- `rw` — for background workers, a pointer to the `RegisteredWorker` entry
- `bgworker_notify` — whether the bgworker launcher should be notified when this child exits

## Slot Lifecycle

`AssignPostmasterChildSlot(btype)` dequeues a slot from the appropriate pool's freelist, pushes it onto `ActiveChildList`, and calls `MarkPostmasterChildSlotAssigned()` to update the shared-memory mirror. The caller fills in `pmchild->pid` after the fork succeeds.

`ReleasePostmasterChildSlot(pmchild)` removes the entry from `ActiveChildList`, returns it to the pool freelist (WAL senders return to the backend pool regardless of their eventual type), and calls `MarkPostmasterChildSlotUnassigned()`. The function's return value indicates whether the child detached cleanly from shared memory. A false return prompts the postmaster to consider the shared memory state potentially corrupted.

## Dead-End Backends

`pmchild.c` handles dead-end backends — connections that fail authentication before joining shared memory — separately. They have no slot number and no entry in the shared-memory mirror. `AllocDeadEndChild()` pallocs a `PMChild` struct directly (returning `NULL` on OOM rather than erroring) and adds it to `ActiveChildList` with `bkend_type = B_DEAD_END_BACKEND`. When the process exits, the postmaster pfrees it rather than returning it to a pool. There is no hard limit on dead-end backends.

## Crash Restart Invariant

The postmaster calls `InitPostmasterChildSlots()` only at initial startup, not during crash restart. The syslogger process survives crash restarts. Its `PMChild` slot must remain valid across the restart. When children die in response to the crash, normal SIGCHLD processing clears all other child slots. The memory backing the slots is not freed or reallocated.

## Related Topics

- [[architecture/process-architecture]] — the overall postmaster and child process model
- [[architecture/connection-launch]] — how the postmaster forks a new backend
- [[architecture/backend-startup]] — what happens in the child after the fork

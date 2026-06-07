---
title: "Latches and IPC"
aliases:
  - latch
  - WaitLatch
  - WaitEventSet
  - SetLatch
  - shm_mq
  - shared memory message queue
tags:
  - theme/parallelism
source_files:
  - src/backend/storage/ipc/latch.c
  - src/backend/storage/ipc/shm_mq.c
  - src/include/storage/latch.h
  - src/include/storage/shm_mq.h
symbols:
  - Latch
  - WaitEventSet
  - WaitEvent
  - WaitLatch
  - WaitEventSetWait
  - SetLatch
  - ResetLatch
  - InitLatch
  - InitSharedLatch
  - OwnLatch
  - CreateWaitEventSet
  - AddWaitEventToSet
  - shm_mq
  - shm_mq_handle
  - shm_mq_create
  - shm_mq_send
  - shm_mq_receive
---

A **latch** is a per-process binary flag that one process, or a signal handler, can set to interrupt another process sleeping inside a `WaitLatch` call. Latches are the fundamental building block for all inter-process notifications in PostgreSQL: every time a backend parks itself to wait for work, it does so on a latch. The higher-level `WaitEventSet` API generalises this into a unified interface that can wait on latches, sockets, and postmaster death simultaneously. Shared-memory message queues (`shm_mq`) build on top of latches to provide a simple one-way byte-stream channel between two processes.

## Latch semantics

A latch is a `Latch` struct with a boolean `is_set` flag and an owner PID. The three operations on a latch are `SetLatch`, `ResetLatch`, and `WaitLatch`:

- **`SetLatch`** marks the latch as set and, if the owner process is currently sleeping in a wait call, delivers a signal to wake it up. It is safe to call from a signal handler.
- **`ResetLatch`** clears the flag. The owning process must call it, typically at the top of each iteration of a wait loop before checking flags, to avoid a missed-wakeup race.
- **`WaitLatch`** blocks until the latch is set, a timeout expires, or postmaster death is detected.

There are two latch varieties. The calling process owns a **process-local latch** (`InitLatch`) for its entire lifetime. A **shared latch** (`InitSharedLatch`) lives in shared memory and has no initial owner; a process acquires ownership at startup with `OwnLatch` and releases it with `DisownLatch`. PostgreSQL uses shared latches wherever one process needs to wake up another — for example, the `PGPROC.procLatch` in each backend's process-control struct.

### Memory barriers and the reset protocol

`SetLatch` places a memory barrier before reading `is_set`. `ResetLatch` places one after clearing it. This ensures that flag variables set in one process are visible to the other before the wakeup occurs. It also ensures that a clearing process sees any flags set after the wakeup. The standard coding convention for wait loops is:

```
for (;;) {
    ResetLatch(MyLatch);       /* clear before checking flags */
    if (work_to_do)
        handle_work();
    WaitLatch(MyLatch, ...);   /* wait at the bottom, not the top */
}
```

Calling `ResetLatch` at the bottom of the loop (before the wait) instead of the top would introduce a race where a `SetLatch` arriving between the check and the wait is silently lost.

## Platform wait implementations

`WaitLatch` is a thin wrapper around `WaitEventSetWait` on a per-process `WaitEventSet`. The OS's I/O-multiplexing primitive, selected at compile time, does the actual blocking:

| Backend | Platform | Signal delivery mechanism |
|---|---|---|
| `WAIT_USE_EPOLL` | Linux | `signalfd` for SIGURG (no signal handler needed) |
| `WAIT_USE_KQUEUE` | macOS / BSDs | `EVFILT_SIGNAL` for SIGURG |
| `WAIT_USE_POLL` | POSIX fallback | Self-pipe trick |
| `WAIT_USE_WIN32` | Windows | Inheritable Win32 event objects |

The core challenge is a race: a signal can arrive between the moment a process checks whether it needs to sleep and the moment it actually enters the blocking syscall. If the syscall does not see the signal, it sleeps indefinitely.

The **self-pipe trick** (poll) resolves this. The SIGURG handler writes a byte to a pipe, and `poll()` watches the read end of that pipe. A signal that arrives before `poll()` will leave a byte in the kernel pipe buffer; `poll()` sees it immediately and does not sleep. Each child process creates its own pipe at startup (`InitializeLatchSupport`) and closes any inherited pipe from the postmaster.

The **signalfd approach** (epoll on Linux) avoids the signal handler entirely. It blocks SIGURG as a regular signal and instead consumes it via a `signalfd` file descriptor, which epoll watches like any other FD. This sidesteps the re-entrant concerns of signal handlers.

## WaitEventSet

The `WaitEventSet` API allows a process to wait on multiple heterogeneous events in one call, which is more efficient than separate `WaitLatch` calls. A process creates an event set with `CreateWaitEventSet`, registers events with `AddWaitEventToSet`, and blocks with `WaitEventSetWait`. `WaitEventSetWait` returns an array of `WaitEvent` structs describing what fired.

Event types are selected via bitmask flags:

| Flag | Meaning |
|---|---|
| `WL_LATCH_SET` | Wake when the specified latch is set |
| `WL_SOCKET_READABLE` | Wake when a socket has data to read |
| `WL_SOCKET_WRITEABLE` | Wake when a socket is ready to write |
| `WL_TIMEOUT` | Wake after a timeout (only for `WaitLatch`, not `WaitEventSetWait`) |
| `WL_POSTMASTER_DEATH` | Wake if postmaster dies |
| `WL_EXIT_ON_PM_DEATH` | Like above, but exit rather than return |

Postmaster death detection uses a pipe. All child processes inherit its read end. When the postmaster exits, the write end closes automatically, and the kernel notifies all waiters.

Each `AddWaitEventToSet` call carries a `wait_event_info` tag that maps to the `wait_event_type` and `wait_event` columns in `pg_stat_activity`. The tag is a packed integer encoding a subsystem and event name from the `pgstat_wait_event` enum.

## Shared memory message queues (shm_mq)

A shared memory message queue is a fixed-size ring buffer in shared memory connecting exactly one sender and one receiver (`shm_mq.c`). PostgreSQL uses it wherever two processes need to exchange a stream of variable-length messages without copying data through a backend socket: parallel query workers send tuples to the leader, logical replication workers send decoded changes to apply workers, and background workers communicate with their launcher.

The ring buffer (`mq_ring`) has a simple ownership split:
- `mq_bytes_written` (updated only by the sender) marks how far the sender has written.
- `mq_bytes_read` (updated only by the receiver) marks how far the receiver has consumed.

Neither counter needs a lock, because the sender and the receiver touch disjoint regions of the buffer. The sender only writes to the empty region (between `bytes_written` and `bytes_written + ring_size - (bytes_written - bytes_read)`). The receiver only reads from the full region. Both are 64-bit atomic values and never wrap. `shm_mq` uses memory barriers explicitly at the boundaries where one side's updates need to be visible to the other.

When the ring is full (sender) or empty (receiver), the blocking party waits on its own latch and sets the counterparty's latch after making progress. The `shm_mq_handle` struct (`mqh_send_pending`, `mqh_consume_pending`) batches latch notifications: the sender defers updating `mq_bytes_written` until it has written at least a quarter of the ring, reducing the frequency of expensive `SetLatch` calls.

`shm_mq` frames messages with a length prefix. For messages that fit contiguously in the ring, the receiver returns a zero-copy pointer directly into the ring buffer; for messages that wrap around the end of the ring, it copies them into a backend-local buffer (`mqh_buffer`).

Both ends of the queue call `shm_mq_attach` before use and `shm_mq_detach` when done. Detaching sets `mq_detached`. The counterparty sees this the next time it checks, so it can shut down cleanly instead of hanging indefinitely, with no error raised.

## Related Topics

- [[subsystems/storage/shared-memory|Shared Memory]]
- [[subsystems/executor/parallel|Parallel Query]]
- [[subsystems/background/bgworker|Background Workers]]
- [[subsystems/replication/logical|Logical Replication]]
- [[architecture/process-architecture|Process Architecture]]

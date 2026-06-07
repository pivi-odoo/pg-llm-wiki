---
title: "Process Signal Mechanism (procsignal)"
aliases:
  - procsignal
  - ProcSignal
  - interprocess signaling
  - query cancellation internals
source_files:
  - src/backend/storage/ipc/procsignal.c
  - src/include/storage/procsignal.h
symbols:
  - ProcSignalSlot
  - ProcSignalHeader
  - ProcSignalReason
  - SendProcSignal
  - ProcSignalInit
  - procsignal_sigusr1_handler
  - CheckProcSignal
  - EmitProcSignalBarrier
  - WaitForProcSignalBarrier
  - ProcessProcSignalBarrier
  - QueryCancelPending
  - InterruptPending
  - StatementCancelHandler
  - ProcessInterrupts
---

The process signal mechanism lets one backend asynchronously notify another backend — or an auxiliary process such as a WAL sender — without going through PostgreSQL's IPC queues. It multiplexes many distinct event types over a single OS signal (SIGUSR1) by pairing the signal with a per-process array of boolean flags in shared memory. This separation of "wake up" from "what happened" is the key design decision: SIGUSR1 is cheap to deliver and cheap to handle, while the flag array carries the actual meaning without any of the payload limitations of a raw Unix signal.

## Shared memory layout and slot registration

At startup, `ProcSignalShmemInit()` (`procsignal.c`) allocates a `ProcSignalHeader` in shared memory containing one `ProcSignalSlot` for every possible backend ID plus one for each auxiliary process type (checkpointer, background writer, WAL writer, etc.). Each slot holds the PID of the registered process, an array of `volatile sig_atomic_t` flags indexed by `ProcSignalReason`, and state for the barrier mechanism described below.

When a backend starts, it calls `ProcSignalInit()` with its backend ID as the index. This writes the process's PID into the slot and clears any leftover flags from a previously-occupying process. The slot remains owned until process exit. At that point, `CleanupProcSignalState()` zeroes the PID. This is registered as a shared-memory exit callback, so cleanup always happens, even on abnormal exit.

Because `pss_signalFlags` entries are `sig_atomic_t`, reads and writes are individually atomic on every platform PostgreSQL supports, and setting or checking a flag needs no lock. The deliberate trade-off is that the recipient observes only one notification, even if the sender signals the same reason twice before the recipient handles it. This is acceptable, because all current signal types are idempotent or re-fire themselves if still needed.

## Signal types and their purposes

`ProcSignalReason` (defined in `procsignal.h`) enumerates every distinct kind of inter-backend notification:

| Reason | Used for |
|---|---|
| `PROCSIG_CATCHUP_INTERRUPT` | Shared-invalidation catchup: prompts a backend to drain the sinval message queue |
| `PROCSIG_NOTIFY_INTERRUPT` | LISTEN/NOTIFY: wakes a listener that a notification is pending |
| `PROCSIG_PARALLEL_MESSAGE` | Parallel query: signals the leader that a worker has posted an error or notice |
| `PROCSIG_PARALLEL_APPLY_MESSAGE` | Logical replication parallel apply: equivalent worker-to-leader channel |
| `PROCSIG_WALSND_INIT_STOPPING` | Asks WAL senders to prepare for shutdown |
| `PROCSIG_BARRIER` | Global barrier: see the barrier section below |
| `PROCSIG_LOG_MEMORY_CONTEXT` | Asks the target to log its current [[subsystems/memory/contexts|memory context]] tree |
| `PROCSIG_RECOVERY_CONFLICT_*` | Hot standby: informs a query backend of a recovery conflict (lock, snapshot, tablespace, etc.) |

Notably absent from this list are the two most common cancellation events, `SIGINT` (query cancel) and `SIGTERM` (die). Callers send those as bare OS signals directly to the target process — not through the procsignal flag array. PostgreSQL does not use the procsignal mechanism for cancellation from outside; it uses it for backend-to-backend coordination.

## Sending a signal

`SendProcSignal(pid, reason, backendId)` (`procsignal.c`) performs two steps: set the flag at `slot->pss_signalFlags[reason]`, then call `kill(pid, SIGUSR1)`. If the caller provides `backendId`, `SendProcSignal()` finds the slot by direct index; otherwise it scans the array backward (auxiliary processes have high-numbered slots and are the common case for non-ID callers). There is a deliberate race condition: the target process could exit, and another process could reuse its slot between the PID check and the `kill()`. This is acceptable because every signal type is harmless even if it arrives spuriously.

The SIGUSR1 handler `procsignal_sigusr1_handler()` runs in signal-handler context. It calls `CheckProcSignal()` for each known reason in sequence. For each flag that is set, it clears the flag and invokes the corresponding notification function. Because signal handlers must be async-signal-safe, these notification functions only set backend-local `volatile sig_atomic_t` flags (such as `NotifyInterruptPending`, `ParallelMessagePending`) and call `SetLatch(MyLatch)` to wake the main loop. PostgreSQL always defers the actual work to normal process context.

## Query cancellation and interrupt processing

Query cancellation illustrates how the procsignal approach interacts with the OS signal layer. When a client sends a cancel request, the postmaster receives it on a new connection and validates the backend PID and cancel key. It then delivers `SIGINT` directly to the target backend with `kill()` — bypassing the procsignal mechanism entirely. Similarly, `pg_cancel_backend()` (`signalfuncs.c`) sends `SIGINT` directly.

The `StatementCancelHandler()` signal handler (`postgres.c`) runs in the target backend when `SIGINT` arrives. It sets two flags: `InterruptPending = true` and `QueryCancelPending = true`, then returns. The backend continues executing whatever instruction it was on and reaches the next `CHECK_FOR_INTERRUPTS()` call, which expands to:

```c
if (INTERRUPTS_PENDING_CONDITION())
    ProcessInterrupts();
```

`ProcessInterrupts()` (`postgres.c`) checks all pending interrupt flags and raises errors or terminates as appropriate. For a query cancel, it raises `ERROR` with `ERRCODE_QUERY_CANCELED`. This unwinds through the normal exception machinery, aborts the current transaction, and returns the backend to idle state.

This polling model — check a flag at safe points rather than handling the interrupt asynchronously — is deliberate. Asynchronous signal handling is extremely difficult to make correct in the presence of complex data structures like memory contexts, lock tables, and transaction state. By deferring action to `CHECK_FOR_INTERRUPTS()`, PostgreSQL ensures that cancellation can only happen at points where the code is prepared for it. `HOLD_INTERRUPTS()` / `RESUME_INTERRUPTS()` bracket sections where even a pending cancel must wait. `HOLD_CANCEL_INTERRUPTS()` / `RESUME_CANCEL_INTERRUPTS()` allow die signals through while still deferring query cancels.

`statement_timeout` and `lock_timeout` follow the same path. When the timeout fires, the timeout subsystem sets `QueryCancelPending` (and `InterruptPending`) from the `SIGALRM` handler. The next `CHECK_FOR_INTERRUPTS()` call picks this up. From the backend's perspective, a timeout cancel and a client-requested cancel are identical after the flag is set; the error message differs because `ProcessInterrupts()` checks `cancel_from_timeout` to distinguish the cause.

Recovery conflict signals (`PROCSIG_RECOVERY_CONFLICT_*`) use the procsignal mechanism rather than direct SIGINT because they originate from the startup process (not the client), carry typed reasons, and may need to either cancel or terminate the conflicting backend depending on the conflict type and `max_standby_*` settings.

## Global barrier mechanism

Some state changes require every active backend to acknowledge the change before the caller proceeds. One example is closing all open file descriptors for a dropped relation (`PROCSIGNAL_BARRIER_SMGRRELEASE`). The barrier mechanism handles this.

`EmitProcSignalBarrier(type)` sets a bit in `pss_barrierCheckMask` for every slot and increments the global `psh_barrierGeneration` counter. It then signals all active processes via SIGUSR1. Each recipient's handler sets `ProcSignalBarrierPending`, which causes the next `CHECK_FOR_INTERRUPTS()` to call `ProcessProcSignalBarrier()`. That function reads the check mask, clears it, and processes each requested barrier type. It then writes the current global generation into its own `pss_barrierGeneration`. The caller of `EmitProcSignalBarrier()` calls `WaitForProcSignalBarrier(generation)`. It blocks until every slot shows a generation at least as high as the one returned, using condition variable waits with 5-second timeouts to log stalled backends.

This is expensive — it interrupts every backend — so PostgreSQL uses it only for infrequent global-state transitions.

## Interrupt suppression and critical sections

Code that must not be interrupted uses `HOLD_INTERRUPTS()`. This increments `InterruptHoldoffCount`. `CHECK_FOR_INTERRUPTS()` is a no-op while this counter is non-zero. Critical sections (marked with `START_CRIT_SECTION()`) go further. They increment `CritSectionCount`, which makes any `ereport(ERROR)` escalate to `PANIC` and prevents any controlled unwind. WAL-writing code uses critical sections to ensure that it never silently abandons a partial write.

## Implications for application developers

Query cancellation cooperates with the backend rather than killing it outright. As a result, a backend blocked in an uninterruptible kernel call — most commonly a long-running `read()` or `write()` on a socket or file — will not see the cancel until it returns from the kernel. A backend blocked waiting for a [[subsystems/locking/lwlocks|lightweight lock]] or a [[subsystems/storage/latch-and-ipc|latch]] always has `CHECK_FOR_INTERRUPTS()` in the wait path and will respond promptly. A backend blocked waiting for a heavyweight lock calls `ProcessInterrupts()` in the lock wait loop and will also respond promptly to cancellation.

This means `statement_timeout` is not a hard wall-clock guarantee — it fires at the next safe interrupt point. That point is almost always within milliseconds for normal query execution, but filesystem or network I/O that the backend cannot interrupt could delay it. Extensions that implement long loops in C must call `CHECK_FOR_INTERRUPTS()` periodically to remain cancellable.

## Related Topics

- [[subsystems/background/pmsignal|postmaster signals]]
- [[subsystems/storage/latch-and-ipc|latches and IPC]]
- [[subsystems/background/timeouts|timeout handling]]

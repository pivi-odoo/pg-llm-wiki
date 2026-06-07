---
title: "Global State and Process-Level Utilities"
aliases:
  - global variables
  - backend globals
  - process utilities
  - globals.c
source_files:
  - src/backend/utils/init/globals.c
  - src/backend/utils/init/usercontext.c
  - src/backend/utils/error/assert.c
  - src/backend/utils/hash/pg_crc.c
  - src/backend/libpq/pqsignal.c
  - src/backend/postmaster/interrupt.c
symbols:
  - MyProcPid
  - MyDatabaseId
  - MyDatabaseTableSpace
  - MaxBackends
  - NBuffers
  - InterruptPending
  - QueryCancelPending
  - ProcDiePending
  - InterruptHoldoffCount
  - CritSectionCount
  - SwitchToUntrustedUser
  - RestoreUserContext
  - UserContext
  - ExceptionalCondition
  - pg_crc32_table
  - pqsignal
  - pqinitmask
  - HandleMainLoopInterrupts
  - SignalHandlerForConfigReload
  - SignalHandlerForShutdownRequest
  - ConfigReloadPending
  - ShutdownRequestPending
---

PostgreSQL backends share a small set of process-level variables that act as ambient context, available to any subsystem without being threaded through every call. These globals — declared in `src/backend/utils/init/globals.c` — capture facts that are process-scoped and constant for the lifetime of a backend: which database it is connected to, what its PID is, and how large shared memory structures are. Several companion files provide utilities that depend on this context: safe signal installation, interrupt-flag checking, a privilege-bracket mechanism for running code as another user, and low-level primitives for assertions and checksums.

## Backend-global variable declarations

`globals.c` is the single authoritative definition point for variables that cross subsystem boundaries. The file's own comment says as much: "Globals used all over the place should be declared here and not in other modules." The corresponding `extern` declarations live in `src/include/miscadmin.h`, which nearly every backend file includes.

The variables fall into a few natural groups:

**Process identity.** `MyProcPid` is the backend's own PID. `MyDatabaseId` and `MyDatabaseTableSpace` are the OIDs of the connected database and its default tablespace. `PostmasterPid` is the postmaster's PID, used by signal-routing code to verify it is not running in an accidentally forked child. `MyProcNumber` is the index into the shared `PGPROC` array. It starts as `INVALID_PROC_NUMBER`. The process fills it in once it claims a slot.

**Shared-memory sizing parameters.** `NBuffers`, `MaxConnections`, `MaxBackends`, `max_worker_processes`, and `max_parallel_workers` are the primary inputs to shared memory layout calculations. `PostmasterMain()` computes `MaxBackends` after background-worker registration is complete. The others reflect GUC settings loaded at startup.

**Interrupt flags.** Signal handlers write a dense cluster of `volatile sig_atomic_t` flags (`InterruptPending`, `QueryCancelPending`, `ProcDiePending`, `IdleInTransactionSessionTimeoutPending`, and others). Code reads these flags at safe points. The `CHECK_FOR_INTERRUPTS()` macro (defined in `miscadmin.h`) checks `InterruptPending` only when both `InterruptHoldoffCount` and `CritSectionCount` are zero. This ensures that interrupts cannot fire inside a critical section or within a `HOLD_INTERRUPTS()` / `RESUME_INTERRUPTS()` bracket.

**GUC-backed operating parameters.** Variables such as `work_mem`, `maintenance_work_mem`, `enableFsync`, and the vacuum cost knobs (`VacuumCostLimit`, `VacuumCostDelay`) are declared here because they are read pervasively. GUC entries elsewhere wire these variables. `globals.c` provides only their storage.

## In-process privilege brackets

`src/backend/utils/init/usercontext.c` provides `SwitchToUntrustedUser()` and `RestoreUserContext()`, which allow a block of code to run with a different `CurrentUserId` without issuing a SQL `SET ROLE`. Security-definer functions and extensions that need to perform catalog operations as a specific user use this pattern.

The `UserContext` struct captures three pieces of state before the switch: the caller's user OID, the current security context flags, and a GUC nest level. The restore function uses all three to unwind the switch cleanly:

- If the target user cannot `SET ROLE` back to the caller, `SwitchToUntrustedUser()` imposes `SECURITY_RESTRICTED_OPERATION`. It also opens a new GUC nest level with `NewGUCNestLevel()`. This prevents the code running under the target identity from making persistent session-level changes.
- `RestoreUserContext()` rolls back GUC changes within the nest level (`AtEOXact_GUC(false, nestlevel)`) before restoring the original user ID and security flags.

When the two users mutually trust each other — each can `SET ROLE` to the other — no restriction is imposed and no nest level is opened (`context->save_nestlevel = -1`).

This is a purely in-process mechanism. It does not interact with `SET ROLE`. It does not create a transaction savepoint. It does not affect session-level state outside the bracketed code.

## Assert implementation

`src/backend/utils/error/assert.c` contains `ExceptionalCondition()`, the function invoked when an `Assert()` macro fires. On debug builds (compiled with `PG_USE_ASSERT_CHECKING`), the `Assert()` macro evaluates its condition and calls `ExceptionalCondition()` on failure. On production builds, the macro expands to nothing, and this file is essentially empty.

When called, `ExceptionalCondition()` writes the failed condition string, source file, and line number directly to stderr using `write_stderr()`. This deliberately bypasses `elog()` to minimise the infrastructure required at the point of failure, since an assertion failure may indicate that the error-reporting machinery itself is compromised. If the platform provides `backtrace_symbols_fd()`, a stack trace follows. The function ends with `abort()`, which generates a core dump on most Unix systems. The `SLEEP_ON_ASSERT` build option inserts a long sleep before `abort()` to allow a debugger to attach.

## CRC-32C checksum computation

`src/backend/utils/hash/pg_crc.c` provides the lookup table (`pg_crc32_table[256]`) and the software polynomial loop used by the `COMP_TRADITIONAL_CRC32` and `COMP_LEGACY_CRC32` macros defined in `src/include/utils/pg_crc.h`. PostgreSQL retains these older variants for compatibility with on-disk structures that predate PostgreSQL 9.5.

The checksum variant used for [[subsystems/storage/toast|WAL]] record integrity and page checksums is CRC-32C (Castagnoli polynomial), declared in `src/include/port/pg_crc32c.h`. The `COMP_CRC32C` macro dispatches at runtime to the fastest available implementation: on x86 with SSE4.2, `pg_comp_crc32c_sse42()` (`src/port/pg_crc32c_sse42.c`) uses the hardware `crc32` instruction; on ARMv8, `pg_comp_crc32c_armv8()` uses the equivalent ARM instruction; on other architectures, `pg_comp_crc32c_sb8()` (`src/port/pg_crc32c_sb8.c`) provides a software slice-by-8 fallback. Architecture-specific "choose" files select the function pointer `pg_comp_crc32c` once at startup.

CRC-32C is not CRC-32. The Castagnoli polynomial has better error-detection properties for short messages. iSCSI and SCTP also mandate it as their checksum. PostgreSQL adopted it for page checksums and WAL precisely because of those properties.

## Signal setup and signal masks

`pqsignal()` (`src/port/pqsignal.c`) is a wrapper around `sigaction()` that always sets `SA_RESTART`. PostgreSQL designs signal handlers to set a flag and return immediately. The `SA_RESTART` flag ensures that the kernel automatically restarts any system call the signal interrupts, rather than returning `EINTR`. All signal registrations in the backend go through `pqsignal()` to enforce this invariant consistently.

Beyond `SA_RESTART`, the wrapper maintains a process-local table of the actual handler functions. It installs a single `wrapper_handler` as the `sa_handler`. The wrapper checks that `MyProcPid` matches the current process's PID before dispatching. A process accidentally forked by `system(3)` or similar would have a stale `MyProcPid`. It would reinstall the default handler rather than running PostgreSQL's handler against shared memory it did not initialise.

`pqinitmask()` (`src/backend/libpq/pqsignal.c`) manages signal masks through three global `sigset_t` variables: `BlockSig` (all blockable signals), `UnBlockSig` (empty), and `StartupBlockSig` (all signals except `SIGTERM`, `SIGQUIT`, and `SIGALRM`). Code blocks signals by calling `sigprocmask()` with `BlockSig`. It restores them with `UnBlockSig`. The `StartupBlockSig` variant allows the process to be killed or alarmed during the startup-packet exchange while still blocking most other signals.

## Background-worker interrupt handling

`src/backend/postmaster/interrupt.c` provides the interrupt-check entry point for [[subsystems/background/autovacuum|autovacuum]], background workers, and similar long-running processes. Regular backend sessions check for interrupts through `PostgresMain()`. Background workers call `HandleMainLoopInterrupts()` in their own main loops instead.

`HandleMainLoopInterrupts()` processes three conditions in sequence:

- `ProcSignalBarrierPending`: calls `ProcessProcSignalBarrier()` to handle cross-process signals delivered through the proc-signal mechanism.
- `ConfigReloadPending`: clears the flag and calls `ProcessConfigFile(PGC_SIGHUP)` to reload `postgresql.conf`.
- `ShutdownRequestPending`: calls `proc_exit(0)` for a clean shutdown.

The corresponding signal handlers set these flags: `SignalHandlerForConfigReload()` sets `ConfigReloadPending` in response to `SIGHUP`. `SignalHandlerForShutdownRequest()` sets `ShutdownRequestPending` in response to `SIGTERM` (or `SIGUSR2` for some processes). Both handlers also call `SetLatch(MyLatch)` to wake the main loop if it is sleeping in `WaitLatch()`. This ensures that PostgreSQL checks the flag promptly without the process needing to poll.

## See also

- [[architecture/process-architecture|Process architecture]] — how backends and background workers fit into the process model
- [[architecture/shared-memory|Shared memory layout]] — how NBuffers and MaxBackends drive shared memory sizing
- [[architecture/backend-initialization|Backend initialization]] — when globals are populated during startup
- [[subsystems/memory/contexts|Memory contexts]] — per-query memory management complementing the process-level globals
- [[subsystems/background/autovacuum|Autovacuum]] — a background worker that uses HandleMainLoopInterrupts

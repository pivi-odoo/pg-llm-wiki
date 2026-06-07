---
title: SQL Signal Functions
aliases:
  - pg_terminate_backend
  - pg_cancel_backend
  - pg_reload_conf
  - pg_rotate_logfile
  - pg_log_backend_memory_contexts
tags:
  - symptom/lock-wait
  - symptom/connection-exhaustion
source_files:
  - src/backend/storage/ipc/signalfuncs.c
  - src/backend/utils/adt/mcxtfuncs.c
  - src/include/storage/pmsignal.h
symbols:
  - pg_signal_backend
  - pg_terminate_backend
  - pg_cancel_backend
  - pg_reload_conf
  - pg_rotate_logfile_v2
  - pg_log_backend_memory_contexts
  - SendPostmasterSignal
  - PMSIGNAL_ROTATE_LOGFILE
---

PostgreSQL exposes a small family of SQL-callable functions that let administrators manage running backends and control server-level operations without dropping to the OS shell. These functions translate SQL calls into Unix signals or postmaster inter-process messages, making them auditable, permission-controlled, and composable with standard query tools like [[subsystems/observability/pg-stat-activity|pg_stat_activity]].

## Cancel vs Terminate

The most commonly used pair — `pg_cancel_backend(pid)` and `pg_terminate_backend(pid)` — differ in intent and consequence. Both resolve to `pg_signal_backend()` in `signalfuncs.c`, but they deliver different Unix signals.

`pg_cancel_backend()` sends **SIGINT**. A backend receiving SIGINT interrupts whatever statement is currently executing. It rolls back any open subtransaction and returns to idle — the connection stays alive. This is equivalent to pressing Ctrl-C in `psql`. The client sees an error message and can immediately send new queries.

`pg_terminate_backend()` sends **SIGTERM**. The backend performs an orderly shutdown: it rolls back any open transaction, releases locks, removes its entry from shared memory, and exits. This closes the TCP connection, so the client must reconnect. Because the backend runs cleanup code, this is always safer than `kill -9` from the OS shell. A raw `kill -9` would leave shared-memory state inconsistent until the postmaster detects the crash.

```mermaid
TD
    A[pg_cancel_backend] -->|SIGINT| B[Backend interrupts query]
    B --> C[Rolls back subtransaction]
    C --> D[Returns to idle — connection survives]

    E[pg_terminate_backend] -->|SIGTERM| F[Backend begins shutdown]
    F --> G[Rolls back open transaction]
    G --> H[Releases locks and shared memory]
    H --> I[Exits — connection closes]
```

`pg_terminate_backend()` in PostgreSQL 14+ accepts an optional second argument: a timeout in milliseconds. When you give a non-zero timeout, the function polls — using a latch wait — until the process is no longer visible to the OS (`kill(pid, 0)` returns ESRCH). It then returns `true`. On timeout it emits a warning and returns `false`. This makes it safe to use in scripts that need to confirm a slot is actually free before proceeding.

## Permission Model

Both signal functions share the same permission check in `pg_signal_backend()`. Three levels are enforced in order:

1. **Superuser-owned target**: if the target backend's `roleId` is a superuser (or is unset, as auxiliary processes may be), PostgreSQL allows only a superuser caller. Non-superusers get `SIGNAL_BACKEND_NOSUPERUSER`.
2. **Role membership**: a non-superuser may signal backends owned by any role they have membership in, checked via `has_privs_of_role()`.
3. **`pg_signal_backend` role**: any role granted membership in the built-in `pg_signal_backend` role may signal non-superuser backends, regardless of specific role membership.

The practical consequence: granting `pg_signal_backend` to a monitoring role lets it cancel runaway queries without giving it access to data or the ability to touch superuser sessions. This is the recommended approach for automated query-management tooling.

`pg_reload_conf()` and `pg_rotate_logfile()` (the v2 variant, which is the core built-in) delegate permission checks entirely to the SQL `GRANT` system rather than performing their own checks, so they can be granted to non-superuser roles as needed.

## Process Identification and the Race Window

Before sending any signal, `pg_signal_backend()` calls `BackendPidGetProc()` to look up the target PID in shared memory's `ProcArray`. This confirms the PID belongs to a genuine PostgreSQL backend (not an arbitrary OS process). It also retrieves the role that owns it, for the permission check.

A narrow race exists: after the lookup but before `kill()` executes, the target could exit. Its PID could then be reassigned by the OS. The code acknowledges this and deliberately accepts it. On Linux and most Unix systems, the OS assigns PIDs sequentially with a large wrap-around range, so the probability is negligible. More importantly, all callers of this mechanism intend to end the process anyway, so inadvertently missing an already-dead process is not harmful.

PostgreSQL intentionally excludes auxiliary processes (the WAL writer, background writer, checkpointer, etc.): `BackendPidGetProc()` returns NULL for them. This causes `pg_signal_backend()` to emit a warning and return an error. You cannot signal those processes via SQL.

On systems with `setsid()` support, PostgreSQL actually sends the signal to the backend's entire process group (`kill(-pid, sig)`), not just the single backend process. This ensures any helper child processes spawned by that backend also receive the signal.

## Configuration Reload

`pg_reload_conf()` does not directly reload `postgresql.conf`. It sends **SIGHUP** to the postmaster via `kill(PostmasterPid, SIGHUP)`. The postmaster responds to SIGHUP by re-reading its own configuration and then forwarding SIGHUP to all its child processes. Each child independently re-reads the configuration files on receipt.

This two-level fan-out means that a configuration change applied via `pg_reload_conf()` propagates to all existing backends. Only parameters marked as `sighup`-level in the catalog actually take effect without a restart. Parameters requiring a restart (like `shared_buffers` or `max_connections`) remain at their old values until the server is restarted; they appear in `pg_settings` with `pending_restart = true`.

Each backend can change session-level parameters (like `work_mem`) independently — `pg_reload_conf()` only updates the server-level default that new sessions inherit.

## Log File Rotation

`pg_rotate_logfile()` (the current v2 version exposed in core) works through a different channel than the backend-signal path. Rather than sending a Unix signal directly to the syslogger process, it calls `SendPostmasterSignal(PMSIGNAL_ROTATE_LOGFILE)`. This writes a flag into a shared-memory bitmap. The postmaster reads that bitmap on its next SIGUSR1 delivery. It finds `PMSIGNAL_ROTATE_LOGFILE` set and sends SIGUSR1 to the syslogger. The syslogger closes its current log file and opens a new one.

This indirection through the postmaster is necessary because the syslogger's PID is not published to backends. The shared-memory signal flags in `PMSignalData` serve as a reliable postmaster-only rendezvous for requests that backends cannot deliver directly.

The function returns `false` with a warning if the `logging_collector` GUC is not enabled — rotation only applies to the collector-managed log files, not to stderr or syslog destinations.

## Memory Context Diagnostics

`pg_log_backend_memory_contexts(pid)` is the odd one out in this family. It does not use `pg_signal_backend()` and does not send SIGTERM or SIGINT. Instead it calls `SendProcSignal(pid, PROCSIG_LOG_MEMORY_CONTEXT, backendId)`, which uses the [[subsystems/storage/procsignal|process signal mechanism]] — a dedicated shared-memory channel for in-process notification.

On the receiving end, the backend's SIGUSR1 handler sets a flag. The next call to `CHECK_FOR_INTERRUPTS()` in the backend's execution path sees the flag and logs a full dump of all [[subsystems/memory/contexts|memory context]] statistics to the server log. The dump includes every context's name, total allocated bytes, and used bytes, making it possible to attribute memory consumption to specific subsystems.

Generating and logging the full context tree is a non-trivial operation that could flood the log. Only superusers can call this function by default. Administrators must explicitly grant additional roles via `GRANT EXECUTE`. Unlike `pg_cancel_backend`, it can also target auxiliary processes such as the WAL sender — PostgreSQL tries `AuxiliaryPidGetProc()` as a fallback when `BackendPidGetProc()` returns NULL.

## Practical Patterns

**Draining a connection pool slot**: when a pool member hangs and must be replaced, `pg_terminate_backend(pid, 5000)` (with a 5-second timeout) provides a synchronous guarantee that the slot is free before opening the new connection. Using the fire-and-forget form without a timeout can lead to a brief window where both the old and new connection exist for the same pool slot.

**Handling `max_connections` exhaustion**: identify the oldest idle connections with a query against `pg_stat_activity` (filtering `state = 'idle'` and ordering by `state_change`), then terminate them in a loop. The loop form is safe because `pg_terminate_backend()` emits only a warning (not an error) when a PID has already vanished.

**Applying a configuration change without service interruption**: edit `postgresql.conf`, call `pg_reload_conf()`, then query `pg_settings WHERE pending_restart` to confirm that no restart-requiring parameters were changed unintentionally.

**Diagnosing a memory-leaking backend**: identify the PID via `pg_stat_activity` or `pg_stat_bgwriter`, call `pg_log_backend_memory_contexts(pid)`, then search the server log for that PID's context dump to locate which subsystem is retaining memory across transactions.

## Related Topics

- [[subsystems/storage/procsignal|Process signal mechanism]] — the SIGUSR1-based in-process notification channel used by `pg_log_backend_memory_contexts`
- [[subsystems/storage/latch-and-ipc|Latches and IPC]] — how `pg_terminate_backend`'s timeout loop waits efficiently
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]] — the primary way to find PIDs to target with these functions
- [[subsystems/memory/contexts|Memory contexts]] — what `pg_log_backend_memory_contexts` dumps to the log

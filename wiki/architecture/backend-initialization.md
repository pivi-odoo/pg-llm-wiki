---
title: "Backend Process Initialization"
aliases:
  - backend startup
  - process initialization
  - miscinit
source_files:
  - src/backend/utils/init/miscinit.c
symbols:
  - InitPostmasterChild
  - InitStandaloneProcess
  - InitProcessGlobals
  - InitializeSessionUserId
  - InitializeSessionUserIdStandalone
  - InitializeSystemUser
  - SwitchToSharedLatch
  - SwitchBackToLocalLatch
  - ProcessingMode
  - BackendType
  - SecurityRestrictionContext
  - CreateDataDirLockFile
  - ValidatePgVersion
  - process_session_preload_libraries
  - process_shared_preload_libraries
---

Every PostgreSQL backend process — whether it is a client-facing query backend, an [[subsystems/background/autovacuum|autovacuum]] worker, or an auxiliary process like the checkpointer — must complete a common initialization sequence before it can interact with shared memory, the lock manager, or catalogs. This initialization establishes process-local state that cannot be inherited cleanly from the postmaster: per-process randomness seeds, latch infrastructure, signal masks, and the process's identity within the cluster. The code in `miscinit.c` is the central home for this cross-cutting setup. It covers everything from data-directory validation and lock-file management to the layered user-identity model, which governs every permission check in the system.

## ProcessingMode and the initialization state machine

Every process begins life in `InitProcessing` mode. This global flag (`Mode`, miscinit.c) gates certain behaviors that are only safe once the system is fully initialised. For example, some catalog access paths check `IsNormalProcessingMode()` before taking shortcuts. Those shortcuts assume a consistent catalog state. Three modes exist:

| Mode | When active |
|---|---|
| `BootstrapProcessing` | During `initdb`, when system catalogs are being created from scratch and all transactions commit unconditionally |
| `InitProcessing` | Default at process start; used while connecting, authenticating, and loading initial GUCs |
| `NormalProcessing` | Set by `PostgresMain()` once the backend is ready to serve queries |

The transition from `InitProcessing` to `NormalProcessing` is the point at which the backend formally declares itself ready. Code that checks for `IsInitProcessingMode()` can safely relax certain invariants. No client queries are in progress yet, so the relaxation is safe.

## Common process startup: InitPostmasterChild and InitProcessGlobals

All postmaster child processes call `InitPostmasterChild()` (miscinit.c) as their first act after the fork. This function sets `IsUnderPostmaster = true` and establishes a stack-depth reference with `set_stack_base()`. It then calls `InitProcessGlobals()` and resets the proc_exit handler list. This reset ensures the child does not inherit the postmaster's cleanup callbacks. Finally, it configures signals.

`InitProcessGlobals()` (postmaster.c) captures `MyStartTimestamp` and seeds the process-local PRNG. It uses strong entropy when available. Otherwise, it falls back to a PID-XOR-timestamp combination. The seed is intentionally per-process. If all backends shared a seed, an attacker observing outputs from one backend could predict outputs from another.

Signal handling deserves attention. `InitPostmasterChild()` unblocks only `SIGQUIT` from the signal mask and installs `SignalHandlerForCrashExit` as its handler. Every postmaster child must respond to `SIGQUIT` at all times, because `SIGQUIT` is the postmaster's mechanism for forcing an immediate exit during cluster crash recovery. All other signals remain blocked at this point. Each process type installs its own handlers for the signals it cares about later in startup. The child also calls `setsid()` to become a process group leader. This lets the postmaster deliver signals to the entire group if needed.

Standalone processes (the `postgres` binary invoked directly, not via a postmaster) take a slightly different path through `InitStandaloneProcess()` (miscinit.c). Because there is no postmaster to inherit from, it must locate the executable path itself. It also does not call `setsid()` or configure the postmaster-death pipe. `InitStandaloneProcess()` sets `MyBackendType` to `B_STANDALONE_BACKEND` immediately.

## BackendType: process identity

`MyBackendType` (miscinit.c) identifies what kind of process this is. The `BackendType` enum covers all process types the postmaster can spawn:

| BackendType | Process |
|---|---|
| `B_BACKEND` | Regular client-facing backend |
| `B_AUTOVAC_LAUNCHER` | Autovacuum launcher |
| `B_AUTOVAC_WORKER` | Autovacuum worker |
| `B_BG_WORKER` | Registered background worker |
| `B_BG_WRITER` | Background writer |
| `B_CHECKPOINTER` | Checkpointer |
| `B_WAL_WRITER` | WAL writer |
| `B_WAL_SENDER` | WAL sender (streaming replication) |
| `B_WAL_RECEIVER` | WAL receiver (streaming replication) |
| `B_STARTUP` | Startup process (WAL recovery) |
| `B_ARCHIVER` | WAL archiver |
| `B_LOGGER` | Syslogger |
| `B_STANDALONE_BACKEND` | Direct-invocation postgres process |

`GetBackendTypeDesc()` (miscinit.c) converts these values to human-readable strings used in log messages and `pg_stat_activity.backend_type`. The macro `AmRegularBackendProcess()` checks `MyBackendType == B_BACKEND`. Some code paths use this macro to distinguish client backends from system processes. For example, connection-limit enforcement in `InitializeSessionUserId()` only applies to regular backends.

## The latch transition

During early startup, before the backend registers in the procarray, it cannot use the shared latch embedded in its `PGPROC` slot. That slot does not yet exist. Instead, the backend uses a process-local latch allocated as a static variable (`LocalLatchData`, miscinit.c), initialised by `InitProcessLocalLatch()`.

Once the backend has acquired a `PGPROC` slot (via `InitProcess()`), it calls `SwitchToSharedLatch()` (miscinit.c). This function redirects `MyLatch` from `&LocalLatchData` to `&MyProc->procLatch`. This matters because other processes signal `MyProc->procLatch`. For example, the postmaster might deliver a wakeup, or a lock manager might signal that a lock has been acquired. If `MyLatch` still pointed at the local latch, those signals would be lost.

`SwitchBackToLocalLatch()` performs the reverse. Cleanup paths call it after the process releases `MyProc` back to the free list. This ensures that any subsequent latch waits, during exit processing, use the local latch rather than a slot that may have been reassigned to another process.

## User identity layers

`miscinit.c` maintains a user-identity model that is more layered than it initially appears. It tracks four distinct OIDs:

| Variable | Meaning | When it changes |
|---|---|---|
| `AuthenticatedUserId` | The role proven by authentication | Set once at connection start; never changes |
| `SessionUserId` | The role for `SESSION_USER`; can differ from authenticated if `SET SESSION AUTHORIZATION` was used | Changed by `SET SESSION AUTHORIZATION` (superuser only) |
| `OuterUserId` | The current role at the outer transaction level; reflects `SET ROLE` | Changed by `SET ROLE` |
| `CurrentUserId` | The effective role used for all permission checks | Changed transiently by `SECURITY DEFINER` functions and security-restricted operations |

Authentication calls `SetAuthenticatedUserId()` (miscinit.c) exactly once per session. It also writes the role OID into `MyProc->roleId` (no lock needed, since it is an atomic store and the slot has not been published to other processes yet).

`InitializeSessionUserId()` (miscinit.c) performs the catalog lookup that resolves the authenticated role into a full `pg_authid` row. It first flushes the syscache with `AcceptInvalidationMessages()`, to catch roles created on the fly during authentication. Then it checks `rolcanlogin` and `rolconnlimit`. Parallel workers skip this function entirely. They inherit their identity from the leader instead, and `ParallelWorkerMain()` validates that identity.

Autovacuum workers and background workers that do not have an associated role call `InitializeSessionUserIdStandalone()` (miscinit.c) instead. This function unconditionally sets the identity to `BOOTSTRAP_SUPERUSERID`. This is intentional: these processes need superuser-level access to run system maintenance. They also do not go through the normal authentication path.

## SecurityRestrictionContext

Alongside `CurrentUserId`, the integer `SecurityRestrictionContext` records why the current user ID may differ from the outer-level ID. `miscadmin.h` defines three flags:

- `SECURITY_LOCAL_USERID_CHANGE` (0x0001): A temporary local change is active. While this flag is set, the backend disallows `SET ROLE`. Allowing it would leave GUC state and `CurrentUserId` out of sync.
- `SECURITY_RESTRICTED_OPERATION` (0x0002): Operations such as autovacuum and `REINDEX` set this flag when they enumerate relations and invoke associated functions as the relation owner. While set, it prevents `SET ROLE`, replacement of prepared statements, and other session-state changes. Without this protection, a malicious function could weaponise those changes to affect the calling session after the operation completes.
- `SECURITY_NOFORCE_RLS` (0x0004): This flag tells the row-security machinery to ignore `FORCE ROW LEVEL SECURITY` table settings. The backend uses it during referential integrity checks. Those checks always run as the table owner, so forced RLS must not block them.

`GetUserIdAndSecContext()` and `SetUserIdAndSecContext()` are safe to call even when `CurrentUserId` is not yet valid. `StartTransaction()` and `AbortTransaction()` use them to save and restore state around the backend's very first transaction, before `InitializeSessionUserId()` has run.

## Data directory lock file and version validation

`checkDataDir()` (miscinit.c) validates that the data directory exists, is owned by the current UID, and has permissions of either `0700` or `0750`. The ownership check is part of the interlock that prevents two postmaster processes from sharing a data directory. A postmaster can only create the lock file if it already owns the directory. If group execute is present on the data directory, `SetDataDirectoryCreatePerm()` propagates that mode to the umask for all newly created files.

`ValidatePgVersion()` (miscinit.c) checks that `PG_VERSION` in the data directory matches the major version of the running binary. A mismatch at this stage is always fatal — there is no in-place upgrade path during a running server.

`CreateDataDirLockFile()` (miscinit.c) creates `postmaster.pid`, recording the postmaster PID, data directory path, start time, and port number. It creates the lock file atomically with `O_EXCL`. It checks for stale ownership by signalling the recorded PID. It syncs the file to disk before returning. This prevents a crash immediately after creation from leaving a corrupt file that would block a subsequent restart. `CreateLockFile()` registers an `on_proc_exit` callback that removes the file on normal exit.

## Library preloading

The postmaster calls `process_shared_preload_libraries()` (miscinit.c) before it forks any children. It loads each library listed in `shared_preload_libraries` with `load_file()`. The library's `_PG_init` hook then runs in the postmaster's address space. Because the postmaster later forks all backends, they inherit the shared library mappings automatically on Unix, without needing to reload them. Libraries that need shared memory must call `RequestAddinShmemSpace()` during this hook. This call must happen before `CreateSharedMemoryAndSemaphores()` runs.

The backend calls `process_session_preload_libraries()` (miscinit.c) once, after it has fully initialised. It loads libraries from `session_preload_libraries` (no path restriction) and `local_preload_libraries` (restricted to `$libdir/plugins/`, callable by unprivileged users). The flag `process_shared_preload_libraries_in_progress` prevents certain operations, notably creating background workers, from running at the wrong phase.

## Related Topics

- [[architecture/startup-sequence|Startup Sequence]] — the postmaster's role in launching processes and managing the cluster lifecycle
- [[architecture/process-architecture|Process Architecture]] — the fork-on-connect model, shared memory layout, and auxiliary process roles
- [[subsystems/memory/contexts|memory context]] — TopMemoryContext used to store `DatabasePath` and `SystemUser`
- [[subsystems/transactions/mvcc|MVCC]] — the procarray that `AuthenticatedUserId` is written into via `MyProc->roleId`

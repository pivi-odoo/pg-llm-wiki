---
title: "Process Architecture"
aliases:
  - "PostgreSQL Process Model"
  - "Postmaster"
  - "Backend Processes"
source_files:
  - src/backend/postmaster/postmaster.c
  - src/backend/storage/ipc/shmem.c
  - src/backend/storage/ipc/ipci.c
  - src/include/storage/shmem.h
symbols:
  - PostmasterMain
  - ServerLoop
  - BackendStartup
  - HandleChildCrash
  - CreateSharedMemoryAndSemaphores
  - InitShmemAllocation
  - ShmemInitStruct
  - PMState
  - bkend
  - PGPROC
---

# Process Architecture

PostgreSQL is built entirely around OS processes rather than threads. The thread-based design that most modern servers adopt trades isolation for efficiency. Threads share an address space. This makes communication cheap. It also makes crashes expensive: a single misbehaving thread can corrupt heap memory or clobber global state in ways that affect all threads. PostgreSQL rejects that trade-off: by giving each client connection its own OS process, the kernel's memory protection becomes the primary isolation mechanism. A backend that scribbles past the end of a buffer, dereferences a null pointer, or calls an unsafe third-party library will at worst corrupt its own private memory. The other backends are unaffected. Portability is a secondary benefit. POSIX `fork()` semantics are universal across every platform PostgreSQL targets. Threading libraries vary in subtle ways, particularly around signal handling and async-signal safety. The process model also makes the memory model simple. Private data lives in private address space. Shared data lives in a single explicit shared-memory segment. The boundary between the two is always clear. This choice is foundational — it shapes how connections are established, how memory is shared, how crashes are contained, and how auxiliary work like vacuuming and checkpointing is structured.

## The shared-memory segment

Before forking any child, the postmaster allocates a single contiguous shared-memory segment large enough to hold all inter-process data structures for the lifetime of the cluster. This happens in `CreateSharedMemoryAndSemaphores()` (ipci.c), which calculates the required size and creates the segment via `PGSharedMemoryCreate()`. It then passes a pointer to `InitShmemAccess()`, which sets `ShmemBase` and `ShmemEnd`. All subsequent allocations from that segment go through `ShmemAlloc()` (shmem.c), which advances a bump-pointer protected by `ShmemLock`.

The postmaster never frees or reallocates the segment dynamically. Shared memory, once allocated, can never be freed. The allocator never compacts or recycles space outside of hash tables. Every major subsystem reserves space at startup through `ShmemInitStruct()` or `ShmemInitHash()` (shmem.c). These functions look up a named entry in the shmem index, a small hash table (`ShmemIndex`) that maps string names to locations inside the segment. When a forked backend initialises, it finds its way to each structure by name rather than by re-doing arithmetic. This is why the fork model works without the `EXEC_BACKEND` indirection needed on Windows.

Key regions within the shared segment:

| Region | Purpose |
|---|---|
| Buffer pool (`BufferDescriptors`, `BufferBlocks`) | Fixed-size cache of heap and index pages; size controlled by `shared_buffers` |
| WAL buffers (`XLogCtl`) | Ring buffer for WAL records before they are flushed to disk |
| [[subsystems/storage/clog|CLOG]] and related buffers | Commit status bits for transaction IDs |
| Lock table (`LockMethodLockHash`, `LockMethodProcLockHash`) | Per-lock and per-process-lock records for the heavyweight lock manager |
| Procarray (`PGPROC[]`) | One slot per backend tracking XID, snapshot info, and wait state |
| [[subsystems/locking/lwlocks|LWLock]] array | Lightweight spinlock/sleep primitives for protecting all the above |
| PMSignal state | Flags used to communicate between backends and the postmaster |

`shared_buffers` dominates the size of the segment. On most production systems it is the single largest allocation. Everything else — WAL buffers, CLOG, lock tables, the procarray — is a rounding error by comparison.

## The postmaster

The postmaster (`PostmasterMain()`, postmaster.c) is the root of the PostgreSQL process tree. It starts by calling `CreateSharedMemoryAndSemaphores()`, setting up signals, listening on one or more TCP/Unix sockets, and then entering `ServerLoop()` — an event loop that waits for connection attempts and signals.

The postmaster deliberately avoids entering shared memory for routine work. It is not registered in the `PGPROC` array and cannot participate in lock-manager operations. This is by design: a postmaster that held locks or manipulated shared state would be vulnerable to hangs. A crashing backend that left a spinlock spinning could cause exactly that kind of hang. Keeping the postmaster clean means it can always reap children, log their deaths, and initiate recovery regardless of what state shared memory is in.

The postmaster tracks its own state in a `PMState` enum:

| State | Meaning |
|---|---|
| `PM_INIT` | Postmaster starting, not yet ready |
| `PM_STARTUP` | Startup process running, waiting for WAL recovery |
| `PM_RECOVERY` | Archive recovery in progress |
| `PM_HOT_STANDBY` | Standby accepting read-only connections |
| `PM_RUN` | Normal operation; all backends welcome |
| `PM_STOP_BACKENDS` | Shutdown or crash; new connections rejected |
| `PM_WAIT_BACKENDS` | Draining live backends |
| `PM_SHUTDOWN` / `PM_SHUTDOWN_2` | Waiting for checkpointer, archiver, WAL senders |
| `PM_WAIT_DEAD_END` | Waiting for "dead-end" children to exit |
| `PM_NO_CHILDREN` | All children gone; ready to exit or restart |

The postmaster only launches backends in `PM_RUN` or `PM_HOT_STANDBY`. In all other states, the postmaster still forks a child for each connection attempt. That child is a "dead-end" process whose only job is to send the client an error message and exit. The postmaster tracks it in `BackendList` because it holds a reference to shared memory. It must be drained before the segment can be destroyed.

## Connection lifecycle

When a client connects, the postmaster accepts the file descriptor in `ServerLoop()`. It immediately forks a child backend before authentication runs. This way, a slow or misbehaving client can never stall the accept loop. See [[architecture/startup-sequence|Startup Sequence]] for the full fork-then-authenticate sequence and how it differs on `EXEC_BACKEND` platforms. The postmaster records the child's PID in its `BackendList` (`bkend` struct, postmaster.c), where each entry carries the PID, a cancel key, the backend type, and a flag indicating whether the child is a dead-end process.

The cancel key deserves mention: when a client sends a cancel request, it opens a new connection to the postmaster and supplies the key. The postmaster looks up the matching entry in `BackendList` and sends `SIGINT` to that backend. Backends handle `SIGINT` as a query-cancel request, not a shutdown.

## Auxiliary processes

Several recurring cluster-level tasks run as dedicated child processes rather than within client backends. The postmaster starts them on demand and restarts them if they exit unexpectedly. The postmaster launches each through `StartChildProcess()` (postmaster.c) with an `AuxProcType` tag:

**Checkpointer** (`CheckpointerProcess`): Owns the checkpoint lifecycle — writing all dirty shared buffers to disk at regular intervals and on explicit `CHECKPOINT` commands. It also manages the background flush of recently dirtied pages (the `bgwriter_lru_*` behaviour was folded into the checkpointer in later versions). The checkpointer runs the shutdown checkpoint before the cluster exits.

**Background writer** (`BgWriterProcess`): Scans the buffer pool for dirty pages and flushes them proactively. This reduces the burst of I/O that would otherwise hit at checkpoint time. Unlike the checkpointer, it does not advance the checkpoint LSN. It only reduces pressure on the buffer pool.

**WAL writer** (`WalWriterProcess`): Flushes WAL buffers to disk on a short timer. Without this process, WAL would only be written when a transaction commits or when the buffer fills. The WAL writer reduces commit latency for backends that are not in synchronous-commit mode.

**WAL sender** (`BACKEND_TYPE_WALSND`): One per standby replica. Streams WAL records to a physical or logical replication subscriber. The postmaster launches WAL senders like backends. They start in the `BackendList` as `BACKEND_TYPE_NORMAL`. The postmaster relabels them to `BACKEND_TYPE_WALSND` after it notices their `PMChildFlags` entry change.

**[[subsystems/background/autovacuum|Autovacuum]] launcher** (`AutoVacPID`): Monitors the age of tables and the bloat accumulated since the last vacuum, then launches autovacuum worker processes as needed. The launcher itself is lightweight. Worker backends launched with `StartAutovacuumWorker()` do the actual table-scanning work.

**Archiver** (`PgArchPID`): Copies completed WAL segment files to the archive location. It runs only when `archive_mode` is enabled. During archive recovery, the postmaster starts the archiver as soon as `pmState` reaches `PM_RECOVERY`. This happens if `archive_mode = always` is set.

**Syslogger** (`SysLoggerPID`): Captures stderr output from all other processes and writes it to log files when `logging_collector` is enabled.

These processes use the same shared-memory segment as backends — they hold `PGPROC` slots and participate in locking. However, they are not visible to `pg_stat_activity` in the same way as client backends.

## Signal-based coordination

The postmaster and its children coordinate through Unix signals. The postmaster installs handlers for several signals in `PostmasterMain()`:

| Signal | Sender | Meaning |
|---|---|---|
| `SIGTERM` | `pg_ctl stop -m smart` | Smart shutdown: refuse new connections, wait for existing ones to finish |
| `SIGINT` | `pg_ctl stop -m fast` | Fast shutdown: ask all backends to exit, then shut down |
| `SIGQUIT` | `pg_ctl stop -m immediate` | Immediate shutdown: send `SIGQUIT` to all children without waiting |
| `SIGHUP` | `pg_ctl reload` | Reload configuration files (`postgresql.conf`) |
| `SIGCHLD` | Kernel | A child process has exited; reap it |
| `SIGUSR1` | Children (via PMSignal) | Child-to-postmaster notification (used for checkpoint completion, WAL receiver status, etc.) |

Backends handle their own signal set differently. `SIGTERM` tells a backend to perform a clean shutdown of its current transaction and exit. `SIGQUIT` (sent as "immediate" shutdown) tells the backend to exit immediately without cleanup. It calls `quickdie()`, which clears shared memory state as best it can and exits. `SIGHUP` causes a backend to reload its configuration on the next opportunity. `SIGINT` cancels the current query.

The three-way distinction between `SIGTERM`, `SIGINT` (to the postmaster), and `SIGQUIT` (to the postmaster) maps to PostgreSQL's three shutdown modes: smart, fast, and immediate. Smart shutdown (`Shutdown = SmartShutdown`) lets existing sessions finish naturally. Fast shutdown (`Shutdown = FastShutdown`) interrupts sessions. Immediate shutdown (`Shutdown = ImmediateShutdown`) skips cleanup entirely and relies on crash recovery at next startup.

## What happens when a backend crashes

When any child exits abnormally — a segfault, an unhandled signal, or an exit with a non-zero status — the kernel delivers `SIGCHLD` to the postmaster. The postmaster's `SIGCHLD` handler sets a flag. The main loop then calls `process_pm_child_exit()`, which calls `waitpid()` to collect the exit status. Depending on whether the exit was normal, it then dispatches to `CleanupBackend()` or `HandleChildCrash()`.

`HandleChildCrash()` (postmaster.c) applies conservative logic: if this is the first crash and the cluster is not already in immediate shutdown, it sends `SIGQUIT` to all remaining backends, background workers, and auxiliary processes. The reasoning is that the crashed backend may have corrupted shared memory — it might have held a spinlock, left a page buffer in an inconsistent state, or partially written a shared data structure. Sending `SIGQUIT` to surviving backends is not punishment; it is the correct response to potentially corrupted shared state.

After all children have exited, the postmaster resets shared memory — reallocating and reinitialising the segment — and then restarts the auxiliary processes and begins accepting connections again. `restart_after_crash` (default `true`) controls this full restart cycle. If `restart_after_crash = off`, the postmaster logs a message and shuts down instead.

The `FatalError` boolean tracks whether the cluster is mid-crash-recovery. While it is set, the `PMState` machine will not advance to `PM_RUN`. The machine advances again only once the startup process has successfully completed WAL recovery and exited cleanly.

## See Also

- [[architecture/overview]] — high-level picture of how all these processes relate to queries
- [[subsystems/storage/buffer-manager]] — the buffer pool that dominates the shared segment
- [[subsystems/wal/overview]] — what the WAL writer and WAL sender are flushing
- [[subsystems/transactions/mvcc]] — how the procarray is used for snapshot generation
- [[subsystems/locking/overview]] — the lock table structures inside shared memory
- [[code-paths/vacuum]] — how autovacuum workers are launched and what they do

## Related Topics

- [[architecture/shared-memory|Shared Memory Layout]] — detailed breakdown of the shared-memory segment regions and their sizing
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the buffer pool that consumes the largest share of shared memory
- [[subsystems/wal/checkpoint|Checkpoint]] — what the checkpointer process does during normal and shutdown checkpoints
- [[subsystems/background/autovacuum|Autovacuum]] — how the autovacuum launcher and its workers are scheduled and managed
- [[subsystems/locking/lwlocks|LWLocks]] — the lightweight locks that protect shared data structures accessed across backends
- [[subsystems/transactions/snapshot|Snapshots]] — how the procarray is consulted to build MVCC snapshots for each query

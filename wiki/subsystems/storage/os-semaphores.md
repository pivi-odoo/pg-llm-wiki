---
title: "OS-Level Semaphores and Atomics"
aliases:
  - PGSemaphore
  - PGSemaphoreLock
  - PGSemaphoreUnlock
  - posix semaphore
  - sysv semaphore
  - win32 semaphore
  - pg atomics
tags:
  - theme/concurrency-control
source_files:
  - src/backend/port/atomics.c
  - src/backend/port/posix_sema.c
  - src/backend/port/sysv_sema.c
  - src/backend/port/win32/crashdump.c
  - src/backend/port/win32/signal.c
  - src/backend/port/win32/socket.c
  - src/backend/port/win32/timer.c
  - src/backend/port/win32_sema.c
  - src/backend/port/win32_shmem.c
symbols:
  - PGSemaphoreLock
  - PGSemaphoreUnlock
  - PGSemaphoreReset
  - PGSemaphoreTryLock
  - PGSemaphoreCreate
  - PGReserveSemaphores
  - PGSemaphoreShmemSize
  - PGSemaphoreData
  - pg_atomic_flag
  - pg_atomic_uint32
  - pg_atomic_uint64
  - pg_atomic_compare_exchange_u32_impl
  - pg_atomic_fetch_add_u32_impl
  - pg_spinlock_barrier
  - pgwin32_signal_event
  - pgwin32_signal_initialize
---

PostgreSQL must put backends to sleep when they contend for a resource — a buffer pin, a heavyweight lock, an LWLock. The OS must be the agent that suspends and wakes them. The engine abstracts this behind a thin `PGSemaphore` interface (`src/include/storage/pg_sema.h`) so the rest of the codebase is insulated from the three different OS primitives it may use at runtime. PostgreSQL selects the implementation at build time: POSIX unnamed semaphores on Linux and macOS, System V semaphores as a fallback on older UNIX systems, and Win32 semaphore objects on Windows.

## The PGSemaphore abstraction

`PGSemaphoreLock()`, `PGSemaphoreUnlock()`, and `PGSemaphoreReset()` are the only entry points that higher-level code uses. [[subsystems/locking/lwlocks|LWLocks]] call `PGSemaphoreLock()` when a backend must block waiting for a lock. The lock machinery adds the waiter to the lock's wait queue, and the waiter then parks on its per-process semaphore. The waking backend calls `PGSemaphoreUnlock()` on the waiter's semaphore to resume it. Heavyweight locks follow the same pattern through `ProcSleep()` and `ProcWakeup()` in `proc.c`.

Each PGPROC has exactly one `PGSemaphore`. `PGSemaphoreCreate()` initialises the semaphore to count 1, so that the first `PGSemaphoreLock()` call succeeds immediately (the backend is not yet blocked). Before sleeping, the backend calls `PGSemaphoreReset()` to drive the count to zero, so the subsequent `PGSemaphoreLock()` will block until another backend increments it.

All three backends share the same interface contract: a semaphore is a binary counter clamped between 0 and some small maximum. `PGSemaphoreLock()` decrements and blocks if the result would go negative; `PGSemaphoreUnlock()` increments; `PGSemaphoreReset()` drains the counter to zero. Both `PGSemaphoreLock()` and `PGSemaphoreUnlock()` retry on `EINTR` so that signal delivery does not abort a wait mid-stream.

## POSIX semaphore backend

The preferred backend on Linux and macOS uses unnamed `sem_t` objects created with `sem_init()` (`src/backend/port/posix_sema.c`). At startup, `PGReserveSemaphores()` calls `ShmemAllocUnlocked()` to carve out a contiguous array of `PGSemaphoreData` structs — each a cache-line-padded `sem_t` — from the main [[subsystems/storage/shared-memory|shared memory]] segment. Because `PGReserveSemaphores()` uses `ShmemAllocUnlocked()` rather than the spinlock-protected `ShmemAlloc()`, this must happen before the spinlock itself is ready; spinlock emulation on some platforms internally uses semaphores, making the ordering circular otherwise.

The result is that all backends share the same physical `sem_t` objects at the same virtual addresses, because they all map the same shared memory. `PGSemaphoreLock()` calls `sem_wait()` directly on the array element. `PGSemaphoreUnlock()` calls `sem_post()` directly on the array element.

PostgreSQL compiles the alternative code path — named semaphores via `sem_open()` — when the OS lacks `sem_init()`. It creates named semaphores with paths like `/pgsql-<N>` and immediately unlinks them, so that they cannot be accessed by name after creation. Their `sem_t` objects live in the postmaster's private memory rather than shared memory, which means `fork()`ed children inherit them but `exec()`ed backends cannot access them. For this reason, the build system enforces that named POSIX semaphores and `EXEC_BACKEND` are mutually exclusive (`#error` at compile time, `posix_sema.c`).

`posix_sema.c` registers cleanup via `on_shmem_exit(ReleaseSemaphores, 0)`. For unnamed semaphores the cleanup destroys each `sem_t` with `sem_destroy()`; for named semaphores it calls `sem_close()`.

## System V semaphore backend

On platforms where POSIX semaphores are unavailable, PostgreSQL falls back to System V IPC semaphores (`src/backend/port/sysv_sema.c`). The SysV API allocates semaphores in sets. `semget()` creates a set of N semaphores under a single integer key, and an index addresses each semaphore within the set. Because the kernel enforces a per-set maximum (`SEMMSL`, often 25 or less), PostgreSQL cannot put all its semaphores in one set.

`PGReserveSemaphores()` computes the number of sets needed as `ceil(maxSemas / SEMAS_PER_SET)` where `SEMAS_PER_SET = 19`. PostgreSQL chose that constant conservatively, to stay within the tight defaults on NetBSD and OpenBSD (default `SEMMNS` of 60 allows three sets of 19 useful semaphores). `IpcSemaphoreCreate()` allocates each set on demand. It iterates through keys derived from the data directory's inode number until it finds a free one. This automatically recycles sets left over from crashed postmasters, by checking the creator PID with `semctl(..., GETPID, ...)`.

Each SysV `PGSemaphoreData` stores a `(semId, semNum)` pair — the set ID and the index within it. `PGSemaphoreLock()` issues `semop()` with `sem_op = -1` to decrement and block; `PGSemaphoreUnlock()` issues `semop()` with `sem_op = +1` to increment. `sysv_sema.c` also registers the `IPC_RMID` cleanup that destroys all allocated sets with `on_shmem_exit()`. The postmaster keeps its own private array of set IDs for this purpose, to avoid depending on shared memory contents during a potentially corrupted shutdown.

A deliberately placed extra semaphore at index `numSems` within each set carries a "magic" value (`PGSemaMagic = 537`). During startup key scanning, PostgreSQL may find an existing set whose extra semaphore already has this value. In that case, PostgreSQL knows the set belongs to a Postgres instance, and it can safely reclaim the set if the creator process is dead.

## Win32 semaphore backend

Windows provides its own semaphore object type through `CreateSemaphore()`. PostgreSQL uses it directly in `src/backend/port/win32_sema.c`. Win32 semaphores are anonymous kernel objects identified by `HANDLE`; they require no shared memory, since child processes can inherit handles. `PGReserveSemaphores()` allocates a private array of `HANDLE` values. `PGSemaphoreCreate()` calls `CreateSemaphore()` with `bInheritHandle = TRUE` so that the handle is valid in child processes.

`PGSemaphoreLock()` on Windows is the most complex variant. Because Windows has no POSIX signal mechanism, a blocking wait must also remain interruptible. The function waits on two handles simultaneously: the semaphore and `pgwin32_signal_event`, a dedicated Windows event used to wake the backend when a signal arrives (`win32/signal.c`). `WaitForMultipleObjectsEx()` with `INFINITE` timeout returns either when the semaphore is acquired (`WAIT_OBJECT_0 + 1`) or when a signal event fires (`WAIT_OBJECT_0`); in the latter case, `pgwin32_dispatch_queued_signals()` runs the pending signal handlers and the wait loop continues.

The Win32 compatibility layer extends beyond semaphores. Windows lacks `fork()`, so backends start via `exec()` and re-attach to the existing shared memory segment (identified by a name derived from the data directory path, stored in the `Global\` namespace). `win32_shmem.c` implements this by calling `CreateFileMapping()` and `MapViewOfFileEx()` and mapping the segment at the same virtual address in every process. The signal emulation layer in `win32/signal.c` routes POSIX-style signal delivery through a background thread that posts to `pgwin32_signal_event`; `win32/socket.c` wraps Winsock to match the BSD socket API; `win32/timer.c` emulates `setitimer()` with a Windows timer thread. When a backend crashes, `win32/crashdump.c` installs an unhandled exception filter. The filter calls `MiniDumpWriteDump()` to write a `.dmp` file for post-mortem debugging.

## Atomic operations and their fallbacks

Hardware atomic operations — compare-and-swap, fetch-and-add, memory barriers — are the foundation for spinlocks, LWLock state words, and several internal counters. PostgreSQL exposes them through a type-generic API in `src/include/port/atomics.h` as inline functions. On x86/x86-64, ARM, and other modern architectures, the compiler directly emits the appropriate instruction (`LOCK CMPXCHG`, `LDXR/STXR`, etc.) via GCC/Clang `__atomic_*` or `__sync_*` builtins.

`src/backend/port/atomics.c` provides non-inline fallback implementations for platforms where the compiler cannot generate the native instructions. These fallbacks emulate each atomic operation under a spinlock embedded in the atomic variable's storage:

- When `PG_HAVE_ATOMIC_U32_SIMULATION` is defined, `pg_atomic_compare_exchange_u32_impl()` and `pg_atomic_fetch_add_u32_impl()` acquire a per-variable `slock_t`, perform the operation on the plain integer field, and release the spinlock. The same pattern applies to `uint64` variants under `PG_HAVE_ATOMIC_U64_SIMULATION`.
- When `PG_HAVE_MEMORY_BARRIER_EMULATION` is defined, `pg_spinlock_barrier()` emits a barrier by calling `kill(PostmasterPid, 0)`. This is a deliberate trick: even a no-op signal (`kill` with signal 0 checks existence without delivering a signal) forces a system call that the kernel implements with appropriate memory ordering. PostgreSQL never reaches this path on Windows, where `MemoryBarrier()` is always available.

The spinlock used inside these fallbacks must be a different semaphore set from the one used for per-process `PGSemaphore` objects. `atomics.c` calls `s_init_lock_sema(..., true)` on platforms without hardware spinlocks (the `true` selects the alternate set). This prevents a deadlock that could arise if a backend attempted an atomic operation while it already held the main semaphore lock.

## See also

- [[subsystems/locking/lwlocks|LWLocks and Spinlocks]]
- [[subsystems/storage/shared-memory|Shared Memory Layout]]
- [[subsystems/storage/latch-and-ipc|Latches and IPC]]
- [[subsystems/storage/ipc-primitives|IPC Primitives: Exit Callbacks and Shared Memory TOC]]
- [[architecture/process-architecture|Process Model]]

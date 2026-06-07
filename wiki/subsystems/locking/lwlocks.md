---
title: LWLocks and Spinlocks
aliases:
  - LWLock
  - spinlock
  - lightweight lock
tags:
  - theme/concurrency-control
source_files:
  - src/backend/storage/lmgr/lwlock.c
  - src/include/storage/lwlock.h
  - src/backend/storage/lmgr/s_lock.c
  - src/include/storage/s_lock.h
  - src/backend/storage/lmgr/lwlocknames.txt
symbols:
  - LWLock
  - LWLockAcquire
  - LWLockRelease
  - LWLockWakeup
  - LWLockQueueSelf
  - LWLockAttemptLock
  - slock_t
  - SpinLockAcquire
  - SpinLockRelease
  - TAS
  - S_LOCK
  - LWLockMode
  - LWLockWaitState
  - LWLockPadded
  - MainLWLockArray
  - NUM_BUFFER_PARTITIONS
---

# LWLocks and Spinlocks

PostgreSQL protects shared memory with two families of primitive locks that sit below the full lock manager: spinlocks for the shortest possible critical sections, and LWLocks for everything that requires sleeping or shared-mode access. Understanding both is essential for reading any code that touches shared data structures — buffer pool, WAL, transaction status, the process array, and many more.

## Spinlocks

A spinlock is a single integer in shared memory whose value is flipped between zero (free) and one (held) using an atomic CPU instruction. The acquire path issues a test-and-set (`TAS`) in a tight loop: if the exchange returns a non-zero old value, the lock was already held. The loop spins, emitting a `SPIN_DELAY()` hint (x86 `PAUSE`, ARM64 `ISB`) between retries to avoid pipeline penalties and reduce bus traffic. No kernel involvement, no sleeping.

The x86-64 implementation uses a single-byte `lock xchgb` instruction. ARM uses `__sync_lock_test_and_set`. On weakly-ordered architectures (ARM, PowerPC, SPARC) the unlock path must include an explicit memory fence (`lwsync`, `membar`, `stbar`) to ensure all stores inside the critical section are visible before the lock byte is cleared. The s_lock.h header contains all platform-specific implementations.

```c
/* Typical acquire/release pattern (via spin.h wrappers) */
SpinLockAcquire(&ShmemLock);
/* ... modify shared state ... */
SpinLockRelease(&ShmemLock);
```

`SpinDelayStatus` tracks spin count and escalating delay intervals. If a spinlock cannot be acquired within roughly one minute, `s_lock()` calls `abort()` — a sign of a catastrophic livelock rather than normal contention. Spinlocks have no queue, no deadlock detection, and no concept of "who holds the lock."

Spinlocks belong only in sections that complete in a handful of CPU cycles. The entire purpose of the design is to avoid a kernel call: even a single `futex` or semaphore operation costs far more than a few dozen spins. If a critical section might do I/O, call into the query executor, or block waiting for another lock, a spinlock is the wrong tool.

## LWLocks

LWLocks add two capabilities that spinlocks lack: shared (read-only) as well as exclusive mode, and the ability to sleep while waiting rather than burning CPU. A backend that cannot acquire an LWLock joins a wait queue and blocks on its process semaphore. The releasing backend is responsible for waking it. This layer provides no deadlock detection, so callers must avoid cycles themselves.

### Lock structure

```c
typedef struct LWLock
{
    uint16              tranche;   /* identifies the lock class */
    pg_atomic_uint32    state;     /* exclusive/shared counts + flags */
    proclist_head       waiters;   /* list of waiting PGPROCs */
} LWLock;
```

The `state` word encodes everything without a protecting spinlock. The upper bits carry three flags:

| Bit | Constant | Meaning |
|-----|----------|---------|
| 30 | `LW_FLAG_HAS_WAITERS` | the wait list is non-empty |
| 29 | `LW_FLAG_RELEASE_OK` | a releaser may wake waiters |
| 28 | `LW_FLAG_LOCKED` | the wait list itself is being modified |

Below those flags, bit 24 is `LW_VAL_EXCLUSIVE` (set when the lock is held exclusively) and bits 0–23 count concurrent shared holders. Because `MAX_BACKENDS` is capped at 2²³−1, the shared counter and the exclusive sentinel never collide.

`LWLockPadded`, a union, pads each `LWLock` to a full cache line, preventing false sharing between adjacent locks in the main array.

### Acquiring an LWLock

The fast path is a single atomic compare-and-exchange (`LWLockAttemptLock`). For a shared acquire, the CAS succeeds as long as the exclusive bit is clear; for exclusive, the entire lock-and-count field must be zero. If the CAS succeeds, the caller proceeds immediately.

When the fast path fails, the acquiring backend cannot simply join the wait queue and sleep. By the time it finishes queuing, the lock might already be free. The protocol is:

1. Queue self (`LWLockQueueSelf`): atomically set `LW_FLAG_HAS_WAITERS` and append `MyProc` to the `waiters` list.
2. Attempt the CAS once more. If it succeeds now, dequeue self (`LWLockDequeueSelf`) and proceed.
3. If the CAS still fails, the current holder is guaranteed to see the waiter entry before releasing. Block on `PGSemaphoreLock(MyProc->sem)`.
4. On wakeup, loop back to step 1 and retry — the wakeup signal only means the lock might be free, not that it is definitely free.

The two-phase attempt (queue then retry) is the key race-freedom property: once the backend is on the wait list, the releasing backend will not miss it.

### Waiting state machine

`PGPROC.lwWaiting` tracks a backend's position in the protocol:

| State | Value | Meaning |
|-------|-------|---------|
| `LW_WS_NOT_WAITING` | 0 | not blocked on any LWLock |
| `LW_WS_WAITING` | 1 | on the wait list, semaphore not yet posted |
| `LW_WS_PENDING_WAKEUP` | 2 | removed from list; semaphore post is imminent |

### Releasing an LWLock

`LWLockRelease` decrements the state atomically. If the result reaches zero (for exclusive) or the exclusive bit was the last holder (the arithmetic works out the same way), it calls `LWLockWakeup`. That function locks the wait list using the `LW_FLAG_LOCKED` spinlet embedded in `state`, walks the waiters, and moves eligible entries to a local wake list. It then atomically clears the list lock and updates `LW_FLAG_HAS_WAITERS` and `LW_FLAG_RELEASE_OK`. Finally it posts `PGSemaphoreUnlock` for each waiter it collected.

Shared waiters at the head of the queue can all be woken at once; the first exclusive waiter stops the scan. This is the only fairness guarantee: shared and exclusive requests are served in arrival order. This prevents exclusive starvation.

Any backend can release any LWLock — the lock does not track an owner for enforcement purposes. This is intentional and commonly used: WAL insertion acquires a lock in one code path and releases it in another.

### LWLock modes

```c
typedef enum LWLockMode
{
    LW_EXCLUSIVE,           /* no other holder permitted */
    LW_SHARED,              /* concurrent shared holders permitted */
    LW_WAIT_UNTIL_FREE      /* internal: wait for lock to reach zero holders */
} LWLockMode;
```

`LW_WAIT_UNTIL_FREE` is not a real acquisition mode. It places a backend at the front of the wait list so it can be notified when the lock becomes completely free. It does not actually acquire the lock itself. WAL machinery uses this to detect when all concurrent inserters have finished.

The API also provides `LWLockWaitForVar` / `LWLockUpdateVar`. These let a companion variable stored alongside the lock be watched atomically while holding no lock. This enables patterns where a reader needs to observe a value change rather than acquire the lock outright.

## The named LWLock array

All fixed LWLocks live in a single shared memory array, `MainLWLockArray`, allocated once by the postmaster. The array has three layers:

1. **Individual named locks** — a fixed set defined in `lwlocknames.txt`, each with a unique tranche ID corresponding to its position.
2. **Partitioned lock groups** — immediately following the individual locks, allocated in bulk.
3. **Named tranche locks** — registered before postmaster forks workers using `RequestNamedLWLockTranche`.

The individually named locks cover well-known shared data structures:

| Lock | Protects |
|------|----------|
| `ShmemIndexLock` | shared memory index hash table |
| `OidGenLock` | OID generator counter |
| `XidGenLock` | XID assignment |
| `ProcArrayLock` | `PGPROC` array and snapshot generation |
| `SInvalReadLock` / `SInvalWriteLock` | shared invalidation message queue |
| `WALBufMappingLock` | WAL buffer page mapping |
| `WALWriteLock` | WAL file writes |
| `ControlFileLock` | `pg_control` file |
| `XactSLRULock` | transaction status SLRU |
| `MultiXactGenLock` | MultiXact ID generation |
| `BtreeVacuumLock` | B-tree vacuum cycle IDs |
| `AutovacuumLock` | [[subsystems/background/autovacuum|autovacuum]] worker list |
| `SyncRepLock` | synchronous replication state |
| `ReplicationSlotAllocationLock` | replication slot lifecycle |
| `CommitTsLock` | commit timestamp data |
| `LogicalRepWorkerLock` | logical replication worker table |

This is not exhaustive; `lwlocknames.txt` lists 47 individual locks in PG 16.

## Lock partitioning

Some shared data structures are hot enough that a single LWLock would become a bottleneck. The solution is partitioning: divide the data structure into N independent shards and assign one lock per shard, allowing N concurrent operations.

The hash table for buffer mapping uses `NUM_BUFFER_PARTITIONS = 128` separate `BufferMapping` LWLocks. A lookup computes `hash_value % NUM_BUFFER_PARTITIONS` to choose the right partition lock before probing the hash table. Under heavy OLTP workloads this reduces contention by two orders of magnitude compared to a single lock.

The heavyweight lock manager's own shared hash tables use `NUM_LOCK_PARTITIONS = 16` LWLocks (`LockManager` tranche). The predicate lock tables use another 16 (`PredicateLockManager` tranche). Buffer content locks (`BufferContent` tranche) are one per buffer descriptor — thousands of them — serving as the per-buffer reader/writer lock that [[subsystems/storage/buffer-manager]] describes.

## How LWLocks differ from heavyweight locks

Heavyweight locks (the full lock manager in `lock.c`) sit above LWLocks and serve a completely different purpose:

| Dimension | Spinlock | LWLock | Heavyweight lock |
|-----------|----------|--------|-----------------|
| Modes | exclusive only | shared + exclusive | many (AccessShare through AccessExclusive + row-level) |
| Waiting | busy-spin | sleep on semaphore | sleep on semaphore |
| Deadlock detection | none | none | yes |
| Owner tracking | none | none | yes (per transaction) |
| MVCC interaction | none | none | yes |
| Held across transaction boundary | no | no | yes |
| Who can release | acquirer | any backend | acquirer's transaction |
| Primary use | microsecond critical sections | shared-memory data structure protection | user-visible table/row/advisory locking |

LWLocks protect heavyweight locks in turn: the `LockManager` partition locks guard access to the hash tables that back heavyweight locks, making them a good illustration of the layering. [[subsystems/locking/overview]] covers the full locking hierarchy.

## Extension LWLocks

Extensions that need their own LWLocks can request them at postmaster startup by calling `RequestNamedLWLockTranche(name, count)`. During backend startup they retrieve the pointer with `GetNamedLWLockTranche(name)`. For locks embedded in dynamic shared memory segments, extensions use the lower-level `LWLockNewTrancheId` / `LWLockRegisterTranche` / `LWLockInitialize` sequence instead. Tranche registrations are per-process rather than global, because DSM is not necessarily mapped at the same address in every backend.

## When to use which primitive

- **Spinlock**: the critical section is a handful of instructions and will complete in under a microsecond. Nothing inside the section can sleep, call another lock, or do I/O. Examples: incrementing a shared counter, reading and writing a small flag.
- **LWLock (exclusive)**: the critical section modifies a shared data structure and may take tens of microseconds but will not block on user-visible operations. Examples: inserting into the buffer mapping hash table, updating `ProcArray`, writing a WAL buffer.
- **LWLock (shared)**: many backends need read access concurrently. Examples: scanning `ProcArray` during snapshot acquisition, reading the buffer mapping table.
- **Heavyweight lock**: the operation needs to be visible to the SQL lock monitoring views, survive commit/rollback, participate in deadlock detection, or protect a resource that users can explicitly lock. Examples: table-level `LOCK TABLE`, row locks set by `SELECT FOR UPDATE`.

## Key source files

- `src/backend/storage/lmgr/lwlock.c` — all LWLock logic: acquisition, release, wait queue, wakeup, tranche management.
- `src/include/storage/lwlock.h` — `LWLock`, `LWLockMode`, `LWLockWaitState`, partition counts, API declarations.
- `src/backend/storage/lmgr/lwlocknames.txt` — source of truth for individual named lock IDs; generates `lwlocknames.h` and `lwlocknames.c`.
- `src/include/storage/s_lock.h` — platform-specific spinlock implementations (`TAS`, `SPIN_DELAY`, `S_UNLOCK`).
- `src/backend/storage/lmgr/s_lock.c` — fallback `s_lock()` function with escalating delay and timeout.
- `src/include/storage/spin.h` — `SpinLockAcquire` / `SpinLockRelease` wrappers over `S_LOCK` / `S_UNLOCK`.

## Related Topics

- [[subsystems/locking/spinlocks|Spinlocks]] — deep dive into the platform-specific spin-wait primitives that underpin the shortest critical sections protected here
- [[subsystems/locking/overview|Locking Overview]] — the full locking hierarchy showing where LWLocks sit relative to spinlocks and heavyweight locks
- [[subsystems/locking/deadlock|Deadlock Detection]] — the heavyweight lock layer above LWLocks that does provide deadlock detection, contrasting with LWLocks
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the heaviest consumer of LWLocks, using both `BufferContent` and `BufferMapping` partitioned lock arrays
- [[subsystems/storage/shared-memory|Shared Memory]] — where `MainLWLockArray` and all LWLock state live, allocated once by the postmaster
- [[subsystems/wal/overview|WAL Overview]] — relies on `WALInsertLock` tranche and `WALWriteLock` LWLocks to coordinate concurrent WAL insertion
- [[subsystems/locking/lock-contention-and-slow-queries|Lock Contention and Slow Queries]] — how LWLock wait events surface in `pg_stat_activity` and contribute to observable performance problems
- [[subsystems/transactions/mvcc|MVCC]] — snapshot acquisition holds `ProcArrayLock` in shared mode.
- [[architecture/process-architecture|Process Architecture]] — why per-process semaphores enable the sleep-based wait mechanism.

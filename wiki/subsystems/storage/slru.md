---
title: "SLRU: Simple LRU Page Cache"
aliases:
  - SLRU
  - Simple LRU
  - slru cache
tags:
  - theme/caching
source_files:
  - src/backend/access/transam/slru.c
  - src/include/access/slru.h
symbols:
  - SlruSharedData
  - SlruCtlData
  - SlruPageStatus
  - SimpleLruReadPage
  - SimpleLruReadPage_ReadOnly
  - SimpleLruWritePage
  - SimpleLruWriteAll
  - SimpleLruTruncate
  - SimpleLruZeroPage
  - SlruSelectLRUPage
  - SlruInternalWritePage
  - SlruPhysicalReadPage
  - SlruPhysicalWritePage
  - SlruRecentlyUsed
---

# SLRU: Simple LRU Page Cache

PostgreSQL maintains several small, sequentially addressed files that track transaction state: the commit log (`pg_xact`), subtransaction parents (`pg_subtrans`), multixact members and offsets (`pg_multixact/members`, `pg_multixact/offsets`), commit timestamps (`pg_commit_ts`), and async notification state (`pg_notify`). These files share a common access pattern — lookups are keyed by a transaction ID or similar integer that maps directly to a page number. Their working set is typically small relative to the whole database. SLRU (Simple LRU) is the dedicated page cache that serves all of them.

The reason these files do not use the main [[subsystems/storage/buffer-manager]] is partly historical and partly intentional. PostgreSQL designs the buffer pool for heap and index pages, which need buffer descriptors, pin counts, visibility checks, and complex eviction heuristics. SLRU data needs none of that. A transaction status lookup reduces to dividing the XID by the number of transactions packed per page, reading that page, and checking two bits. The simpler SLRU implementation has lower overhead. It is also easier to reason about, for the correctness guarantees that transaction visibility depends on.

## Buffer Layout

Each SLRU instance occupies a single contiguous region of shared memory, allocated at startup by `SimpleLruInit()`. The region holds a `SlruSharedData` header followed by parallel arrays — one entry per slot — and then the actual page data (one `BLCKSZ` block per slot).

```c
typedef struct SlruSharedData
{
    LWLock         *ControlLock;       /* protects all shared state */
    int             num_slots;
    char          **page_buffer;       /* pointers into the BLCKSZ data area */
    SlruPageStatus *page_status;       /* per-slot state machine */
    bool           *page_dirty;        /* needs write before eviction */
    int            *page_number;       /* which logical page occupies this slot */
    int            *page_lru_count;    /* recency stamp for eviction */
    LWLockPadded   *buffer_locks;      /* per-slot I/O locks */
    XLogRecPtr     *group_lsn;         /* WAL LSN groups, or NULL */
    int             lsn_groups_per_page;
    int             cur_lru_count;     /* monotonically increasing counter */
    int             latest_page_number;
} SlruSharedData;
```

`SimpleLruInit()` fixes the number of slots at init time. [[subsystems/storage/clog|CLOG]] uses 64 slots by default; other SLRUs use smaller counts. Slot counts are intentionally modest: SLRU access patterns are sequential and write-heavy at the high end (new transactions always write to the latest page), so a small window of recently active pages is almost always sufficient.

**PostgreSQL 17:** Buffer counts for each SLRU cache were previously hard-coded constants. PostgreSQL 17 exposes them as GUCs: `commit_timestamp_buffers`, `multixact_member_buffers`, `multixact_offset_buffers`, `notify_buffers`, `serializable_buffers`, `subtransaction_buffers`, and `transaction_buffers`. Tuning these helps workloads with heavy subtransaction use, many multixacts, or frequent serializable isolation. The defaults match the historical hard-coded values.

A separate, per-process `SlruCtlData` struct holds the directory path and a `PagePrecedes` callback. This callback encodes the modular arithmetic needed to compare page numbers across the [[subsystems/transactions/xid-wraparound|XID wraparound]] boundary. Both truncation and eviction tie-breaking use it.

## Page Status

Each slot is governed by a four-state machine:

| Status | Meaning |
|---|---|
| `SLRU_PAGE_EMPTY` | Slot is free; `page_number` is undefined |
| `SLRU_PAGE_READ_IN_PROGRESS` | A backend is reading this page from disk |
| `SLRU_PAGE_VALID` | Page is loaded and usable (may be dirty) |
| `SLRU_PAGE_WRITE_IN_PROGRESS` | A backend is writing this page to disk |

A page can be dirty only in `SLRU_PAGE_VALID` or `SLRU_PAGE_WRITE_IN_PROGRESS` states. In the latter case, `page_dirty` being true means a backend re-dirtied the page after the write started. SLRU will need to write it again.

## Locking Design

SLRU uses two levels of locking. The single `ControlLock` (an LWLock held exclusively for most operations) protects all fields in `SlruSharedData`. A separate per-slot `buffer_locks[slotno]` LWLock serializes physical I/O on each buffer. The pattern for doing I/O is always: acquire `ControlLock` exclusively, mark the slot as in-progress, acquire the per-buffer lock exclusively, *release* `ControlLock`, do the disk I/O, reacquire `ControlLock`, update state, release the per-buffer lock.

This design means I/O never holds the control lock, so other backends can inspect and update unrelated slots in parallel. A backend waiting for someone else's I/O releases the control lock. It takes the per-buffer lock in shared mode, which blocks until the I/O holder releases it. It then immediately releases that lock, reacquires the control lock, and rechecks state.

`SimpleLruReadPage_ReadOnly()` is an optimised path for read-only access: it first tries a shared-mode control lock scan. If the target page is already loaded and valid, it returns without ever upgrading to exclusive mode, allowing concurrent read-only accesses to proceed in parallel. PostgreSQL explicitly designs the `SlruRecentlyUsed` macro to tolerate this concurrency — a slightly stale LRU stamp causes a non-optimal eviction choice at worst, never a correctness failure.

## Finding or Loading a Page

`SimpleLruReadPage()` is the core entry point. With the control lock held, it calls `SlruSelectLRUPage()`, which does the following:

1. Scans all slots linearly. If any slot already holds the requested page number with a non-empty status, it returns that slot immediately — this is the common (cache hit) path.
2. If not found, scans again looking for an `EMPTY` slot or the least-recently-used `VALID` slot. The function computes the LRU age of a slot as `cur_lru_count - page_lru_count[slotno]`; the highest delta is oldest.
3. The function never selects the slot holding `latest_page_number` for eviction. The most recently written page is almost certain to be accessed again shortly.
4. The function deprioritises I/O-busy slots (those in `READ_IN_PROGRESS` or `WRITE_IN_PROGRESS`). If no clean VALID slot exists, it evicts one that is in WRITE_IN_PROGRESS and waits, or writes a dirty VALID slot first and then loops.

When the function confirms a miss and chooses a victim slot, `SimpleLruReadPage()` marks the slot `READ_IN_PROGRESS`. It takes its per-buffer lock, drops the control lock, and reads the page with `pg_pread()`. It then reacquires the control lock, transitions the slot to `SLRU_PAGE_VALID`, and releases the per-buffer lock.

The `SlruRecentlyUsed` macro stamps the slot with a fresh `cur_lru_count` value after any successful access:

```c
#define SlruRecentlyUsed(shared, slotno)    \
    do { \
        int new_lru_count = (shared)->cur_lru_count; \
        if (new_lru_count != (shared)->page_lru_count[slotno]) { \
            (shared)->cur_lru_count = ++new_lru_count; \
            (shared)->page_lru_count[slotno] = new_lru_count; \
        } \
    } while (0)
```

The guard condition suppresses no-op increments for repeated accesses to the same page. This slows the drift of lru counters and reduces the chance of integer wraparound affecting eviction decisions.

## Write-on-Eviction

SLRU does not eagerly flush dirty pages. SLRU writes a dirty page only when it needs the slot for a different page — lazy write-on-eviction. When `SlruSelectLRUPage()` picks a victim slot that is dirty, it calls `SlruInternalWritePage()` on it before the slot is repurposed. This is the same laziness the main buffer pool uses. It is efficient for the write-heavy latest-page access pattern.

Before writing, if `group_lsn` tracking is enabled (as it is for `pg_xact`), SLRU flushes WAL up to the highest LSN associated with any transaction on that page. This enforces the write-WAL-before-data rule. It mirrors the behaviour of `FlushBuffer()` in [[subsystems/storage/buffer-manager]].

At checkpoint, `SimpleLruWriteAll()` iterates every slot. It calls `SlruInternalWritePage()` on each, flushing all dirty pages to disk. It reuses file descriptors across multiple page writes within the same segment file, to reduce open/close overhead.

## File Layout on Disk

Each SLRU stores its pages in a subdirectory of `$PGDATA`. Within that directory, SLRU groups pages into segment files of 32 pages each (`SLRU_PAGES_PER_SEGMENT`). SLRU names a segment file by its segment number, in four hex digits. For CLOG, each page holds status bits for 32,768 transactions (two bits each, 8KB page), so one segment covers about 1 million transactions. The mapping from transaction ID to page number is:

```
page_number = xid / CLOG_XACTS_PER_PAGE
```

Segment number and page offset within the segment follow from there. This purely arithmetic addressing is what makes SLRU feasible without a hash table or complex lookup structure.

## Truncation

As the database ages, old segments become unreachable — no active transaction needs to check transactions that committed long ago. VACUUM and checkpoint both trigger `SimpleLruTruncate()`, passing a cutoff page derived from the oldest XID still relevant. The function:

1. Acquires the control lock and invalidates any slots holding pages below the cutoff (setting them to `SLRU_PAGE_EMPTY`), waiting for in-progress I/O where necessary.
2. Releases the control lock.
3. Scans the directory with `SlruScanDirectory()` and unlinks all segment files whose page range falls entirely below the cutoff.

Truncation uses the `PagePrecedes` callback to handle modular arithmetic correctly across the 32-bit XID wraparound. A wraparound safety check prevents accidentally deleting the segment containing `latest_page_number`.

## Crash Recovery

PostgreSQL does not WAL-log SLRU pages individually. On a crash, it loses any dirty SLRU pages that a backend had not yet written to disk. For `pg_xact` (CLOG), this is safe: recovery replays WAL commit records, each of which sets the transaction's commit status bits in CLOG. By the time recovery completes, CLOG accurately reflects all transactions that were committed before the crash. The same reasoning applies to `pg_commit_ts`.

PostgreSQL handles `pg_subtrans` and `pg_multixact` differently: subtransaction data is only needed during a transaction's lifetime. PostgreSQL rebuilds multixact state as needed. During recovery, if a SLRU file is missing entirely (for example, because it was never flushed before crash), `SlruPhysicalReadPage()` detects `ENOENT` while `InRecovery` is true. It silently returns a zeroed page instead, which is a safe default.

## SLRU Consumers

| SLRU | Directory | Use |
|---|---|---|
| CLOG | `pg_xact` | Two-bit commit status per transaction (in-progress / committed / aborted / sub-committed) |
| Subtrans | `pg_subtrans` | Parent XID for each subtransaction |
| MultiXact members | `pg_multixact/members` | Per-member entries for shared row locks |
| MultiXact offsets | `pg_multixact/offsets` | Starting offset in the members file per multixact |
| Commit timestamp | `pg_commit_ts` | Commit timestamp and replication origin per XID |
| Async notify | `pg_notify` | Queued LISTEN/NOTIFY payloads |

`pg_notify` is the only consumer that disables fsync (`SYNC_HANDLER_NONE`), since notification data does not need to survive a crash.

## Statistics and Observability

`pg_stat_slru` exposes SLRU activity, reporting per-cache hit, miss, read, write, and flush counts. **PostgreSQL 17:** `pg_stat_reset_shared()` can reset all shared statistics at once when called with no argument or `NULL`. Callers can reset SLRU statistics specifically by passing `'slru'` as the argument.

## Relationship to Other Subsystems

SLRU interacts closely with [[subsystems/transactions/mvcc]]: every visibility check for a tuple must determine whether the writing transaction committed or aborted. That determination ultimately reads a CLOG page. The [[subsystems/locking/lwlocks]] infrastructure provides both the control lock and the per-slot buffer locks. At checkpoint, the checkpointer processes sync requests queued by `SlruPhysicalWritePage()`, integrating SLRU durability into the broader checkpoint protocol described in [[subsystems/wal/overview]].

## Related Topics

- [[subsystems/storage/clog|CLOG]] — the commit log is the primary SLRU consumer, storing two-bit transaction status for every XID and directly driving tuple visibility checks.
- [[subsystems/transactions/mvcc|MVCC]] — every heap tuple visibility decision ultimately bottoms out in a CLOG (SLRU) page read to determine whether a transaction committed or aborted.
- [[subsystems/transactions/multixact|MultiXact]] — uses two SLRU caches (members and offsets) to track shared row locks across multiple transactions.
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the main shared buffer pool that SLRU deliberately avoids. Understanding both clarifies why transaction-state files warrant a separate, simpler cache.
- [[subsystems/locking/lwlocks|LWLocks]] — provides the ControlLock and per-slot buffer locks that underpin all SLRU concurrency guarantees.
- [[subsystems/wal/checkpoint|Checkpoint]] — triggers `SimpleLruWriteAll()` to flush all dirty SLRU pages. It integrates SLRU sync requests into the broader checkpoint protocol.
- [[subsystems/observability/slru-stats|SLRU Statistics]] — `pg_stat_slru` exposes per-cache hit, miss, read, write, and flush counters for monitoring SLRU efficiency.

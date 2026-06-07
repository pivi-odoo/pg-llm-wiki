---
title: "Transaction ID SQL Types: xid, xid8, and Snapshot Functions"
aliases:
  - "xid"
  - "xid8"
  - "FullTransactionId SQL"
  - "pg_current_xact_id"
  - "pg_snapshot"
  - "txid_snapshot"
  - "transaction ID types"
tags:
  - theme/concurrency-control
source_files:
  - src/backend/utils/adt/xid.c
  - src/backend/utils/adt/xid8funcs.c
  - src/include/access/transam.h
  - src/include/utils/xid8.h
symbols:
  - TransactionId
  - FullTransactionId
  - pg_current_xact_id
  - pg_current_xact_id_if_assigned
  - pg_xact_status
  - pg_current_snapshot
  - pg_snapshot
  - pg_visible_in_snapshot
  - pg_snapshot_xmin
  - pg_snapshot_xmax
  - pg_snapshot_xip
  - xid8toxid
  - TransactionIdPrecedes
  - FullTransactionIdPrecedes
  - EpochFromFullTransactionId
  - XidFromFullTransactionId
  - TransactionIdInRecentPast
---

# Transaction ID SQL Types: xid, xid8, and Snapshot Functions

PostgreSQL exposes two distinct SQL types for transaction identifiers — `xid` (32-bit, wrap-around) and `xid8` (64-bit, monotone) — along with a family of functions for inspecting the current XID, querying commit status, and working with snapshots. Understanding the difference between these types, and the trade-offs each imposes, is essential for auditing, MVCC debugging, and concurrent modification detection.

## Special XID Values

The lowest four XID values are reserved and never assigned to real transactions:

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `InvalidTransactionId` | Sentinel: "no transaction". Used in tuple headers to mean the slot is unused. |
| 1 | `BootstrapTransactionId` | Written during `initdb`; considered visible to all transactions. |
| 2 | `FrozenTransactionId` | The freeze target; always treated as in the past by visibility checks. |
| 3 | `FirstNormalTransactionId` | Lowest XID ever assigned to a user transaction. |

Normal XID assignment starts at 3 and increments. After reaching `0xFFFFFFFF` (about 4.29 billion), the counter wraps back to 3. This wraparound is the root cause of the age/horizon concerns described below.

## xid — the 32-bit Modular Type

`xid` maps directly to `TransactionId` (`uint32`) in C. Because the domain wraps, PostgreSQL intentionally omits `<` and `>` ordering operators for the type. Two XIDs can only be compared with `=` (equality) unless you layer on the signed-difference trick.

### Signed-difference trick and `age()`

The `age(xid)` function (`TransactionIdPrecedes` family in C) treats the 32-bit difference as a signed integer. If `current - xid` interpreted as `int32` is positive, `xid` is "in the past"; if negative, it would be "in the future" relative to the current XID. This lets PostgreSQL determine which of two XIDs came first, but only when the two XIDs are within `2^31 - 1` (about 2.1 billion) of each other. That half-cycle horizon is exactly the vacuum safety margin.

Consequences:
- `SELECT age(t.xmin)` grows monotonically as long as no wraparound occurs in the current half-cycle.
- Once two XIDs are more than 2.1 billion apart (or on opposite sides of wraparound) the comparison becomes unreliable.
- `ORDER BY xmin` on `xid` columns is therefore not a safe substitute for ordering by commit time; it is only reliable within a single "half cycle" of XIDs.

Registering `<` for `xid` would mislead the planner and users alike. The operator would need a reference point (the current XID) to interpret results correctly, and no binary operator can provide that. For this reason, PostgreSQL omits ordering operators entirely. This forces callers to use `age()` or to cast to `xid8` when monotone ordering is required.

## xid8 — the 64-bit Full Transaction ID

`xid8` maps to `FullTransactionId` in C, defined in `src/include/utils/xid8.h` as a simple struct:

```c
typedef struct FullTransactionId {
    uint64 value;
} FullTransactionId;
```

The 64-bit value encodes both an epoch (upper 32 bits, incremented on every 32-bit wraparound) and the low 32-bit XID:

- `EpochFromFullTransactionId(f)` — extract the epoch counter
- `XidFromFullTransactionId(f)` — extract the classic `TransactionId`

Because the 64-bit space never wraps in any realistic operational lifetime, `xid8` has full ordering operators (`<`, `<=`, `>`, `>=`). You can use it directly in `ORDER BY`, index it with a B-tree index, and compare it across snapshots without any horizon concern.

### Casting between xid and xid8

| Function | Direction | Notes |
|----------|-----------|-------|
| `xid8toxid(x xid8)` | `xid8 → xid` | Drops the epoch; safe only when the epoch matches the current epoch. |
| `pg_upgrade_xact_id(x xid)` | `xid → xid8` | Promotes a legacy 32-bit XID by attaching the current epoch, or epoch-1 if the XID appears to be in the past relative to the current 32-bit counter. Used during `pg_upgrade`. |

## Current Transaction ID Functions

### Allocating a Real XID on Demand

```sql
SELECT pg_current_xact_id();  -- returns xid8
```

Returns the `FullTransactionId` of the current transaction, allocating a real XID if one has not yet been assigned. This is the XID-allocation side effect: calling `pg_current_xact_id()` in a read-only transaction or a transaction that has not yet written will cause the system to assign an XID, incrementing the global counter and potentially triggering [[subsystems/storage/clog|CLOG]] and pg_xact file extensions. At scale, in workloads with many small read-only transactions, this can meaningfully accelerate XID consumption and move the wraparound horizon closer.

### Reading the Current XID Without Allocating One

```sql
SELECT pg_current_xact_id_if_assigned();  -- returns xid8, or NULL
```

`pg_current_xact_id_if_assigned()` returns the current transaction's `FullTransactionId` only if an XID has already been assigned; otherwise returns `NULL`. This variant is safe to call without causing XID allocation. Prefer it in monitoring queries, health checks, and any read path where you only need to observe whether a transaction has written. It does not force the transaction to acquire a XID.

Use the `_if_assigned` variant when:
- Building audit trails that should not influence XID consumption
- Checking whether the current session is in a write transaction
- Monitoring dashboards that run in idle or read-only sessions

## Commit status lookup

```sql
SELECT pg_xact_status('12345'::xid8);
-- returns: 'committed', 'aborted', 'in progress', or NULL
```

### XactTruncationLock protocol

Before consulting CLOG (the commit log), `pg_xact_status()` must hold `XactTruncationLock` in shared mode. This lock protects against a race where `VACUUM` truncates the CLOG segment for the requested XID between the age check and the CLOG read. The sequence is:

1. Acquire `XactTruncationLock` (shared)
2. Check `TransactionIdIsInRecentPast()` — if the XID is too old (older than `oldest_xmin`), release the lock and return `NULL`
3. Check the proc array for an in-progress XID
4. Look up CLOG
5. Release `XactTruncationLock`

### Proc-array-first check

Before consulting CLOG, the function checks the proc array (the list of currently running backends and their XIDs). This is the same check visibility rules use. If the proc array shows the XID as a running transaction, the result is `'in progress'` without a CLOG access.

### NULL for too-old XIDs

If the XID is older than what CLOG still retains (i.e., VACUUM has truncated the relevant segment), `pg_xact_status()` returns `NULL` rather than raising an error. This distinguishes the "XID predates available history" case from a genuine unknown-status case.

## pg_snapshot — Snapshot Encoding

A snapshot captures the MVCC visibility state at a point in time. The `pg_snapshot` type (formerly `txid_snapshot`) encodes three components:

- **xmin** — the oldest XID still active when the snapshot was taken; any XID below this is visible.
- **xmax** — one past the highest XID assigned when the snapshot was taken; any XID at or above this is invisible (not yet assigned).
- **xip_list** — the set of XIDs between xmin and xmax that were in-progress (not yet committed or aborted) when the snapshot was taken. Rows with these XIDs are invisible even though their XID is below xmax.

### bsearch optimisation threshold

PostgreSQL stores the xip_list as a sorted array. Membership tests use a linear scan for small lists and switch to `bsearch` when the list exceeds a threshold (defined in `xid8funcs.c`). For typical OLTP workloads with few concurrent writers the list is short; for high-concurrency bulk loads it can grow large enough that the binary search path becomes important.

### Snapshot functions

| Function | Return type | Description |
|----------|-------------|-------------|
| `pg_current_snapshot()` | `pg_snapshot` | Capture the current visibility snapshot. |
| `pg_snapshot_xmin(s)` | `xid8` | Extract the xmin of a snapshot. |
| `pg_snapshot_xmax(s)` | `xid8` | Extract the xmax of a snapshot. |
| `pg_snapshot_xip(s)` | `setof xid8` | Return the in-progress XIDs as a set. |
| `pg_visible_in_snapshot(xid, s)` | `boolean` | Test whether a given XID is visible in snapshot `s`. |

### Snapshot Visibility Test

`pg_visible_in_snapshot(xid, s)` returns true — an XID is visible in snapshot `s` — if and only if:
1. `xid < s.xmin`, OR
2. `xid < s.xmax` AND `xid` is NOT in `s.xip_list`

This mirrors exactly the C-level `XidInMVCCSnapshot()` check that the heap AM uses during tuple visibility evaluation.

## Legacy txid_* vs pg_* Names

PostgreSQL 13 renamed the `txid_*` family to `pg_*` and changed the return type of snapshot functions from the old `txid_snapshot` to `pg_snapshot`. Both names remain available as aliases for backward compatibility.

| Legacy name | Current name |
|-------------|--------------|
| `txid_current()` | `pg_current_xact_id()` |
| `txid_current_if_assigned()` | `pg_current_xact_id_if_assigned()` |
| `txid_status(bigint)` | `pg_xact_status(xid8)` |
| `txid_current_snapshot()` | `pg_current_snapshot()` |
| `txid_snapshot_xmin(txid_snapshot)` | `pg_snapshot_xmin(pg_snapshot)` |
| `txid_snapshot_xmax(txid_snapshot)` | `pg_snapshot_xmax(pg_snapshot)` |
| `txid_snapshot_xip(txid_snapshot)` | `pg_snapshot_xip(pg_snapshot)` |
| `txid_visible_in_snapshot(bigint, txid_snapshot)` | `pg_visible_in_snapshot(xid8, pg_snapshot)` |

Note that the legacy `txid_current()` returns `bigint` (equivalent to the 64-bit XID value) while `pg_current_xact_id()` returns `xid8`. The numeric value is identical; the type differs.

## Practical Use Cases

### Auditing and change detection

Store `pg_current_xact_id()` in an audit column to record the writing transaction. Because `xid8` has full ordering, you can later query `WHERE audit_xid > $last_seen_xid` to find rows modified since a known point, without relying on timestamps (which can be skewed by clock adjustments).

### Concurrent modification detection (optimistic locking)

```sql
-- At read time:
SELECT xmin, * FROM orders WHERE id = 42;

-- At write time, verify no intervening write:
UPDATE orders SET ... WHERE id = 42 AND xmin = $saved_xmin::xid;
```

If the `xmin` has changed, another transaction modified the row. The `UPDATE` then affects zero rows, signalling a conflict. This is a classic optimistic concurrency pattern that avoids explicit locking.

### Replication lag estimation

```sql
SELECT pg_current_xact_id()::xid8 - replay_lsn_xid AS xid_lag
FROM pg_stat_replication;
```

XID-based lag complements LSN-based lag. An idle primary generates no LSN movement, but XIDs still monotonically record how many write transactions have occurred.

### Commit status lookup for async operations

After submitting work asynchronously (e.g., via a queue), record `pg_current_xact_id()` before committing. A consumer can later call `pg_xact_status()` on that XID to determine whether the producer's transaction committed, without needing a shared table or lock.

```sql
-- Producer:
INSERT INTO jobs (...) VALUES (...);
SELECT pg_current_xact_id();  -- record this value alongside the job
COMMIT;

-- Consumer (later):
SELECT pg_xact_status($recorded_xid::xid8);
-- 'committed' → job data is durable
-- 'aborted'   → producer rolled back; job should be discarded
-- NULL        → XID too old; assume committed (or consult your own log)
```

## Related Topics

- [[subsystems/transactions/snapshot|MVCC snapshot]] — how snapshots interact with tuple visibility in the heap AM
- [[code-paths/vacuum|vacuum freeze]] — why `age(xmin)` must be kept below `autovacuum_freeze_max_age`
- [[subsystems/storage/clog|CLOG SLRU]] — the CLOG SLRU that backs `pg_xact_status()`
- [[subsystems/transactions/transaction-lifecycle|transaction lifecycle]] — the full life of a transaction from `BEGIN` to CLOG write

---
title: Predicate Locking and Serializable Isolation
aliases:
  - SSI
  - Serializable Snapshot Isolation
  - SIREAD locks
tags:
  - theme/concurrency-control
source_files:
  - src/backend/storage/lmgr/predicate.c
  - src/include/storage/predicate.h
  - src/include/storage/predicate_internals.h
symbols:
  - SERIALIZABLEXACT
  - RWConflictData
  - PredicateLockData
  - PREDICATELOCK
  - PREDICATELOCKTARGET
  - PREDICATELOCKTARGETTAG
  - FlagRWConflict
  - CheckForSerializableConflictIn
  - CheckForSerializableConflictOut
  - PreCommit_CheckForSerializationFailure
  - ReleaseOneSerializableXact
  - OnConflict_CheckForSerializationFailure
---

# Predicate Locking and Serializable Isolation

PostgreSQL implements its `SERIALIZABLE` isolation level not through traditional two-phase locking but through Serializable Snapshot Isolation (SSI), a technique that detects anomalies rather than preventing them upfront. The machinery behind it — predicate locks, rw-conflict tracking, and cycle detection — lives in `src/backend/storage/lmgr/predicate.c` and is entirely separate from the regular lock manager.

## The gap that snapshot isolation leaves

[[subsystems/transactions/mvcc|MVCC]]-based snapshot isolation is already strong: it prevents dirty reads, non-repeatable reads, and lost updates. What it does not prevent are anomalies that only become visible in the transaction dependency graph across multiple concurrent transactions.

The two canonical examples are **write skew** and **phantom reads under write skew conditions**. In write skew, two transactions each read a set of rows and make decisions based on what they saw. Each then writes a disjoint set of rows. No single write conflicts with any other write, so neither transaction sees anything unusual. Yet the combined outcome could never have resulted from any serial execution. Phantom reads compound this. A transaction reads a set of rows matching a predicate. Another transaction inserts a row matching that predicate. The first transaction's decision is retroactively invalidated.

The key insight behind SSI (due to Cahill, Röhm, and Fekete, 2008) is that all serialization anomalies under snapshot isolation are characterised by a particular structure in the transaction dependency graph: a cycle involving at least two **read-write (rw) dependencies**. SSI tracks those dependencies and aborts transactions when the dangerous pattern is detected.

## rw-dependencies and the dangerous pattern

An rw-dependency from transaction T1 to transaction T2 exists when T1 reads a version that T2 later overwrites or deletes. In other words, T1 read something that T2 "conflicted with" by writing. Unlike a classical write-write conflict, this relationship never causes a blocking problem under snapshot isolation; T1 and T2 both proceed without knowing about each other.

The structure that guarantees a serialization failure is a cycle of exactly two rw-edges through a shared transaction called the **pivot**:

```
Tin ---rw---> Tpivot ---rw---> Tout
```

`Tout` must commit before the anomaly is confirmed; `Tin` and `Tpivot` may still be active when the pattern is detected. When such a structure is found, PostgreSQL aborts the pivot (or, if the pivot has already committed, the reader on the in-side) with `SQLSTATE 40001`, serialization failure.

Detecting this pattern in real time — rather than at commit — is what makes SSI efficient. Each time a new rw-dependency edge is added to the graph, the code checks immediately whether the addition completes a dangerous two-edge cycle (`OnConflict_CheckForSerializationFailure()`, `predicate.c`).

## SIREAD locks: recording reads without blocking

To track rw-dependencies, the system must know what each serializable transaction has read. Regular lock modes are unsuitable: a read lock that blocked writers would reduce to two-phase locking, eliminating SSI's concurrency advantage. Instead, PostgreSQL uses **SIREAD locks** (Serializable Isolation READ locks) — markers that say "this transaction read this object" while allowing concurrent writers to proceed unimpeded.

PostgreSQL acquires SIREAD locks when a serializable transaction reads a relation, page, or tuple. Each lock records the target object (database OID, relation OID, and optionally block number and offset) and the owning transaction. Writers, on their side, consult the SIREAD lock table when they write: if another serializable transaction holds a SIREAD lock on data being modified, that constitutes an rw-conflict in to the writer.

Two functions drive conflict detection:
- The table AM calls `CheckForSerializableConflictOut()` when a read encounters a tuple version written by another serializable transaction. The reader's SIREAD locks establish that there is a rw-dependency *out* from the current transaction.
- PostgreSQL calls `CheckForSerializableConflictIn()` on every write. It scans SIREAD locks held on the target tuple, page, and relation and records a rw-conflict *in* to the current transaction for each concurrent reader found.

SIREAD locks survive transaction commit. The rw-dependency graph may not be fully resolved at commit time, because a concurrent transaction might still be active. A transaction's predicate locks must therefore remain visible until all overlapping transactions have finished. Rolling back is the only case where locks can be released immediately.

## Lock granularity and promotion

Holding one SIREAD lock per tuple would be exact but impractical at scale. PostgreSQL supports three granularity levels: relation, page, and tuple (encoded in `PREDICATELOCKTARGETTAG` via `locktag_field3` and `locktag_field4`). A relation lock covers all current and future tuples in the relation; a page lock covers everything on that page; a tuple lock covers only the named row.

Granularity promotion is the mechanism that bounds memory use. When a single transaction accumulates more than `max_pred_locks_per_page` tuple locks on a single page, those locks collapse into one page lock. Similarly, when a transaction exceeds `max_pred_locks_per_relation` page locks on a relation, they collapse into a relation lock. The GUC variables `max_predicate_locks_per_xact`, `max_predicate_locks_per_relation`, and `max_predicate_locks_per_page` control these thresholds.

Promotion is conservative in the safe direction: a coarser lock covers strictly more data than the set of finer locks it replaces, so no existing rw-dependencies are lost. The cost is an increase in false-positive aborts: a relation-level SIREAD lock will record a conflict against any write to that relation, even writes to rows the transaction never actually read.

Each serializable transaction also maintains a local copy of its predicate locks in `LOCALPREDICATELOCK` entries (a per-backend hash table). The local table, carrying a `childLocks` count, lets the system make promotion decisions without taking any [[subsystems/locking/lwlocks|LWLocks]]. It acquires the shared locks only when it actually executes the promotion.

When an index page splits, SIREAD locks on the old page must be copied to the new page (`PredicateLockPageSplit()`, `predicate.c`). The new page now covers some of the rows that fell under the old lock.

## Key data structures

PostgreSQL represents every serializable transaction with a `SERIALIZABLEXACT` in shared memory for the duration of its overlap with any other serializable transaction.

| Field | Purpose |
|---|---|
| `vxid` | Virtual transaction ID of the running backend |
| `prepareSeqNo` / `commitSeqNo` | Monotone sequence numbers to establish commit order |
| `outConflicts` | List of rw-conflicts pointing out to transactions this one couldn't read |
| `inConflicts` | List of rw-conflicts pointing in from transactions that couldn't see this one's writes |
| `predicateLocks` | Linked list of `PREDICATELOCK` objects owned by this transaction |
| `possibleUnsafeConflicts` | For read-only transactions: set of concurrent read-write transactions that might make the snapshot unsafe |
| `finishedBefore` | XID watermark used to determine when the sxact is safe to clean up |
| `flags` | OR of the `SXACT_FLAG_*` constants below |

### SXACT_FLAG constants

| Flag | Meaning |
|---|---|
| `SXACT_FLAG_COMMITTED` | Transaction has committed |
| `SXACT_FLAG_PREPARED` | Has passed `PreCommit_CheckForSerializationFailure`; cannot abort |
| `SXACT_FLAG_ROLLED_BACK` | Has rolled back; locks can be released |
| `SXACT_FLAG_DOOMED` | Will roll back at the next opportunity |
| `SXACT_FLAG_CONFLICT_OUT` | Has a rw-conflict out to a transaction that committed ahead of it |
| `SXACT_FLAG_READ_ONLY` | Transaction declared or found to be read-only |
| `SXACT_FLAG_RO_SAFE` | Read-only transaction has a safe snapshot |
| `SXACT_FLAG_RO_UNSAFE` | Read-only transaction has been found to have an unsafe snapshot |
| `SXACT_FLAG_SUMMARY_CONFLICT_IN` | Has a conflict-in edge to a summarized (evicted) transaction |
| `SXACT_FLAG_SUMMARY_CONFLICT_OUT` | Has a conflict-out edge to a summarized (evicted) transaction |
| `SXACT_FLAG_DEFERRABLE_WAITING` | A `READ ONLY DEFERRABLE` transaction waiting for a safe snapshot |

`RWConflictData` tracks conflicts between pairs of transactions:

| Field | Purpose |
|---|---|
| `sxactOut` | Transaction that read; the rw-dependency points out of it |
| `sxactIn` | Transaction that wrote; the rw-dependency points into it |
| `outLink` / `inLink` | Intrusive list nodes for fast traversal from either direction |

The lock itself is `PREDICATELOCK`, with a `PREDICATELOCKTAG` combining the target (`PREDICATELOCKTARGET`) and the owning `SERIALIZABLEXACT`. The `commitSeqNo` field is populated only when the lock has been transferred to the special `OldCommittedSxact` dummy entry during summarization (described below).

`PredicateLockData` is a snapshot structure used only by `GetPredicateLockStatusData()` to expose lock information through the `pg_locks` view.

## Safe snapshots and deferrable transactions

A read-only transaction can never be the writer in a dangerous pattern: it produces no rw-dependency edges going out, so it cannot serve as `Tpivot`. However, it could be `Tin`, reading data that a pivot later overwrites. That anomaly is real.

The question for a read-only transaction is therefore whether any read-write transaction that committed before its snapshot was taken might have a conflict-out edge that could complete a dangerous structure with the read-only transaction on the `Tin` side. If no such transaction exists, or if all such candidates have committed without the dangerous flag set, the snapshot is **safe** and the transaction can release its predicate locks and stop participating in conflict tracking entirely.

The `possibleUnsafeConflicts` list in `SERIALIZABLEXACT` tracks which concurrent read-write transactions could theoretically make the snapshot unsafe. As each such transaction finishes, it either flags the read-only transaction as unsafe (setting `SXACT_FLAG_RO_UNSAFE`) or removes itself from the list. When the list empties, the transaction is flagged `SXACT_FLAG_RO_SAFE`.

`READ ONLY DEFERRABLE` transactions exploit this: they block at snapshot acquisition time, setting `SXACT_FLAG_DEFERRABLE_WAITING`, until a safe snapshot becomes available. Once safe, they proceed without any predicate lock overhead and are guaranteed never to be aborted for serialization reasons. This makes `DEFERRABLE` suitable for long-running reporting queries where occasional delay at start is acceptable.

## Summarized transactions

Shared memory for `SERIALIZABLEXACT` entries is finite. When the pool runs low, old committed transactions are **summarized**: their individual `PREDICATELOCK` entries are transferred to a single dummy transaction (`OldCommittedSxact`), collapsing multiple locks on the same target into one with the highest `commitSeqNo` among the originals. The detailed `inConflicts` and `outConflicts` lists are replaced by the coarse summary flags `SXACT_FLAG_SUMMARY_CONFLICT_IN` and `SXACT_FLAG_SUMMARY_CONFLICT_OUT` on the surviving transactions.

Summarization (`ReleaseOneSerializableXact()` with `summarize=true`, `predicate.c`) is deliberately conservative: a summarized conflict-out is treated as if it could have committed at any time. As a result, conflict detection against summarized transactions errs on the side of false positives rather than missing real anomalies. The SLRU (via `SerialSlruCtl`) provides longer-term storage for commit sequence numbers of very old transactions, allowing conflict checks against transactions that have been fully evicted from shared memory.

The commit sequence number (`SerCommitSeqNo`) is a strictly monotone 64-bit counter (`PredXact->LastSxactCommitSeqNo`) that establishes a total commit order. `CanPartialClearThrough` and `HavePartialClearedThrough` track cleanup progress: when all active transactions are read-only, the system can safely release predicate lock sets for committed read-write transactions whose `commitSeqNo` falls below the threshold. It retains only their `outConflicts`, to catch late-arriving readers.

## Commit-time checking

Even with continuous conflict detection, some dangerous structures can only be confirmed at commit. `PreCommit_CheckForSerializationFailure()` performs a final scan of the committing transaction's conflict lists, looking for the two-rw-edge pattern that could not have been ruled out earlier. If a dangerous structure is found, the pivot is aborted; if the pivot is already prepared (and thus past the point of no return), the reader side is aborted instead.

The asymmetry in what gets aborted reflects a practical constraint: a prepared transaction has already told remote participants it will commit. Aborting it would require an expensive rollback protocol. SSI therefore prefers to abort the not-yet-prepared side when both choices would produce a correct result.

## Performance characteristics

SSI adds cost relative to plain snapshot isolation on two fronts. Every read by a serializable transaction may acquire a SIREAD lock (or confirm that a coarser lock already covers the target). Every write must scan the SIREAD lock table for the affected tuples, pages, and relation. Conflict graph maintenance — allocating `RWConflictData` entries, threading them onto the `inConflicts` and `outConflicts` lists, checking for the dangerous pattern — happens in the hot path on both reads and writes.

Against two-phase locking, the comparison is more favourable. SSI never blocks a read on a write or a write on a read. False-positive aborts occur under SSI only when granularity promotion (coarsening to page or relation level) creates phantom conflicts, or when summarization causes conservative conflict assumptions. Under 2PL, false-positive blocking is inherent to the mechanism.

Transactions operating under `REPEATABLE READ` or lower isolation levels do not acquire SIREAD locks and are invisible to the SSI machinery, making the overhead pay-as-you-use for applications that do not need full serializability.

## Interaction with indexes and vacuum

Index scans use predicate locking at the page granularity (`PredicateLockPage()`) for B-tree and similar indexes, because the index page defines the range that was scanned. Sequential scans of heap pages follow the same pattern. PostgreSQL acquires tuple-level locks (`PredicateLockTID()`) when visibility checks resolve a specific tuple as the version being returned to the query.

VACUUM and index operations that restructure storage must cooperate with the predicate lock machinery. When a heap page is vacuumed away, any predicate locks on it must be promoted to the relation level. When an index is dropped or truncated, all predicate locks on the index are transferred to the heap relation (`TransferPredicateLocksToHeapRelation()`, `predicate.c`), preserving the coverage invariant.

## Related Topics

- [[subsystems/transactions/isolation-levels|Isolation Levels]] — covers the SERIALIZABLE, REPEATABLE READ, and READ COMMITTED levels that determine whether SSI machinery is engaged
- [[subsystems/transactions/snapshot|Snapshots]] — explains how transaction snapshots are acquired and how DEFERRABLE transactions wait for a safe snapshot before proceeding
- [[subsystems/locking/deadlock|Deadlock Detection]] — the complementary conflict-resolution mechanism for regular lock waits, contrasted with SSI's abort-on-cycle approach
- [[subsystems/locking/row-level-locking|Row-Level Locking]] — covers FOR UPDATE/SHARE locks that coexist with SIREAD locks during serializable reads
- [[subsystems/transactions/deferrable-constraints|Deferrable Constraints]] — shares the deferred-validation model used by READ ONLY DEFERRABLE transactions waiting for a safe snapshot
- [[subsystems/storage/slru|SLRU]] — the simple LRU buffer manager backing SerialSlruCtl, which stores commit sequence numbers of evicted serializable transactions
- [[troubleshooting/lock-waits|Lock Waits]] — practical guidance on diagnosing serialization failures and 40001 errors that SSI produces
- [[subsystems/locking/overview|Locking Overview]] — the regular heavyweight lock manager and lock modes that SIREAD locks operate alongside, without participating in.
- [[subsystems/transactions/mvcc|MVCC]] — the snapshot isolation and tuple visibility model that SSI builds on top of to detect serialization anomalies.
- [[subsystems/indexes/btree|B-tree Indexes]] — B-tree index internals and the page-level granularity at which predicate locks are taken for index scans.
- [[subsystems/transactions/transaction-lifecycle|Transaction Lifecycle]] — how commit and rollback drive `SERIALIZABLEXACT` cleanup and predicate lock release.
- [[code-paths/vacuum|VACUUM Code Path]] — vacuum's interaction with predicate lock cleanup, including promotion when a heap page is vacuumed away.

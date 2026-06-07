---
title: Sequences
aliases:
  - sequence
  - serial
  - identity column
  - nextval
tags:
  - theme/durability
source_files:
  - src/backend/commands/sequence.c
  - src/include/commands/sequence.h
symbols:
  - nextval_internal
  - DefineSequence
  - ResetSequence
  - do_setval
  - SeqTableData
  - FormData_pg_sequence_data
  - xl_seq_rec
  - SEQ_LOG_VALS
---

# Sequences

A sequence is PostgreSQL's mechanism for generating a monotonically increasing (or decreasing) series of integers without the contention that would come from updating a shared counter under normal transaction semantics. The defining characteristic — and the one that surprises most users — is that sequence advancement is intentionally non-transactional: a value consumed by `NEXTVAL` is gone, regardless of whether the calling transaction commits or rolls back. That trade-off is the entire point; it lets thousands of concurrent inserts claim unique identifiers without blocking one another.

## Storage layout

Despite their special behaviour, sequences are stored as ordinary heap relations. `DefineSequence()` calls `DefineRelation()` with `RELKIND_SEQUENCE`, which allocates a single-page heap file in the normal way. The page carries a magic number (`SEQ_MAGIC = 0x1717`) in its special area, to distinguish it from a table page. It holds exactly one tuple.

The on-disk tuple has three fields, defined in `FormData_pg_sequence_data` (`sequence.h`):

| Field | Type | Meaning |
|---|---|---|
| `last_value` | `int64` | The highest value already handed out (or the start value before first use) |
| `log_cnt` | `int64` | How many future values have been pre-logged to WAL; decrements as values are consumed |
| `is_called` | `bool` | False until the first `NEXTVAL`; controls whether `last_value` itself should be returned on the next call |

PostgreSQL stores sequence parameters — `INCREMENT`, `MINVALUE`, `MAXVALUE`, `START`, `CACHE`, `CYCLE` — in the system catalog `pg_sequence` rather than in the data tuple, keeping the hot path (the single-page buffer) small.

Because sequences are never vacuumed, `fill_seq_fork_with_data()` forcibly stamps the tuple header with `FrozenTransactionId` at creation. Without this, the tuple's `xmin` would age past the 2-billion-transaction wraparound horizon and become invisible.

## NEXTVAL: the fast path

`nextval_internal()` (`sequence.c`) is the core of sequence operation. Its first action is to check the backend-local cache.

Each backend maintains a hash table of `SeqTableData` entries, one per sequence it has ever touched in the session:

```c
typedef struct SeqTableData {
    Oid     relid;       /* pg_class OID — hash key */
    int64   last;        /* last value returned to caller */
    int64   cached;      /* last value pre-allocated into local cache */
    int64   increment;
    bool    last_valid;
    ...
} SeqTableData;
```

When `last != cached`, the backend has unused values sitting in its local cache. It simply advances `last` by `increment`, closes the relation without touching shared memory, and returns. No buffer lock, no WAL write, no shared-state modification. This is the common case for any sequence with `CACHE > 1`.

When the local cache is exhausted, `nextval_internal()` acquires an exclusive buffer lock on the sequence page, reads the tuple, and pre-allocates a fresh batch of `cache` values. It writes `last_value = <last value in the new batch>` back to the page and releases the lock. Other backends waiting for that lock then get their own batches in turn.

## Caching and the WAL pre-logging trick

Updating the sequence page on every single `NEXTVAL` would generate one WAL record per call — expensive at high rates. PostgreSQL avoids this with a pre-logging scheme controlled by `SEQ_LOG_VALS = 32`.

When `nextval_internal()` must write back to the page, it checks two conditions that force a WAL record:

1. `log_cnt < fetch` — not enough pre-logged values remain to cover the requested cache.
2. `PageGetLSN(page) <= GetRedoRecPtr()` — the last sequence update predates the most recent checkpoint, so crash recovery would not see it.

When either condition is true, the function fetches `cache + SEQ_LOG_VALS` values instead of just `cache`. It writes a WAL record that reflects the sequence state as if all those extra values had already been handed out. The `log_cnt` field tracks how many of those pre-logged slots remain. For the next `SEQ_LOG_VALS` cache refills, `nextval_internal()` writes no new WAL record. It dirties the page in the buffer pool with the real `last_value`, relying on the earlier WAL record to cover crash recovery.

The consequence is that a crash can cause a sequence to skip over up to `SEQ_LOG_VALS` values beyond what was truly consumed, in addition to any values lost from the per-backend cache. This is acceptable: the guarantee is uniqueness, not contiguity.

## Sources of gaps

Gaps in sequence values have three independent sources:

**Cache pre-allocation.** Each backend pre-allocates `CACHE` values at once. If a backend exits after using only some of them, PostgreSQL discards the rest. With the default `CACHE 1`, this source disappears, at the cost of one shared-memory operation per `NEXTVAL`.

**Transaction rollback.** `NEXTVAL` updates the on-disk sequence tuple outside the calling transaction's undo chain. A transaction that calls `NEXTVAL` and then rolls back has permanently consumed that value. Making `NEXTVAL` transactional would mean holding the sequence lock — or blocking subsequent callers — for the duration of every transaction that ever needed an ID. This would serialize all concurrent inserts.

**Server crashes.** Values covered by the WAL pre-logging lookahead (up to `SEQ_LOG_VALS` per cache refill) are lost on crash because the WAL record describes a state further ahead than the actual last-used value.

Applications that truly require gap-free numbering must implement that logic themselves, typically with a locking counter table — accepting the serialization that sequences are designed to avoid.

## SERIAL and GENERATED AS IDENTITY

`SERIAL` and `BIGSERIAL` are syntactic sugar. The parser rewrites `col SERIAL` into `col INTEGER NOT NULL DEFAULT nextval('seq_name')` and issues a `CREATE SEQUENCE` for the backing sequence. The parser then marks the sequence as owned by the column, so that `DROP TABLE` (or `DROP COLUMN`) cascades to drop the sequence automatically.

`GENERATED AS IDENTITY` (`GENERATED ALWAYS` or `GENERATED BY DEFAULT`) is the SQL-standard equivalent, introduced in PostgreSQL 10. It creates the same backing sequence but wires it through the identity mechanism rather than a column default. The key practical difference is that `GENERATED ALWAYS` rejects explicit `INSERT` values unless `OVERRIDING SYSTEM VALUE` is specified, making accidental ID collisions harder. New code should prefer identity columns over `SERIAL`.

Both mechanisms store sequence parameters in `pg_sequence` and ownership in `pg_depend`, linking the sequence to the column with a `DEPENDENCY_OWNED_BY` (`i`) dependency type.

## WAL logging and standbys

Every write to the sequence page that triggers the pre-logging condition emits an `XLOG_SEQ_LOG` record via `XLogInsert(RM_SEQ_ID, XLOG_SEQ_LOG)`. The WAL record embeds the full sequence tuple (the `xl_seq_rec` header contains the `RelFileLocator`; the tuple data follows inline). On replay, `seq_redo()` reconstructs the page from scratch. It registers the buffer with `REGBUF_WILL_INIT`, so WAL replay does not need the prior page image.

On a physical standby, sequences are read-only. Calling `NEXTVAL` on a standby raises an error. This restriction exists because the standby replays the primary's WAL stream; a standby-side sequence write would diverge from that stream and corrupt the sequence state on failover. Logical replication does not replicate sequence state at all; after a failover, the operator must manually advance the new primary's sequences with `SETVAL`.

## Overflow and cycling

Each sequence has a `MINVALUE` and `MAXVALUE`. `nextval_internal()` checks the bound on every fetch:

```c
if (maxv >= 0 && next > maxv - incby) {
    if (!cycle) ereport(ERROR, ...);
    next = minv;   /* wrap around */
}
```

With `CYCLE`, the sequence wraps silently; without it, the next `NEXTVAL` after the last valid value raises `ERRCODE_SEQUENCE_GENERATOR_LIMIT_EXCEEDED`. The default for `bigint` sequences (the type underlying `BIGSERIAL` and `GENERATED AS IDENTITY`) has `MAXVALUE 9223372036854775807`, so exhaustion is not a practical concern in most applications. This contrasts with `integer` sequences, whose range tops out at roughly 2.1 billion.

## Locking

Sequence operations use two layers of locking:

- **Relation-level lock.** `init_sequence()` acquires a `SequenceLock` (effectively `AccessShareLock`) via the lock manager. The backend caches this lock per transaction after the first access, which is why the lock manager is not consulted on every `NEXTVAL` call once the cache is warm.
- **Buffer-level lock.** The actual read-modify-write of the sequence tuple holds an exclusive buffer content lock for the duration of the critical section in `nextval_internal()`. This is a lightweight [[subsystems/locking/lwlocks|LWLock]], not a heavyweight lock, so contention on a hot sequence shows up as spinlock or LWLock wait time rather than in `pg_locks`.

`SETVAL` follows the same locking path as `nextval_internal()` but also sets `log_cnt = 0`, forcing the next `NEXTVAL` to write a fresh WAL record immediately.

## Key data structures

**`FormData_pg_sequence_data`** (`sequence.h`) — the on-disk tuple layout for the sequence page.

**`SeqTableData`** (`sequence.c`) — the backend-local cache entry. One entry per sequence per session, stored in `seqhashtab`.

**`xl_seq_rec`** (`sequence.h`) — the WAL record header for sequence log entries; contains only the `RelFileLocator`, with the tuple data appended inline.

## Related Topics

- [[code-paths/sequence|Sequence Code Path]] — walkthrough of the SQL-level DDL commands (`CREATE`, `ALTER`, `DROP SEQUENCE`) that set up and modify the sequences described here.
- [[subsystems/storage/sequence-am|Sequence Access Method]] — the storage access method layer that sits between `sequence.c` and the buffer manager, handling page-level reads and writes for sequence relations.
- [[subsystems/catalog/pg-depend|pg_depend]] — tracks the `DEPENDENCY_OWNED_BY` links between a `SERIAL` or identity-column sequence and its owning column, enabling cascade drops.
- [[subsystems/locking/lwlocks|LWLocks]] — the buffer content lock used inside `nextval_internal()` to serialize the read-modify-write of the sequence tuple; contention here surfaces as LWLock wait events.
- [[subsystems/wal/overview|WAL Overview]] — covers the WAL infrastructure used by `XLOG_SEQ_LOG` records and the pre-logging scheme that lets sequences avoid a WAL write on every cache refill.
- [[subsystems/transactions/mvcc|MVCC]] — explains why sequences deliberately bypass normal transaction semantics; understanding MVCC clarifies the deliberate design decision to make `NEXTVAL` non-transactional.
- [[subsystems/replication/logical|Logical Replication]] — discusses the limitation that sequence state is not replicated logically, requiring manual `SETVAL` after failover to a logical replica or new primary.
- [[subsystems/storage/buffer-manager|Buffer Manager]] — sequence pages flow through the buffer manager like any other relation page, so buffer pinning and eviction rules apply here too.
- [[subsystems/executor/overview|Executor Overview]] — resolves column defaults that reference sequences during executor initialization for `INSERT` statements.

---
title: "Transaction WAL Records"
aliases:
  - "XLOG_XACT_COMMIT"
  - "XLOG_XACT_ABORT"
  - "xl_xact_commit"
  - "xl_xact_abort"
  - "xl_xact_prepare"
  - "transaction WAL"
tags:
  - theme/durability
source_files:
  - src/backend/access/rmgrdesc/xactdesc.c
  - src/include/access/xact.h
  - src/backend/access/transam/xact.c
symbols:
  - xl_xact_commit
  - xl_xact_abort
  - xl_xact_prepare
  - xl_xact_invals
  - xl_xact_subxacts
  - xl_xact_relfilelocators
  - xl_xact_parsed_commit
  - xl_xact_parsed_abort
  - XactLogCommitRecord
  - XactLogAbortRecord
  - ParseCommitRecord
  - ParseAbortRecord
  - ParsePrepareRecord
  - xact_desc
  - xact_identify
---

Transaction WAL records are the most structurally important records in the WAL stream. Recovery depends on them to decide which transactions to replay or discard. They also carry side-effect metadata — invalidation messages, dropped relation files, subtransaction XIDs — that must be replayed in exactly the right order for the database to reach a consistent state after a crash. The `Transaction` resource manager (RM ID 1) writes them. `pg_waldump --rmgr=Transaction` displays them.

## The commit record

The commit record (`XLOG_XACT_COMMIT`, opcode `0x00`) is the durability boundary for a write transaction. Once PostgreSQL flushes this record to stable storage, the transaction is committed. Nothing that happens afterward can un-commit it. `XactLogCommitRecord()` (`xact.c`) builds the record. `RecordTransactionCommit()` emits it before PostgreSQL stamps the transaction's XID as committed in [[subsystems/storage/clog|CLOG]].

The on-disk layout starts with a fixed header (`xl_xact_commit`, `xact.h`) containing only the commit timestamp. PostgreSQL appends everything else as optional sub-records, each guarded by a bit in a secondary `xinfo` field:

| Sub-record type | `xinfo` flag | Contents |
|---|---|---|
| `xl_xact_dbinfo` | `XACT_XINFO_HAS_DBINFO` | Database OID and tablespace OID |
| `xl_xact_subxacts` | `XACT_XINFO_HAS_SUBXACTS` | Array of committed subtransaction XIDs |
| `xl_xact_relfilelocators` | `XACT_XINFO_HAS_RELFILELOCATORS` | Relation file locators to unlink on commit |
| `xl_xact_stats_items` | `XACT_XINFO_HAS_DROPPED_STATS` | Cumulative stats entries to drop |
| `xl_xact_invals` | `XACT_XINFO_HAS_INVALS` | Shared-cache invalidation messages |
| `xl_xact_twophase` | `XACT_XINFO_HAS_TWOPHASE` | XID of the original prepared transaction (2PC only) |
| `xl_xact_origin` | `XACT_XINFO_HAS_ORIGIN` | Replication origin LSN and timestamp |

This variable-length design keeps commit records small for the common case. A simple single-statement transaction that touched no catalog tables and dropped no relations produces a minimal record — just the timestamp and perhaps a small invalidation list.

### Cache invalidation messages in the commit record

When a transaction modifies a system catalog, PostgreSQL must ensure that other backends drop their cached copies of the affected catalog entries. The mechanism is shared-cache invalidation: at commit time, PostgreSQL collects any pending invalidation messages and embeds them in the commit record as `xl_xact_invals` (`xact.h`).

During recovery, the startup process replays these messages by calling the normal invalidation machinery. This guarantees that catalog caches reflect the committed state in exactly the order the original transaction produced it. That guarantee holds whether the caches live on a standby or were rebuilt after a crash. Deferring invalidation to commit time — rather than writing a separate WAL record per catalog change — has a useful consequence: if the transaction aborts, no invalidation is ever replayed. This is the correct behavior.

The abort record deliberately omits invalidation messages. An aborted transaction's catalog changes were never committed, so there is nothing to invalidate on replay (`xl_xact_abort` in `xact.h` has no `xl_xact_invals` field).

### Dropped relation files

When a transaction drops a table or index, the relation's file cannot be unlinked immediately. A checkpoint in progress might still reference the file. WAL replay must also be able to reconstruct the correct final state. Instead, the commit record stores the file locator in `xl_xact_relfilelocators`. During recovery, `xact_redo()` calls the storage manager to unlink the relation files as part of replaying the commit. This ensures that PostgreSQL cleans up dropped files exactly when, and only when, it knows the transaction committed.

### Subtransaction XIDs

Each subtransaction (`SAVEPOINT`) that was not rolled back before the top-level commit has its own XID. The commit record for the top-level transaction includes all of those sub-XIDs in `xl_xact_subxacts`. Recovery uses this list to call `TransactionIdCommitTree()`, which stamps the top-level XID and every sub-XID as committed in CLOG in a single operation. This atomicity is essential: a crash between committing the top-level and committing the subtransactions would leave the subtransactions' tuple changes visible but their CLOG entries absent, creating an inconsistent view of the data.

The same reasoning applies to the ProcArray: a single commit record lets recovery clear the entire XID group at once. Before it writes the commit record, the backend accumulates committed child XIDs in an in-memory list via `xactGetCommittedChildren()` (`xact.c`).

## The abort record

The abort record (`XLOG_XACT_ABORT`, opcode `0x20`, built by `XactLogAbortRecord()`) is simpler than the commit record. Its mandatory content is just the abort timestamp. Optional sub-records cover relation file locators to unlink (for files created during the transaction that must now be removed), subtransaction XIDs, and a two-phase XID for prepared-transaction aborts.

Crucially, the abort record does not require synchronous WAL flush. The default assumption during recovery is that any XID whose commit record is absent was aborted. Even if a crash loses the abort record, recovery still reaches the correct result. Finding no commit record, it treats the transaction as aborted. Writing the abort record at all is useful for physical standbys — it allows them to promptly release locks and clean up without waiting for the primary to issue a new checkpoint.

## The prepare record

For [[subsystems/transactions/two-phase-commit|two-phase commit]], `PREPARE TRANSACTION` writes a `XLOG_XACT_PREPARE` record before the transaction detaches from the backend. This record must carry everything needed to either commit or abort the transaction later, possibly after a crash and restart. At that point, the prepared transaction state is orphaned from any specific backend session.

The `xl_xact_prepare` struct (`xact.h`) is denser than `xl_xact_commit` because it uses a fixed header with explicit counts rather than optional sub-records. It carries two sets of relation file locators (one for commit-time deletion, one for abort-time deletion), two sets of dropped-stats entries for the same reason, the full list of subtransaction XIDs, and the invalidation messages. The global transaction identifier (GID) follows the header as a null-terminated string.

Unlike commit and abort records, PostgreSQL always flushes the prepare record synchronously. `PREPARE TRANSACTION` would be meaningless if the prepared state were lost in a crash.

## XLOG_XACT_ASSIGNMENT records

When a backend has accumulated more subtransaction XIDs than the ProcArray can cache per process (`PGPROC_MAX_CACHED_SUBXIDS`), it writes a separate record type: `XLOG_XACT_ASSIGNMENT` (opcode `0x50`). This record logs the overflow XIDs into WAL. Hot-standby processes can then track which XIDs are in flight without needing the full sub-XID list to fit in shared memory. The record carries the top-level XID (`xl_xact_assignment.xtop`) and an array of sub-XIDs (`xsub[]`).

## XLOG_XACT_INVALIDATIONS records

A long-running transaction may issue DDL that must reach standbys promptly, rather than waiting until commit. In that case, PostgreSQL can emit a standalone `XLOG_XACT_INVALIDATIONS` record containing a bare `xl_xact_invals` payload. This allows hot-standby replicas to process catalog invalidations mid-transaction rather than accumulating them until the commit record arrives.

## xactdesc.c and pg_waldump

The file `src/backend/access/rmgrdesc/xactdesc.c` provides the `Transaction` resource manager's `rm_desc` and `rm_identify` callbacks. These are the functions that `pg_waldump` and the recovery log call to produce human-readable output for transaction records.

`xact_desc()` (`xactdesc.c`) dispatches to `xact_desc_commit()`, `xact_desc_abort()`, or `xact_desc_prepare()` based on the opcode. Each of these calls the corresponding `ParseCommitRecord()`, `ParseAbortRecord()`, or `ParsePrepareRecord()` function to decode the variable-length record into a flat `xl_xact_parsed_commit` or `xl_xact_parsed_abort` struct. It then formats the fields into human-readable text.

`ParseCommitRecord()` and its siblings deliberately live in `xactdesc.c` rather than `xact.c`, because both backend code (used during WAL replay in recovery) and frontend code (`pg_waldump`) share them. This file is the only transaction-specific source that compiles in both contexts.

`xact_identify()` returns a string name for each opcode — `"COMMIT"`, `"ABORT"`, `"PREPARE"`, etc. `pg_waldump` prints that name before the record description. Running `pg_waldump --rmgr=Transaction` on a WAL segment produces one line per transaction record, with the timestamp, the list of dropped relations, the subtransaction XIDs, and the invalidation messages rendered legibly.

## Related Topics

- [[subsystems/wal/wal-records|WAL record format]]
- [[subsystems/wal/overview|WAL overview]]
- [[subsystems/transactions/transaction-lifecycle|transaction lifecycle]]
- [[subsystems/transactions/two-phase-commit|two-phase commit]]
- [[subsystems/transactions/subtransactions|subtransactions]]

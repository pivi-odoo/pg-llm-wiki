---
title: Logical Replication
aliases:
  - logical replication
  - logical decoding
  - pgoutput
  - reorder buffer
tags:
  - symptom/replication-lag
source_files:
  - src/backend/replication/logical/logical.c
  - src/backend/replication/logical/decode.c
  - src/backend/replication/logical/reorderbuffer.c
  - src/backend/replication/pgoutput/pgoutput.c
symbols:
  - LogicalDecodingContext
  - ReorderBuffer
  - ReorderBufferTXN
  - OutputPluginCallbacks
  - RelationSyncEntry
  - LogicalDecodingProcessRecord
  - pgoutput_change
  - pgoutput_row_filter
  - SUBREL_STATE_READY
---

# Logical Replication

Physical replication ships raw WAL pages. The standby applies the same byte-level changes as the primary, which makes it fast but completely opaque to anything other than an identical PostgreSQL install. Logical replication takes a different approach. It decodes the WAL into a structured stream of row-level change events — INSERT, UPDATE, DELETE, TRUNCATE. It delivers them to consumers that understand table-and-column semantics. That shift enables selective replication of individual tables, replication between major versions, change data capture, and external data feeds via custom output plugins.

The feature has two largely independent parts: the **logical decoding** infrastructure that produces a change stream from WAL, and the **publication/subscription** mechanism that routes that stream to a subscriber database and applies it as ordinary DML.

## The Decoding Pipeline

A `LogicalDecodingContext` (`logical.c`) sits at the center of every logical replication consumer. The context wires together three cooperating subsystems: a WAL reader, the `ReorderBuffer`, and an output plugin. It also enforces the prerequisite that `wal_level = logical` is set on the server, because the additional WAL detail needed for row reconstruction is only written at that level (`CheckLogicalDecodingRequirements()`, `logical.c`).

WAL records arrive one at a time through `LogicalDecodingProcessRecord()` (`decode.c`). The function inspects each record's resource manager and dispatches to the appropriate decoder — `DecodeInsert`, `DecodeUpdate`, `DecodeDelete`, `DecodeTruncate`, `DecodeCommit`, `DecodeAbort`, and so on. The decoders do not emit changes directly. They feed them into the `ReorderBuffer`, tagging each change with its transaction ID. The decoder assigns records for subtransactions to their top-level parent as soon as the relationship is known. This happens either when PostgreSQL emits an `XLOG_XACT_ASSIGNMENT` record (for large subtransaction stacks) or at commit time.

A catalog snapshot built by `snapbuild.c` runs in parallel. It tracks which transaction IDs were running at each point. It also constructs an MVCC-compatible snapshot that the output plugin can use to look up system catalog rows — necessary for resolving type OIDs to names, expanding composite types, and similar catalog lookups.

## The ReorderBuffer and Output Plugins

PostgreSQL writes WAL records in the order transactions execute, not in the order they commit. As a result, a single stream of WAL interleaves rows from dozens of concurrent transactions. The `ReorderBuffer` (`reorderbuffer.c`) untangles this. It accumulates changes per transaction and delivers a transaction's changes to the output plugin only after it reads that transaction's commit record. This guarantees that the consumer sees each transaction as an atomic, ordered unit. The ReorderBuffer spills long-running transactions that exceed `logical_decoding_work_mem` (defaulting to 64 MB) to disk rather than holding them in memory indefinitely. It holds [[subsystems/storage/toast|TOAST]]-reassembled column values per-transaction until it replays the owning change.

Once a transaction is ready for delivery, the ReorderBuffer drives a fixed sequence of output plugin callbacks (`OutputPluginCallbacks`) — `begin_cb`, `change_cb`, `truncate_cb`, `commit_cb`, plus variants for streaming and message events. **pgoutput**, PostgreSQL's built-in plugin, is what native logical replication uses. It filters changes through publication rules and serializes rows in the binary logical replication protocol before handing them to the walsender.

See [[subsystems/replication/logical-decoding|Logical Decoding]] for the `ReorderBufferChange` field layout, the `ReorderBufferChangeType` and `txn_flags` enums, spill-to-disk and transaction-streaming mechanics, the full `OutputPluginCallbacks` table, and other output plugins such as wal2json.

**PostgreSQL 18:** The default value of the subscription `streaming` option changed from `off` to `parallel`. PostgreSQL now streams large in-progress transactions to parallel apply workers rather than buffering them until commit. This reduces apply latency without any configuration change.

## Publications and Subscriptions

A **publication** is a named set of tables and allowed operations, stored in `pg_publication` and `pg_publication_rel`. Its `FormData_pg_publication` row records four boolean flags — `pubinsert`, `pubupdate`, `pubdelete`, `pubtruncate` — along with `puballtables` for the `FOR ALL TABLES` shorthand and `pubviaroot` to publish partitioned tables under the root schema. The publication says nothing about where changes go; it is purely a filter specification on the publisher side.

**PostgreSQL 18:** Publications gain a `publish_generated_columns` option that controls whether the replicated row image includes generated column values. By default, pgoutput excludes generated columns, matching the pre-18 behavior. Setting the option causes pgoutput to serialize the computed values alongside the base columns.

A **subscription** connects a specific publication on a remote server to a local database. Each subscription owns a replication slot on the publisher. This slot serves two purposes. It ensures the server retains WAL back to the last position the subscriber confirmed (stored as `confirmed_flush_lsn` in `ReplicationSlot`). It also gives the walsender a stable handle for the decoding context. PostgreSQL tracks the subscription's state in `pg_subscription` and per-table state in `pg_subscription_rel`.

### Row filters and column lists

PostgreSQL 15 added two finer-grained controls. A **row filter** is a `WHERE`-style expression attached to a publication for a specific table. pgoutput replicates only rows satisfying the expression. **Column lists** restrict replication to a named subset of columns. pgoutput evaluates both, in `pgoutput_row_filter()` and `pgoutput_column_list_init()` respectively. The `RelationSyncEntry` caches the compiled `ExprState` for row filter evaluation, one per operation type (INSERT, UPDATE, DELETE), because update and delete expressions have additional constraints around replica identity columns.

Replica identity determines what old-tuple data appears in UPDATE and DELETE change events. The `relreplident` column of `pg_class` drives this:

| Value | Meaning |
|---|---|
| `REPLICA_IDENTITY_DEFAULT` (`'d'`) | Primary key columns |
| `REPLICA_IDENTITY_NOTHING` (`'n'`) | No old-tuple data (prevents UPDATE/DELETE replication) |
| `REPLICA_IDENTITY_FULL` (`'f'`) | Entire old row |
| `REPLICA_IDENTITY_INDEX` (`'i'`) | Nominated unique index columns |

Without a replica identity, the subscriber cannot match the incoming change to an existing row. As a result, it rejects updates and deletes on such tables.

## The Apply Worker

On the subscriber, the **apply worker** drives logical replication. It is a background worker process launched for each active subscription. The apply worker connects to the publisher using the walsender protocol, sends `START_REPLICATION SLOT ... LOGICAL` with the pgoutput plugin and the publication name list, and then processes the decoded change stream.

Changes arrive as the logical replication protocol messages (`BEGIN`, `RELATION`, `INSERT`, `UPDATE`, `DELETE`, `COMMIT`, etc.) defined in `logicalproto.h`. The apply worker translates each message into a standard executor operation — `heap_insert`, `heap_update`, `heap_delete` — using the same paths as ordinary DML. This means all triggers, constraint checks, row security policies, and expression indexes fire normally on the subscriber. That is both a correctness guarantee and a performance consideration.

The apply worker tracks its progress by sending `Standby Status Update` messages back to the publisher, advancing `confirmed_flush_lsn` on the replication slot. The publisher uses this to know which WAL segments can be recycled.

**PostgreSQL 17:** Apply workers can use hash indexes — not just btree — to locate the target row on the subscriber when applying UPDATE and DELETE changes. Tables with a hash index on the replica identity key benefit from O(1) lookups instead of sequential scans or btree traversal.

PostgreSQL 15 introduced **parallel apply workers** for high-throughput subscriptions. When streaming mode is enabled and the apply worker detects that changes for multiple transactions are available simultaneously, it can delegate transactions to a pool of parallel workers. Each worker applies one transaction independently.

**PostgreSQL 18:** Logical replication conflict detection is improved. New columns in `pg_stat_subscription_stats` expose conflict counts and types, making it easier to monitor and diagnose apply conflicts without relying solely on server logs.

## Initial Table Sync

When an operator first creates a subscription or adds a table to an existing subscription, the subscriber does not yet have any data for that table. A dedicated **tablesync worker** handles the initial copy: it opens a transaction on the publisher, takes a snapshot, runs a `COPY` to export the full table, and ships that to the subscriber. Once the copy completes, the tablesync worker transitions the table through a state machine tracked in `pg_subscription_rel`:

| State | Meaning |
|---|---|
| `'i'` (`SUBREL_STATE_INIT`) | Initializing, no data yet |
| `'d'` (`SUBREL_STATE_DATASYNC`) | COPY in progress |
| `'f'` (`SUBREL_STATE_FINISHEDCOPY`) | COPY done, catching up |
| `'s'` (`SUBREL_STATE_SYNCDONE`) | Sync complete, LSN recorded |
| `'c'` (`SUBREL_STATE_CATCHUP`) | Applying incremental changes |
| `'r'` (`SUBREL_STATE_READY`) | Table in sync, apply worker takes over |

The handoff from tablesync to apply worker is delicate. While the copy is running, the apply worker continues streaming changes for other tables. The replication slot buffers changes to the table being copied. After the copy finishes, the tablesync worker replays those buffered changes until it has caught up past the LSN at which the copy snapshot was taken. It then marks the table `READY` and exits. From that point on, the main apply worker handles all incremental changes for the table.

**PostgreSQL 17:** The `pg_createsubscriber` utility automates converting a physical standby into a logical subscriber. Rather than standing up a fresh instance and running initial table sync from scratch, `pg_createsubscriber` reuses the standby's already-replicated data, creates the necessary replication slots and subscriptions on the publisher, and leaves the converted instance ready to apply incremental logical changes — avoiding a full base backup.

## Replication Slots and WAL Retention

A replication slot (`ReplicationSlot`) anchors logical decoding to a specific point in the WAL stream. Its two critical LSNs are `restart_lsn`, the oldest WAL position the slot might still need for decoding, and `confirmed_flush_lsn`, the latest position the consumer has explicitly acknowledged. The server refuses to remove WAL segments ahead of any slot's `restart_lsn`, regardless of checkpoints or archive commands. An inactive slot with a stale `restart_lsn` will cause unbounded WAL accumulation — one of the more common operational hazards of logical replication.

Decoding requires `wal_level = logical` because at lower levels the WAL omits the tuple data needed to reconstruct row images. The publisher creates the slot itself. The subscriber never touches WAL directly.

**PostgreSQL 17:** An operator can mark logical replication slots as failover candidates via the `failover` flag on the slot. A dedicated slot-sync worker on a physical standby mirrors those flagged slots. On promotion, the new primary inherits the slots with their `confirmed_flush_lsn` intact, and downstream subscribers can reconnect without data loss or re-synchronization. **PostgreSQL 17** also preserves logical replication slot state across `pg_upgrade`: when upgrading both publisher and subscriber clusters from PG 17 or later, slots on the publisher and full subscription state on the subscriber survive the upgrade without manual recreation.

## Known Limitations

Logical replication deliberately excludes several categories of change that either cannot be decoded from WAL or whose semantics do not transfer cleanly across independent catalog namespaces:

- **Logical replication does not replicate DDL.** An operator must apply schema changes manually or via a separate tooling layer. The subscriber's table structure must match (or be compatible with) the publisher's at the time changes arrive.
- **Logical replication does not replicate sequences.** WAL tracks sequence values as [[subsystems/transactions/hint-bits|hint bits]], not logical operations. The concepts of "next value" differ between independent sequences.
- **Logical replication does not replicate large objects.** `FOR ALL TABLES` publications exclude the `pg_largeobject` system table.
- **Logical replication excludes temporary and unlogged tables.** Temporary tables are session-local. Unlogged tables intentionally skip WAL.
- **Not all data types work with all output plugins.** pgoutput's binary protocol handles all built-in types, but custom plugins using text format may struggle with types lacking a stable text representation.
- **Replica identity constraints apply to UPDATE and DELETE.** Tables without a primary key or suitable unique index require `REPLICA IDENTITY FULL` (which ships every column as the old-image) or will refuse to replicate updates and deletes.

## Related Topics

- [[subsystems/replication/logical-decoding|Logical Decoding]] — the WAL-reading and ReorderBuffer infrastructure that logical replication is built on top of
- [[subsystems/replication/slots|Replication Slots]] — slot mechanics, WAL retention, and the `restart_lsn` / `confirmed_flush_lsn` lifecycle
- [[subsystems/replication/output-plugins|Output Plugins]] — writing custom output plugins using the `OutputPluginCallbacks` API
- [[subsystems/replication/subscriptions|Subscriptions]] — subscription management, tablesync workers, and `pg_subscription_rel` state machine
- [[subsystems/replication/logical-conflicts|Logical Conflicts]] — conflict detection and resolution when apply workers encounter constraint violations
- [[subsystems/replication/parallel-apply|Parallel Apply]] — parallel apply workers for high-throughput subscription workloads
- [[subsystems/catalog/pg-publication|pg_publication]] — catalog layout for publications, including `puballtables`, `pubviaroot`, and per-table filters
- [[subsystems/replication/streaming|Streaming Replication]] — physical replication via WAL streaming, the byte-level counterpart to logical decoding
- [[subsystems/wal/overview|WAL Overview]] — WAL structure and record format that the logical decoder reads
- [[subsystems/transactions/mvcc|MVCC]] — snapshot mechanics used by `snapbuild.c` to build the catalog snapshot for decoding
- [[subsystems/storage/buffer-manager|Buffer Manager]] — buffer management for tuple access during apply-worker DML
- [[code-paths/insert|INSERT]] — the executor path that apply workers use for INSERT changes

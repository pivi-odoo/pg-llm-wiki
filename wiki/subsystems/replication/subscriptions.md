---
title: Logical Replication Subscriptions
aliases:
  - subscription
  - pg_subscription
  - apply worker
  - tablesync worker
  - logical subscriber
  - CREATE SUBSCRIPTION
tags:
  - symptom/replication-lag
source_files:
  - src/backend/catalog/pg_subscription.c
  - src/include/catalog/pg_subscription.h
symbols:
  - Subscription
  - Form_pg_subscription
  - GetSubscription
  - DisableSubscription
  - AddSubscriptionRelState
  - UpdateSubscriptionRelState
  - GetSubscriptionRelState
  - RemoveSubscriptionRel
  - GetSubscriptionRelations
  - LOGICALREP_TWOPHASE_STATE_DISABLED
  - LOGICALREP_TWOPHASE_STATE_PENDING
  - LOGICALREP_TWOPHASE_STATE_ENABLED
  - LOGICALREP_STREAM_OFF
  - LOGICALREP_STREAM_ON
  - LOGICALREP_STREAM_PARALLEL
  - SUBREL_STATE_READY
---

A logical replication subscription is the subscriber side of the [[subsystems/replication/logical|logical replication]] pipeline: it holds the connection credentials, slot reference, and behavioral options that allow a background worker to pull decoded changes from a remote publisher and apply them as ordinary DML. Every subscription spawns at least one long-running apply worker. Each table that needs initial data gets its own short-lived tablesync worker. Together they take a remote publication and turn it into a continuously synchronized local copy.

## The pg_subscription Catalog

`pg_subscription` is a shared catalog (OID 6100), meaning it lives in `pg_global` rather than a per-database tablespace. The launcher process needs to read all subscriptions at startup to know which workers to start. This requires shared visibility across databases. The catalog is publicly readable on all columns except `subconninfo`. Access to that column is gated by a `GRANT` in `system_views.sql`, because it may contain passwords.

The key fields of the `Subscription` in-memory struct mirror the catalog row:

| Field | Catalog column | Meaning |
|---|---|---|
| `name` | `subname` | Unique name within the database |
| `conninfo` | `subconninfo` | `libpq` connection string to the publisher |
| `slotname` | `subslotname` | Name of the replication slot on the publisher; NULL when `WITH (slot_name = NONE)` |
| `publications` | `subpublications` | Text array of publication names to subscribe to |
| `enabled` | `subenabled` | Whether the apply worker should be running |
| `binary` | `subbinary` | Request the publisher to send data in binary format |
| `stream` | `substream` | How to handle in-progress (not-yet-committed) transactions |
| `twophasestate` | `subtwophasestate` | Two-phase commit support state |
| `origin` | `suborigin` | Filter changes by replication origin (`none` or `any`) |
| `skiplsn` | `subskiplsn` | Skip all changes whose final LSN is at or below this value |
| `disableonerr` | `subdisableonerr` | Auto-disable subscription when a worker error occurs |

Because `subconninfo` and `subpublications` are variable-length, the catalog uses a [[subsystems/storage/toast|TOAST]] table (`pg_subscription_toast`, OID 4183) for overflow storage. `GetSubscription()` fetches both fixed and variable fields from the syscache in a single heap tuple lookup. This populates the `Subscription` struct that workers hold throughout their lifetimes.

`pg_subscription` has two unique indexes: one on `oid` and one on `(subdbid, subname)`, enforcing that subscription names are unique within a database while allowing the same name in different databases on the same server.

## Two Types of Workers

Every active subscription runs two distinct kinds of background workers, each with a different scope and lifetime.

### The Apply Worker

The apply worker is the long-running process that streams [[subsystems/wal/overview|WAL]] changes from the publisher. It connects to the publisher using `subconninfo`, issues `START_REPLICATION SLOT subslotname LOGICAL` with the pgoutput plugin, and continuously processes the decoded change stream. Changes arrive as logical replication protocol messages (`BEGIN`, `RELATION`, `INSERT`, `UPDATE`, `DELETE`, `COMMIT`, and so on). The apply worker applies them as ordinary heap DML — the same executor paths used by interactive queries. This means triggers, constraints, row security policies, and generated columns all fire on the subscriber exactly as they would for a local write.

The apply worker sends `Standby Status Update` messages back to the publisher to advance `confirmed_flush_lsn` on the replication slot. That advance tells the publisher which [[subsystems/wal/overview|WAL]] segments can be recycled. If the apply worker is killed or the subscription is disabled, the slot retains its position, allowing the worker to resume from where it left off when restarted.

### Tablesync Workers

When a subscription is first enabled, or when tables are added via `ALTER SUBSCRIPTION ... REFRESH PUBLICATION`, each table that lacks data on the subscriber gets a dedicated tablesync worker. A tablesync worker is one-shot: it connects to the publisher, opens a consistent transaction using a snapshot, streams the full table via `COPY`, then catches up with any incremental changes that accumulated in the slot during the copy. Once the table's state reaches `SUBREL_STATE_READY` (`'r'`) in `pg_subscription_rel`, the tablesync worker exits and the apply worker takes sole responsibility for incremental changes.

The per-table state machine in `pg_subscription_rel` (`srsubstate`) coordinates the handoff:

| State char | Constant | Meaning |
|---|---|---|
| `'i'` | `SUBREL_STATE_INIT` | Worker not yet started |
| `'d'` | `SUBREL_STATE_DATASYNC` | COPY in progress |
| `'f'` | `SUBREL_STATE_FINISHEDCOPY` | COPY done, catching up on WAL |
| `'s'` | `SUBREL_STATE_SYNCDONE` | Catch-up complete, LSN recorded in `srsublsn` |
| `'c'` | `SUBREL_STATE_CATCHUP` | Transitioning to apply-worker control |
| `'r'` | `SUBREL_STATE_READY` | Fully synchronized, apply worker owns it |

`RemoveSubscriptionRel()` enforces a safety invariant: if a table's `srsubstate` is anything other than `READY`, PostgreSQL blocks dropping the table's relation mapping, unless the subscription itself is also being dropped. This prevents orphaned tablesync slots from accumulating on the publisher.

## Subscription Lifecycle

**`CREATE SUBSCRIPTION`** creates the `pg_subscription` row with `subenabled = true`, establishes a replication slot on the publisher (unless `slot_name = NONE` is specified), inserts `pg_subscription_rel` rows for all covered tables in state `INIT`, and signals the launcher to start the apply worker. If `connect = false` is given, PostgreSQL creates no slot and attempts no initial sync — useful when copying slot state from an existing physical standby.

**`ALTER SUBSCRIPTION ... DISABLE`** sets `subenabled = false` and calls `DisableSubscription()`, which updates the catalog and sends a signal to terminate the apply worker. `ALTER SUBSCRIPTION ... DISABLE` leaves the replication slot on the publisher intact. It also preserves `confirmed_flush_lsn`, so the subscription can resume cleanly on re-enable.

**`ALTER SUBSCRIPTION ... ENABLE`** sets `subenabled = true` and signals the launcher to restart the apply worker from the slot's last confirmed position.

**`ALTER SUBSCRIPTION ... REFRESH PUBLICATION`** re-queries the publisher for the current set of published tables and reconciles the difference with `pg_subscription_rel`. Newly discovered tables get `INIT` entries and fresh tablesync workers. `REFRESH PUBLICATION` drops tables no longer published from the state table.

**`DROP SUBSCRIPTION`** disables the worker, drops the replication slot on the publisher, and deletes all `pg_subscription` and `pg_subscription_rel` rows. If the publisher is unreachable and the slot cannot be dropped remotely, `DROP SUBSCRIPTION` with `slot_name = NONE` or a subsequent manual `pg_drop_replication_slot()` on the publisher is necessary to avoid WAL accumulation. See [[subsystems/replication/slots|replication slots]] for the operational implications of orphaned slots.

## Binary Mode

When `subbinary = true` (set with `CREATE SUBSCRIPTION ... WITH (binary = true)`), the subscriber instructs the publisher's pgoutput plugin to encode column values in the type's binary send/receive format rather than its text representation. The subscriber deserializes them with the corresponding `receive` function.

Binary mode is faster — it skips text-to-binary conversion on both ends — and is lossless for types like `float4`, `float8`, and `numeric` where the text representation may round-trip with precision loss. The trade-off is portability: binary format is not guaranteed stable across PostgreSQL major versions or between different hardware architectures (endianness, alignment). Binary mode is appropriate when publisher and subscriber run the same major version and the same architecture. This is the common case for live-migration or read-scaling setups. Cross-version upgrades should use text mode.

## Streaming In-Progress Transactions

The `substream` field (`LOGICALREP_STREAM_OFF`, `LOGICALREP_STREAM_ON`, or `LOGICALREP_STREAM_PARALLEL`) controls how the apply worker handles transactions that the publisher has started but not yet committed.

Without streaming (`LOGICALREP_STREAM_OFF`, the historical default), the reorder buffer on the publisher accumulates the entire transaction in memory, or spills it to disk past `logical_decoding_work_mem`. The subscriber receives nothing until the commit record arrives. For large transactions this can cause significant latency spikes and [[subsystems/executor/work-mem-and-spill|work_mem]]-class memory pressure on the publisher.

With `LOGICALREP_STREAM_ON`, the publisher begins streaming the transaction's changes before commit. The subscriber writes them to a temporary file and applies the whole batch atomically when the commit arrives. This trades publisher memory for subscriber disk I/O, but the apply still happens as one atomic operation.

With `LOGICALREP_STREAM_PARALLEL` (the default since PostgreSQL 18), the publisher streams in-progress transactions directly to a parallel apply worker that applies changes immediately. If the transaction is later rolled back, the parallel worker rolls back too. This mode gives the lowest apply latency for large transactions and is now the recommended default.

## Two-Phase Commit Support

`subtwophasestate` tracks whether the subscription is able to replicate transactions that use `PREPARE TRANSACTION` / `COMMIT PREPARED`. The three states defined in `pg_subscription.h` are:

| Constant | Char | Meaning |
|---|---|---|
| `LOGICALREP_TWOPHASE_STATE_DISABLED` | `'d'` | Two-phase replication off (default) |
| `LOGICALREP_TWOPHASE_STATE_PENDING` | `'p'` | Requested but initial sync not yet complete |
| `LOGICALREP_TWOPHASE_STATE_ENABLED` | `'e'` | Active: prepared transactions are replicated |

The pending state exists because PostgreSQL cannot safely activate two-phase replication until all tables are in `SUBREL_STATE_READY`. The apply worker automatically transitions from `PENDING` to `ENABLED` once the last tablesync worker exits. While in `PENDING` state, prepared transactions fall back to the normal (non-two-phase) apply path so no changes are lost during the sync window.

Two-phase replication matters when the publisher uses distributed transactions coordinated across multiple databases or systems. In that scenario, the subscriber must mirror the same prepare/commit boundary. For ordinary single-database workloads it adds overhead with no benefit.

## Monitoring

Three system views expose subscription runtime state:

**`pg_stat_subscription`** shows one row per active worker (the apply worker plus any running tablesync workers). The key columns are `last_msg_send_time`, `last_msg_receipt_time`, and `latest_end_lsn`, useful for diagnosing latency. `relid` identifies which table a tablesync worker is processing.

**`pg_subscription_rel`** is the persistent state table discussed above. Querying it gives the synchronization state of every table under every subscription in the database. A table stuck in `'d'` or `'f'` for an unusually long time indicates a tablesync worker that has stalled or crashed.

**`pg_replication_slots`** on the **publisher** shows the slot created for the subscription. The gap between `confirmed_flush_lsn` and the current WAL write position is the subscription's replication lag in bytes. The `active` column shows whether the apply worker is currently connected. An inactive slot with a growing lag is a sign that the subscription is disabled or the worker has crashed; see [[subsystems/replication/slots|replication slots]] for WAL-retention consequences.

## Related Topics

- [[subsystems/replication/logical|Logical Replication]] — the publisher, decoding pipeline, pgoutput plugin, and WAL-to-change-stream mechanics
- [[subsystems/replication/slots|Replication Slots]] — slot lifecycle, WAL retention, and the risk of inactive slots
- [[subsystems/replication/parallel-apply|Parallel Apply]] — how parallel apply workers process streamed transactions concurrently
- [[subsystems/replication/logical-conflicts|Logical Replication Conflicts]] — conflict detection and resolution on the subscriber
- [[subsystems/replication/replication-origins|Replication Origins]] — the origin-tracking mechanism that prevents change loops in multi-master setups

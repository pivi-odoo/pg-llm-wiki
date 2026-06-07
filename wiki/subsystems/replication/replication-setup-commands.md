---
title: "Logical Replication Setup Commands"
aliases:
  - CREATE PUBLICATION internals
  - CREATE SUBSCRIPTION internals
  - publication DDL
  - subscription DDL
  - publicationcmds
  - subscriptioncmds
source_files:
  - src/backend/commands/publicationcmds.c
  - src/backend/commands/subscriptioncmds.c
  - src/include/commands/publicationcmds.h
  - src/include/commands/subscriptioncmds.h
symbols:
  - CreatePublication
  - AlterPublicationOptions
  - TransformPubWhereClauses
  - CheckPubRelationColumnList
  - pub_rf_contains_invalid_column
  - PublicationAddTables
  - CreateSubscription
  - AlterSubscription
  - AlterSubscription_refresh
  - DropSubscription
  - fetch_table_list
  - parse_subscription_options
---

`CREATE PUBLICATION`, `ALTER PUBLICATION`, `CREATE SUBSCRIPTION`, and `ALTER SUBSCRIPTION` are the DDL surface of logical replication. Their implementations in `publicationcmds.c` and `subscriptioncmds.c` do more than write catalog rows: they validate filtering expressions, manage locking across partitioned table hierarchies, establish remote connections to publisher clusters, and coordinate replication slot creation.

## Publication commands (publicationcmds.c)

### CREATE PUBLICATION

`CreatePublication()` processes a `CreatePublicationStmt` in several phases:

1. **Option parsing**: `parse_publication_options()` validates `publish` (the set of allowed DML operations: insert, update, delete, truncate) and `publish_via_partition_root`. The defaults enable all four DML operation types.

2. **Catalog write**: `CreatePublication()` inserts a new `pg_publication` row with the parsed flags. The OID becomes the publication identifier for all subsequent references.

3. **Table and schema enumeration**: `PublicationAddTables()` / `PublicationAddSchemas()` enumerate the listed relations and schemas. For each relation, `PublicationAddTables()` inserts a `pg_publication_rel` row. For each schema, `PublicationAddSchemas()` inserts a `pg_publication_namespace` row. `FOR ALL TABLES` sets `puballtables` in the `pg_publication` row and skips per-table tracking.

4. **Row filter transformation**: if any table has a `WHERE` clause, `TransformPubWhereClauses()` analyzes the raw expression against each relation's schema. `check_simple_rowfilter_expr()` validates the resulting expression tree. It walks the tree to reject mutable functions, user-defined functions, subqueries, and any construct that would make filter evaluation on the publisher side unsafe or unpredictable. `TransformPubWhereClauses()` stores the compiled filter in `pg_publication_rel.prqual`.

5. **Column list validation**: `CheckPubRelationColumnList()` ensures that any column list includes all replica identity columns. The publication must replicate a replica identity column so that the subscriber can locate the matching row for UPDATE and DELETE operations.

6. **Locking**: `CreatePublication()` takes `ShareUpdateExclusiveLock` on each listed table. This conflicts with concurrent `ALTER TABLE` and ensures the table schema does not change between validation and catalog write.

For partitioned tables, `publish_via_partition_root = false` (the default) publishes each partition individually. With `publish_via_partition_root = true`, PostgreSQL publishes changes to any partition using the root table's schema and row filter. This simplifies the subscriber-side table mapping but requires the subscriber to have the same partitioned table structure.

### Row filter and column list constraints

Row filters are subject to additional constraints beyond standard SQL expression analysis. `pub_rf_contains_invalid_column()` and `pub_collist_contains_invalid_column()` verify that:

- For `UPDATE` and `DELETE`, the filter and column list must include the replica identity columns. If not, the subscriber cannot uniquely identify the row to modify.
- For partitioned tables with `publish_via_partition_root = false`, PostgreSQL evaluates the row filter against the partition's columns, not the root's. The filter must be compatible with the partition's schema.

The `rf_context` struct carries the replica identity bitmask (`bms_replident`) assembled from `pg_index` rows. PostgreSQL uses this bitmask to verify that the filter references only allowed columns.

### ALTER PUBLICATION

`AlterPublicationOptions()` handles option changes (`SET (publish = ...)`, etc.) and writes the updated `pg_publication` row. It also sends a cache invalidation so that walsender processes re-read the publication on their next change.

`PublicationAddTables()` / `PublicationDropTables()` handle altering the table set (`ADD TABLE`, `DROP TABLE`, `SET TABLE`). PostgreSQL does not interrupt walsender processes that are currently encoding changes for this publication mid-transaction — they continue with the old publication definition until their current transaction completes, then re-read the catalog.

## Subscription commands (subscriptioncmds.c)

### CREATE SUBSCRIPTION

`CreateSubscription()` is significantly more complex than its publication counterpart because it involves a remote connection to the publisher:

1. **Option parsing**: `parse_subscription_options()` processes a bitmask of `SUBOPT_*` flags, validating the `connection`, `publication`, `slot_name`, `copy_data`, `streaming`, `binary`, `two_phase`, and other parameters.

2. **Connection**: if `connect = true` (the default), `CreateSubscription()` opens a `WalReceiverConn` to the publisher via `walrcv_connect()`. This uses the `conninfo` string from the `CONNECTION` clause. PostgreSQL stores that string in `pg_subscription.subconninfo`. `CreateSubscription()` validates the connection string and checks the remote PostgreSQL version.

3. **Publication existence check**: `check_publications()` queries the publisher to verify that the named publications exist, producing appropriate errors if they do not.

4. **Replication slot creation**: if `create_slot = true`, `walrcv_create_slot()` creates a logical replication slot on the publisher for the `pgoutput` plugin. The slot name defaults to the subscription name. `CreateSubscription()` stores the initial `confirmed_flush_lsn` returned by the slot creation in `pg_subscription_rel` for each table as the starting LSN for initial sync.

5. **Initial table list**: `fetch_table_list()` sends `SELECT ... FROM pg_publication_tables` to the publisher to enumerate the current set of replicated tables. For each table, `CreateSubscription()` inserts a `pg_subscription_rel` row with state `SUBREL_STATE_INIT` or `SUBREL_STATE_READY` (the latter if `copy_data = false`).

6. **Catalog write**: `CreateSubscription()` writes the `pg_subscription` row and sends a logical replication launcher notification (`logicalrep_worker_wakeup_msg`). This prompts the launcher to start the apply worker.

### ALTER SUBSCRIPTION REFRESH PUBLICATION

`AlterSubscription_refresh()` synchronises the local table list with the current publication contents on the publisher:

1. Reconnects to the publisher and calls `fetch_table_list()` to get the current set of replicated tables.
2. Compares this with the existing `pg_subscription_rel` rows.
3. Adds `pg_subscription_rel` rows for newly published tables (state `SUBREL_STATE_INIT`).
4. Removes `pg_subscription_rel` rows for tables that are no longer in the publication.

Newly added tables trigger a new tablesync worker to perform initial data copy. For removed tables, `AlterSubscription_refresh()` signals their tablesync worker (if running) to exit.

### DROP SUBSCRIPTION

`DropSubscription()` performs cleanup in the right order to avoid leaving orphaned resources:

1. Acquires `AccessExclusiveLock` on `pg_subscription` to prevent concurrent apply workers from re-reading the row.
2. Disables the subscription. This signals the apply worker to exit.
3. If the subscription has a managed slot (i.e. `subslotname` is set), connects to the publisher and drops the replication slot via `ReplicationSlotDropAtPubNode()`. If the connection fails (e.g. publisher is down), the drop proceeds locally but logs a warning that an operator must drop the slot manually on the publisher to free WAL retention.
4. Deletes `pg_subscription` and all `pg_subscription_rel` rows.
5. Cleans up any replication origins associated with the subscription.

The check for an inaccessible publisher is deliberate: a subscriber being decommissioned must not block indefinitely waiting for a publisher that may be permanently gone.

## Related Topics

- [[subsystems/replication/logical|Logical Replication]]
- [[subsystems/replication/slots|Replication Slots]]
- [[subsystems/replication/logical-decoding|Logical Decoding]]
- [[subsystems/catalog/pg-publication|pg_publication catalog]]

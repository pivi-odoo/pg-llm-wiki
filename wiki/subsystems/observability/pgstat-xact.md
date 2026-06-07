---
title: "Statistics System Transactional Integration"
aliases:
  - pgstat_xact
  - stats transactional
  - AtEOXact_PgStat
  - PgStat_SubXactStatus
  - pgstat_create_transactional
  - pgstat_drop_transactional
tags:
  - theme/durability
source_files:
  - src/backend/utils/activity/pgstat_xact.c
  - src/include/pgstat.h
  - src/include/utils/pgstat_internal.h
symbols:
  - AtEOXact_PgStat
  - AtEOSubXact_PgStat
  - AtPrepare_PgStat
  - PostPrepare_PgStat
  - pgstat_create_transactional
  - pgstat_drop_transactional
  - pgstat_get_transactional_drops
  - pgstat_execute_transactional_drops
  - pgstat_get_xact_stack_level
  - PgStat_SubXactStatus
---

The statistics system tracks object-level statistics (table row counts, index scans, etc.) in shared memory. A complication arises with transactions: if a transaction creates a table and later rolls back, PostgreSQL must also discard any statistics accumulated for that table. Conversely, if a committed transaction drops a table, PostgreSQL must clean up its statistics entry. `src/backend/utils/activity/pgstat_xact.c` provides the hooks that integrate statistics management with the transaction machinery.

## The Subtransaction Stack

Each backend maintains a stack of `PgStat_SubXactStatus` records, one per live subtransaction nesting level. The top of the stack corresponds to the innermost active savepoint. Each record holds:

- `nest_level` — the transaction nesting depth (1 = top-level transaction)
- `pending_drops` — a doubly-linked list of `PgStat_PendingDroppedStatsItem` records
- `first` — pending relation stats changes at this nesting level (maintained in `pgstat_xact_relations.c`)

`pgstat_get_xact_stack_level()` lazily creates a new stack frame whenever a statistics operation first touches a given nesting level.

## Transactional Create and Drop

**`pgstat_create_transactional(kind, dboid, objoid)`** runs when a transaction creates a database object (table, index, etc.). It records the creation on the pending-drops list with `is_create = true`. If the transaction aborts, PostgreSQL drops the stats entry for the newly created object — it would be meaningless for a nonexistent object to persist in the stats system.

**`pgstat_drop_transactional(kind, dboid, objoid)`** runs when a transaction drops an object. It records the drop with `is_create = false`. If the transaction commits, PostgreSQL drops the stats entry. If the transaction aborts, the entry survives — the drop never happened.

The symmetry is intentional: `is_create` and `is_drop` are mirror images of each other. The cleanup logic simply checks `isCommit XOR is_create` to decide whether to drop the entry.

## Transaction End

**`AtEOXact_PgStat(isCommit, parallel)`** is the main cleanup hook, called from `access/transam/xact.c` at the end of every top-level transaction. It:

1. Calls `AtEOXact_PgStat_Database()` to flush commit/rollback counters.
2. Calls `AtEOXact_PgStat_Relations()` to flush pending relation stats.
3. Calls `AtEOXact_PgStat_DroppedStats()` to act on the pending-drops list:
   - On commit: drop stats for objects that were dropped in the transaction (`!is_create` entries).
   - On abort: drop stats for objects that were created in the transaction (`is_create` entries).
4. Clears the stats snapshot so stale cached view data is not returned.

PostgreSQL excludes parallel workers from transaction-level aggregation — it merges their stats into the leader's counters through a different path.

## Subtransaction End

**`AtEOSubXact_PgStat(isCommit, nestDepth)`** handles savepoint commit and rollback. On subtransaction commit, pending-drop records bubble up to the parent's list — the enclosing transaction is still live and must make the final decision. On subtransaction abort, PostgreSQL immediately drops stats for objects created within the subtransaction, since those objects no longer exist.

```
Transaction level 1
  Savepoint A (level 2) — creates table T
    Savepoint B (level 3) — drops table U
    ROLLBACK TO B         → drop stats for U (never happened); keep stats for T
  RELEASE A               → T's pending_drop bubbles to level 1
ROLLBACK                  → drop stats for T (never existed)
```

## 2PC Integration

Two-phase commit requires PostgreSQL to encode the dropped-stats list in the WAL record, so that recovery and standby processing know which stats to remove. **`AtPrepare_PgStat()`** gathers pending relation stats into the prepare record via `AtPrepare_PgStat_Relations()`. **`PostPrepare_PgStat()`** cleans up local state after a successful prepare.

**`pgstat_get_transactional_drops(isCommit, &items)`** extracts the list of pending drops as an `xl_xact_stats_item` array for inclusion in `XLOG_XACT_COMMIT` / `XLOG_XACT_ABORT` records. **`pgstat_execute_transactional_drops()`** acts on those items during WAL replay (`xact_redo_commit`, `xact_redo_abort`) and during `COMMIT PREPARED` / `ABORT PREPARED` processing. Without this WAL integration, a crash between a `DROP TABLE` commit and the stats cleanup would leave a stale stats entry pointing at a nonexistent table OID. On a standby, replay applies the drop WAL record. Because the same record carries the stats cleanup instruction, replaying the drop also triggers the stats cleanup. This keeps the stats system consistent across crashes and standbys without a separate crash-recovery scan.

## Related Topics

- [[subsystems/observability/overview|Statistics Collector Overview]]
- [[subsystems/observability/pgstat-shmem|Stats Shared Memory]]
- [[subsystems/observability/pg-stat-database|pg_stat_database]]
- [[subsystems/transactions/transaction-lifecycle|Transaction Lifecycle]]
- [[subsystems/wal/overview|WAL Overview]]

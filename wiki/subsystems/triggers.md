---
title: Triggers
aliases:
  - trigger system
  - trigger firing
  - BEFORE trigger
  - AFTER trigger
source_files:
  - src/backend/commands/trigger.c
  - src/include/commands/trigger.h
  - src/include/utils/reltrigger.h
  - src/backend/executor/nodeModifyTable.c
symbols:
  - TriggerData
  - TriggerDesc
  - Trigger
  - AfterTriggersData
  - AfterTriggerEventData
  - AfterTriggerSharedData
  - AfterTriggersTableData
  - TransitionCaptureState
  - ExecBRInsertTriggers
  - ExecARInsertTriggers
  - ExecBSInsertTriggers
  - ExecASInsertTriggers
  - AfterTriggerEndQuery
  - AfterTriggerFireDeferred
  - AfterTriggerSaveEvent
  - ExecCallTriggerFunc
---

# Triggers

Triggers let user-defined logic run automatically in response to data-modification events. They enforce invariants that CHECK constraints cannot express, replicate rows to audit tables, and implement the referential integrity machinery that backs every foreign key. Because they execute within the modifying transaction, they see changes before those changes become visible to other sessions. Any error they raise rolls back the statement that caused them.

The trigger system is implemented almost entirely in `src/backend/commands/trigger.c`, with data-structure declarations in `src/include/commands/trigger.h` and `src/include/utils/reltrigger.h`.

## The Taxonomy

Every trigger is classified along three independent dimensions:

**Timing — BEFORE, AFTER, or INSTEAD OF.** BEFORE triggers run before the storage engine touches the row. AFTER triggers run after the heap write is committed to the page. INSTEAD OF is a special timing available only on views. The trigger completely replaces the write; without it, the write would fail because the target is not a table.

**Granularity — FOR EACH ROW or FOR EACH STATEMENT.** Row-level triggers fire once per affected row and receive the old and new tuple. Statement-level triggers fire once for the whole statement, regardless of how many rows it touches. They receive no individual row data, except through transition tables (described below).

**Event — INSERT, UPDATE, DELETE, or TRUNCATE.** A single trigger definition can cover multiple events. INSTEAD OF is restricted to INSERT, UPDATE, and DELETE on views. TRUNCATE supports only statement-level triggers. The other events support both granularities.

PostgreSQL encodes the combination of these three dimensions in `pg_trigger.tgtype` as a bitmask. At runtime, `TriggerDesc` (defined in `reltrigger.h`) caches a boolean flag for every possible combination — `trig_insert_before_row`, `trig_update_after_statement`, `trig_truncate_before_statement`, and so on. This lets the executor skip searching the trigger array when no trigger of a given class is registered.

```c
typedef struct TriggerDesc
{
    Trigger    *triggers;        /* array of Trigger structs */
    int         numtriggers;     /* number of array entries */
    bool        trig_insert_before_row;
    bool        trig_insert_after_row;
    bool        trig_insert_instead_row;
    bool        trig_insert_before_statement;
    bool        trig_insert_after_statement;
    /* ... analogous flags for UPDATE, DELETE, TRUNCATE ... */
    bool        trig_insert_new_table;   /* transition table flags */
    bool        trig_update_old_table;
    bool        trig_update_new_table;
    bool        trig_delete_old_table;
} TriggerDesc;
```

## Firing Sequence for a Single Statement

The executor brackets every data-modification statement with calls to the trigger machinery. For an INSERT, the sequence is:

```
ExecBSInsertTriggers    ← BEFORE STATEMENT triggers (fire once, synchronously)

for each row to insert:
    ExecBRInsertTriggers    ← BEFORE ROW triggers (fire synchronously; may modify row)
    heap_insert / table_tuple_insert
    ExecARInsertTriggers    ← enqueue AFTER ROW events

ExecASInsertTriggers    ← enqueue AFTER STATEMENT event
AfterTriggerEndQuery    ← dequeue and fire all immediate AFTER events
```

The asymmetry between BEFORE and AFTER is fundamental. BEFORE triggers fire directly during the row loop. The executor waits for each one to return before proceeding. AFTER triggers are not fired immediately. Instead, PostgreSQL appends their events to the per-query event list maintained in `AfterTriggersData.query_stack[query_depth].events`. Only when `ExecutorFinish` calls `AfterTriggerEndQuery` does the system scan that list, fire the immediate-mode AFTER triggers, and transfer any deferred events to the transaction-level deferred list.

The BEFORE STATEMENT guard (`before_stmt_triggers_fired`) prevents the same statement triggers from firing more than once when the executor visits multiple target partitions. The executor records each combination of relation OID and command type on first fire.

## BEFORE ROW Triggers: Modifying and Suppressing Rows

The ability to change the row being inserted or updated is the distinguishing property of BEFORE ROW triggers. `ExecBRInsertTriggers` calls each matching trigger via `ExecCallTriggerFunc`, which passes a `TriggerData` context through the function call mechanism. The trigger function can:

- Return the same tuple unchanged — normal case.
- Return a different tuple — the returned tuple replaces the one being written. The executor calls `ExecForceStoreHeapTuple` to update the slot before the heap write.
- Return NULL — the row is silently discarded. `ExecBRInsertTriggers` returns `false`, signaling the caller to skip the heap insert entirely.

BEFORE STATEMENT triggers cannot return a value; returning a non-NULL pointer raises `TRIGGER_PROTOCOL_VIOLATED`.

For UPDATE, the BEFORE ROW trigger receives both old (`tg_trigtuple`) and new (`tg_newtuple`) rows. The returned tuple becomes the replacement for the new tuple only. The old tuple is not modified.

One constraint worth knowing: a BEFORE ROW trigger on a partitioned table cannot move its returned row to a different partition. If the returned tuple no longer satisfies the partition's constraint, the executor raises an error rather than rerouting the row.

## AFTER ROW Triggers: The Event Queue

AFTER ROW triggers observe the world after the heap write has committed to the page. To achieve this, they are not fired immediately. Instead, `AfterTriggerSaveEvent` records a compact event descriptor into the per-query chunk list.

The event queue is designed for low allocation overhead. Events are stored in contiguous chunks (`AfterTriggerEventChunk`), growing as needed. Each `AfterTriggerEventData` record is just status flags plus up to two `ItemPointerData` CTIDs — enough to re-fetch the before and after images at firing time. For UPDATE events the two CTIDs point to the old and new heap tuple versions. For cross-partition updates, the record also stores the source and destination partition OIDs.

```c
typedef struct AfterTriggerEventData
{
    TriggerFlags ate_flags;       /* DONE/IN_PROGRESS bits, offset to shared data */
    ItemPointerData ate_ctid1;    /* inserted/deleted/old-updated tuple */
    ItemPointerData ate_ctid2;    /* new updated tuple */
    Oid          ate_src_part;    /* cross-partition update only */
    Oid          ate_dst_part;
} AfterTriggerEventData;
```

PostgreSQL stores shared metadata (trigger OID, relation OID, event type, firing cycle ID) in `AfterTriggerSharedData` records, packed at the opposite end of the same chunk. This reduces per-event overhead when multiple rows trigger the same trigger on the same relation.

When `AfterTriggerEndQuery` runs, it calls `afterTriggerMarkEvents` to classify each queued event as either immediately fireable or deferred, then `afterTriggerInvokeEvents` to execute the immediate ones. Deferred events are moved to `afterTriggers.events`, the transaction-level list.

## Transition Tables

Statement-level AFTER triggers can declare transition tables with `REFERENCING OLD TABLE AS old_data NEW TABLE AS new_data`. This gives a trigger a complete view of every row that the statement changed, not just the final count.

`Tuplestorestate` instances, managed in `AfterTriggersTableData`, back transition tables. As each row is processed, `TransitionCaptureState` directs `nodeModifyTable.c` to append the row to the appropriate tuplestore — `old_tuplestore` for DELETE and the old side of UPDATE, `new_tuplestore` for INSERT and the new side of UPDATE. When the AFTER STATEMENT trigger fires, `TriggerData.tg_oldtable` and `tg_newtable` point to these tuplestores.

```c
typedef struct TransitionCaptureState
{
    bool tcs_delete_old_table;
    bool tcs_update_old_table;
    bool tcs_update_new_table;
    bool tcs_insert_new_table;
    TupleTableSlot *tcs_original_insert_tuple;
    struct AfterTriggersTableData *tcs_insert_private;
    struct AfterTriggersTableData *tcs_update_private;
    struct AfterTriggersTableData *tcs_delete_private;
} TransitionCaptureState;
```

PostgreSQL does not permit transition tables for deferred triggers. Because the tuplestores live only until `AfterTriggerEndQuery`, deferring such a trigger to transaction commit would cause it to reference freed memory.

## Deferred Triggers

PostgreSQL does not fire a trigger declared `DEFERRABLE INITIALLY DEFERRED` (or made deferred by `SET CONSTRAINTS ... DEFERRED`) at statement end. Its events accumulate in `afterTriggers.events`, the transaction-level event list, and fire only when pre-commit processing calls `AfterTriggerFireDeferred`.

The `afterTriggerCheckState` function determines, for each event, whether its trigger is currently immediate or deferred. It first checks the `SetConstraintState` for a per-trigger override set by `SET CONSTRAINTS`, then falls back to the trigger's default (`tginitdeferred`). A trigger declared `NOT DEFERRABLE` always returns false immediately, with no further lookup needed.

`AfterTriggerFireDeferred` loops until no pending deferred events remain. This looping is necessary because a deferred trigger can itself queue new deferred events during pre-commit.

Subtransaction handling preserves correctness. `AfterTriggerBeginSubXact` saves the current list tail pointer and `firing_counter`. This lets `AfterTriggerEndSubXact` discard events appended by the aborted subtransaction, by truncating the list back to the saved position.

## The TriggerData Interface

Every trigger function, regardless of language, receives a `TriggerData` node as the `fcinfo->context` pointer. The `CALLED_AS_TRIGGER` macro verifies this before a function uses the data.

```c
typedef struct TriggerData
{
    NodeTag          type;
    TriggerEvent     tg_event;        /* timing, event, row/statement flags */
    Relation         tg_relation;     /* target relation */
    HeapTuple        tg_trigtuple;    /* old row (or row being inserted) */
    HeapTuple        tg_newtuple;     /* new row (UPDATE only) */
    Trigger         *tg_trigger;      /* trigger metadata including tgargs */
    TupleTableSlot  *tg_trigslot;
    TupleTableSlot  *tg_newslot;
    Tuplestorestate *tg_oldtable;     /* OLD TABLE transition data */
    Tuplestorestate *tg_newtable;     /* NEW TABLE transition data */
    const Bitmapset *tg_updatedcols;  /* columns touched by UPDATE */
} TriggerData;
```

`tg_event` is a bitmask. The macros `TRIGGER_FIRED_BY_INSERT`, `TRIGGER_FIRED_FOR_ROW`, `TRIGGER_FIRED_BEFORE`, and `TRIGGER_FIRED_INSTEAD` decode it. PL/pgSQL trigger functions access these through the `TG_OP`, `TG_LEVEL`, and `TG_WHEN` special variables, which the PL/pgSQL runtime reads from `TriggerData`.

`ExecCallTriggerFunc` invokes the trigger function. It executes the function in the per-tuple [[subsystems/memory/contexts|memory context]], so that leaked memory is reclaimed on the next row. The function receives no ordinary arguments. All context comes through `TriggerData`. String arguments declared in `CREATE TRIGGER ... ARGS` are accessible via `tg_trigger->tgargs`.

## Constraint Triggers and Foreign Keys

Constraint triggers are a restricted subclass used internally to implement foreign keys. They are created with `CREATE CONSTRAINT TRIGGER` (or internally by `CREATE FOREIGN KEY`) and are always AFTER ROW triggers. They carry a `tgconstraint` OID linking them to a `pg_constraint` row. They are also deferrable by default.

Every foreign key relationship registers a pair of constraint triggers: one on the referencing table (fires on INSERT/UPDATE of the FK column) and one on the referenced table (fires on UPDATE/DELETE of the PK column). The trigger functions are in `src/utils/adt/ri_triggers.c` and implement the `NO ACTION`, `RESTRICT`, `CASCADE`, `SET NULL`, and `SET DEFAULT` behaviors by executing SPI queries under a snapshot that includes the triggering statement's changes.

Foreign key triggers are deferred when the constraint is deferrable. This lets PostgreSQL postpone the entire validation of a batch of inserts to commit time, allowing circular references to be inserted without ordering.

## Replication Role and Trigger Enabling

The `tgenabled` column of `pg_trigger` controls whether a trigger fires under the current `session_replication_role`. The possible states are:

| `tgenabled` value | Meaning |
|---|---|
| `'O'` (`TRIGGER_FIRES_ON_ORIGIN`) | Fire only when role is ORIGIN or LOCAL |
| `'R'` (`TRIGGER_FIRES_ON_REPLICA`) | Fire only when role is REPLICA |
| `'A'` (`TRIGGER_FIRES_ALWAYS`) | Fire regardless of role |
| `'D'` (`TRIGGER_DISABLED`) | Never fire |

Logical replication subscribers typically run with `session_replication_role = replica`, which suppresses `'O'` triggers (the default for user-defined triggers) and allows `'R'` triggers to run instead. This lets replicas apply the same data transformations that the origin applies without triggering the standard user-defined logic a second time.

The `TriggerEnabled` function checks this state along with any `WHEN` clause expression before dispatching to `ExecCallTriggerFunc`.

## Key Data Structures at a Glance

| Structure | Location | Purpose |
|---|---|---|
| `TriggerDesc` | `reltrigger.h` | Per-relation trigger inventory; bitmask flags for fast skip |
| `Trigger` | `reltrigger.h` | Per-trigger metadata mirroring `pg_trigger` |
| `TriggerData` | `trigger.h` | Context passed to trigger functions |
| `AfterTriggersData` | `trigger.c` | Global singleton; transaction-scoped event queue and SET CONSTRAINTS state |
| `AfterTriggersQueryData` | `trigger.c` | Per-query-depth event list and transition-table registry |
| `AfterTriggerEventData` | `trigger.c` | Compact event record (flags + CTIDs) |
| `AfterTriggerSharedData` | `trigger.c` | Shared metadata for a group of similar events |
| `AfterTriggersTableData` | `trigger.c` | Per-table tuplestores for transition tables |
| `TransitionCaptureState` | `trigger.h` | Routing state for populating transition tuplestores during row processing |

## Related Topics

- [[subsystems/rewriter/rules-vs-triggers]] — Compares rewrite rules and triggers as two mechanisms for intercepting data-modification statements, covering when each is appropriate.
- [[subsystems/plpgsql/trigger-functions]] — How PL/pgSQL implements trigger functions, populates TG_OP/TG_NEW/TG_OLD, and handles the TriggerData interface from the language side.
- [[subsystems/trigger-builtin-functions]] — Built-in trigger functions such as `suppress_redundant_updates_trigger` and `tsvector_update_trigger` that ship with PostgreSQL.
- [[subsystems/event-triggers]] — Event triggers fire on DDL events rather than DML, complementing row/statement triggers with schema-level interception.
- [[subsystems/transactions/deferrable-constraints]] — How deferrable constraint triggers integrate with transaction commit and the SET CONSTRAINTS mechanism shared with foreign key triggers.
- [[subsystems/executor/tuplestore]] — The tuplestore machinery that backs transition tables (OLD TABLE / NEW TABLE) exposed to statement-level AFTER triggers.
- [[subsystems/rewriter/updatable-views]] — INSTEAD OF triggers on views are central to making views updatable; this page covers how the rewriter and trigger system collaborate.
- [[code-paths/insert]] — The full INSERT code path showing where trigger calls are placed in `nodeModifyTable.c`
- [[subsystems/executor/overview]] — How `EState` and `ResultRelInfo` carry trigger state through query execution
- [[subsystems/storage/heap]] — The heap write that BEFORE ROW triggers precede and AFTER ROW triggers follow
- [[subsystems/transactions/mvcc]] — Snapshot visibility and why AFTER triggers see the just-written tuple
- [[subsystems/locking/overview]] — Row-level locks acquired by BEFORE ROW triggers on DELETE and UPDATE

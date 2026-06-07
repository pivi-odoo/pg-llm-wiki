---
title: "NamedTuplestoreScan Executor Node"
aliases:
  - NamedTuplestoreScan
  - NamedTuplestoreScanState
  - ENR scan
  - ephemeral named relation scan
source_files:
  - src/backend/executor/nodeNamedtuplestorescan.c
  - src/include/executor/nodeNamedtuplestorescan.h
  - src/include/nodes/execnodes.h
  - src/include/utils/queryenvironment.h
symbols:
  - NamedTuplestoreScan
  - NamedTuplestoreScanState
  - EphemeralNamedRelationData
  - EphemeralNamedRelationMetadataData
  - QueryEnvironment
  - get_ENR
  - register_ENR
  - ENRMetadataGetTupDesc
  - ExecInitNamedTuplestoreScan
  - ExecReScanNamedTuplestoreScan
---

The `NamedTuplestoreScan` executor node reads rows from an **Ephemeral Named Relation (ENR)** — a named, session-scoped [[subsystems/executor/tuplestore|tuplestore]] registered in the query's `EState` before execution begins. ENRs allow plan nodes to scan an in-memory result set by name without a catalog entry, a heap file, or a shared buffer allocation. The two primary sources are materialized [[sql-features/ctes|CTEs]] and trigger transition tables declared with `REFERENCING OLD TABLE / NEW TABLE`.

## Ephemeral Named Relations

An ENR is a pair of structs: `EphemeralNamedRelationMetadataData` carries the name, an optional relation OID or explicit `TupleDesc`, an enum tag (`ENR_NAMED_TUPLESTORE`), and a row-count estimate. The outer `EphemeralNamedRelationData` wraps the metadata and adds a `void *reldata` pointer. At runtime, this pointer holds the live `Tuplestorestate *`.

ENRs live inside a `QueryEnvironment`, an opaque registry stored in `EState.es_queryEnv`. The `QueryEnvironment` is populated before `ExecutorStart` runs. Plan nodes that run later find their ENR by calling `get_ENR(estate->es_queryEnv, enrname)`.

The parser gives ENR-referenced relations an `RTEKind` of `RTE_NAMEDTUPLESTORE` rather than `RTE_RELATION` or `RTE_CTE`. This tag tells the planner to emit a `NamedTuplestoreScan` plan node carrying the ENR name string.

Multiple `NamedTuplestoreScan` nodes can point to the same ENR — for example, when the same transition table is referenced twice in the same trigger function body, or when a materialized CTE is referenced in several branches of a query.

## Materialized CTEs

When the planner decides not to inline a CTE — because it is recursive, contains volatile functions, contains DML, is referenced more than once, or is explicitly marked `MATERIALIZED` — it treats the CTE body as an independent subplan. The executor runs the subplan under an `InitPlan` subtree. It accumulates the subplan's output into an ENR tuplestore. Every consumer reference to that CTE name becomes a `NamedTuplestoreScan` node that reads from the same ENR.

This differs from an inlined CTE. There, the planner substitutes the CTE body at each reference site as an ordinary subquery, leaving no trace in the plan under its original name. With a materialized CTE, the `EXPLAIN` output shows a `CTE Scan` node name (the planner wraps the ENR scan for display). The `InitPlan` populates the tuplestore before any consumer can execute.

The ENR for a materialized CTE has no `reliddesc` — it carries a literal `TupleDesc` derived from the CTE's output target list. `ENRMetadataGetTupDesc` returns this directly when `reliddesc` is `InvalidOid`.

## Trigger Transition Tables

`AFTER` statement-level triggers that declare `REFERENCING OLD TABLE AS old_tbl NEW TABLE AS new_tbl` receive their transition data through ENRs. During DML execution, the trigger machinery collects affected rows into tuplestores hanging off `AfterTriggersTableData`. When a PL procedure is about to fire the trigger, it calls `SPI_register_trigger_data`. This function wraps each populated tuplestore in an `EphemeralNamedRelationData` and registers it via `register_ENR` into the SPI query environment. SQL queries inside the trigger body then reference `old_tbl` or `new_tbl` as `RTE_NAMEDTUPLESTORE` range table entries, backed by `NamedTuplestoreScan` nodes.

For transition tables, the trigger machinery sets `enr->md.reliddesc` to the OID of the triggering relation. `ENRMetadataGetTupDesc` then calls `lookup_rowtype_tupdesc` on that OID rather than using a stored `TupleDesc`. This lookup allows cached plans to remain valid across DDL changes to the relation.

## Scan Initialization and TupleDesc Handling

At init time, `ExecInitNamedTuplestoreScan` looks up the ENR by name. It asserts that `enr->reldata` is non-null, then:

1. Casts `reldata` to `Tuplestorestate *` and stores it as `scanstate->relation`.
2. Calls `ENRMetadataGetTupDesc` to obtain the row format, storing it as `scanstate->tupdesc`.
3. Allocates a dedicated read pointer with `tuplestore_alloc_read_pointer(relation, EXEC_FLAG_REWIND)`. This pointer is independent of the tuplestore's default read pointer (index 0), which may be positioned anywhere by other readers.
4. Immediately selects and rewinds the new pointer so subsequent reads start from the beginning.

`ExecInitNamedTuplestoreScan` initializes the scan tuple slot with `TTSOpsMinimalTuple`, because tuplestores store `MinimalTuple` on disk and return the same format during `tuplestore_gettupleslot`. It projects a separate result slot on top when the node's output columns differ from the ENR's schema.

The node rejects `EXEC_FLAG_BACKWARD` and `EXEC_FLAG_MARK` at init time — backward scans and mark/restore are not supported.

## The Scan Loop

Each call to `ExecNamedTuplestoreScan` delegates to `ExecScan`, which calls `NamedTuplestoreScanNext`. That function:

1. Selects the node's own read pointer with `tuplestore_select_read_pointer`.
2. Calls `tuplestore_gettupleslot(relation, true, false, slot)` — the `true` argument requests forward direction; `false` means do not copy the tuple, just reference the in-memory slot.
3. Returns the slot, which is empty if the tuplestore is exhausted.

`ExecScan` then applies any qual expressions attached to the node before returning the tuple to the parent.

Because each `NamedTuplestoreScan` node owns its own read pointer, two sibling scans over the same ENR advance independently. One scan reaching end-of-store does not affect the position of another.

## Rescan

`ExecReScanNamedTuplestoreScan` rewinds the node's read pointer back to the beginning of the tuplestore by selecting it and calling `tuplestore_rescan`. This is always possible because `ExecInitNamedTuplestoreScan` allocated the read pointer with `EXEC_FLAG_REWIND`. That flag causes the tuplestore to retain all data even after it has spilled to disk. A tuplestore in `TSS_READFILE` mode rewinds by seeking the underlying `BufFile` to the start. One still in `TSS_INMEM` resets the array index. See [[subsystems/executor/tuplestore|tuplestore internals]] for the state machine.

Rescan is commonly triggered by a nested-loop join using a `NamedTuplestoreScan` on its inner side, or by a re-execution of a subquery that references a materialized CTE.

## See also

- [[subsystems/executor/tuplestore|Tuplestore and Tuplesort Variants]]
- [[sql-features/ctes|Common Table Expressions (WITH clauses)]]
- [[subsystems/executor/materialization|Materialize Node]]
- [[subsystems/executor/subplan-nodes|Subplan Nodes]]

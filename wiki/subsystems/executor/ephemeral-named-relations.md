---
title: "Ephemeral Named Relations"
aliases:
  - ENR
  - ephemeral named relation
  - transition tables
  - OLD TABLE
  - NEW TABLE
  - QueryEnvironment
source_files:
  - src/backend/utils/misc/queryenvironment.c
  - src/backend/parser/parse_enr.c
  - src/include/utils/queryenvironment.h
  - src/include/parser/parse_enr.h
symbols:
  - QueryEnvironment
  - EphemeralNamedRelationData
  - EphemeralNamedRelationMetadataData
  - EphemeralNameRelationType
  - create_queryEnv
  - register_ENR
  - unregister_ENR
  - get_ENR
  - get_visible_ENR_metadata
  - ENRMetadataGetTupDesc
  - name_matches_visible_ENR
  - get_visible_ENR
---

Ephemeral Named Relations (ENRs) are temporary, named result sets that exist only for the duration of a single SQL statement. They are visible to the parser as if they were ordinary tables. The primary use case is trigger transition tables. When a statement fires an AFTER trigger that declares `REFERENCING OLD TABLE AS old_data NEW TABLE AS new_data`, the `old_data` and `new_data` names must be resolvable as relations inside the trigger's query environment. This holds even though they have no existence in the system catalog. ENRs are the mechanism that makes this work without modifying the catalog or introducing special-case parser logic.

## The QueryEnvironment

Every query that runs in a context where ENRs might be visible carries a `QueryEnvironment` pointer alongside its parse tree, plan, and execution state. The structure (`queryenvironment.c`) is intentionally opaque. Its internal representation is a simple list of `EphemeralNamedRelation` entries. The implementation is therefore free to change without affecting callers.

```c
struct QueryEnvironment
{
    List *namedRelList;
};
```

A `QueryEnvironment` is created with `create_queryEnv()`. It is then populated by calling `register_ENR()` for each relation that should be visible. Lookups use `get_ENR()` (by name) or `get_visible_ENR_metadata()` (the parser-facing variant). The list is expected to be very short in practice — typically one or two entries per statement — so a linear scan is adequate and no hash table is needed.

The query environment is threaded through the system alongside the query tree. The `ParseState` carries a `p_queryEnv` pointer during parsing, and the executor has access to the same structure. This means ENRs registered before parsing begins are visible throughout the entire parse–plan–execute pipeline without any separate lookup mechanism.

## Metadata and Tuple Descriptors

Each ENR carries two layers of information. The `EphemeralNamedRelationMetadataData` struct holds the name and the tuple descriptor information needed for parsing and planning:

```c
typedef struct EphemeralNamedRelationMetadataData
{
    char     *name;        /* name used to identify the relation */
    Oid       reliddesc;   /* OID of a real relation to derive TupleDesc */
    TupleDesc tupdesc;     /* or a direct TupleDesc, if no real relation */
    EphemeralNameRelationType enrtype;  /* currently always ENR_NAMED_TUPLESTORE */
    double    enrtuples;   /* estimated row count for the planner */
} EphemeralNamedRelationMetadataData;
```

Exactly one of `reliddesc` and `tupdesc` must be set. For trigger transition tables, the ENR's tuple descriptor mirrors the triggering table's schema. The ENR records the table's OID in `reliddesc`, so that `ENRMetadataGetTupDesc()` can open the real relation and retrieve its descriptor, without storing a potentially stale copy. For ENRs that are independent of any real table (for instance, a named result set exposed by SPI), `tupdesc` is set directly. `reliddesc` is `InvalidOid` in that case.

The `enrtuples` field gives the planner an estimated cardinality. For trigger transition tables this is derived from statistics on the triggering relation. For other uses, it can be set to whatever estimate is available.

The `EphemeralNamedRelationData` struct wraps the metadata with a `reldata` pointer:

```c
typedef struct EphemeralNamedRelationData
{
    EphemeralNamedRelationMetadataData md;
    void *reldata;  /* execution-time data, typically a Tuplestorestate */
} EphemeralNamedRelationData;
```

At execution time, `reldata` points to a [[subsystems/executor/tuplestore|tuplestore]] containing the actual rows. This field is NULL when the ENR is registered purely for planning purposes (such as when `CREATE RULE` or `COPY` parses a query that will only be executed later).

## Parser Integration

The parser checks for ENRs through two thin functions in `parse_enr.c`:

- `name_matches_visible_ENR()` — returns true if a name resolves to an ENR in the current query environment.
- `get_visible_ENR()` — returns the `EphemeralNamedRelationMetadata` for the named ENR.

Both delegate immediately to `get_visible_ENR_metadata()` in `queryenvironment.c`. The parser calls these when it encounters a relation name in a `FROM` clause or similar context and does not find it in the system catalog. If the name matches an ENR, the parser produces a range table entry of kind `RTE_NAMEDTUPLESTORE` rather than `RTE_RELATION`. From the planner's perspective, this range table entry is served by a `NamedTuplestoreScan` executor node (see `subsystems/executor/named-tuplestore-scan.md`) rather than a heap scan.

This approach means that `SELECT * FROM new_table` inside a trigger's PL/pgSQL body resolves correctly to the ENR's tuplestore without any special handling in the parser beyond the name lookup. The rest of the parse–plan–execute chain operates on the range table entry as it would for any other data source.

## Lifecycle in Trigger Transition Tables

Trigger transition tables are the canonical and most visible application of ENRs. When a statement fires an AFTER trigger with `REFERENCING OLD TABLE` or `NEW TABLE`, the executor sets up the ENR infrastructure before the triggering statement begins:

1. `AfterTriggersTableData` structures are allocated for the relevant event types (old rows, new rows, or both), each backed by a tuplestore.
2. A `TransitionCaptureState` is created and attached to the `ModifyTableState`. As each row is processed — inserted, updated, or deleted — the executor appends copies to the appropriate tuplestore via the capture state.
3. When the AFTER trigger fires at statement end, the tuplestores are wrapped in ENRs and registered in a query environment passed to the trigger's SPI or PL/pgSQL execution context. The trigger function can then query `old_data` or `new_data` as if they were regular tables.
4. At `AfterTriggerEndQuery()`, the tuplestores are freed and the ENRs are discarded.

The constraint that transition tables cannot be used with deferred triggers follows directly from this lifecycle. The tuplestores are freed at statement end, long before transaction commit, when deferred triggers would fire. Attempting to reference freed memory at commit time would corrupt the process.

For AFTER STATEMENT triggers, the entire set of rows changed by the triggering statement is available in the transition tables at once. For AFTER ROW triggers with transition tables (also permitted), the trigger fires after the full statement completes, not after each individual row. So the transition tables again contain the complete set.

## Beyond Triggers: MERGE and SPI

ENRs are not limited to trigger transition tables. `MERGE` uses the same infrastructure internally to hold per-row state during its execution. More generally, any code that invokes the parser through SPI and needs to expose a named intermediate result set can register an ENR in a query environment. It can then pass that environment to `SPI_execute_with_args()` or similar SPI entry points. The parser will resolve the name as an ENR. The executor will scan the underlying tuplestore.

This generality is by design. The `QueryEnvironment` abstraction was introduced specifically to separate "things the parser can see" from "things in the catalog", making it straightforward to add new temporary, named data sources without catalog modifications or parser special-cases.

## Key Data Structures

| Structure | File | Purpose |
|---|---|---|
| `QueryEnvironment` | `queryenvironment.c` | Opaque container for the named-relation list |
| `EphemeralNamedRelationMetadataData` | `queryenvironment.h` | Name, tuple descriptor, cardinality estimate |
| `EphemeralNamedRelationData` | `queryenvironment.h` | Metadata plus execution-time `reldata` pointer |
| `EphemeralNameRelationType` | `queryenvironment.h` | Enum; currently only `ENR_NAMED_TUPLESTORE` |

## Related Topics

- [[subsystems/triggers|triggers]]
- [[subsystems/plpgsql/trigger-functions|PL/pgSQL trigger functions]]
- [[subsystems/executor/tuplestore|tuplestore]]
- [[code-paths/merge|MERGE]]

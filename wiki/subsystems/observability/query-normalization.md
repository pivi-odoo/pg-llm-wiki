---
title: "Query Normalization and Fingerprinting (Query Jumbling)"
aliases:
  - query jumbling
  - query fingerprinting
  - queryId
  - query normalization
tags:
  - symptom/slow-query
source_files:
  - src/backend/nodes/queryjumblefuncs.c
  - src/include/nodes/queryjumble.h
symbols:
  - JumbleQuery
  - JumbleState
  - LocationLen
  - AppendJumble
  - RecordConstLocation
  - EnableQueryId
  - CleanQuerytext
  - compute_query_id
  - query_id_enabled
---

Query normalization is the mechanism PostgreSQL uses to recognize structurally identical queries even when their literal constant values differ. After parse analysis, the server recursively traverses the query tree. It hashes the fields that express query structure while skipping constant values, and stores the resulting 64-bit hash as `Query.queryId`. This lets tools like [[subsystems/observability/pg-stat-statements|pg_stat_statements]] fold thousands of individually distinct SQL strings into a single tracked entry, making workload analysis tractable.

## The Jumble: Selective Tree Serialization

The core idea is that two queries are "the same" if they have identical structure — same tables, same operators, same join shape. The literal values in their `WHERE` clauses or `VALUES` lists do not matter. The process is called a *query jumble*: a compact serialization of only those fields that are structurally essential.

`JumbleQuery` allocates a `JumbleState`, which holds a 1024-byte scratch buffer (`jumble`). It then drives `_jumbleNode` recursively through the entire `Query` tree. Each node contributes its `NodeTag` type identifier first, followed by whichever fields the generated dispatch code judges significant. The jumble includes string fields (column names, function names, CTE names) but excludes Var collations. When the scratch buffer fills, `JumbleQuery` folds it into an 8-byte intermediate hash and reuses it. This lets the final hash cover arbitrarily deep trees without unbounded memory.

`AppendJumble` writes fields into the buffer. The macro `JUMBLE_FIELD` serializes a struct field by its raw bytes; `JUMBLE_STRING` serializes a null-terminated string; `JUMBLE_NODE` recurses into a child node. Generated code in `queryjumblefuncs.funcs.c` and `queryjumblefuncs.switch.c` expands these macros. PostgreSQL produces that code from node definitions, so new node types are automatically covered.

`hash_any_extended` computes the final hash over the filled portion of the jumble buffer. PostgreSQL remaps a result of zero to 1 (or 2 for utility statements) to preserve zero as a sentinel meaning "no queryId assigned."

## How Constants Are Skipped — and Remembered

PostgreSQL deliberately excludes constants from the hash, so that `WHERE x = 1` and `WHERE x = 42` produce the same `queryId`. The `_jumbleA_Const` function, called for pre-analysis `A_Const` nodes (raw parse tree constants), handles this by calling `RecordConstLocation` rather than `AppendJumble`. It stores the byte offset of the constant within the original query string into the `clocations` array inside `JumbleState`, with an initial length of -1 (to be filled in later by the caller, typically pg_stat_statements).

`RecordConstLocation` keeps a dynamically grown array of `LocationLen` structs, starting at capacity 32 and doubling on overflow. After `JumbleQuery` returns, the caller has both the `queryId` and a map of every constant position. pg_stat_statements uses that map to rewrite the stored query text, replacing each constant span with a `$1`, `$2`, ... placeholder in source order. This is how PostgreSQL produces normalized query text — the hash computation and the text normalization share a single tree walk.

`JumbleQuery` includes `Param` nodes (external parameters already in the tree, i.e., `$1` in a prepared statement) in the hash. It also updates `highest_extern_param_id` for them. This counter tells pg_stat_statements where to start numbering replacement parameters so that mixed queries — containing both pre-existing `$N` markers and literal constants — are numbered correctly in their normalized form.

## The queryId: Scope and Exposure

`Query.queryId` is a `uint64` populated at the end of parse analysis when query-id computation is enabled. The server propagates this value into plan trees, making it available throughout execution. This lets the executor attribute costs to the right query shape.

The queryId surfaces in several places:

- **`pg_stat_statements`** — the primary consumer; groups execution statistics by queryId.
- **`pg_stat_activity.query_id`** — added in PG 14, lets an administrator correlate an in-flight session (`pg_stat_activity`) with aggregated statistics (`pg_stat_statements`) in a single join on `query_id`.
- **`auto_explain`** — can log the queryId alongside slow-query plans, enabling post-hoc correlation.

PostgreSQL computes the queryId from structural features of the parse tree, not from raw SQL text. It is therefore not stable across major PostgreSQL versions: node layouts, OID assignments, and the dispatch tables in the generated files all change, so the same logical query may hash to a different value after an upgrade.

PostgreSQL handles DDL statements with reduced fidelity. `utilityStmt` carries a raw parse node rather than a fully analyzed `Query`. The jumbling of utility statements is correspondingly shallower. The zero-remapping logic explicitly assigns `queryId = 2` to utility queries with an unlucky hash, keeping them distinguishable from DML.

## The compute_query_id GUC

The `compute_query_id` GUC controls when jumbling runs:

| Setting | Behavior |
|---------|----------|
| `off` | Jumbling is disabled entirely. `queryId` stays zero. CPU overhead is eliminated. |
| `on` | Jumbling always runs, whether or not any consumer is present. |
| `auto` | Jumbling runs only when a module has called `EnableQueryId()`. This is the default. |
| `regress` | Like `off` for the purposes of queryId exposure, used by regression tests to suppress non-deterministic output. |

The `auto` mode is the practical default. When pg_stat_statements loads (typically via `shared_preload_libraries`), its module initialization calls `EnableQueryId()`. This sets the global `query_id_enabled = true`. From that point forward, `IsQueryIdEnabled()` returns true. PostgreSQL jumbles every subsequent query. If pg_stat_statements is not loaded and no other module requests query IDs, PostgreSQL never enters the jumbling code path. This keeps overhead at zero for installations that do not need it.

`EnableQueryId()` respects the GUC: if `compute_query_id = off`, the call is a no-op. This gives administrators a hard override to disable jumbling, even when a consuming extension is loaded.

## Prepared Statements and ORM Queries

A query sent as a prepared statement already uses `$1`, `$2`, ... parameters in its text. During jumbling, `JumbleQuery` hashes those `Param` nodes as part of the tree structure — their parameter numbers contribute to the hash via `JUMBLE_FIELD` on `paramid` — but the *values* bound at execution time are never part of the parse tree and never influence the hash.

An ad-hoc query using a literal `42` yields an `A_Const` node. `RecordConstLocation` skips that constant from the hash. Both paths therefore produce the same `queryId` for the same logical query shape.

This is why ORMs that use bind parameters (prepared statements or protocol-level parameterized queries) cluster correctly in pg_stat_statements. Django's ORM, SQLAlchemy in its default mode, and ActiveRecord all send parameterized queries; each distinct query shape produces exactly one pg_stat_statements row regardless of the data values used across millions of executions. A poorly configured ORM that interpolates literals directly into SQL text can still produce the same queryId after normalization, but only if pg_stat_statements is present to map those literal positions to `$N`. The queryId itself is computed the same way either way.

## Diagnosing Plan Cache Pollution

When PostgreSQL executes a parameterized query frequently and caches a generic plan, all executions share one plan tree. If that generic plan is suboptimal for certain parameter values (a common cause of skewed-data performance problems), `pg_stat_statements` will show the queryId with high variance in execution time. Joining `pg_stat_activity` on `query_id` during a slow period reveals which sessions are executing the same logical query. `pg_stat_statements.plans` (PG 13+) shows how many times planning occurred versus execution. A ratio near 1:1 indicates that PostgreSQL is generating custom plans each time (the planner decided generic plans were not safe). A very low ratio indicates that PostgreSQL is reusing a single generic plan.

`JumbleQuery` sets the `highest_extern_param_id` field in `JumbleState` during jumbling. pg_stat_statements uses this field when normalizing a query that already contains markers like `$3`. New placeholders for inlined constants then start at `$4` rather than `$1`. This prevents renumbering collisions.

## Related Topics

- [[subsystems/observability/pg-stat-statements|pg_stat_statements]] — the primary consumer of queryId, aggregates per-shape execution statistics
- [[subsystems/planner/overview|planner]] — receives the queryId from the parsed Query and propagates it into plan trees
- [[subsystems/memory/contexts|memory contexts]] — JumbleState is allocated in the current memory context and freed with it after parse analysis completes

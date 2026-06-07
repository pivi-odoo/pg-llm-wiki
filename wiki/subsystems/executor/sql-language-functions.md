---
title: "SQL-Language Function Execution"
aliases:
  - SQL functions
  - fmgr_sql
source_files:
  - src/backend/executor/functions.c
symbols:
  - fmgr_sql
  - SQLFunctionCache
  - SQLFunctionCachePtr
  - execution_state
  - DR_sqlfunction
  - init_sql_fcache
  - postquel_start
  - postquel_getnext
  - postquel_end
  - postquel_sub_params
  - ShutdownSQLFunction
  - check_sql_fn_retval
  - check_sql_fn_retval_ext
  - prepare_sql_fn_parse_info
  - sql_fn_parser_setup
---

SQL-language functions — created with `LANGUAGE sql` — are the simplest procedural extension point in PostgreSQL. The body is one or more plain SQL statements. The executor manages their parse and plan trees directly, with no separate procedural runtime. Because the function body is pure SQL, the optimizer can sometimes inline it at the call site. Because each statement runs through the standard planner and executor, all query features — including RETURNING, CTEs, and rule rewriting — are available without special handling.

## Parsing and caching plans

On the first invocation of a SQL function, `fmgr_sql()` calls `init_sql_fcache()` to build an `SQLFunctionCache`. This cache holds everything needed for subsequent calls: the parsed and planned statements, type metadata, argument bindings, and a tuplestore for buffering result rows (functions.c).

The parse path has two branches. When the catalog's `pg_proc.prosqlbody` column is not null, `fmgr_sql()` deserializes the stored `Query` nodes. It then runs them through rewriting only (`AcquireRewriteLocks` + `pg_rewrite_query()`). This column is not null for functions created with the `AS $$ … $$` form under PG14+. That form stores pre-parsed trees. For functions that have only a `prosrc` text body, `fmgr_sql()` feeds the source through `pg_parse_query()`, then through `pg_analyze_and_rewrite_withcb()` with custom parser hooks. These hooks resolve `$n` positional parameters and named argument references to `Param` nodes.

The argument resolution hooks (`sql_fn_post_column_ref()`, `sql_fn_param_ref()`) allow the function body to refer to arguments either by `$1`/`$2` positional notation or by bare name (e.g., `amount`) or qualified name (`myfunc.amount`). Table column references take priority. Parameter names act as an outer scope. The parser consults this scope only when it finds no table-column match.

After parsing and rewriting, `check_sql_fn_retval_ext()` verifies that the final result-producing query returns a type compatible with the declared return type, inserting implicit casts in the target list as needed. For composite return types it validates column count and type per column. It can also inject NULL placeholders for dropped columns. When the engine cannot apply coercions in place (for example, when the affected column carries a `sortgroupref` used by ORDER BY), it injects a wrapping projection query over the original.

`fmgr_sql()` tags the `SQLFunctionCache` with the `LocalTransactionId` and `SubTransactionId` of its creation time. On every call, `fmgr_sql()` checks those values against the current transaction state. If they have changed — indicating a new transaction or subtransaction — `fmgr_sql()` discards the cache. It then rebuilds it. This matters because the `FmgrInfo` that owns `fn_extra` can outlive a transaction, particularly for index-support functions. Stale plan trees must not be reused across transaction boundaries.

## Execution state machine

An `execution_state` node represents each SQL statement in the function body. Each node contains a `PlannedStmt`, a `QueryDesc` (non-null only while the statement is running), and a lifecycle status (functions.c):

| Status | Meaning |
|---|---|
| `F_EXEC_START` | Not yet started |
| `F_EXEC_RUN` | Executor is active; `qd` is valid |
| `F_EXEC_DONE` | Executor finished or cleanly closed |

Rule expansion of a single parsed statement can produce multiple `PlannedStmt` values. The engine chains these via the `next` pointer. The outer list in `SQLFunctionCache.func_state` groups chains by original statement boundary. The boundary matters for snapshot management. The engine takes a fresh snapshot at each original-query boundary when the function is writable (non-STABLE/IMMUTABLE).

The loop in `fmgr_sql()` executes all statements in the function body in sequence. `fmgr_sql()` designates only the last statement whose `canSetTag` flag is true as the result producer (`setsResult = true`). All other statements run to completion. They discard their output through the `None_Receiver`.

## Lazy evaluation vs. materialise

When a caller calls a set-returning SQL function and the calling context supports both return modes, the executor chooses between two strategies for the result-producing SELECT:

- **Lazy (value-per-call)**: `postquel_start()` creates the executor with `EXEC_FLAG_SKIP_TRIGGERS`. `postquel_getnext()` fetches one row at a time. `fmgr_sql()` returns `ExprMultipleResult` until the statement is exhausted. The engine registers the `ShutdownSQLFunction` callback on the expression context, so that a premature exit (e.g., the caller stops after LIMIT rows) cleans up the still-running executor.
- **Materialise**: the executor collects all rows into the `tstore` tuplestore before returning any. It then hands them back to the caller via `ReturnSetInfo.setResult`. It allocates the tuplestore with `work_mem` as its memory budget (functions.c line 1144).

The engine uses lazy evaluation whenever the caller does not prefer materialise and the result statement is a plain SELECT with no modifying CTEs. The engine forces it off when the function returns a rowtype-valued scalar, to avoid the complexity of expanding a composite column into multiple output columns. The cache-build step records the choice once and does not re-evaluate it per call.

## Snapshot management for writable functions

STABLE and IMMUTABLE functions (`readonly_func = true`) reuse the surrounding query's snapshot without modification. VOLATILE functions must take their own snapshots so that each statement sees the effects of all prior statements in the same transaction, including earlier statements within the same function call.

The mechanism is: before each new statement in a VOLATILE function, `CommandCounterIncrement()` advances the command counter. If no snapshot has been pushed yet for this call, `PushActiveSnapshot(GetTransactionSnapshot())` takes a fresh one. Otherwise, `UpdateActiveSnapshotCommandId()` bumps the command ID in the existing snapshot so the new statement sees the incremented counter. When the function crosses an original-query boundary (the outer list boundary in `func_state`), `fmgr_sql()` pops the snapshot. It takes a fresh snapshot for the next statement group. This matches the snapshot-per-statement behavior that interactive sessions see.

## Parameter passing and expanded datums

Before the first statement is executed, `postquel_sub_params()` constructs a `ParamListInfo` from the caller's `FunctionCallInfo` arguments. `fmgr_sql()` reuses the same `ParamListInfo` on subsequent calls for a set-returning function, avoiding repeated allocation.

A subtle correctness constraint applies to read-write expanded datums, such as arrays passed as `PARAM_EXTERN`. If multiple `Param` nodes reference the same parameter across the function body — or if it appears in multiple statements — a mutable reference could allow an earlier use to corrupt the value seen by a later use. To prevent this, `postquel_sub_params()` calls `MakeExpandedObjectReadOnly()` on every argument before storing it in the `ParamListInfo`. This forces all references to be read-only copies (postquel_sub_params(), functions.c).

## Result extraction and the JunkFilter

The result-producing statement's output goes through a `DR_sqlfunction` `DestReceiver`. The `JunkFilter` filters each tuple. It strips junk attributes (system columns like `ctid` that sneak through from rule-expanded queries) and coerces types if needed. `sqlfunction_receive()` then stores the filtered tuple into the tuplestore (functions.c). For VOID-returning functions, `fmgr_sql()` builds no `JunkFilter` and makes no tuplestore entry.

When returning a composite (tuple) result, `ExecFetchSlotHeapTupleDatum()` materialises the whole slot as a heap tuple datum. For scalar results, `slot_getattr()` extracts column 1. Then `datumCopy()` copies it into the caller's [[subsystems/memory/contexts|memory context]] before the slot is cleared. Both paths execute inside `postquel_get_single_result()`.

## Key data structures

| Structure | Purpose |
|---|---|
| `SQLFunctionCache` | Per-FmgrInfo cache: source text, type metadata, plans, tuplestore, JunkFilter |
| `execution_state` | Per-statement node: plan, QueryDesc, lifecycle status, lazy-eval flag |
| `DR_sqlfunction` | DestReceiver that filters and stores result tuples into the tuplestore |
| `SQLFunctionParseInfo` | Argument names and types used by parser callback hooks during body analysis |

## Related Topics

- [[subsystems/executor/overview|Executor Overview]] — the ExecutorStart/Run/Finish/End lifecycle that SQL functions invoke for each statement
- [[subsystems/executor/expression-eval|Expression Evaluation]] — how `ExprState` and `Param` nodes are evaluated at runtime
- [[subsystems/executor/work-mem-and-spill|work_mem]] — controls the tuplestore spill threshold for set-returning functions
- [[subsystems/plpgsql/overview|PL/pgSQL Internals]] — the procedural alternative; uses SPI rather than direct executor calls and maintains its own plan cache

---
title: "Function API for Set-Returning and Composite-Type Functions"
aliases:
  - funcapi
  - SRF
  - Set-Returning Functions
  - FuncCallContext
  - ReturnSetInfo
source_files:
  - src/backend/utils/fmgr/funcapi.c
  - src/include/funcapi.h
symbols:
  - FuncCallContext
  - ReturnSetInfo
  - AttInMetadata
  - TypeFuncClass
  - InitMaterializedSRF
  - init_MultiFuncCall
  - per_MultiFuncCall
  - end_MultiFuncCall
  - get_call_result_type
  - get_expr_result_type
  - get_func_result_type
  - BlessTupleDesc
  - TupleDescGetAttInMetadata
  - BuildTupleFromCStrings
  - build_function_result_tupdesc_t
  - resolve_polymorphic_argtypes
  - extract_variadic_args
---

# Function API for Set-Returning and Composite-Type Functions

`funcapi.c` and `funcapi.h` provide the infrastructure C functions need when they return more than a single scalar value — either a sequence of rows (set-returning functions, or SRFs) or a single composite type (a row). The same API also supplies helpers for resolving polymorphic return types and extracting `VARIADIC` argument lists. Without this layer, every extension author would have to interact directly with the executor's `ReturnSetInfo` structure and the type system's tuple descriptor machinery. funcapi wraps those details into a stable, documented interface.

## Two Modes of Set Return

A function returning a set can operate in one of two modes. The choice has significant consequences for resource management and executor integration.

**Value-per-call mode** (`SRF_*` macros) suspends the function between rows by returning control to the executor after each call. The executor invokes the function repeatedly until `SRF_RETURN_DONE` signals exhaustion. State shared across calls lives in a `FuncCallContext` allocated in a dedicated [[subsystems/memory/contexts|memory context]] (`multi_call_memory_ctx`). The executor is free to stop short — a `LIMIT` clause, a cancelled query, or a calling context that only needs the first few rows — so the function may never reach `SRF_RETURN_DONE`. This means cleanup code placed there is unreliable. The function must instead release resources other than palloc'd memory (file descriptors, cursors, locks) through `RegisterExprContextCallback`. The executor calls this callback on context teardown, regardless of how many rows were consumed.

**Materialize mode** (`InitMaterializedSRF`) runs the function exactly once and expects it to populate a `Tuplestorestate` with all rows before returning. The caller fills `rsinfo->setResult` with the tuplestore and `rsinfo->setDesc` with the tuple descriptor. The executor then reads from the tuplestore at its own pace. This model is safer for functions that hold open resources, because the function body completes atomically before the caller starts consuming rows. The downside is that the function must generate and store all rows in memory (up to [[subsystems/executor/work-mem-and-spill|work_mem]]) before the first row is visible to the caller.

`InitMaterializedSRF` handles the boilerplate: it checks that the calling context permits materialize mode (`rsinfo->allowedModes & SFRM_Materialize`), allocates the tuplestore in the per-query [[subsystems/memory/contexts|memory context]], and stores the tuplestore and tuple descriptor into `rsinfo`. The `MAT_SRF_USE_EXPECTED_DESC` flag tells the helper to take the tuple descriptor from `rsinfo->expectedDesc` (what the caller expects) rather than deriving it from the function's own declared return type. `MAT_SRF_BLESS` additionally calls `BlessTupleDesc` to register the descriptor with the transient-record type cache.

## FuncCallContext and the Value-per-Call Pattern

The `FuncCallContext` struct is the backbone of value-per-call SRFs. `init_MultiFuncCall` allocates it on the first call (`fn_extra == NULL`) and stores it in `fcinfo->flinfo->fn_extra`, making it accessible on every subsequent call via `per_MultiFuncCall`. The struct carries:

- `call_cntr` — a monotonically increasing counter incremented by `SRF_RETURN_NEXT`; the function uses it to track position in the result set.
- `max_calls` — an optional upper bound that the function can set and check against `call_cntr`.
- `user_fctx` — a void pointer for arbitrary per-call state (query results, open cursors, arrays being iterated).
- `attinmeta` — pre-built input metadata for `BuildTupleFromCStrings`, populated if the function constructs tuples from C strings.
- `tuple_desc` — a `TupleDesc` for use with `heap_form_tuple`, populated if the function constructs tuples directly from `Datum` arrays.
- `multi_call_memory_ctx` — the memory context that owns the `FuncCallContext` and anything the function allocates in it.

On the first call, the function switches to `multi_call_memory_ctx`, allocates its per-call state, then switches back. On subsequent calls it retrieves the context via `SRF_PERCALL_SETUP()` and uses the saved state. The shutdown callback registered by `init_MultiFuncCall` deletes `multi_call_memory_ctx` (and hence the entire `FuncCallContext`) when the expression context is torn down. This ensures cleanup happens even if the SRF is never run to completion.

A minimal value-per-call SRF following the documented pattern looks like:

```c
Datum
my_srf(PG_FUNCTION_ARGS)
{
    FuncCallContext *funcctx;

    if (SRF_IS_FIRSTCALL())
    {
        MemoryContext oldctx;
        funcctx = SRF_FIRSTCALL_INIT();
        oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);
        funcctx->max_calls = 10;
        /* allocate user_fctx state here */
        MemoryContextSwitchTo(oldctx);
    }

    funcctx = SRF_PERCALL_SETUP();

    if (funcctx->call_cntr < funcctx->max_calls)
    {
        Datum result = /* compute next value */;
        SRF_RETURN_NEXT(funcctx, result);
    }
    SRF_RETURN_DONE(funcctx);
}
```

## ReturnSetInfo and Executor Integration

`ReturnSetInfo` is the node the executor places in `fcinfo->resultinfo` when it calls a set-returning function. It is the communication channel between the executor and the function for negotiating delivery mode and passing the tuplestore back. Key fields the function reads or writes:

| Field | Direction | Meaning |
|---|---|---|
| `allowedModes` | read | Bitmask of modes the executor will accept (`SFRM_ValuePerCall`, `SFRM_Materialize`, `SFRM_Materialize_Random`). |
| `returnMode` | write | The mode the function has chosen (`SFRM_ValuePerCall` or `SFRM_Materialize`). |
| `isDone` | write | `ExprMultipleResult` while rows remain; `ExprEndResult` when done (value-per-call only). |
| `expectedDesc` | read | The tuple descriptor the executor expects, derived from the query's column definition list or planner inference. |
| `setResult` | write | The tuplestore (materialize mode). |
| `setDesc` | write | The tuple descriptor matching `setResult` (materialize mode). |
| `econtext` | read | The expression context; its `ecxt_per_query_memory` is the right place for structures that must survive for the query duration. |

A function must check `rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo)` before proceeding, because callers that cannot handle set results (for example, a scalar subquery context) will pass a NULL or a non-`ReturnSetInfo` resultinfo node. Attempting to return a set in such a context should raise `ERRCODE_FEATURE_NOT_SUPPORTED`.

## Resolving Return Types

C functions returning composite types, `RECORD`, or polymorphic types face a runtime type-resolution problem: the declared return type in `pg_proc` may be `RECORD` or `ANYELEMENT`, not a concrete type. The `get_call_result_type` family resolves this.

`get_call_result_type(fcinfo, &typeOid, &tupdesc)` returns a `TypeFuncClass` indicating what kind of type was resolved:

| Class | Meaning |
|---|---|
| `TYPEFUNC_SCALAR` | A single base type; `typeOid` is filled. |
| `TYPEFUNC_COMPOSITE` | A named composite type or fully-resolved record; `tupdesc` is filled. |
| `TYPEFUNC_COMPOSITE_DOMAIN` | A domain over a composite type; `tupdesc` reflects the base composite. |
| `TYPEFUNC_RECORD` | An anonymous record whose layout cannot be determined from available context. |
| `TYPEFUNC_OTHER` | A pseudo-type that cannot be returned. |

The function inspects `pg_proc` for the function's declared `prorettype` and OUT parameter list. If OUT parameters define the record layout, it calls `build_function_result_tupdesc_t` to construct a `TupleDesc` from those parameters. For polymorphic result types, it derives the concrete type by examining the actual argument types in the call expression (`fn_expr`). When the return type is anonymous `RECORD` and no OUT parameters exist, it falls back to `rsinfo->expectedDesc` if that is available from the calling context.

`get_expr_result_type` serves the same purpose but starts from an expression node rather than a live `FunctionCallInfo`. It handles `RowExpr` nodes with `RECORD` type by constructing a tuple descriptor directly from the expression's column list — useful during planning and EXPLAIN when no actual call is in progress. `get_func_result_type` starts from a function OID alone, but cannot resolve anonymous `RECORD` results or polymorphic types without call-site information.

Because `get_call_result_type` accesses the system cache, it is relatively expensive. SRFs in value-per-call mode should call it only during the first invocation and cache the result in `FuncCallContext`.

## Tuple Descriptors and Blessing

Before a composite-returning function can hand a `HeapTuple` back to the executor, the `TupleDesc` describing it must be "blessed" — registered in the transient-record type cache with a typmod that uniquely identifies its layout within the query's lifetime. `BlessTupleDesc(tupdesc)` performs this registration. The executor uses the embedded `tdtypeid`/`tdtypmod` to decode the tuple on the receiving side.

The type system pre-blesses composite types from the catalog (`TYPEFUNC_COMPOSITE`). It is only transient `RECORD` types — those synthesized from OUT parameters, `ROW()` expressions, or runtime construction — that require explicit blessing. Forgetting to bless a dynamically constructed `TupleDesc` results in an error when the executor tries to look up the record type.

`TupleDescGetAttInMetadata` builds an `AttInMetadata` struct that caches the type-input functions and type-modifier values for each column. This allows `BuildTupleFromCStrings` to construct a `HeapTuple` from an array of C strings without repeating syscache lookups on every row — the pattern most commonly used by functions that wrap external data sources returning text.

`build_function_result_tupdesc_t` constructs a `TupleDesc` from a function's `pg_proc` row by scanning `proallargtypes`, `proargmodes`, and `proargnames` for OUT and INOUT parameters. It returns NULL for functions that do not define multiple output parameters (single-output scalar functions do not need a tuple descriptor). This is the preferred way to discover a function's output shape without a live call.

## Polymorphic Type Resolution

Functions declared with polymorphic pseudotypes (`ANYELEMENT`, `ANYARRAY`, `ANYRANGE`, `ANYMULTIRANGE`, and their `ANYCOMPATIBLE*` siblings) do not have a fixed return type. PostgreSQL must deduce the actual return type from the concrete argument types provided at the call site. `resolve_polymorphic_argtypes` performs this deduction for IN/OUT argument arrays. The internal `resolve_polymorphic_tupdesc` variant does the same for a `TupleDesc` built from OUT parameters.

The deduction follows a priority chain: if the resolver needs to resolve an `ANYELEMENT` output but received no `ANYELEMENT` input, it tries to derive the element type from an `ANYARRAY`, `ANYRANGE`, or `ANYMULTIRANGE` input instead. PostgreSQL resolves the `ANYCOMPATIBLE` family independently from the `ANY` family. They are separate polymorphism groups that happen to use the same resolution logic.

The resolver also carries collation through: if the resolved element type is collatable, it looks for an input collation on the call expression and propagates it to the output columns. Range and multirange types are not collatable and do not receive collation propagation.

## VARIADIC Argument Handling

`extract_variadic_args` normalises the two calling conventions for `VARIADIC` functions. When the caller writes `f(VARIADIC arr)`, the variadic arguments arrive as a single array that must be deconstructed. When the caller writes `f(a, b, c)` with individual arguments, the values are already separate `Datum`s. `extract_variadic_args` detects which case applies via `get_fn_expr_variadic` and produces a uniform set of `Datum *`, `Oid *`, and `bool *` arrays regardless of how the function was called. The `convert_unknown` parameter controls whether the function silently promotes `UNKNOWN`-typed string literals to `text`. This is useful for functions declared as taking `"any"` that need to handle undecorated string constants.

## Related Topics

- [[subsystems/memory/contexts|memory context]] — per-query and multi-call contexts used by SRF infrastructure
- [[subsystems/extensions/overview|extension overview]] — how extension libraries are loaded and initialised
- [[subsystems/extensions/hooks|hooks]] — RegisterExprContextCallback for SRF resource cleanup
- [[subsystems/executor/overview|executor]] — ReturnSetInfo, expression contexts, and tuple slot lifecycle

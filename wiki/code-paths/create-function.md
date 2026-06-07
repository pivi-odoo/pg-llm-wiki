---
title: "CREATE FUNCTION Code Path"
aliases:
  - "CREATE FUNCTION"
  - "CREATE PROCEDURE"
  - "PL/pgSQL compilation"
  - "ProcedureCreate"
source_files:
  - src/backend/commands/functioncmds.c
  - src/pl/plpgsql/src/pl_comp.c
  - src/pl/plpgsql/src/pl_exec.c
  - src/backend/utils/fmgr/fmgr.c
  - src/backend/catalog/pg_proc.c
symbols:
  - ProcedureCreate
  - plpgsql_compile
  - PLpgSQL_function
  - fmgr_info
  - FmgrInfo
---

# CREATE FUNCTION Code Path

`CREATE FUNCTION` and `CREATE PROCEDURE` create a new callable object by inserting a row into `pg_proc` and optionally loading a shared library. For most languages, PostgreSQL defers the actual compilation of the function body to first call.

## Entry point

```
ProcessUtility
  └── standard_ProcessUtility
        └── CreateFunction   (commands/functioncmds.c)
              └── ProcedureCreate   (catalog/pg_proc.c)
```

`CreateFunction` validates the statement, resolves types and the language, then calls `ProcedureCreate` to write the catalog row.

## pg_proc row

`ProcedureCreate` inserts one row into `pg_proc`. Key fields set at CREATE time:

| Field | Source |
|---|---|
| `proname` | Function name |
| `prolang` | OID of the language (from `pg_language`) |
| `pronargs` / `proargtypes` | Argument count and types |
| `prorettype` | Return type OID |
| `prosrc` | Source text (SQL/PL body) or C function name |
| `probin` | Shared library path (C functions only) |
| `provolatile` | `i`=IMMUTABLE, `s`=STABLE, `v`=VOLATILE |
| `prokind` | `f`=function, `p`=procedure, `a`=aggregate, `w`=window |
| `prosecdef` | SECURITY DEFINER flag |
| `proleakproof` | LEAKPROOF flag |
| `proconfig` | Per-function GUC settings (SET clause) |

No compilation happens at this point for interpreted languages. `ProcedureCreate` stores the source as text.

## Language dispatch (fmgr)

When the executor calls a function, `fmgr_info` (`fmgr.c`) looks up the `pg_proc` row and builds an `FmgrInfo` struct with:

- `fn_addr`: the C function pointer to call
- `fn_oid`: the function OID
- `fn_nargs`: argument count
- `fn_strict`: whether to short-circuit on NULL arguments
- `fn_retset`: whether the function returns a set

For interpreted languages, `fn_addr` points to the language handler's call handler (e.g. `plpgsql_call_handler`). The handler then does the language-specific work.

## SQL functions

SQL functions store the query text in `prosrc`. At call time:

1. The fmgr calls `fmgr_sql` (the SQL language call handler).
2. `fmgr_sql` parses and plans the query text on first call. It caches the plan in a `SQLFunctionCache` struct keyed on the function OID and `pg_proc.xmin`.
3. `fmgr_sql` executes the plan via `SPI_execute_plan`.
4. If the cache is invalidated (schema change), `fmgr_sql` re-parses and re-plans the function.

The planner can **inline** SQL functions: if the function is `IMMUTABLE` or `STABLE`, has a single SQL query, and meets other criteria, the planner replaces the function call with the query body via `inline_function`.

## C functions

C functions set `probin` to the shared library path and `prosrc` to the C symbol name. At first call:

1. `fmgr_info` calls `load_external_function`. `load_external_function` calls `pg_dlopen` to load the `.so`.
2. `pg_dlsym` resolves the C symbol.
3. `fmgr_info` then sets `fn_addr` to the resolved function pointer.

C functions use the `PG_FUNCTION_ARGS` / `PG_RETURN_*` macros and the `FunctionCallInfo` struct to pass arguments and return values in a version-independent way.

## PL/pgSQL compilation

PL/pgSQL defers compilation to **first call** (or when the cached compilation is invalidated).

### plpgsql_compile

`plpgsql_call_handler` calls `plpgsql_compile` (`pl_comp.c`) on the first call:

1. Looks up the compiled function in `plpgsql_HashTable` (keyed on OID + `pg_proc.xmin`).
2. If not found, parses and compiles the function body.
3. Returns the cached `PLpgSQL_function` struct.

### PLpgSQL_function

The compiled representation is a `PLpgSQL_function` struct containing:

- `datums[]`: array of variable/row/record descriptors (`PLpgSQL_var`, `PLpgSQL_row`, `PLpgSQL_rec`)
- `action`: the top-level `PLpgSQL_stmt_block`
- `fn_cxt`: a dedicated [[subsystems/memory/contexts|memory context]] holding the compiled function

### Statement nodes

PL/pgSQL compiles the body into a tree of statement nodes:

| Node type | SQL equivalent |
|---|---|
| `PLpgSQL_stmt_assign` | `variable := expr` |
| `PLpgSQL_stmt_if` | `IF … THEN … ELSIF … END IF` |
| `PLpgSQL_stmt_loop` | `LOOP … END LOOP` |
| `PLpgSQL_stmt_fori` | `FOR i IN 1..10` |
| `PLpgSQL_stmt_forc` | `FOR rec IN cursor` |
| `PLpgSQL_stmt_execsql` | `INSERT`/`UPDATE`/`DELETE`/`SELECT INTO` |
| `PLpgSQL_stmt_perform` | `PERFORM query` |
| `PLpgSQL_stmt_return` | `RETURN expr` |
| `PLpgSQL_stmt_raise` | `RAISE NOTICE/EXCEPTION` |
| `PLpgSQL_stmt_dynexecute` | `EXECUTE format(...)` |

### Plan caching

Each `PLpgSQL_expr` (an expression or SQL statement within the function) holds a `CachedPlan`. PL/pgSQL prepares the plan via `SPI_prepare` on first execution and stores it in the expr. When the schema changes (sinval message), PL/pgSQL invalidates the cached plan and re-prepares it on the next execution. This means PL/pgSQL functions adapt to schema changes without requiring recreation.

## SECURITY DEFINER

When `prosecdef = true`, `fmgr` saves the current role, switches to the function's owner role (`proowner`) before the call, and restores the original role after. This applies to all language handlers.

## Function volatility and the planner

The `provolatile` flag affects planner decisions:

| Volatility | Effect |
|---|---|
| `IMMUTABLE` | Can be evaluated at plan time (constant-folded); can be inlined |
| `STABLE` | Can be called once per query; result may be cached within a query |
| `VOLATILE` | Called once per row; cannot be optimised away |

## See also

- [[subsystems/catalog/core-catalogs]] — pg_proc catalog structure
- [[subsystems/executor/expression-eval]] — how FuncExpr nodes are evaluated
- [[subsystems/parser/semantic-analysis]] — how function calls are resolved to pg_proc rows
- [[subsystems/extensions/overview]] — how extension SQL scripts use CREATE FUNCTION

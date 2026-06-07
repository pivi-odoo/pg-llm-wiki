---
title: "PREPARE / EXECUTE / DEALLOCATE"
aliases:
  - prepared statements
  - PREPARE
  - EXECUTE
  - DEALLOCATE
  - PreparedStatement
tags:
  - theme/caching
source_files:
  - src/backend/commands/prepare.c
  - src/include/commands/prepare.h
symbols:
  - PrepareQuery
  - ExecuteQuery
  - DeallocateQuery
  - StorePreparedStatement
  - FetchPreparedStatement
  - DropPreparedStatement
  - EvaluateParams
  - PreparedStatement
  - pg_prepared_statement
---

The SQL-level prepared statement system lets a client parse, analyse, and plan a statement once, then execute it many times with different parameter values. Each backend maintains its own private hash table of named statements. Plans are not shared across backends. `src/backend/commands/prepare.c` handles the SQL `PREPARE` / `EXECUTE` / `DEALLOCATE` commands. It sits on top of the [[subsystems/executor/jit-llvm|plan cache]] machinery in `utils/cache/plancache.c`.

## The Statement Hash Table

Each backend lazily creates a hash table called `prepared_queries` on the first `PREPARE`. Keys are statement names up to `NAMEDATALEN` (64 bytes). Values are `PreparedStatement` structs that record the `CachedPlanSource`, a `prepare_time` timestamp, and a `from_sql` flag. The `from_sql` flag distinguishes SQL-level statements from protocol-level ones created via the extended query protocol.

`PrepareQuery()` (in `prepare.c`) explicitly rejects empty-string statement names, because the protocol reserves the unnamed statement `""` for the simple extended-query flow. A collision would be confusing.

## PREPARE

`PrepareQuery()` does three things:

1. **Parse and analyse** — `PrepareQuery()` wraps the raw parse tree in a `RawStmt`. It then calls `pg_analyze_and_rewrite_varparams()`. During analysis, `PrepareQuery()` can infer from context any parameter types that `PREPARE` did not declare (e.g. `$1 + 1` implies `int4`). It passes the inferred type array back via in-out parameters.
2. **Create a plan source** — `CreateCachedPlan()` records the raw parse tree before analysis. PostgreSQL can then re-create the plan if schema changes later invalidate the cached plan.
3. **Store** — `StorePreparedStatement()` adds the entry to the hash table. It then calls `SaveCachedPlan()`, which moves the plan source to long-lived memory. This lets the plan source survive the current transaction.

## EXECUTE

`ExecuteQuery()` retrieves the named `PreparedStatement`. It evaluates the supplied parameter expressions, type-checking and coercing each against the declared parameter types. Then it runs the plan through the standard portal machinery:

1. **Evaluate parameters** — `EvaluateParams()` runs parse analysis on the raw parameter expressions. It coerces them to the expected types. It builds a `ParamListInfo`.
2. **Replan if needed** — `GetCachedPlan()` checks whether the cached plan is still valid (schema hasn't changed). It replans if necessary, incrementing either `num_generic_plans` or `num_custom_plans` in the plan source.
3. **Run** — `ExecuteQuery()` creates a transient portal. It binds the plan to the portal. `PortalRun()` then drives execution. The portal is torn down after the run.

`CREATE TABLE ... AS EXECUTE` goes through the same path, but it verifies the prepared statement is a `SELECT`. It also passes `eflags` and an `IntoClause` to the portal machinery.

## Plan Caching: Generic vs Custom Plans

After a statement has been executed five times, the planner considers switching to a *generic plan* — one compiled without knowing the specific parameter values. The planner compiles a *custom plan* for each execution, using the actual parameter values. This lets the planner use statistics about those values, but it costs a full planning pass each time.

The plan source tracks both counters (`num_generic_plans`, `num_custom_plans`). It exposes them through `pg_prepared_statements`. `GetCachedPlan()` makes the choice between them inside `plancache.c`, not inside `prepare.c`.

## DEALLOCATE

`DeallocateQuery()` calls `DropPreparedStatement()` (or `DropAllPreparedStatements()` for `DEALLOCATE ALL`). This releases the `CachedPlanSource` via `DropCachedPlan()` and removes the entry from the hash table. The statement memory is reclaimed immediately.

## EXPLAIN EXECUTE

`ExplainExecuteQuery()` retrieves the statement and optionally replans it. It then runs `ExplainOnePlan()` on each query in the plan list. It measures planning time. It optionally captures buffer usage. It formats the output the same way as a direct `EXPLAIN`.

## pg_prepared_statements

`pg_prepared_statement()` implements the `pg_prepared_statements` view. It walks the `prepared_queries` hash table into a tuplestore. The view exposes name, SQL text, prepare time, parameter types, result types, whether the statement was created via SQL or the protocol (`from_sql`), and the generic/custom plan counters.

```sql
-- Inspect active prepared statements in the current session
SELECT name, statement, prepare_time, parameter_types,
       generic_plans, custom_plans
FROM pg_prepared_statements
ORDER BY prepare_time;
```

## Protocol-Level Prepared Statements

The extended query protocol (`src/backend/tcop/postgres.c`) also uses `StorePreparedStatement()` to register named statements. However, it sets `from_sql = false`. The protocol layer handles the unnamed statement `""` as a special case, without going through `prepared_queries` at all. It replaces the statement on each `Parse` message.

## Related Topics

- [[code-paths/extended-query|Extended Query Protocol]]
- [[subsystems/planner/overview|Planner Overview]]
- [[subsystems/executor/overview|Executor Overview]]
- [[subsystems/memory/resource-owner|ResourceOwner]]

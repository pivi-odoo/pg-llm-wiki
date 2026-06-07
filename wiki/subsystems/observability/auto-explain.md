---
title: auto_explain
aliases:
  - auto_explain extension
  - automatic explain
  - slow query plan logging
tags:
  - symptom/slow-query
source_files:
  - contrib/auto_explain/auto_explain.c
symbols:
  - explain_ExecutorStart
  - explain_ExecutorEnd
  - explain_ExecutorRun
  - explain_ExecutorFinish
  - auto_explain_log_min_duration
  - ExplainPrintPlan
  - ExplainState
  - InstrEndLoop
---

`auto_explain` is a contrib extension that hooks into the executor and emits `EXPLAIN` (optionally `EXPLAIN ANALYZE`) output to the server log whenever a query exceeds a configurable duration threshold. It removes the need to manually reproduce slow queries to capture their plans. Reproducing them is usually impossible in production, where bind parameters vary and table state changes between executions.

## How It Works

The extension installs four executor hooks in `_PG_init`: `ExecutorStart_hook`, `ExecutorRun_hook`, `ExecutorFinish_hook`, and `ExecutorEnd_hook`. These replace (or chain onto) the standard executor entry points.

```
_PG_init()
  └─ installs hooks:
       ExecutorStart_hook  → explain_ExecutorStart
       ExecutorRun_hook    → explain_ExecutorRun
       ExecutorFinish_hook → explain_ExecutorFinish
       ExecutorEnd_hook    → explain_ExecutorEnd
```

**`explain_ExecutorStart`** runs before query execution begins. At the top level (`nesting_level == 0`), it decides whether the current statement will be sampled (using `auto_explain_sample_rate`). If sampling is active and `log_analyze` is enabled, it sets `queryDesc->instrument_options` to request per-node instrumentation — `INSTRUMENT_TIMER`, `INSTRUMENT_ROWS`, `INSTRUMENT_BUFFERS`, or `INSTRUMENT_WAL` depending on GUC settings. It also allocates `queryDesc->totaltime` via `InstrAlloc` in the per-query [[subsystems/memory/contexts|memory context]].

**`explain_ExecutorRun` and `explain_ExecutorFinish`** simply increment and decrement a static `nesting_level` counter around the real executor calls. This counter tracks whether a query is a top-level statement or a nested one (e.g., from a PL/pgSQL function). The `PG_TRY`/`PG_FINALLY` pattern ensures the counter is decremented even on error.

**`explain_ExecutorEnd`** is where the work happens. It calls `InstrEndLoop` to finalize timing. It then compares `queryDesc->totaltime->total * 1000.0` (milliseconds) against `auto_explain_log_min_duration`. On threshold breach, it builds an `ExplainState`, calls `ExplainPrintPlan` to render the plan tree, and emits the result via `ereport` at `auto_explain_log_level` (default `LOG`).

```c
/* Threshold check inside explain_ExecutorEnd */
InstrEndLoop(queryDesc->totaltime);
msec = queryDesc->totaltime->total * 1000.0;
if (msec >= auto_explain_log_min_duration)
{
    ExplainState *es = NewExplainState();
    es->analyze = (queryDesc->instrument_options && auto_explain_log_analyze);
    /* ... configure es fields ... */
    ExplainPrintPlan(es, queryDesc);
    ereport(auto_explain_log_level,
            (errmsg("duration: %.3f ms  plan:\n%s", msec, es->str->data),
             errhidestmt(true)));
}
```

The `errhidestmt(true)` call suppresses the usual `statement:` line that would otherwise appear in the log. The query text is already embedded in the plan output via `ExplainQueryText`, so the line would be redundant.

## Loading the Extension

`auto_explain` is not a traditional extension installed with `CREATE EXTENSION`. It must be loaded as a shared library because it registers hooks at module load time.

**Cluster-wide (persistent):** Add to `postgresql.conf`:

```ini
shared_preload_libraries = 'auto_explain'
auto_explain.log_min_duration = '1s'
```

Requires a server restart. The hooks are active for all sessions.

**Session-level (debugging):** No restart required, no superuser needed for the `LOAD` itself (but `PGC_SUSET` GUCs require superuser or `pg_read_all_settings` to set):

```sql
LOAD 'auto_explain';
SET auto_explain.log_min_duration = 0;   -- log everything
SET auto_explain.log_analyze = on;
SET auto_explain.log_format = 'json';
```

Session-level loading is useful when debugging a specific workload. It affects only that session's queries.

## GUC Reference

| GUC | Type | Default | Notes |
|-----|------|---------|-------|
| `auto_explain.log_min_duration` | integer (ms) | `-1` | `-1` disables; `0` logs all queries |
| `auto_explain.log_analyze` | bool | `off` | Runs EXPLAIN ANALYZE; high overhead |
| `auto_explain.log_timing` | bool | `on` | Per-node wall-clock times; requires `log_analyze` |
| `auto_explain.log_buffers` | bool | `off` | Buffer hit/miss counts; requires `log_analyze` |
| `auto_explain.log_wal` | bool | `off` | WAL bytes generated; requires `log_analyze` |
| `auto_explain.log_triggers` | bool | `off` | Trigger execution times; requires `log_analyze` |
| `auto_explain.log_verbose` | bool | `off` | EXPLAIN VERBOSE output (targetlists, schemas) |
| `auto_explain.log_settings` | bool | `off` | Logs non-default GUCs affecting planning |
| `auto_explain.log_nested_statements` | bool | `off` | Logs queries invoked from PL/pgSQL/functions |
| `auto_explain.log_format` | enum | `text` | `text`, `json`, `yaml`, `xml` |
| `auto_explain.log_level` | enum | `log` | Severity for the log message |
| `auto_explain.log_parameter_max_length` | integer | `-1` | Max bytes of bind parameters to log; `-1` = unlimited |
| `auto_explain.sample_rate` | float | `1.0` | Fraction of queries to log (0.0–1.0) |

All GUCs are `PGC_SUSET` — superuser-settable or grantable via `ALTER ROLE ... SET`.

## The log_analyze Overhead Problem

When `log_analyze = on`, `explain_ExecutorStart` sets `INSTRUMENT_TIMER` on the `QueryDesc`. This causes the executor to call `INSTR_TIME_SET_CURRENT` at the entry and exit of every plan node for every tuple batch. On a query touching millions of rows across a complex plan tree, this can add 10–40% wall-clock overhead.

The overhead is not uniform. `INSTR_TIME_SET_CURRENT` affects hash joins and sequential scans over large tables the most, because it calls `clock_gettime` per node transition. Setting `log_timing = off` (which falls back to `INSTRUMENT_ROWS`) eliminates the per-node clock calls, but it loses actual timing. You still get actual row counts. This is a reasonable compromise for wide reporting queries.

`log_buffers = on` adds `INSTRUMENT_BUFFERS`, which reads `SharedBufferDesc` state. The incremental cost is lower than timing instrumentation but adds noise on buffer-intensive workloads.

## Nested Statements

By default (`log_nested_statements = off`), auto_explain only fires for top-level statements. `explain_ExecutorRun` and `explain_ExecutorFinish` increment the `nesting_level` counter. As a result, any query invoked from within a PL/pgSQL function, a trigger body, or an SPI call has `nesting_level > 0`, and auto_explain silently skips it.

The `auto_explain_enabled()` macro encodes this logic:

```c
#define auto_explain_enabled() \
    (auto_explain_log_min_duration >= 0 && \
     (nesting_level == 0 || auto_explain_log_nested_statements) && \
     current_query_sampled)
```

When `log_nested_statements = on`, auto_explain evaluates every constituent query inside a stored procedure independently against the threshold. A procedure calling ten queries will potentially produce ten plan log entries. This is invaluable for diagnosing performance regressions in stored procedures: you can see the plan for each internal query rather than only the `CALL` statement.

```sql
-- Session-level debugging of a stored procedure
LOAD 'auto_explain';
SET auto_explain.log_min_duration = 0;
SET auto_explain.log_nested_statements = on;
SET auto_explain.log_analyze = on;
CALL my_slow_procedure();
-- Check server log for per-statement plans
```

## Correlating with pg_stat_statements

`pg_stat_statements` tracks aggregate statistics per normalized query. `auto_explain` captures individual-execution plans. Connecting them requires the query fingerprint.

In PostgreSQL 14+, `EXPLAIN (FORMAT JSON)` output includes a `"Query Identifier"` field populated from `pgss_hash_query` when `pg_stat_statements` is also loaded. `auto_explain` exposes this when `log_format = 'json'` and `log_verbose = on`.

Workflow:

1. Identify the slow `queryid` from `pg_stat_statements` (`mean_exec_time`, `max_exec_time`).
2. Match it against the `"Query Identifier"` field in the JSON plan logs.
3. Compare the captured plan against the normalized query's aggregate stats to determine whether the plan regressed or the data changed.

When both extensions are in `shared_preload_libraries`, the order matters: list `pg_stat_statements` before `auto_explain`. This way, query IDs are populated before `auto_explain` reads them.

```ini
shared_preload_libraries = 'pg_stat_statements, auto_explain'
```

## Output Format Considerations

**Text format** (default) is human-readable and integrates naturally with `pg_badger` and most log parsers. Each plan is emitted as a multi-line log message with the `duration:` prefix.

**JSON format** is machine-parseable and preserves all numeric fields without text truncation. Useful for shipping to log aggregation systems (Elasticsearch, Loki). Note the source code fixup: `auto_explain` rewrites the generated JSON array wrapper (`[{...}]`) to a plain object (`{...}`) by replacing the first and last characters before logging.

**YAML/XML** are rarely used in practice; JSON is the structured-format choice for tooling.

## Practical Guidance

**Set a conservative threshold first.** Start with `log_min_duration = 5000` (5 seconds) in production. You will generate enough signal to find real problems without log volume that masks the signal. Lower to 1000 ms once log infrastructure is confirmed.

**Do not enable `log_analyze` in production without understanding the cost.** Even at a high threshold, auto_explain sets `INSTRUMENT_TIMER` on execution start before the threshold check. Every qualifying query pays the instrumentation cost, regardless of whether the plan is ultimately logged. Measure the overhead in staging under production-representative load before enabling.

**Prefer `log_timing = off, log_buffers = on` as a middle ground.** Row counts plus buffer stats usually identify the problematic node (unexpected full scans, large hash tables) without the clock overhead.

**Use `sample_rate` to reduce overhead under load.** Setting `sample_rate = 0.01` logs 1% of queries meeting the threshold. Useful when a pathological query fires thousands of times per minute and you only need one representative plan.

**Session-level LOAD for safe debugging.** Load and configure in a single session, reproduce the problematic query, read the server log. The hooks uninstall when the session disconnects.

**Use JSON format with a log aggregator.** Parse `"Query Identifier"` out of the JSON plan to join with `pg_stat_statements` data in your monitoring system. This gives per-execution plan detail alongside statistical aggregates.

**Watch for parallel workers.** `explain_ExecutorStart` explicitly skips sampling inside parallel workers (`IsParallelWorker()` check). The parent session's `EXPLAIN ANALYZE` output will still show parallel node data collected and reported back from workers via `InstrAggNode`.

## Related Topics

- [[subsystems/observability/pg-stat-statements|pg_stat_statements]] — complements auto_explain by providing aggregate per-query statistics; correlating queryid across both extensions reveals which normalized queries produce the worst plans.
- [[subsystems/planner/reading-explain|Reading EXPLAIN Output]] — explains how to interpret the plan tree that auto_explain emits, including node costs, row estimates, and buffer statistics.
- [[code-paths/explain|EXPLAIN Code Path]] — covers the executor-level ExplainState machinery and ExplainPrintPlan that auto_explain invokes to render plan output.
- [[subsystems/extensions/hooks|Extension Hooks]] — describes the executor hook API (ExecutorStart_hook, ExecutorEnd_hook, etc.) that auto_explain uses to intercept query execution.
- [[subsystems/observability/structured-logging|Structured Logging]] — covers the server logging infrastructure and ereport machinery through which auto_explain emits its plan messages.
- [[troubleshooting/slow-queries|Slow Queries]] — practical guide to diagnosing slow queries, for which auto_explain is a primary diagnostic tool.
- [[subsystems/observability/overview|Observability Overview]] — survey of PostgreSQL's observability facilities and how auto_explain fits alongside pg_stat_statements, wait events, and other instrumentation.

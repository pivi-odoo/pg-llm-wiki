---
title: Structured Log Output (CSV and JSON)
aliases:
  - csvlog
  - jsonlog
  - structured logging
  - log_destination
tags:
  - symptom/slow-query
source_files:
  - src/backend/utils/error/csvlog.c
  - src/backend/utils/error/jsonlog.c
  - src/backend/utils/error/elog.c
  - src/include/utils/elog.h
symbols:
  - write_csvlog
  - write_jsonlog
  - emit_log_hook
  - emit_log_hook_type
  - Log_destination
  - check_log_of_query
---

PostgreSQL's default log output is a human-readable text line prefixed by `log_line_prefix`, which is convenient for manual inspection but brittle for machine parsing. The `csvlog` and `jsonlog` destinations emit the same `ErrorData` envelope in a fixed, self-describing format that log aggregators (Datadog, Loki, ELK, CloudWatch) can ingest without fragile regex parsing. Both formats carry identical fields derived from the same `ErrorData` struct. The difference is entirely in serialisation.

## log_destination and Multi-Sink Output

`log_destination` is a comma-separated list of sinks, not a single choice. A server can write to several simultaneously:

```ini
log_destination = 'stderr,csvlog,jsonlog'
```

Each value maps to a bitmask constant (`LOG_DESTINATION_STDERR`, `LOG_DESTINATION_CSVLOG`, `LOG_DESTINATION_JSONLOG`). When `EmitErrorReport` fires, it iterates the bitmask. It calls the appropriate writer for each active destination. The syslogger process collects output from backend pipes. It writes the output to separate files with `.log`, `.csv`, and `.json` extensions — all under `log_directory`, named according to `log_filename`. The structured destinations only produce files when `logging_collector = on`. Without the collector, there is no file to write to.

`pg_current_logfile('csvlog')` and `pg_current_logfile('jsonlog')` return the active file path for each format, which is useful when the rotation pattern makes the filename unpredictable.

## The emit_log_hook and How Formats Are Invoked

Every log message travels through `EmitErrorReport` in `elog.c`. Before format-specific writers run, `emit_log_hook` fires if set:

```c
if (edata->output_to_server && emit_log_hook)
    (*emit_log_hook)(edata);
```

This hook is the extension point used by external logging libraries and by `pg_stat_statements`-style instrumentation. The structured writers (`write_csvlog`, `write_jsonlog`) are not called via the hook — they are called directly by `send_message_to_server_log` after the hook returns, keyed on the `Log_destination` bitmask. Extensions that want to intercept every log record should install `emit_log_hook`; extensions that want to add a new file format would need to modify the core bitmask logic.

Each backend assembles the log record in a local `StringInfo` buffer. It writes the record to the syslogger pipe via `write_pipe_chunks`. If the caller is the syslogger process itself (backend type `B_LOGGER`), it writes directly to the file with `write_syslogger_file`, bypassing the pipe.

## CSV Format: Fixed Column Contract

`write_csvlog` writes a single CSV row per log message. The column order is fixed and documented. Tools consuming these files must treat the column position as the API, not the column header (there is no header row). The columns in order are:

| Position | Field | Notes |
|----------|-------|-------|
| 1 | `log_time` | Timestamp with milliseconds |
| 2 | `user_name` | Blank for non-client backends |
| 3 | `database_name` | Blank for non-client backends |
| 4 | `process_id` | OS PID |
| 5 | `connection_from` | `host:port` of client |
| 6 | `session_id` | `hex(start_time).hex(pid)` — stable per session |
| 7 | `session_line_num` | Per-process monotonic counter, resets on fork |
| 8 | `command_tag` | PS display string (e.g. `SELECT`, `idle`) |
| 9 | `session_start_time` | When the session connected |
| 10 | `virtual_transaction_id` | `backendId/localXid` |
| 11 | `transaction_id` | Top-level XID, 0 if none assigned |
| 12 | `error_severity` | `LOG`, `WARNING`, `ERROR`, `FATAL`, `PANIC` |
| 13 | `sql_state_code` | Five-character SQLSTATE |
| 14 | `message` | Primary error message |
| 15 | `detail` | `errdetail` or `errdetail_log` text |
| 16 | `hint` | `errhint` text |
| 17 | `internal_query` | Query that triggered an internal error |
| 18 | `internal_query_pos` | Cursor position within `internal_query` |
| 19 | `context` | `errcontext` stack |
| 20 | `query` | User query (when `log_statement` or duration logging triggers) |
| 21 | `query_pos` | Cursor position within `query` |
| 22 | `location` | `func, file:line` when `log_error_verbosity = verbose` |
| 23 | `application_name` | From `application_name` GUC |
| 24 | `backend_type` | `client backend`, `autovacuum worker`, `walsender`, etc. |
| 25 | `leader_pid` | Set only for parallel workers; PID of the leader backend |
| 26 | `query_id` | Numeric query identifier (matches `pg_stat_activity.query_id`) |

The `session_id` column (position 6) is the primary correlation key: it is stable for the lifetime of a connection and identical across CSV, JSON, and stderr lines. `virtual_transaction_id` correlates rows within a single transaction. The full `transaction_id` is only non-zero once a transaction has written to the WAL.

The quoting convention follows RFC 4180: fields are quoted only when they contain a comma, double-quote, or newline. Doubling a double-quote within a field escapes it.

## JSON Format: Named Fields and Sparse Records

`write_jsonlog` (added in PostgreSQL 15) emits one JSON object per line, terminated by a newline — NDJSON / JSON Lines format. Each object begins with `timestamp` and includes only non-null fields. `write_jsonlog` simply omits absent fields rather than emitting them as `null`. This makes JSON records self-describing: a parser does not need to know the column count to extract a field.

The field names used by the JSON format are:

```
timestamp, user, dbname, pid, remote_host, remote_port,
session_id, line_num, ps, session_start, vxid, txid,
error_severity, state_code, message, detail, hint,
internal_query, internal_position, context, statement,
cursor_position, func_name, file_name, file_line_num,
application_name, backend_type, leader_pid, query_id
```

The `detail` field receives `errdetail_log` when present (server-only detail with full technical information), falling back to `errdetail` (the client-visible version). This is identical behaviour to csvlog. The `ps` field is the process-status display string equivalent to the `command_tag` CSV column.

Parsing with `jq` is straightforward:

```bash
# Slow queries from a specific database in the last log file
jq -r 'select(.dbname == "mydb" and .statement != null)
        | [.timestamp, .pid, .query_id, .statement] | @tsv' \
   /var/log/postgresql/postgresql-2024-01-15_000000.json
```

Every field has a stable name, so adding new fields in future releases does not break existing parsers. This differs from CSV, where new columns would change positional offsets.

## Session and Query Correlation

Both formats expose two independent correlation axes:

- **Session axis**: `session_id` (CSV col 6, JSON `session_id`) — a hex string encoding `start_time.pid`. Every log line from the same connection shares this value, making it possible to reconstruct a complete session timeline across log rotations without relying on PID reuse.
- **Query axis**: `query_id` (CSV col 26, JSON `query_id`) — the same identifier exposed in `pg_stat_activity.query_id` and, when the extension is loaded, in [[subsystems/observability/pg-stat-statements]]. This allows correlating a slow-query log line with accumulated statistics in `pg_stat_statements`.

`virtual_transaction_id` provides transaction-level grouping within a session. It resets between transactions, so it cannot be used alone for cross-transaction correlation.

## Slow Query Logging and Statement Capture

The `statement` / `query` field is populated only when `check_log_of_query` returns true. This happens when:

- `log_statement` is set to `all`, `mod`, or `ddl` and the query matches the category, or
- The query duration exceeded `log_min_duration_statement` (logs every qualifying statement), or
- The query duration exceeded `log_min_duration_sample` and the session was selected by `log_parameter_max_length_on_error` sampling (logs a statistical sample).

`log_min_duration_statement = 0` logs every statement with its duration, which is the most common setting for comprehensive slow-query capture. `log_min_duration_sample` with `log_transaction_sample_rate` provides a lower-overhead alternative that captures a representative fraction of all queries.

## Recommended Production GUC Configuration

A pragmatic baseline for production systems that feed a log aggregator:

```ini
# Enable structured output alongside stderr for human fallback
log_destination = 'stderr,jsonlog'
logging_collector = on
log_directory = '/var/log/postgresql'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
log_rotation_age = 1d
log_rotation_size = 100MB

# Capture slow queries with their text
log_min_duration_statement = 1000   # ms; adjust per workload
log_line_prefix = '%m [%p] %q%u@%d '  # still useful for stderr

# Include SQLSTATE in all destinations
log_error_verbosity = default

# For parallel query debugging, leader_pid is invaluable
# No extra config needed — it appears automatically in both CSV and JSON
```

When only the aggregator consumes logs, `log_destination = 'jsonlog'` alone reduces I/O. If the aggregator is unavailable during an incident, having `stderr` as a fallback means the text log is always present.

The `log_line_prefix` GUC applies only to the stderr format. CSV and JSON carry equivalent information in dedicated columns and fields, so `log_line_prefix` has no effect on structured output.

## Related Topics

- [[subsystems/observability/overview]]
- [[subsystems/observability/pg-stat-statements]]
- [[subsystems/observability/pg-stat-activity]]

---
title: Syslogger Background Process
aliases:
  - logging collector
  - log collector
  - syslogger
  - SysLoggerMain
  - logging_collector
tags:
  - theme/observability
source_files:
  - src/backend/postmaster/syslogger.c
  - src/include/postmaster/syslogger.h
symbols:
  - SysLoggerMain
  - SysLogger_Start
  - logfile_rotate
  - logfile_rotate_dest
  - logfile_getname
  - write_syslogger_file
  - PipeProtoHeader
  - PipeProtoChunk
  - sigUsr1Handler
  - process_pipe_input
---

The syslogger is an optional background process, activated only when `logging_collector = on`. It captures all stderr output from every PostgreSQL process and writes it to rotating log files on disk. Because it is the sole writer to those files, it can safely rotate them without locking. It also provides the only mechanism for structured CSV and JSON log formats that require multi-line, field-delimited records.

## The Pipe Redirection Architecture

The design exploits the fact that every backend already writes log messages to stderr via `elog.c`. Rather than giving each backend its own file handle, the postmaster arranges for all of their stderr output to converge at a single point.

During `SysLogger_Start()`, the postmaster creates an OS pipe (`syslogPipe[0]` read end, `syslogPipe[1]` write end) before forking the syslogger child. The child inherits both ends of the pipe, immediately closes the write end, and enters `SysLoggerMain()` to read from `syslogPipe[0]` in a loop. Back in the parent, the postmaster then `dup2`s the write end of the pipe over its own `STDOUT_FILENO` and `STDERR_FILENO`. Every subsequently forked backend inherits that redirected stderr, so any write to `stderr` — including every `elog`/`ereport` call — lands in the pipe without the backend knowing or caring that a log collector is present.

A subtle consequence is that the syslogger's own stderr must not point at the pipe, or it would create a feedback loop. On startup the syslogger checks `redirection_done`. If the pipe is already redirected, it closes its own stdout and stderr, then reopens them to `/dev/null`. Its internal log messages reach the log file via `write_syslogger_file()`, which `elog.c` calls directly when `MyBackendType == B_LOGGER`.

If the syslogger crashes and is restarted, the postmaster cannot recreate the pipe because existing backends already hold the write end open. It therefore keeps `syslogPipe[0]` open across restarts and passes the same pipe to the replacement syslogger child.

## The Pipe Protocol

Plain `write()` calls to a pipe are atomic only up to `PIPE_BUF` bytes (at least 512 bytes per POSIX; commonly 4096 or 65536). Long log messages would be split and interleaved across backends without coordination. The syslogger therefore defines a framing protocol using `PipeProtoHeader`:

- Two leading NUL bytes serve as a recognizable magic marker.
- A 16-bit `len` field counts the payload bytes in this chunk.
- A 32-bit `pid` field identifies the originating backend.
- A `flags` byte carries `PIPE_PROTO_IS_LAST` (whether this is the final chunk of a message) and a destination flag (`PIPE_PROTO_DEST_STDERR`, `PIPE_PROTO_DEST_CSVLOG`, or `PIPE_PROTO_DEST_JSONLOG`).

Each chunk is at most `PIPE_CHUNK_SIZE` bytes, kept within the `PIPE_BUF` atomicity guarantee. The receiver, `process_pipe_input()`, reassembles multi-chunk messages per source PID using a hash-bucketed array of `save_buffer` structs (256 buckets, keyed by `pid % 256`). For non-protocol data (e.g., output from third-party libraries that write to stderr directly), the syslogger writes the data immediately to the stderr log file without reassembly.

## Log Destinations and the log_destination GUC

`log_destination` is a bitmask that controls where log output goes. It is independent of whether the syslogger is running:

| Destination | Requires syslogger | Notes |
|---|---|---|
| `stderr` | No (or yes, with collector) | Plain text to terminal or, when collector is on, to `.log` files |
| `csvlog` | Yes | Structured CSV; requires `logging_collector = on` |
| `jsonlog` | Yes | Structured JSON; requires `logging_collector = on` |
| `syslog` | No | Delegates to the OS syslog daemon |
| `eventlog` | No | Windows Event Log |

The syslogger maintains up to three simultaneously open file handles — `syslogFile` (plain text, always open even when `stderr` is not in `log_destination`), `csvlogFile`, and `jsonlogFile` — because the pipe protocol embeds the destination in each chunk's flags. This means a single backend log call can write the same message to multiple formats: `elog.c` sends the message once per destination flag set, each as its own framed chunk.

When `log_destination` changes at runtime (via SIGHUP), the syslogger detects that a format's file handle should be opened or closed and forces an immediate rotation to bring the file set into sync.

## Log Rotation

Rotation is the act of closing the current log file and opening a new one. Three triggers exist:

**Time-based rotation** (`log_rotation_age`, in minutes): `SysLoggerMain()` computes `next_rotation_time` aligned to the local timezone at startup, then sleeps no longer than the remaining interval. When the deadline arrives it sets `time_based_rotation = true`.

**Size-based rotation** (`log_rotation_size`, in kilobytes): On each main-loop iteration the syslogger calls `ftell()` on each open log file. If any exceeds the threshold, it sets a bitmask indicating which format(s) need rotation.

**On-demand rotation**: `pg_rotate_logfile()` (SQL function) and `pg_ctl logrotate` both create a signal file at `$PGDATA/logrotate`. The postmaster watches for this file via `CheckLogrotateSignal()` on receiving `SIGUSR1` from the SQL function, then forwards the signal to the syslogger via `SIGUSR1`. The `sigUsr1Handler` sets `rotation_requested = true` and calls `SetLatch(MyLatch)` to wake the main loop immediately — this is the self-pipe trick replaced by PostgreSQL's latch mechanism. When neither the time nor size conditions triggered the rotation, `logfile_rotate()` treats it as a manual request and rotates all three formats unconditionally.

The rotation logic in `logfile_rotate_dest()` decides whether to open the new file in append (`"a"`) or truncate (`"w"`) mode: truncation is allowed only when `log_truncate_on_rotation = on`, the rotation was time-based (not manual), and the new filename differs from the previous one. This prevents accidentally overwriting a recently-created file when the clock-aligned name would recycle.

## Log Filename Patterns

The syslogger treats `log_filename` as a `strftime(3)` pattern. The default `postgresql-%Y-%m-%d_%H%M%S.log` produces a timestamped name per rotation. Patterns that do not include time components (e.g., `postgresql.log`) produce a stable filename instead. When `log_truncate_on_rotation = on`, the syslogger overwrites this file on each rotation — useful for keeping only the most recent log.

For CSV and JSON logs, `logfile_getname()` strips a trailing `.log` extension from the pattern result (if present) and appends `.csv` or `.json` respectively, so all three formats share the same base name and timestamp.

For time-based rotations, the syslogger derives the filename timestamp from `next_rotation_time` rather than the actual clock at rotation time. This prevents "slippage", where a brief delay would otherwise cause consecutive files to share a name.

## Shutdown Ordering

The syslogger deliberately ignores `SIGTERM`, `SIGQUIT`, and `SIGINT`. It exits only when it reads EOF on the pipe, which happens only after every process holding the write end — the postmaster and all backends — has exited. This ensures that no dying-gasp error messages are lost. The normal exit path is a clean `proc_exit(0)` call from inside `SysLoggerMain()`, which intentionally does not close `syslogFile` first. This lets any `proc_exit` callback that calls `elog` still reach the log.

## The current_logfiles Metainfo File

After each rotation, `update_metainfo_datafile()` rewrites `$PGDATA/current_logfiles` atomically (write to a `.tmp` file, then `rename(2)`) with one line per active destination mapping the format name to its current log file path. This allows monitoring tools and the `pg_current_logfile()` SQL function to discover the active log file name without parsing the log directory.

## Related Topics

- [[subsystems/background/autovacuum|autovacuum]] — another optional background process started by the postmaster
- [[subsystems/background/bgwriter|bgwriter]] — background writer, illustrates the general postmaster child lifecycle
- [[subsystems/wal/checkpoint|checkpointer]] — uses a similar latch-based main loop and SIGHUP reload pattern

---
title: "Per-Backend Activity Statistics"
aliases:
  - pg_stat_backend
  - backend statistics
  - pgstat_backend
  - PgStat_Backend
tags:
  - symptom/high-io
source_files:
  - src/backend/utils/activity/pgstat_backend.c
  - src/include/utils/pgstat_internal.h
symbols:
  - PgStat_Backend
  - PgStat_BackendPending
  - PgStatShared_Backend
  - pgstat_create_backend
  - pgstat_flush_backend
  - pgstat_count_backend_io_op
  - pgstat_count_backend_io_op_time
  - pgstat_fetch_stat_backend
  - pgstat_fetch_stat_backend_by_pid
  - pgstat_tracks_backend_bktype
  - pgstat_backend_flush_cb
---

Per-backend activity statistics, introduced in PostgreSQL 18, expose cumulative I/O and WAL counters for each running backend process in the `pg_stat_backend` view. Where `pg_stat_io` aggregates I/O across all processes of a given backend type, these statistics give the same breakdown at the individual-process level. This makes it possible to identify the specific backend generating unusual I/O load, without relying on OS-level tools.

The implementation lives in `pgstat_backend.c`. It follows the same statistical subsystem pattern used by relation stats, WAL stats, and others. Per-process pending counters accumulate locally. The flush machinery later moves them into a shared-memory entry keyed by proc number.

## What Is Tracked

Each backend entry (`PgStat_Backend`) stores two groups of counters:

**I/O statistics** (`io_stats`, type `PgStat_BktypeIO`): a three-dimensional array indexed by IOObject (relation, temp relation, WAL buffer), IOContext (normal, vacuum, bulk read, etc.), and IOOp (read, write, extend, fsync, etc.), recording hit counts, byte counts, and cumulative time (when `track_io_timing` or `track_wal_io_timing` is enabled). The shape of this array is the same as the per-type entries in `pg_stat_io`.

**WAL statistics** (`wal_counters`, type `PgStat_WalCounters`): `wal_records`, `wal_fpi` (full-page images), `wal_buffers_full`, and `wal_bytes` — the same fields found in `pg_stat_wal_usage` on a per-query basis, but accumulated over the backend's lifetime.

A `stat_reset_timestamp` records when `pg_stat_reset_backend_stats()` last reset the counters.

## Pending Counter Design

PostgreSQL holds pending counters in process-local static memory (`PendingBackendStats`) rather than the normal `PgStat_EntryRef->pending` pathway. This makes it safe to call `pgstat_count_backend_io_op()` and `pgstat_count_backend_io_op_time()` from within critical sections, where memory allocation is forbidden. PostgreSQL sets the flag `pgstat_report_fixed = true` on each update, so the flush machinery knows there is work to do.

WAL counters use a different approach: PostgreSQL computes them as a diff against `prevBackendWalUsage` at flush time, subtracting saved global WAL counters from the current `pgWalUsage`. This avoids the need to maintain a separate running total.

## Flush and Lifetime

`pgstat_flush_backend()` merges pending data into the shared-memory entry under `PGSTAT_KIND_BACKEND`. It accepts `flags` to flush only I/O, only WAL, or both, allowing callers to amortize locking costs. The flush is lock-free for `nowait=true`. If the lock is contested, the function returns `true`. The caller then retries the data at the next opportunity.

PostgreSQL creates a backend's entry when the backend attaches (`pgstat_create_backend()`) and releases it when the backend exits. Unlike most pgstat entries, PostgreSQL **does not write backend stats to the on-disk stats file** — they exist only in shared memory for the lifetime of the process. Once the backend exits, its statistics disappear from `pg_stat_backend`.

## Which Backends Participate

Not all backend types contribute to this subsystem. `pgstat_tracks_backend_bktype()` returns true for:

- Regular client backends (`B_BACKEND`)
- [[subsystems/background/autovacuum|Autovacuum]] workers (`B_AUTOVAC_WORKER`)
- Background workers (`B_BG_WORKER`)
- WAL sender, receiver, writer, and summarizer
- Slot sync worker and standalone backends

PostgreSQL excludes single-instance auxiliary processes whose I/O already appears in `pg_stat_io` by type ([[subsystems/background/bgwriter|bgwriter]], checkpointer, startup process), to avoid double-counting at the aggregate level.

## Related Topics

- [[subsystems/observability/pgstat-shmem]] — the shared-memory statistics storage architecture
- [[subsystems/observability/pgstat-per-subsystem]] — how per-subsystem stats are structured
- [[subsystems/observability/pg-stat-io]] — the aggregate I/O view these counters complement
- [[subsystems/observability/pg-stat-activity]] — per-backend wait state and current query

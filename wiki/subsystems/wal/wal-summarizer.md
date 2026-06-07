---
title: "WAL Summarizer"
aliases:
  - "WAL Summary"
  - "wal_summary"
  - "summarize_wal"
  - "incremental backup WAL"
source_files:
  - src/backend/postmaster/walsummarizer.c
  - src/include/postmaster/walsummarizer.h
  - src/common/blkreftable.c
  - src/include/common/blkreftable.h
  - src/backend/backup/walsummary.c
  - src/backend/backup/walsummaryfuncs.c
  - src/bin/pg_walsummary/pg_walsummary.c
symbols:
  - WalSummarizerMain
  - BlockRefTable
  - pg_available_wal_summaries
  - pg_wal_summary_contents
  - pg_get_wal_summarizer_state
---

The WAL summarizer is a background process introduced in PostgreSQL 17 that reads the WAL stream and produces compact binary summary files recording which data blocks were modified in each LSN range. These summary files are the foundation for incremental backups: instead of comparing every data block, `pg_basebackup --incremental` reads the summary files to determine exactly which blocks have changed since the previous backup.

## What the Summarizer Produces

For each completed WAL segment (or group of segments), the summarizer writes a summary file under `$PGDATA/pg_wal/summaries/`. Each file covers a specific LSN range and records a `BlockRefTable` — a mapping from relation forks and segment numbers to the set of block numbers modified within that LSN range.

The `BlockRefTable` format (`src/backend/access/common/blkreftable.c`) stores block references in a compressed form. The format groups blocks by relation fork (main, [[subsystems/storage/fsm|FSM]], VM, init). Within each fork, the format encodes blocks using a combination of per-block entries and "limit block" entries. A limit-block entry records the block count of the relation at a point in time. This covers all blocks that existed at that moment, for truncation detection. A "modified" flag and a separate bitmask cover the most recently referenced blocks efficiently.

PostgreSQL names summary files by their LSN range: `$start_lsn-$end_lsn-$tli.summary`. They accumulate until the summarizer removes them, based on `wal_summary_keep_time`. Introspect them with `pg_available_wal_summaries()` and `pg_wal_summary_contents()`, or from the command line with `pg_walsummary`.

## Process Lifecycle

The summarizer process (`WalSummarizerMain()`) starts at server startup when `summarize_wal = on` (required for incremental backup). It runs as a long-lived background process, similar to the checkpointer or [[subsystems/background/bgwriter|bgwriter]].

The process reads WAL records from the beginning of the current WAL timeline, tracking the current summarized LSN. It maintains its position in shared memory so that `pg_get_wal_summarizer_state()` can report progress. After it catches up to the current write position, it waits for new WAL using latches. It wakes when the WAL writer advances the write position.

For each WAL record it reads, it extracts all block references using the standard WAL resource manager interface — the same mechanism REDO uses. The difference is that the summarizer does not apply any page-level changes. It only records which blocks changed. The summarizer treats full-page writes (FPIs) as block references like any other modification.

When the summarizer crosses a segment boundary, it flushes the accumulated `BlockRefTable` to a new summary file on disk and advances the durable summarized LSN recorded in `pg_control`. The durable LSN is what `pg_basebackup --incremental` reads when starting an incremental backup. It represents the upper bound of blocks that the summarizer has definitely covered.

## Interaction with Incremental Backup

`pg_basebackup --incremental` takes a manifest from the previous backup as input. The previous backup's manifest records the summarized LSN at the time PostgreSQL took that backup. The incremental backup reads all available WAL summary files covering LSNs between that prior LSN and the current summarized LSN. It merges their `BlockRefTable` contents and copies only the blocks listed as modified. It copies any blocks not yet summarized conservatively.

The WAL summarizer must be ahead of the backup start LSN for the incremental backup to succeed. If `summarize_wal` was off during the interval since the previous backup, no summary files exist. The incremental backup then fails with a clear error.

## Configuration

| GUC | Default | Effect |
|-----|---------|--------|
| `summarize_wal` | `off` | Enables the WAL summarizer process. Required for incremental backup. |
| `wal_summary_keep_time` | `10 days` | How long summary files are retained before the summarizer removes them. |

Set `summarize_wal = on` in `postgresql.conf` to enable the process. Enabling summarization adds modest overhead, because the summarizer reads all WAL sequentially. On a very write-heavy server, the summarizer may briefly lag behind the WAL writer, but it catches up quickly.

## Inspecting Summary State

```sql
-- Current summarizer position
SELECT * FROM pg_get_wal_summarizer_state();

-- Available summary files
SELECT * FROM pg_available_wal_summaries() ORDER BY start_lsn;

-- Contents of a specific summary file
SELECT * FROM pg_wal_summary_contents(start_lsn, end_lsn, tli)
ORDER BY relfilenode, reltablespace, relforknum, block_num;
```

From the command line:

```
pg_walsummary -i $PGDATA/pg_wal/summaries/<name>.summary
```

## Related Topics

- [[subsystems/replication/base-backup|pg_basebackup and Base Backups]] — incremental backup using WAL summary files
- [[subsystems/wal/overview|WAL Overview]] — WAL record structure and resource managers
- [[subsystems/wal/wal-records|WAL Record Format]] — how block references are encoded in WAL records

---
title: "Incremental Base Backup"
aliases:
  - incremental backup
  - pg_basebackup --incremental
  - IncrementalBackupInfo
  - INCREMENTAL. files
source_files:
  - src/backend/backup/basebackup_incremental.c
  - src/include/backup/basebackup_incremental.h
symbols:
  - IncrementalBackupInfo
  - CreateIncrementalBackupInfo
  - AppendIncrementalManifestData
  - FinalizeIncrementalManifest
  - PrepareForIncrementalBackup
  - GetFileBackupMethod
  - GetIncrementalFilePath
  - GetIncrementalHeaderSize
  - GetIncrementalFileSize
  - FileBackupMethod
---

Incremental base backup, introduced in PostgreSQL 17, allows `pg_basebackup` to take a backup that contains only the data blocks changed since a prior backup rather than every block in the cluster. An incremental backup is much smaller than a full backup for typical workloads, and `pg_basebackup` can produce it in a fraction of the time. The cost is a reconstruction step (`pg_combinebackup`) before the result can serve as a recovery or standby base.

The server-side code in `basebackup_incremental.c` is responsible for understanding the prior backup's manifest and deciding, file by file and block by block, what to send. The [[subsystems/wal/wal-summarizer|WAL summarizer]] actually performs the block-change tracking; it records every modified block in summary files under `pg_wal/summaries/`. Incremental backup reads those summaries to determine which blocks have changed.

## The Prior Backup Manifest

Every `pg_basebackup` produces a `backup_manifest` — a JSON document listing every file in the backup, its size and checksum, and the WAL range required to make the backup consistent. For an incremental backup, the client supplies this manifest from the *prior* backup. The server parses it incrementally (in up to 128 KB chunks) via `AppendIncrementalManifestData()` and `FinalizeIncrementalManifest()` to extract:

- **WAL ranges** — the timeline(s) and LSN span covered by the prior backup, used to identify which WAL summaries to read.
- **File list** — the set of files present in the prior backup, used as a sanity check: a file that qualifies for incremental transfer but does not appear in the prior backup's manifest gets sent in full instead.

The server rejects manifest version 1 (from pre-PG17 servers), because it lacks the information needed to support incremental backups.

## Preparing the Block Reference Table

`PrepareForIncrementalBackup()` is the central operation. It:

1. Validates the WAL ranges in the manifest against this server's timeline history, ensuring it is taking the incremental backup against a direct descendant of the prior backup.
2. Waits for the WAL summarizer to finish summarizing up to the backup start LSN (`WaitForWalSummarization()`).
3. Reads all WAL summary files that overlap the LSN range from the prior backup's start through the current backup's start, merging them into a single in-memory `BlockRefTable` (`ib->brtab`).

The merged block reference table records, for every relation fork, the complete set of blocks that were written since the prior backup. It also records "limit blocks" — the maximum block count of each relation at the time of certain WAL records — which allow the reconstruction tool to detect truncations correctly.

## Per-File Backup Decisions

For each file in the cluster, `GetFileBackupMethod()` returns one of two values:

- `BACK_UP_FILE_FULLY` — send the entire file as in a regular backup.
- `BACK_UP_FILE_INCREMENTALLY` — send only the changed blocks, plus a header listing which blocks are included.

`GetFileBackupMethod()` takes the full-backup path unconditionally for:
- Files not present in the prior backup's manifest (new files since the last backup).
- Free-space map forks — [[subsystems/storage/fsm|FSM]] is not fully WAL-logged, so the server always sends it completely.
- Files whose size is not a multiple of `BLCKSZ`, or which are larger than a segment.
- Files whose entire database OID / tablespace OID combination was created after the prior backup (the WAL summary will record this as a limit block of zero for the database entry).
- Files where over 90% of blocks have changed — the overhead of an incremental file exceeds its benefit.

When `GetFileBackupMethod()` chooses `BACK_UP_FILE_INCREMENTALLY`, it returns the sorted list of relative block numbers to include and a `truncation_block_length` — the number of blocks the reconstructed file should have at minimum. `pg_combinebackup` should treat blocks at or beyond the limit block that are not present in the incremental backup as new (zero-filled or absent), while it should fetch blocks below the limit block that are absent from the prior backup.

## Incremental File Format

Incremental files are placed under `INCREMENTAL.<original-name>[.<segno>]` within the same directory as the original file. Their format (`GetIncrementalHeaderSize()`, `GetIncrementalFileSize()`) is:

1. A magic number (4 bytes).
2. The truncation block length (4 bytes).
3. The count of included blocks (4 bytes).
4. The included block numbers (4 bytes each), padded to a multiple of `BLCKSZ` when any blocks follow.
5. The page data for each included block (one `BLCKSZ`-sized page each).

`pg_combinebackup` reads this format to reconstruct the full file by merging the included blocks with the corresponding blocks from the prior backup.

## Relationship to WAL Summaries

`PrepareForIncrementalBackup` constructs the block reference table that drives incremental backup decisions entirely from WAL summary files (`walsummary.c`). Each summary file covers a contiguous LSN range on one timeline; `PrepareForIncrementalBackup()` checks that the set of available summaries covers the full range without gaps (`WalSummariesAreComplete()`), returning an error that names the first uncovered LSN if any gap exists.

The WAL summarizer must be running (controlled by the `summarize_wal` GUC, on by default when `wal_level >= replica`) for incremental backups to be available. If summarization has fallen behind, `PrepareForIncrementalBackup()` waits for it to catch up.

## Related Topics

- [[subsystems/wal/wal-summarizer]] — the background process that produces the summary files
- [[subsystems/replication/base-backup]] — full base backup internals
- [[subsystems/replication/basebackup-sinks]] — how the backup stream is assembled and sent
- [[subsystems/replication/pitr]] — using backups for point-in-time recovery

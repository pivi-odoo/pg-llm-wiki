---
title: WAL Archiver Process
aliases:
  - pgarch
  - WAL archiver
  - archive_command
tags:
  - symptom/disk-full
  - theme/durability
source_files:
  - src/backend/postmaster/pgarch.c
  - src/include/postmaster/pgarch.h
symbols:
  - PgArchiverMain
  - pgarch_MainLoop
  - pgarch_ArchiverCopyLoop
  - pgarch_archiveXlog
  - pgarch_readyXlog
  - pgarch_archiveDone
  - PgArchWakeup
  - PgArchForceDirScan
  - PgArchData
  - arch_files_state
  - ArchiveModuleCallbacks
---

# WAL Archiver Process

PostgreSQL's crash-recovery guarantee only covers data that can be replayed from WAL segments still present in `pg_wal/`. Once a segment is recycled — which happens at every checkpoint — that history is gone from the primary. The WAL archiver exists to bridge that gap. It copies each completed segment to an external location before the segment can be recycled, making point-in-time recovery (PITR) and off-site disaster recovery possible. Without the archiver, an operator can only restore a cluster to the last base backup plus whatever WAL has not yet been overwritten.

## How the Archiver Fits Into the Process Hierarchy

The archiver is a dedicated auxiliary process, forked by the postmaster when `archive_mode` is `on` or `always`. It runs independently of backends. It shares no buffer pool work. Its only job is to consume completed WAL segments and hand them off to the configured archive destination.

The archiver communicates with the rest of the system through two thin interfaces: a small shared memory structure (`PgArchData` in `pgarch.c`) and the `pg_wal/archive_status/` directory. The shared memory holds the archiver's `pgprocno`, so that other processes can wake it via its latch. It also holds a `force_dir_scan` flag that requests an immediate rescan of the status directory.

```c
typedef struct PgArchData
{
    int     pgprocno;       /* pgprocno of archiver process */
    bool    force_dir_scan; /* protected by arch_lck */
    slock_t arch_lck;
} PgArchData;
```

## The .ready / .done Convention

Small status files in `pg_wal/archive_status/` mediate the handoff between WAL production and archival. When the WAL writer or checkpointer finishes filling a segment, it creates a file named `<segment>.ready` in that directory. The archiver scans for `.ready` files and archives each one. It then renames the status file to `<segment>.done`. A checkpoint process later sees the `.done` file. It may then recycle or remove the WAL segment itself.

This two-phase convention — write `.ready`, archive, rename to `.done` — is the enforcement point for WAL retention. PostgreSQL **cannot** recycle a segment until its `.done` file exists. Replication slots impose a parallel constraint: they hold a segment until every slot's `restart_lsn` has advanced past it (see [[subsystems/replication/slots]]).

The `.done` rename is intentionally non-durable. `pgarch_archiveDone()` calls `rename()` without an `fsync()`. If the server crashes between a successful archive and the rename, the archiver will archive the segment again after restart. Archive commands and libraries must therefore tolerate re-archival of a segment that already exists at the destination.

Status filenames follow strict rules. Valid segment names are 16–40 characters drawn from `[0-9A-F.historybackuppartial]` (as defined by `VALID_XFN_CHARS` in `pgarch.h`). Timeline history files (`.history`) and backup label files (`.backup`) also pass through the same pipeline.

## How the Archiver Is Woken Up

The archiver's main loop (`pgarch_MainLoop`) sleeps on its process latch with a hard-wired 60-second timeout (`PGARCH_AUTOWAKE_INTERVAL`). Any process that creates a new `.ready` file calls `PgArchWakeup()`, which sets the archiver's latch directly using `ProcGlobal->allProcs[arch_pgprocno].procLatch`. This avoids lock acquisition: if `pgprocno` has become stale between the read and the `SetLatch`, the worst outcome is a spurious wakeup of the wrong process; the archiver will catch up on its next 60-second poll anyway.

When a timeline switch produces a `.history` file that must be archived urgently, the WAL machinery calls `PgArchForceDirScan()`, which sets `PgArch->force_dir_scan` under the spinlock. The next directory scan then ignores its cached file list and re-reads the directory from scratch.

## Selecting Which Segment to Archive Next

Each time the archiver wakes, it calls `pgarch_ArchiverCopyLoop()`. This function drives a loop that exhausts all pending `.ready` files before sleeping again. The inner function `pgarch_readyXlog()` maintains a local cache to avoid re-scanning the status directory for every file.

On a fresh scan, the archiver reads up to 64 entries (`NUM_FILES_PER_DIRECTORY_SCAN`) into a max-heap ordered by archival priority. The comparator (`ready_file_comparator`) places timeline history files above all segment files. Within each category, it prefers lexicographically earlier names, which correspond to older LSNs. Segments on smaller timeline IDs sort before those on larger ones. The archiver then drains the heap into an array in ascending priority order, so the highest-priority file is popped last.

This ordering matters: WAL segments form a chain. A restore process replaying them must consume them in sequence. Archiving out of order would leave gaps that block recovery.

## Archive Execution and the Module System

PG 16 generalises archive execution through a module interface. When `archive_library` is empty, the archiver loads a built-in shell module (`shell_archive_init` from `shell_archive.c`) that wraps the traditional `archive_command` string by forking a shell. When `archive_library` names a shared library, the archiver calls its `_PG_archive_module_init()` entry point. It receives an `ArchiveModuleCallbacks` struct in return:

| Callback | Purpose |
|---|---|
| `startup_cb` | Called once when the archiver starts; allocate resources. |
| `check_configured_cb` | Returns false if archiving is not yet ready (e.g., destination not reachable). |
| `archive_file_cb` | The core callback; must copy the file and return true on success. |
| `shutdown_cb` | Called on archiver exit; release resources. |

The shell module's `archive_file_cb` substitutes `%p` (pathname) and `%f` (filename) into `archive_command`. It then forks a shell and waits for exit status 0. The module treats any non-zero exit as failure. Custom modules can bypass the shell entirely, writing to S3, GCS, or any other medium.

If archival fails, the archiver retries up to `NUM_ARCHIVE_RETRIES` (3) times with a 1-second pause between attempts. After exhausting retries it logs a WARNING and returns, deferring the next attempt until the next wakeup cycle.

## Orphan Status Files

A crash can leave `.ready` files in `archive_status/` for WAL segments that have already been recycled or removed — for example, if a checkpoint deleted the segment but the server crashed before cleaning up the status file. When `pgarch_ArchiverCopyLoop()` picks up a `.ready` file and finds that the corresponding WAL segment no longer exists in `pg_wal/`, it removes the orphan status file rather than failing. The archiver makes up to `NUM_ORPHAN_CLEANUP_RETRIES` (3) removal attempts.

## archive_mode = always and Standby Archiving

The default `archive_mode = on` starts the archiver only on a primary. Setting `archive_mode = always` also starts the archiver on standbys. A standby running in `always` mode archives WAL segments that it streams or fetches from the primary, producing an independent archive stream. This enables PITR of the standby itself without burdening the primary's archive. It also guarantees that the archiver captures WAL even during a failover window when the primary may be unavailable.

The tradeoff is that both the primary and any `always` standby will attempt to archive the same segment names. Archive commands and libraries must handle this gracefully — for example by making the copy idempotent, or by writing to separate destination paths.

## WAL Retention: Three Pillars

The archiver is one of three mechanisms that hold WAL in `pg_wal/` and prevent premature recycling:

1. **Checkpoint recycling threshold** — controlled by `wal_keep_size`; keeps a rolling window of recent segments regardless of archival state.
2. **Replication slots** — hold segments until every slot's consumer has advanced past them (see [[subsystems/replication/slots]]).
3. **Archiver** — holds segments until a `.done` file exists, i.e., until the archive command exits 0.

These three constraints are independent. A segment is only eligible for recycling when it satisfies all applicable constraints. An archive destination that is unreachable will cause `pg_wal/` to grow unboundedly — a well-known operational risk that monitoring must catch.

## Monitoring

The `pg_stat_archiver` view exposes the archiver's cumulative counters, updated via `pgstat_report_archiver()` after each attempt:

| Column | Meaning |
|---|---|
| `archived_count` | Total segments successfully archived since last stats reset. |
| `last_archived_wal` | Name of the most recently archived segment. |
| `last_archived_time` | Timestamp of that successful archive. |
| `failed_count` | Total failed archive attempts. |
| `last_failed_wal` | Name of the segment that last failed. |
| `last_failed_time` | Timestamp of that failure. |
| `stats_reset` | When the statistics were last reset. |

A rising `failed_count` or a `last_failed_time` that is more recent than `last_archived_time` signals that the archive destination is unreachable or that `archive_command` is misconfigured. Because failed segments block recycling, prompt investigation is critical.

## Recovery: The Inverse Direction

The archiver's counterpart during recovery is `restore_command`. When the startup process replays WAL and reaches a segment boundary that is not present in `pg_wal/`, it runs `restore_command` to fetch the segment from the archive. This is the exact inverse of archival: the same segment names, the same `%p` and `%f` substitutions, but pulling rather than pushing. See [[subsystems/wal/recovery]] for how the startup process drives replay.

The archive and `pg_wal/` together form the complete WAL history needed for PITR. A base backup, an unbroken archive, and a target recovery time are the three ingredients for restoring a cluster to any past point.

## Lifecycle and Restart Behaviour

The postmaster enforces a minimum restart interval of 10 seconds (`PGARCH_RESTART_INTERVAL`) before re-spawning a crashed archiver. This prevents a tight crash loop from consuming resources.

The postmaster coordinates shutdown via SIGUSR2. It sends SIGUSR2 when it wants the archiver to stop. The signal handler sets `ready_to_stop`, which causes the main loop to complete one final archive cycle before exiting cleanly. A plain SIGTERM without a subsequent SIGUSR2 causes the archiver to wait up to 60 seconds before self-terminating, giving the postmaster time to send the proper shutdown sequence.

When an operator changes `archive_library` via `SIGHUP`/reload, the archiver process exits immediately (after calling the old module's shutdown callback) so the postmaster can restart it with the new library loaded. `archive_command` changes take effect in-place without a restart.

## Related Topics

- [[subsystems/wal/archiving|WAL Archiving]] — covers the WAL-side mechanics of segment creation and the archive_status directory that the archiver consumes.
- [[subsystems/wal/recovery|WAL Recovery]] — describes the restore_command counterpart that fetches archived segments during PITR replay.
- [[subsystems/wal/checkpoint|Checkpoint]] — checkpoints determine when WAL segments become eligible for recycling, directly interacting with the archiver's .done convention.
- [[subsystems/replication/slots|Replication Slots]] — slots impose an independent WAL retention constraint alongside the archiver, together preventing premature segment recycling.
- [[subsystems/replication/pitr|PITR]] — point-in-time recovery is the primary use case the archiver enables, combining base backups with a continuous archive stream.
- [[subsystems/background/walwriter|WAL Writer]] — the walwriter and checkpointer create the .ready status files that wake and drive the archiver.
- [[subsystems/observability/pg-stat-replication|pg_stat_replication]] — complements pg_stat_archiver for monitoring the full WAL pipeline from primary through archive to standbys.

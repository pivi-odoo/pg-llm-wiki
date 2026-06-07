---
title: WAL Archiving
aliases:
  - WAL Archive
  - archive_command
  - PITR
tags:
  - theme/durability
  - symptom/disk-full
source_files:
  - src/backend/postmaster/pgarch.c
  - src/backend/archive/shell_archive.c
  - src/backend/access/transam/xlogarchive.c
  - src/include/postmaster/pgarch.h
  - src/include/archive/archive_module.h
symbols:
  - PgArchiverMain
  - pgarch_MainLoop
  - pgarch_ArchiverCopyLoop
  - pgarch_archiveXlog
  - pgarch_readyXlog
  - pgarch_archiveDone
  - PgArchWakeup
  - PgArchForceDirScan
  - XLogArchiveNotify
  - XLogArchiveCheckDone
  - XLogArchiveForceDone
  - PgArchData
  - ArchiveModuleCallbacks
  - ArchiveModuleState
---

# WAL Archiving

WAL archiving copies completed WAL segment files to an external location — a network filesystem, object store, or tape — before PostgreSQL is permitted to recycle them. This single mechanism underpins two essential capabilities. Point-in-time recovery (PITR) lets you restore a database to any moment within an archive window. Off-site backup provides durability independent of the primary server's storage. Without archiving, a segment can be deleted as soon as no streaming standby or replication slot needs it; with archiving, every segment must reach the archive before it can leave `pg_wal/`.

## Enabling Archiving: `archive_mode`

The `archive_mode` GUC controls whether the archiver process runs at all. `src/include/access/xlog.h` defines three values:

| Value | Constant | Behaviour |
|-------|----------|-----------|
| `off` | `ARCHIVE_MODE_OFF` | Archiver not started; segments are recycled freely. |
| `on` | `ARCHIVE_MODE_ON` | Archiver runs on a primary; inactive during recovery. |
| `always` | `ARCHIVE_MODE_ALWAYS` | Archiver runs even on standbys. |

The macro `XLogArchivingActive()` is true whenever the mode is not `off`. `XLogArchivingAlways()` is true only for `ARCHIVE_MODE_ALWAYS`. Both require `wal_level >= replica`, enforced by the assertion inside the macro.

`always` matters for cascading PITR setups: if a standby receives WAL from a primary and the primary's archive is unavailable or was never configured, the standby itself needs to push those segments to an archive. Without `always`, the standby's archiver never wakes up.

## Configuring the Archive Destination

### Shell-command archiving (`archive_command`)

The `archive_command` GUC holds a shell command template. `replace_percent_placeholders()` interpolates two placeholders at runtime (`shell_archive.c`):

- `%p` — the full path to the WAL segment inside `pg_wal/`
- `%f` — just the filename (e.g., `000000010000000100000001`)

PostgreSQL declares a segment archived when the command exits with status 0. Any non-zero exit code means failure; the archiver retries up to `NUM_ARCHIVE_RETRIES` (3) times before logging a warning and moving on to the next cycle. The command must be idempotent — after a crash, the archiver may call it again for a segment it already archived successfully.

```text
archive_command = 'rsync -a %p /mnt/wal-archive/%f'
archive_command = 'aws s3 cp %p s3://mybucket/wal/%f'
```

### Archive module API (`archive_library`)

For programmatic archiving without shell overhead, `archive_library` names a shared library that implements the `ArchiveModuleCallbacks` interface (`archive_module.h`):

```c
typedef struct ArchiveModuleCallbacks
{
    ArchiveStartupCB        startup_cb;          /* optional */
    ArchiveCheckConfiguredCB check_configured_cb; /* optional */
    ArchiveFileCB           archive_file_cb;      /* required */
    ArchiveShutdownCB       shutdown_cb;          /* optional */
} ArchiveModuleCallbacks;
```

The library exposes `_PG_archive_module_init()` returning a pointer to a populated `ArchiveModuleCallbacks`. `ArchiveModuleState.private_data` holds state private to the module. The built-in shell archiver (`shell_archive.c`) is just a thin implementation of this same interface.

`archive_command` and `archive_library` are mutually exclusive; setting both raises an error. On a SIGHUP that changes `archive_library`, the archiver process exits so the postmaster can restart it with the new library loaded. There is no mechanism to unload a shared library in-process.

## The Archiver Process

The WAL archiver runs as a dedicated auxiliary process forked by the postmaster (`pgarch.c`). The postmaster starts it whenever `XLogArchivingActive()` is true and restarts it if it dies, subject to a `PGARCH_RESTART_INTERVAL` (10 seconds) safety throttle.

At startup, the archiver calls `LoadArchiveLibrary()` to resolve the callback table. It allocates a small shared-memory segment (`PgArchData`) for cross-process coordination, and enters its main loop.

```c
typedef struct PgArchData
{
    int      pgprocno;       /* for latch-based wakeup */
    bool     force_dir_scan; /* request immediate rescan */
    slock_t  arch_lck;
} PgArchData;
```

The `pgprocno` field lets any backend wake the archiver via `PgArchWakeup()` without holding a lock: the caller reads `pgprocno` and sets the corresponding process latch. If the archiver has not started yet the set is harmless.

### Main loop and wake-up

`pgarch_MainLoop()` sleeps on a latch with a `PGARCH_AUTOWAKE_INTERVAL` (60 second) timeout. Three things can wake it:

1. Wakeup signal (`PgArchWakeup()`): a backend notifies the archiver after completing a WAL segment (via `XLogArchiveNotify()`).
2. The 60-second timeout fires as a safety poll in case a notification was missed.
3. The postmaster sends SIGUSR2 to request a final archiving cycle before shutdown.

When the archiver receives SIGTERM, it does not immediately quit. It continues its work until either SIGUSR2 arrives (the orderly path) or 60 seconds elapse. At that point it exits so the postmaster can restart it if needed.

### Finding and prioritising segments

`pgarch_readyXlog()` scans `pg_wal/archive_status/` for files ending in `.ready`, batching up to `NUM_FILES_PER_DIRECTORY_SCAN` (64) entries per scan into a max-heap. Oldest segments have the highest priority, determined by lexicographic order of the filenames (which encode timeline and LSN). Timeline history files (`.history`) rank above all WAL segments so that timeline switches propagate to the archive quickly. Recovery needs them before it can traverse a timeline branch.

Between scans, `pgarch_readyXlog()` flattens the heap into an array (`arch_files`) and consumes it without re-reading the directory, keeping the directory open time short. A `force_dir_scan` flag in shared memory, set via `PgArchForceDirScan()`, causes `pgarch_readyXlog()` to discard the cache on the next call. This is used after timeline switches to ensure the new history file is discovered immediately.

### Archiving a segment and marking it done

For each candidate filename, `pgarch_archiveXlog()` constructs the full path (`pg_wal/<name>`) and invokes `ArchiveCallbacks->archive_file_cb()`. On success, `pgarch_archiveDone()` renames the status file from `<name>.ready` to `<name>.done`. `pgarch_archiveDone()` does not perform the rename durably (no `fsync`), because the archive command itself must tolerate re-archiving the same file after a crash. A `.ready` file that reappears after a crash simply triggers another copy attempt.

Before attempting the archive, the copy loop checks that the WAL segment still exists in `pg_wal/`. A crash can leave orphan `.ready` files for segments that have already been recycled; the copy loop deletes these with `unlink()` and skips them.

## The `archive_status/` Tracking Directory

The handshake between the WAL writer and the archiver runs through small marker files in `pg_wal/archive_status/`. The lifecycle of a segment's status file (`xlogarchive.c`):

```
(segment written)
      │
      ▼
<name>.ready     ← XLogArchiveNotify() creates this
      │
      │  archiver invokes archive_file_cb
      │  exit 0 returned
      ▼
<name>.done      ← pgarch_archiveDone() renames .ready → .done
      │
      │  checkpoint calls XLogArchiveCheckDone()
      │  recycling or deletion proceeds
      ▼
(status file removed, segment recycled)
```

The checkpoint process calls `XLogArchiveCheckDone()` before it recycles or removes a WAL segment. If neither `.done` nor `.ready` exists (e.g., the segment was created before archiving was turned on), it creates `.ready` to trigger archival. `XLogArchiveForceDone()` creates `.done` directly, bypassing the archiver. Recovery uses it when a restored segment obviously does not need to be re-archived.

`XLogArchiveIsBusy()` answers "is this segment still waiting to be archived?" by checking for `.ready` in the absence of `.done`. `pg_backup_stop()` uses it to block until all WAL segments needed for a base backup have been archived, giving the caller confidence that the backup plus archive is already self-consistent.

## WAL Retention With and Without Archiving

Without archiving, the primary retains WAL only as far back as the oldest streaming replication slot or `wal_keep_size` requires, bounded by `max_wal_size`. Each checkpoint recycles segments beyond that threshold.

With archiving active, PostgreSQL cannot recycle a segment until `XLogArchiveCheckDone()` returns true for it. If `archive_command` fails persistently, `pg_wal/` will grow without bound. Monitoring the output of `pg_stat_archiver` and the count of `.ready` files in `archive_status/` is essential operational practice.

## Archive Recovery and PITR

To restore from an archive, point `restore_command` at the archive (the inverse of `archive_command`). During recovery, `XLogReadRecord()` fetches segments from the local `pg_wal/` first; if a segment is absent it calls the restore command via `ExecuteRecoveryCommand()`. Recovery replays WAL forward until it either runs out of archived segments or reaches a `recovery_target` (time, XID, LSN, or named restore point).

The full PITR workflow is:

```mermaid
flowchart LR
    A[Base backup<br/>pg_basebackup] --> B[WAL archive<br/>filling continuously]
    B --> C[Restore base backup<br/>to new host]
    C --> D[Set restore_command<br/>+ recovery_target]
    D --> E[Start PostgreSQL<br/>replay proceeds]
    E --> F[Target reached<br/>promote]
```

The base backup records the WAL position at which it started. Recovery must replay all archived WAL from that position to the recovery target, so the archive must contain an unbroken chain of segments. Any gap is fatal to recovery.

### `archive_cleanup_command`

During standby or recovery operation, WAL segments fetched from the archive pile up in `pg_wal/`. After a restartpoint (the standby equivalent of a checkpoint), PostgreSQL calls `archive_cleanup_command` to prune stale archived segments. The standard utility `pg_archivecleanup` implements the correct logic: it identifies the oldest segment still needed and removes everything older from the archive. Without this, archives grow indefinitely.

```text
archive_cleanup_command = 'pg_archivecleanup /mnt/wal-archive %r'
```

PostgreSQL expands the `%r` placeholder to the name of the oldest WAL segment still needed for restart. Passing this value to `pg_archivecleanup` tells it the safe cleanup boundary.

## Operational Notes

**Idempotency is required.** PostgreSQL may call `archive_command` more than once for the same segment after a crash. The command should succeed even if the file already exists at the destination (e.g., use `rsync` or add a guard in a custom script).

**Do not delete `.done` files while the server is running.** Checkpoints use their presence to decide whether to recycle segments. Removing them causes PostgreSQL to treat the segment as un-archived; if archiving is still configured, it will be re-queued.

**Statistics.** The `pg_stat_archiver` system view exposes `last_archived_wal`, `last_archived_time`, `last_failed_wal`, `last_failed_time`, and cumulative counts. A growing `failed_count` or a stale `last_archived_time` signals a broken `archive_command`.

## Related Topics

- [[subsystems/wal/overview|WAL Overview]] — covers segment lifecycle, recycling policy, and the conditions under which `pg_wal/` grows, complementing the archiving gate that blocks recycling.
- [[subsystems/wal/checkpoint|Checkpoint]] — checkpoints drive WAL segment recycling and call `XLogArchiveCheckDone()` before removing any segment, making them the direct consumer of archiving state.
- [[subsystems/wal/recovery|WAL Recovery]] — describes `restore_command`, `ExecuteRecoveryCommand()`, and the replay loop that reads archived segments back during PITR.
- [[subsystems/replication/pitr|Point-in-Time Recovery]] — end-to-end PITR workflow combining base backups with the WAL archive, including recovery target options and promotion.
- [[subsystems/replication/base-backup|Base Backup]] — `pg_basebackup` produces the starting point for PITR; archiving must cover all WAL from the backup start LSN forward.
- [[subsystems/replication/slots|Replication Slots]] — replication slots and archiving are competing retention mechanisms; understanding both is essential to avoid unbounded `pg_wal/` growth.
- [[subsystems/background/archiver|Archiver Process]] — background process reference covering the postmaster lifecycle, restart throttle, and signal handling for the WAL archiver auxiliary process.
- [[architecture/process-architecture|Process Architecture]] — how the archiver fits among the postmaster's other auxiliary processes.

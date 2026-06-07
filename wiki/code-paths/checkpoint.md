---
title: "CHECKPOINT (SQL Command)"
aliases:
  - "CHECKPOINT"
  - "CHECKPOINT command"
tags:
  - theme/durability
  - symptom/high-io
source_files:
  - src/backend/access/transam/xlog.c
  - src/backend/postmaster/checkpointer.c
symbols:
  - RequestCheckpoint
  - CreateCheckPoint
  - CHECKPOINT_IMMEDIATE
  - CHECKPOINT_WAIT
---

# CHECKPOINT (SQL Command)

`CHECKPOINT` is the SQL command that forces a checkpoint immediately instead of waiting for the next time- or WAL-volume-triggered one. It is a thin entry point into the same [[subsystems/wal/checkpoint|checkpoint]] machinery that runs automatically in the background. Running it does not do anything different from an automatic checkpoint. It just moves one up and skips the usual pacing.

## What the command does

Executing `CHECKPOINT` calls `RequestCheckpoint()` with `CHECKPOINT_FORCE | CHECKPOINT_WAIT`, handing the request to the checkpointer process and blocking until it signals completion. Two things make it different from a routine checkpoint:

- **No throttling.** The request also carries `CHECKPOINT_IMMEDIATE`. This bypasses `CheckpointWriteDelay()`'s pacing entirely. A routine checkpoint spreads its writes over most of `checkpoint_timeout`. `CHECKPOINT` writes as fast as storage allows, producing a burst of I/O rather than a gradual one.
- **Runs even if idle.** `CHECKPOINT_FORCE` skips the early-exit check that normally lets an automatic checkpoint do nothing. That check applies when there has been no WAL activity since the last one.

Executing it requires the `pg_checkpoint` role (or superuser) since PostgreSQL 15. Earlier versions require superuser.

## When checkpoints happen without the command

Four conditions trigger a checkpoint automatically: `checkpoint_timeout` elapsing, accumulated WAL exceeding a threshold derived from `max_wal_size`, and server shutdown, in addition to an explicit `CHECKPOINT`. See the full checkpoint mechanism page linked below for the trigger conditions, the request-flag bitmask, and everything that happens inside `CreateCheckPoint()` and `CreateRestartPoint()` — REDO LSN handling, `BufferSync()`, `pg_control` updates, and WAL segment recycling.

## When to run it manually

- Before a filesystem-level backup that isn't driven by `pg_basebackup` (which requests its own checkpoint), to bound how much WAL a restore needs to replay to reach consistency.
- After a large bulk load, when the cost of an immediate checkpoint is preferable to a longer crash-recovery replay following a crash before the next scheduled checkpoint.
- During testing, to force buffers to disk and get a deterministic `pg_control` state without waiting for the timer.

Because it bypasses `checkpoint_completion_target` pacing, an unscheduled `CHECKPOINT` produces a sudden write and fsync burst. This burst can compete with foreground query I/O — the same effect covered from a tuning angle in [[troubleshooting/checkpoint-io-spikes|Diagnosing Checkpoint I/O Spikes]]. Avoid calling it routinely from application code or cron jobs on latency-sensitive systems. Let the timeout and WAL-volume triggers manage pacing instead.

## Related Topics

- [[subsystems/wal/checkpoint|Checkpoint]] — the full mechanism: REDO LSN fixing, BufferSync, pg_control updates, and WAL segment recycling.
- [[troubleshooting/checkpoint-io-spikes|Diagnosing Checkpoint I/O Spikes]] — recognizing and tuning away I/O bursts, including those caused by forced checkpoints.
- [[subsystems/background/bgwriter|Bgwriter]] — the background process that pre-cleans dirty pages so routine checkpoints have less to write.
- [[subsystems/wal/recovery|Recovery]] — how the startup process uses the checkpoint's REDO LSN to bound WAL replay after a crash.

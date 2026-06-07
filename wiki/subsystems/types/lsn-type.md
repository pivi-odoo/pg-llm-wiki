---
title: pg_lsn Type
aliases:
  - LSN
  - Log Sequence Number
  - XLogRecPtr
  - pg_lsn
tags:
  - theme/durability
source_files:
  - src/backend/utils/adt/pg_lsn.c
symbols:
  - pg_lsn_in_internal
  - pg_lsn_out
  - pg_lsn_mi
  - pg_lsn_pli
  - pg_lsn_mii
  - pg_lsn_cmp
  - XLogRecPtr
---

The `pg_lsn` type is PostgreSQL's SQL-level representation of a Log Sequence Number (LSN), the monotonically increasing byte offset that uniquely identifies every position in the [[subsystems/wal/overview|WAL]] stream. Internally it maps directly to `XLogRecPtr`, a `uint64` that encodes an absolute byte offset into the ever-growing WAL. Because every WAL record is written to a specific offset, LSNs serve as the universal coordinate system for recovery, replication lag measurement, and point-in-time restore.

## Storage and Display Format

On disk and in memory, an LSN is a plain 8-byte unsigned integer — the raw byte offset since the beginning of WAL for this instance. The display format splits that 64-bit value into two 32-bit halves separated by a slash:

```
high32 / low32
```

PostgreSQL prints both halves as hexadecimal with no leading zeros, so `0/3000020` means high word `0x00000000` and low word `0x03000020`. The high word increments only after the low word exhausts its entire 4 GiB range, so on a lightly-written instance the high word stays at `0` for a long time.

The parser (`pg_lsn_in_internal`) accepts between one and eight hex digits on each side of the slash and rejects anything else with `ERRCODE_INVALID_TEXT_REPRESENTATION`. On the wire, `pg_lsn_recv`/`pg_lsn_send` transmit the value as a raw `int64` with `pq_getmsgint64`/`pq_sendint64`, making binary protocol handling straightforward.

The slash in the text format is a human-readable boundary marker, not a segment boundary in the WAL file sense. WAL segment filenames encode segment number and timeline separately. `pg_walfile_name()` and `pg_walfile_name_offset()` translate an LSN into the physical filename and byte offset within that file.

## Arithmetic

Subtracting two `pg_lsn` values with the `-` operator returns a `numeric` byte count representing how far apart the two positions are in the WAL stream. The result is signed: if the left operand is smaller, the output is negative. This subtraction underlies `pg_wal_lsn_diff()`, which monitoring tools use to measure replication lag in bytes.

`pg_lsn` also supports addition and subtraction of a `numeric` byte offset (`+` and `-` with a `numeric` right-hand operand), producing a new `pg_lsn`. Both operators route through PostgreSQL's `numeric` arithmetic internally to handle the full signed range without overflow surprises — the operator converts the LSN to `numeric`, performs the operation, and converts the result back.

## Comparison and Indexing

Because `XLogRecPtr` is an unsigned integer, LSN comparisons are total-order comparisons on the raw 64-bit value. `pg_lsn` supports all six standard comparison operators (`=`, `<>`, `<`, `>`, `<=`, `>=`). The type has both btree and hash operator classes, so `pg_lsn` columns can be indexed with either index type. The btree support function (`pg_lsn_cmp`) returns the conventional `{-1, 0, 1}` integer. The hash support delegates directly to `hashint8`, since the underlying representation is the same width.

## Key System Functions

| Function | Returns | Description |
|---|---|---|
| `pg_current_wal_lsn()` | `pg_lsn` | Current WAL write position |
| `pg_current_wal_insert_lsn()` | `pg_lsn` | Current WAL insert position (may not be flushed) |
| `pg_current_wal_flush_lsn()` | `pg_lsn` | Highest LSN known durably flushed to disk |
| `pg_walfile_name(lsn)` | `text` | WAL segment filename containing this LSN |
| `pg_walfile_name_offset(lsn)` | `record` | Filename plus byte offset within the segment |

The distinction between write, insert, and flush LSNs matters for durability: `pg_current_wal_flush_lsn()` is the only one that guarantees the data survives a crash.

## Use in Replication

Replication slots carry two `pg_lsn`-typed fields visible in `pg_replication_slots`:

- `confirmed_flush_lsn` — the LSN up to which the subscriber has confirmed it has received and applied data. The primary will not remove WAL before this point for logical slots.
- `restart_lsn` — the oldest WAL position the slot still needs. The primary retains WAL from this point forward to allow the slot to reconnect after a gap.

Streaming replication standby state in `pg_stat_replication` exposes `sent_lsn`, `write_lsn`, `flush_lsn`, and `replay_lsn`, all `pg_lsn` values. Subtracting `pg_current_wal_lsn() - replay_lsn` gives replication lag in bytes, which is the standard approach for lag alerting.

## Related Topics

- [[subsystems/wal/overview|WAL]] — the write-ahead log stream that LSNs index
- [[subsystems/transactions/mvcc|MVCC]] — transaction visibility and the role of WAL in durability

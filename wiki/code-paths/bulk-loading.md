---
title: Bulk Loading Performance
aliases:
  - Bulk Ingest
  - COPY Performance
  - Fast Load
tags:
  - theme/durability
source_files:
  - src/backend/commands/copy.c
  - src/backend/commands/copyfrom.c
  - src/backend/access/heap/heapam.c
symbols:
  - CopyFrom
  - CopyMultiInsertBuffer
  - BeginCopyFrom
  - NextCopyFrom
  - heap_insert
  - CopyFormatOptions
---

## COPY vs Multi-Row INSERT vs Single-Row INSERT

Three ingest patterns cover most use cases, with dramatically different throughput profiles:

| Method | Relative Speed | Notes |
|--------|---------------|-------|
| `COPY FROM` | Fastest | Single WAL record per batch, minimal expression overhead |
| `INSERT INTO t VALUES (...),(...)` | Moderate | One WAL record per statement, parser overhead per call |
| `INSERT INTO t VALUES (...)` per row | Slowest | Full parse/plan/execute cycle per row |

`CopyFrom()` (`copyfrom.c`) implements `COPY FROM`. It accumulates rows into a batch via `CopyFromInsertBatch()`, flushing to heap storage with a single `heap_insert()` call cluster. The critical advantage is that `COPY FROM` writes WAL once per batch rather than once per tuple. `COPY FROM` bypasses expression evaluation: it converts input columns directly from text/binary to internal Datum representation without running the planner.

`multi-row INSERT` is a reasonable middle ground when you control the client and cannot use `COPY` (e.g., over a connection that only supports SQL). Each `VALUES` row still goes through the executor. At least the batch amortizes the parse/plan overhead.

Single-row `INSERT` per statement is almost always wrong for bulk work. Avoid it.

## COPY Binary Format

`COPY` supports two wire formats controlled by `CopyFormatOptions`:

```sql
-- Text/CSV (default): human-readable, moderate parse cost
COPY t FROM '/data/file.csv' WITH (FORMAT csv, HEADER true);

-- Binary: no text parsing, ~5–15% faster ingest
COPY t FROM '/data/file.bin' WITH (FORMAT binary);
```

Binary format skips all text-to-datum conversion. `BeginCopyFrom()` sets `cstate->opts.binary = true`. `NextCopyFrom()` reads raw type-specific binary representations directly. The trade-off is portability: binary files are version- and architecture-sensitive. Use binary format for pipeline-internal staging where you control both ends; use CSV for interchange.

## UNLOGGED Tables for Staging

```sql
CREATE UNLOGGED TABLE staging_orders (LIKE orders);

COPY staging_orders FROM '/data/orders.csv' WITH (FORMAT csv);

-- Validate, transform, then promote
INSERT INTO orders SELECT * FROM staging_orders WHERE ...;

DROP TABLE staging_orders;
```

`UNLOGGED` tables skip WAL writes for data changes (`RELPERSISTENCE_UNLOGGED` in `pg_class`). The WAL skip applies to heap inserts, index inserts, and [[subsystems/storage/visibility-map|visibility map]] updates during the load. Observed speedup is 2–10× depending on WAL volume and storage latency.

Constraints:
- A crash or unclean shutdown truncates the data.
- Not replicated to standbys (the relation exists but is empty after a standby promotion).
- Indexes on UNLOGGED tables are also unlogged.

The staging pattern (load unlogged → validate → insert into durable table) is the standard approach for ETL pipelines that can tolerate re-running the load on failure.

## Dropping and Recreating Indexes

Incremental index maintenance during a bulk load is expensive: each inserted row triggers a btree descent and potential page splits. Building the index after the fact is a single sort pass over the heap.

```sql
-- Before load
DROP INDEX orders_customer_idx;
DROP INDEX orders_created_at_idx;

COPY orders FROM '/data/orders.csv' WITH (FORMAT csv);

-- After load — serial, fastest rebuild
SET maintenance_work_mem = '1GB';
CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_created_at_idx ON orders (created_at);
```

For a live table that must remain readable during the load, use `CREATE INDEX CONCURRENTLY`. It performs two heap scans. It does not hold a lock that blocks reads or writes. The cost is roughly 2× build time. It cannot run inside a transaction block.

```sql
CREATE INDEX CONCURRENTLY orders_customer_idx ON orders (customer_id);
```

## maintenance_work_mem for Index Builds

Index creation sorts tuples before writing the btree. The sort uses `maintenance_work_mem` as its memory budget. When the sort fits in memory, PostgreSQL writes no temp files. Build time drops significantly.

```sql
SET maintenance_work_mem = '1GB';   -- session-local, safe
CREATE INDEX orders_customer_idx ON orders (customer_id);
RESET maintenance_work_mem;
```

The default (64 MB) is almost always too small for any non-trivial index build. A value of 256 MB–4 GB is typical for bulk load scenarios. On shared servers, be aware that each concurrent index build can consume this much memory independently.

## Deferred Constraints

Foreign key and unique constraint checks normally fire per-row during `INSERT`. Deferring them to commit time eliminates redundant lookups during the load:

```sql
BEGIN;
SET CONSTRAINTS ALL DEFERRED;

COPY orders FROM '/data/orders.csv' WITH (FORMAT csv);

COMMIT;  -- FK and unique checks run once here
```

This requires that the constraints were declared `DEFERRABLE` (or `INITIALLY DEFERRED`):

```sql
ALTER TABLE orders
  ADD CONSTRAINT fk_customer
  FOREIGN KEY (customer_id) REFERENCES customers(id)
  DEFERRABLE INITIALLY IMMEDIATE;
```

For tables where constraints are not deferrable and triggers fire per-row, use:

```sql
ALTER TABLE orders DISABLE TRIGGER ALL;
-- load ...
ALTER TABLE orders ENABLE TRIGGER ALL;
```

`DISABLE TRIGGER ALL` requires superuser or table owner. It suppresses FK enforcement triggers. Ensure referential integrity by other means (pre-validate lookups, or re-enable and run a manual check after).

## autovacuum and ANALYZE After Load

After a large bulk load, the planner's statistics are stale. `ANALYZE` is mandatory before the table is queried under load:

```sql
ANALYZE orders;
-- or with verbosity:
ANALYZE VERBOSE orders;
```

autovacuum will eventually trigger ANALYZE. However, a large table may not reach the threshold (`autovacuum_analyze_threshold + autovacuum_analyze_scale_factor * reltuples`) quickly. The timing is also non-deterministic. Run `ANALYZE` explicitly after every significant bulk load.

`VACUUM` is less urgent after a pure insert load (no dead tuples exist). It becomes relevant if you loaded into a table that had prior data or if you ran `DELETE`/`UPDATE` as part of the staging process.

## Parallel COPY (PG 16+)

PostgreSQL 16 reduced contention when multiple `COPY FROM` sessions target the same table. Previously, concurrent sessions would contend on the same heap pages; from PG 16, the buffer manager and heap extension logic allow better parallel ingestion.

The most effective pattern remains partition-parallel loading:

```sql
-- Each session targets one partition exclusively
-- Session 1:
COPY orders_2024_01 FROM '/data/2024_01.csv' WITH (FORMAT csv);
-- Session 2 (concurrent):
COPY orders_2024_02 FROM '/data/2024_02.csv' WITH (FORMAT csv);
```

Each session acquires its own relation extension lock on a different partition, eliminating cross-session contention entirely. For a non-partitioned table, PG 16+ improvements help but partition-level parallelism is still significantly more effective.

## Practical Guidance

**Minimum viable fast load checklist:**

1. Use `COPY FROM` rather than any `INSERT` variant.
2. Load into an `UNLOGGED` staging table; promote to durable storage after validation.
3. Drop indexes before load; recreate after with `SET maintenance_work_mem = '1GB'`.
4. Wrap the load in a transaction with `SET CONSTRAINTS ALL DEFERRED` if FK constraints are deferrable.
5. Run `ANALYZE` immediately after load completes.
6. If using partitioning, launch one `COPY` session per partition in parallel.

**Format choice:** use binary format for internal pipelines (faster), CSV for anything crossing system boundaries.

**maintenance_work_mem:** set it for the session, not globally, to avoid unexpected memory pressure from concurrent autovacuum workers.

**Triggers:** `DISABLE TRIGGER ALL` is a sharp tool. Document it and re-enable promptly. If FK triggers are disabled, run a post-load integrity check:

```sql
-- Re-validate FK after re-enabling triggers
SET CONSTRAINTS fk_customer IMMEDIATE;
```

**Monitoring in-progress COPY:** query `pg_stat_activity` and `pg_stat_progress_copy` (PG 14+):

```sql
SELECT pid, relid::regclass, command, type, bytes_processed, bytes_total,
       tuples_processed, tuples_excluded
FROM pg_stat_progress_copy;
```

## Related Topics

- [[code-paths/copy|COPY Command]] — detailed walk-through of the COPY code path that bulk-loading relies on for text, CSV, and binary ingest
- [[code-paths/create-index|CREATE INDEX]] — covers index build internals and the sort-based construction that makes post-load index creation cheaper than incremental maintenance
- [[subsystems/storage/bulk-write|Bulk Write]] — storage-layer helpers for writing large volumes of pages efficiently, underpinning COPY's heap insertion strategy
- [[subsystems/transactions/deferrable-constraints|Deferrable Constraints]] — explains how DEFERRABLE foreign-key and unique constraints work and why deferring to commit time speeds up bulk loads
- [[subsystems/background/autovacuum|Autovacuum]] — covers the analyze threshold logic that determines when autovacuum fires after a large insert, relevant to the post-load ANALYZE guidance
- [[code-paths/analyze|ANALYZE]] — details the statistics-collection code path that must be run explicitly after every significant bulk load
- [[subsystems/partitioning/overview|Partitioning Overview]] — partition-parallel COPY (one session per partition) is the most effective strategy for high-throughput ingest into large tables
- [[subsystems/storage/heap|Heap Storage]] — the on-disk tuple format and page layout that COPY's bulk insert path writes into directly
- [[subsystems/wal/overview|WAL Overview]] — the write-ahead log records generated during bulk loads, which UNLOGGED tables and the `wal_level=minimal` optimization aim to avoid

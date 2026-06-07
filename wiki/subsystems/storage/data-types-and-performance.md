---
title: Data Type Choices and Performance
aliases:
  - data types performance
  - type selection postgresql
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/uuid.c
  - src/backend/utils/adt/numeric.c
  - src/backend/access/table/toast_helper.c
  - src/include/access/toast_internals.h
symbols:
  - numeric_add
  - uuid_generate_v4
  - heap_toast_insert_or_update
  - TOAST_MAX_CHUNK_SIZE
  - varattrib_1b
---

## UUID vs bigint/bigserial for Primary Keys

UUID (16 bytes) is twice the physical size of `bigint` (8 bytes). This difference compounds across every index that references the column: foreign key indexes, covering indexes, and the primary key index itself all double in size when switching from `bigint` to UUID.

The more damaging issue with UUIDv4 is access pattern. `uuid_generate_v4()` produces uniformly random values. Every insert lands at a random position in the B-tree. PostgreSQL must then read pages that are not in `shared_buffers` from disk before the insert can proceed. At high insert rates this causes:

- **Index bloat**: B-tree pages fill only partially because splits happen before pages are logically "full" in a sequential sense. This leaves pages at ~50–70% fill on average.
- **Cache thrashing**: the working set of hot index pages becomes the entire index rather than the right-most leaf. This evicts other useful data from `shared_buffers`.

Sequential UUID variants (UUIDv7, ULIDs) embed a millisecond-precision timestamp in the high bits. This makes the sort order monotonically increasing. Insert behaviour then matches `bigserial`: appends go to the rightmost page, and that page stays hot in cache. The storage overhead (16 vs 8 bytes) remains.

**When UUIDs are worth it**: distributed systems that generate IDs across multiple nodes without coordination, or when exposing IDs in APIs where exposing sequential integers leaks enumeration information. In those cases, prefer UUIDv7 over v4 to recover the sequential-insert property.

```sql
-- bigserial: 8 bytes, sequential, single-node only
CREATE TABLE orders (id bigserial PRIMARY KEY, ...);

-- UUIDv7: 16 bytes, sequential, globally unique
CREATE TABLE orders (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, ...);
-- gen_random_uuid() is v4; use pg_uuidv7 extension for v7
```

## text vs varchar(n) vs char(n)

All three use the `varlena` storage format internally (`varattrib_1b` for short strings using a 1-byte header). The on-disk representation is identical for `text` and `varchar(n)`: a length prefix followed by the string bytes. There is no padding, no fixed allocation.

`varchar(n)` adds a single length check at insert/update time — one comparison in `varchar_input()`. There is no performance difference relative to `text` for reads, writes, or index operations.

`char(n)` is the outlier. It pads values with spaces to length `n` on storage. It strips trailing spaces on retrieval. This behavior is surprising: comparisons treat `'a'` and `'a  '` as equal in some contexts. It also wastes space when values are shorter than `n` and provides no performance advantage. Avoid it.

**Rule**: use `text` when the length is unconstrained; use `varchar(n)` when the length constraint is a meaningful business rule you want the database to enforce. The choice is semantic, not performance-driven.

## numeric vs float

`numeric` (arbitrary precision) performs arithmetic in software via `numeric_add` and related functions in `src/backend/utils/adt/numeric.c`. Each operation allocates and manipulates variable-length digit arrays. This is 10–100× slower than native floating-point depending on precision and operand size.

`float8` (double precision, IEEE 754) uses native CPU instructions — a single `FADD` or `FMUL`. Aggregation over millions of rows is dramatically faster.

```sql
-- Slow: software arithmetic, exact
SELECT sum(amount) FROM ledger;  -- amount numeric(18,2)

-- Fast: hardware arithmetic, ~1 ULP rounding error
SELECT sum(reading) FROM sensor_data;  -- reading float8
```

Use `numeric` for: monetary values, any domain where rounding must not accumulate (financial reporting, tax calculations). Use `float8` for: scientific measurements, statistics, ML features, any case where small relative error is acceptable. Do not use `numeric` in tight aggregation loops over tens of millions of rows unless correctness demands it — profile first.

## timestamp with time zone vs without

Both `timestamptz` and `timestamp` store 8 bytes: microseconds since 2000-01-01 00:00:00 UTC. At rest, there is no size difference. The difference is semantic:

- `timestamptz` stores UTC internally. On output, PostgreSQL converts to the session's `TimeZone` setting. Comparisons are unambiguous.
- `timestamp` stores whatever value you give it with no timezone interpretation. Two rows inserted in different session timezones are not comparable without out-of-band knowledge.

`timestamptz` is almost always the correct choice. The one exception is storing "wall clock" times that must remain fixed regardless of DST changes (e.g., a recurring alarm set for "09:00 local time").

**Index non-use pitfall**: function application on the indexed column disables index scans.

```sql
-- Uses index on created_at:
WHERE created_at > '2024-01-01'

-- Does NOT use index — applies DATE() function to every row:
WHERE DATE(created_at) = '2024-01-01'

-- Rewrite to use index:
WHERE created_at >= '2024-01-01' AND created_at < '2024-01-02'
```

## Integer Sizes: int4 vs int8

On modern 64-bit hardware, arithmetic on `int4` (4 bytes) and `int8` (8 bytes) is equally fast — both complete in a single cycle. The difference is index page density.

A B-tree leaf page is 8 kB by default. With a 4-byte key, roughly twice as many entries fit per page compared to an 8-byte key. For a high-cardinality join key (foreign keys, lookup columns), halving the key size roughly doubles the number of index entries that fit in `shared_buffers`. This improves cache hit rates under concurrent access.

Use `int8`/`bigserial` for primary keys on any table expected to exceed ~2 billion rows (the `int4` max is 2,147,483,647). For all other integer columns, `int4` is fine and marginally more cache-efficient.

## TOAST and Wide Columns

PostgreSQL's TOAST (The Oversized-Attribute Storage Technique) transparently compresses and/or moves column values exceeding approximately 2 kB to a side table. `TOAST_MAX_CHUNK_SIZE` (defined in `src/include/access/toast_internals.h`; default 2000 bytes) controls the threshold. `heap_toast_insert_or_update` in `src/backend/storage/toast/toast_helper.c` handles the mechanics.

Key implications:

- **Main table rows stay narrow**: queries that do not select wide columns never touch the TOAST table. Main-table scans remain efficient.
- **`SELECT *` is expensive on wide tables**: every wide column triggers a TOAST fetch — a heap scan on a separate relation — for each row returned.
- **TOAST is per-column, not per-row**: a row with three `text` columns may have zero, one, or three TOAST entries depending on actual value sizes.

```sql
-- Forces TOAST reads for all jsonb/text/bytea columns:
SELECT * FROM events;

-- No TOAST reads if payload is toasted but not selected:
SELECT id, event_type, created_at FROM events;
```

`ALTER TABLE ... ALTER COLUMN ... SET STORAGE` tunes the storage strategy (`PLAIN`, `EXTENDED`, `EXTERNAL`, `MAIN`) per column.

## Domain Types and CHECK Constraints

`CREATE DOMAIN` wraps a base type with constraints that run at insert/update time. Beyond data quality, CHECK constraints participate in **constraint exclusion**: the planner uses them to prune partitions or to eliminate branches of a query that cannot satisfy the constraint.

```sql
CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);

-- Partition pruning relies on CHECK constraints on partition columns:
CREATE TABLE measurements (
    sensor_id int,
    recorded_at timestamptz,
    value float8
) PARTITION BY RANGE (recorded_at);
-- Each partition's CHECK constraint allows the planner to skip partitions
-- that cannot contain rows matching a WHERE clause on recorded_at.
```

CHECK constraints on non-partition columns do not currently enable automatic index skipping. They do let the planner apply constraint exclusion in inheritance hierarchies. They also let the planner validate assumptions that would otherwise require application-layer enforcement.

## Practical Guidance

| Scenario | Recommendation |
|---|---|
| Single-node PK, < 2B rows | `int4` / `serial` |
| Single-node PK, any size | `bigint` / `bigserial` |
| Distributed PK, globally unique | UUIDv7 (sequential) |
| Distributed PK, v4 only available | Accept bloat; increase `fill_factor` to 70 |
| String with business length rule | `varchar(n)` |
| Unconstrained string | `text` |
| Fixed-width string | Avoid `char(n)`; use `text` with CHECK |
| Money, exact decimal | `numeric(p,s)` |
| Scientific measurement | `float8` |
| Timestamps in multi-timezone system | `timestamptz` always |
| Table with wide jsonb/text columns | Select only needed columns; never `SELECT *` |
| High-cardinality FK column | Prefer `int4` over `int8` if range allows |

When migrating a UUID v4 primary key to a sequential type, rebuild the primary key index with `REINDEX` after migration; the old B-tree will retain its bloated structure until rebuilt.

Profile with `EXPLAIN (ANALYZE, BUFFERS)` to confirm buffer hit rates before and after type changes on large tables. Index page density changes are often larger than the raw byte-size difference suggests.

## Related Topics

- [[subsystems/storage/toast|TOAST]] — covers the oversized-attribute storage mechanism referenced in the TOAST and Wide Columns section, including compression strategies and chunk layout.
- [[subsystems/indexes/btree|B-tree Indexes]] — explains B-tree page structure and fill factor, directly relevant to the index bloat and cache-thrashing effects of random UUID inserts.
- [[subsystems/storage/heap|Heap Storage]] — describes how rows are laid out on heap pages, complementing the discussion of row width and TOAST thresholds.
- [[subsystems/planner/constraint-exclusion|Constraint Exclusion]] — details how the planner uses CHECK constraints to prune partitions, as introduced in the Domain Types section.
- [[subsystems/indexes/expression-indexes|Expression Indexes]] — relevant to the timestamp index non-use pitfall: expression indexes can index `DATE(created_at)` directly when range rewrites are impractical.
- [[subsystems/types/numeric-scalar-types|Numeric Scalar Types]] — reference for the integer, numeric, and float type families discussed throughout the performance comparisons.
- [[subsystems/storage/shared-buffers-tuning|Shared Buffers Tuning]] — explains cache hit rate mechanics that underpin the page-density arguments for int4 vs int8 and sequential vs random UUIDs.

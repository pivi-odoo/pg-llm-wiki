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
  - gen_random_uuid
  - uuidv7
  - generate_uuidv7
  - get_real_time_ns_ascending
  - heap_toast_insert_or_update
  - TOAST_MAX_CHUNK_SIZE
  - varattrib_1b
---

## UUID vs bigint/bigserial for Primary Keys

UUID (16 bytes) is twice the physical size of `bigint` (8 bytes). This difference compounds across every index that references the column: foreign key indexes, covering indexes, and the primary key index itself all double in size when switching from `bigint` to UUID.

The more damaging issue with UUIDv4 is access pattern. `gen_random_uuid()` (or `uuid_generate_v4()` from `uuid-ossp`) produces uniformly random values. Every insert lands at a random position in the B-tree. PostgreSQL must then read pages that are not in `shared_buffers` from disk before the insert can proceed. At high insert rates this causes:

- **Index bloat**: B-tree pages fill only partially because splits happen before pages are logically "full" in a sequential sense. This leaves pages at ~50–70% fill on average.
- **Cache thrashing**: the working set of hot index pages becomes the entire index rather than the right-most leaf. This evicts other useful data from `shared_buffers`.

Sequential UUID variants (UUIDv7, ULIDs) embed a millisecond-precision timestamp in the high bits. This makes the sort order roughly increasing. Insert behaviour then approaches `bigserial`: appends go to the rightmost page, and that page stays hot in cache. The storage overhead (16 vs 8 bytes) remains.

### Native uuidv7()

PostgreSQL 18 adds a built-in `uuidv7()` (`src/backend/utils/adt/uuid.c`). Earlier versions need an extension or application-side generation. The layout follows RFC 9562:

- The first 48 bits hold the Unix timestamp in milliseconds.
- The next 12 bits (`rand_a`) hold a sub-millisecond fraction of 1/4096 ms, taken from the nanosecond clock (RFC 9562 "Method 3").
- The remaining bits, 62 apart from version and variant, come from `pg_strong_random()`.

`get_real_time_ns_ascending()` keeps a per-backend previous timestamp and forces each new value to advance by a minimum step. IDs from one backend are therefore strictly increasing, even within the same millisecond. IDs from different backends or servers are only ordered to the precision of their clocks, so concurrent writers interleave near the right edge of the index. On macOS and Windows the clock has only microsecond precision, so the two lowest sub-millisecond bits are filled with random bits.

`uuidv7(interval)` shifts the embedded timestamp, which helps when generating test data or backfilling. `uuid_extract_timestamp()` reads the timestamp back from a v7 value. The embedded timestamp reveals row creation time to anyone who sees the ID.

**When UUIDs are worth it**: distributed systems that generate IDs across multiple nodes without coordination, or when exposing IDs in APIs where exposing sequential integers leaks enumeration information. In those cases, prefer UUIDv7 over v4 to recover the sequential-insert property. Keep v4 when the creation time must stay hidden or the ID must be unguessable.

```sql
-- bigserial: 8 bytes, sequential, single-node only
CREATE TABLE orders (id bigserial PRIMARY KEY, ...);

-- UUIDv7: 16 bytes, roughly sequential, globally unique
CREATE TABLE orders (id uuid DEFAULT uuidv7() PRIMARY KEY, ...);
-- uuidv7() is built in from PostgreSQL 18; older versions need an extension
-- gen_random_uuid() produces v4 (random) and gives the bloat described above
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

Every B-tree index entry starts with an 8-byte `IndexTupleData` header, and `index_form_tuple()` pads the whole entry to `MAXALIGN` (8 bytes on 64-bit platforms). A single-column `int4` entry is 8 + 4 rounded up to 16 bytes. A single-column `int8` entry is 8 + 8 = 16 bytes. Primary key indexes and single-column foreign key indexes therefore hold the same number of entries per 8 kB page for both types.

The saving appears in the heap, where each `int4` column is 4 bytes smaller (subject to alignment padding). It also appears in multi-column indexes. A two-column `int4` index entry is 8 + 8 = 16 bytes, and a two-column `int8` entry is 8 + 16 = 24 bytes, so such indexes grow by about half.

Use `int8`/`bigserial` for primary keys on any table expected to exceed ~2 billion rows (the `int4` max is 2,147,483,647). For all other integer columns, `int4` is fine and saves heap space.

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

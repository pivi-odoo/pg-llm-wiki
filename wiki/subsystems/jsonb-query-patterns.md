---
title: JSONB Query Patterns for Performance
aliases:
  - jsonb-performance
  - jsonb-indexes
tags:
  - theme/query-optimization
source_files:
  - src/backend/utils/adt/jsonb.c
  - src/backend/access/gin/ginget.c
  - src/backend/utils/adt/jsonpath_exec.c
symbols:
  - jsonb_contained
  - gin_extract_jsonb
  - gin_extract_jsonb_path
  - JsonPathExecResult
---

# JSONB Query Patterns for Performance

PostgreSQL's JSONB type stores JSON as a decomposed binary format that supports
indexing. Getting good query performance requires choosing the right index type
for each access pattern. The wrong choice often means an invisible sequential
scan.

## GIN Index Types

Two operator classes exist for GIN indexes on JSONB columns.

### jsonb_ops (default)

`jsonb_ops` indexes every key and value in the document. It supports the widest range of
operators but produces a larger index.

```sql
-- Default operator class (jsonb_ops is implicit)
CREATE INDEX idx_data_gin ON t USING GIN (data);
```

### jsonb_path_ops

`jsonb_path_ops` indexes only values, not keys. The index is smaller. Lookups for containment
queries are faster, because the search space is narrower.

```sql
CREATE INDEX idx_data_path_gin ON t USING GIN (data jsonb_path_ops);
```

`?`, `?|`, and `?&` are **not** supported under `jsonb_path_ops`. See
[[subsystems/jsonb-operators|JSONB Operators]] for the full operator-to-opclass
support matrix.

### Choosing Between the Two

- Need `?`, `?|`, or `?&` key-existence checks -> `jsonb_ops`.
- Only need `@>` and want a compact, fast index -> `jsonb_path_ops`.
- Unsure -> start with `jsonb_path_ops`; add a second `jsonb_ops` index later
  only if key-existence operators appear in queries.

## Which Operators Use the GIN Index

```sql
-- GIN index scan via @> (containment) — fast with either operator class
SELECT * FROM events WHERE data @> '{"status": "active", "type": "login"}';

-- GIN index scan via ? (key exists) — only jsonb_ops
SELECT * FROM events WHERE data ? 'payload';

-- Sequential scan — ->> extracts text, no GIN operator matches it
SELECT * FROM events WHERE data->>'status' = 'active';
```

The third query silently falls back to a sequential scan even when a GIN index
exists on `data`. This is one of the most common JSONB performance traps.

## Expression Indexes for Path Access

When a specific key is queried frequently, add an expression index on the
extracted value.

```sql
-- Index the result of the extraction expression
CREATE INDEX idx_status ON events ((data->>'status'));

-- Now this query can use an index scan
SELECT * FROM events WHERE data->>'status' = 'active';
```

The planner matches `data->>'status'` in the WHERE clause to the expression
stored in `pg_index`. The expression must be written identically (same
operator, same path) for the match to occur.

Verify with EXPLAIN:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM events WHERE data->>'status' = 'active';
```

A hit produces output like:

```
Index Scan using idx_status on events
  Index Cond: ((data ->> 'status'::text) = 'active'::text)
```

A miss (sequential scan) looks like:

```
Seq Scan on events
  Filter: ((data ->> 'status'::text) = 'active'::text)
  Rows Removed by Filter: 94821
```

The presence of "Rows Removed by Filter" on a large table is a signal that an
expression index is missing or not being matched.

## Partial + Expression Index

Combining a partial index with an expression index reduces index size. It can also
improve cache efficiency when a large fraction of rows have NULL for the key.

```sql
-- Only index rows where status is present; NULL rows are excluded entirely
CREATE INDEX idx_status_notnull ON events ((data->>'status'))
  WHERE data->>'status' IS NOT NULL;
```

Queries must include the partial-index predicate (or a stronger condition) for
the planner to consider the index:

```sql
-- Planner can use idx_status_notnull
SELECT * FROM events
 WHERE data->>'status' = 'active'
   AND data->>'status' IS NOT NULL;

-- Also usable: 'active' != NULL is implied, so the planner may infer it
-- depending on statistics. Adding the IS NOT NULL clause explicitly is safer.
```

## Containment vs Path Access Tradeoffs

| Pattern | Best index | Notes |
|---------|-----------|-------|
| Match multiple keys at once | GIN `jsonb_path_ops` | One index covers any subset of keys |
| Filter on one specific key | Expression index | Smaller, faster for equality/range |
| Key-existence check | GIN `jsonb_ops` | Only option for `?` |
| Range on extracted value | Expression + btree | GIN cannot do `<`, `>`, `BETWEEN` |

Containment with `jsonb_path_ops` shines for "find documents that have all of
these attributes" queries:

```sql
-- Single GIN scan covers all three attributes simultaneously
SELECT * FROM products
 WHERE data @> '{"category": "electronics", "in_stock": true, "brand": "Acme"}';
```

Expression indexes are better when you filter on one key and also need range
comparisons or sorting:

```sql
CREATE INDEX idx_price ON products ((data->>'price')::numeric);

SELECT * FROM products
 WHERE (data->>'price')::numeric BETWEEN 100 AND 500
 ORDER BY (data->>'price')::numeric;
```

GIN indexes support only equality-style containment. They cannot satisfy
ORDER BY or range conditions on extracted values.

## jsonpath (PostgreSQL 12+)

The `jsonpath` language provides richer navigation. Two key functions:

```sql
-- Returns true/false; can use GIN for simple @> reachable paths
SELECT * FROM events
 WHERE jsonb_path_exists(data, '$.tags[*] ? (@ == "urgent")');

-- Returns a JSON array of matches
SELECT jsonb_path_query_array(data, '$.items[*].price')
  FROM orders;
```

GIN index usage for `jsonb_path_exists` and `jsonb_path_query` depends on
whether the planner can rewrite the path expression into a containment check.
Simple paths like `$.key == value` may be rewritten. Complex filters with
arithmetic, `.recursive()`, or multiple steps typically fall back to a
sequential scan. Always verify with EXPLAIN.

```sql
EXPLAIN SELECT * FROM events
 WHERE jsonb_path_exists(data, '$.status ? (@ == "active")');
```

If the output shows a sequential scan, add an expression index on
`data->>'status'` and rewrite the query to use `->>` directly.

## When to Normalize

JSONB is flexible but is not a substitute for a relational schema. Promote a
key to a real column when:

- It appears in WHERE, JOIN ON, or ORDER BY clauses frequently.
- You need referential integrity or foreign keys on it.
- The value domain is finite and well-understood.

**Generated column approach** (PG 12+):

```sql
ALTER TABLE events
  ADD COLUMN status text GENERATED ALWAYS AS (data->>'status') STORED;

CREATE INDEX idx_events_status ON events (status);
```

The engine automatically maintains the column on INSERT/UPDATE. Standard btree indexes apply to it. Queries rewritten to use `status` instead of
`data->>'status'` gain full planner statistics and range-scan support.

**Application-maintained column**: add a regular column and populate it in
application code or a trigger. Slightly more control; usable before PG 12.

## EXPLAIN Patterns Quick Reference

```
-- Good: GIN Index Scan for @>
Bitmap Index Scan on idx_data_path_gin
  Recheck Cond: (data @> '{"status": "active"}'::jsonb)

-- Good: Expression index for ->>
Index Scan using idx_status on events
  Index Cond: ((data ->> 'status'::text) = 'active'::text)

-- Bad: sequential scan falling back from ->> with no expression index
Seq Scan on events  (cost=0.00..4821.00 rows=241 width=312)
  Filter: ((data ->> 'status'::text) = 'active'::text)
  Rows Removed by Filter: 95982
```

High "Rows Removed by Filter" on a JSONB filter is the clearest signal that an
expression index is missing.

## Practical Guidance

1. Default to `jsonb_path_ops` GIN for new JSONB columns unless you know you
   need key-existence operators.
2. Add expression indexes for any key queried in equality conditions more than
   occasionally. Profile with `pg_stat_statements` to find candidates.
3. Use partial indexes to exclude NULL or unset keys when the column is sparse.
4. Cast extracted values to the right type in the expression index
   (`(data->>'price')::numeric`) so range queries and sorts can use the index.
5. Run EXPLAIN ANALYZE after every index addition to confirm the planner picks
   it up. Expression mismatches (different cast, extra whitespace in path) are
   silent.
6. Evaluate generated columns for any key that becomes a first-class filter or
   join attribute. The planner will then have column-level statistics and the full
   range of index types.

## Related Topics

- [[subsystems/jsonb|JSONB Type Internals]] — covers the binary storage format and decomposition that makes GIN indexing of JSONB documents possible
- [[subsystems/jsonb-operators|JSONB Operators]] — reference for the full set of operators (`@>`, `?`, `#>>`, etc.), their containment semantics, and the comprehensive operator-to-opclass support matrix
- [[subsystems/indexes/gin|GIN Index Access Method]] — explains how GIN posting lists and key extraction work, which underlies both `jsonb_ops` and `jsonb_path_ops`
- [[subsystems/indexes/jsonb-gin|JSONB GIN Operator Classes]] — deep dive into how `gin_extract_jsonb` and `gin_extract_jsonb_path` differ and how each builds its key set
- [[subsystems/indexes/expression-indexes|Expression Indexes]] — how the planner matches a WHERE expression to a stored index expression, critical for `->>` path access
- [[subsystems/indexes/partial-indexes|Partial Indexes]] — restricting an index to rows matching a predicate, used here to exclude NULL or unset JSONB keys
- [[subsystems/planner/reading-explain|Reading EXPLAIN Output]] — how to interpret Bitmap Index Scan, Index Scan, and Seq Scan nodes to confirm index usage
- [[subsystems/planner/index-selection|Index Selection]] — how the planner chooses among multiple candidate indexes for a query
- [[subsystems/generated-columns|Generated Columns]] — promoting a frequently-queried JSONB key to a stored column with full planner statistics and btree index support

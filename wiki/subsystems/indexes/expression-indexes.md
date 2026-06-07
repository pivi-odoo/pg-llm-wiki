---
title: Expression (Functional) Indexes
aliases:
  - Functional Indexes
  - Indexes on Expressions
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/path/indxpath.c
  - src/backend/utils/adt/selfuncs.c
  - src/backend/commands/indexcmds.c
  - src/backend/access/index/indexam.c
symbols:
  - match_index_to_operand
  - IndexOptInfo
  - indexprs
---

# Expression (Functional) Indexes

An expression index (also called a functional index) stores the result of an arbitrary expression rather than a raw column value. Instead of indexing `email`, you index `lower(email)`, making case-insensitive equality lookups index-scannable without touching every row.

```sql
CREATE INDEX idx_users_email_lower ON users (lower(email));
```

The index entries contain the evaluated result of `lower(email)` for each row at the time of insertion or update. Any subsequent query whose `WHERE` clause contains `lower(email) = $1` can use this index transparently.

## How the Planner Matches an Expression Index

The planner's path generation in `src/backend/optimizer/path/indxpath.c` calls `match_index_to_operand()` to determine whether a restriction clause refers to a given index column. For a plain column index this is a trivial Var comparison. For an expression index it must compare the clause operand against each expression stored in `IndexOptInfo.indexprs`.

`IndexOptInfo.indexprs` is a `List` of expression trees, one per expression column in the index. It is populated when the planner calls `BuildIndexInfo()` and deserialises `pg_index.indexprs` from its `pg_node_tree` representation. The comparison in `match_index_to_operand()` uses `equal()` — a structural equality walk over the expression trees — so the query expression must be syntactically identical (after canonicalisation) to the stored expression.

```
match_index_to_operand(operand, indexcol, index)
  └─ if index->indexprs != NIL for this col:
       indexpr_item = list_nth_cell(index->indexprs, ...)
       return equal(operand, indexpr_item->data)  ← structural tree compare
```

The implication is significant: the planner does **not** perform algebraic simplification before the comparison. `lower(email)` and `LOWER(email)` both parse to the same function-call node. However, `lower(email || '')` does not match `lower(email)`, even though they are semantically equivalent for non-null values.

## Operator Family Requirements

Expression indexes, like column indexes, are constrained by operator families. A B-tree index on `lower(email)` stores `text` values and supports the `text_ops` operator family. A query clause `lower(email) = $1` succeeds because `=` on `text` belongs to `text_ops`.

If the expression returns a composite or domain type, the operator family must include operators for that type. For `USING hash` or `USING gin`, the expression return type must be handled by a matching operator class. Mismatches cause `CREATE INDEX` to fail with an `operator class ... does not support data type` error rather than silently building an unusable index.

## Immutability Requirement

`indexcmds.c` enforces that every function appearing in an index expression is marked `IMMUTABLE`. The check is performed by `CheckMutability()` (or `CheckIndexCompatible()` on existing indexes), which walks the expression tree and raises an error for any `STABLE` or `VOLATILE` node.

This constraint exists because index entries must remain consistent across transactions without recomputing the expression. A `STABLE` function such as `now()` could return different values in different sessions, producing an index that diverges from the table.

```sql
-- Fails: now() is STABLE
CREATE INDEX ON events (date_trunc('day', now()));

-- Works: date_trunc on a column is IMMUTABLE
CREATE INDEX ON events (date_trunc('day', created_at));
```

If you need to index a `STABLE` function result, the workaround is a generated column (`GENERATED ALWAYS AS ... STORED`) which evaluates at write time rather than index-scan time.

## Selectivity Estimation

`ANALYZE` treats an expression index column as a virtual attribute. For each expression column, the [[subsystems/background/autovacuum|autovacuum]] or manual `ANALYZE` call evaluates the expression against a sample of live rows and stores the resulting statistics in `pg_statistic` under a synthetic attribute number. These statistics are surfaced in `pg_stats` using the index relation OID as `tablename` and the expression text as `attname`.

```sql
-- After ANALYZE, expression statistics are visible here:
SELECT tablename, attname, n_distinct, histogram_bounds
FROM   pg_stats
WHERE  tablename = 'idx_users_email_lower';
```

The statistics stored include the standard histogram, MCV list, and correlation. This gives `selfuncs.c` routines like `eqsel()` and `rangesel()` the same quality of estimates they have for plain columns. Without this, the planner would fall back to default selectivity constants, often producing poor plan choices.

The key detail: statistics are on the index, not the table. Running `ANALYZE users` updates table-level stats. To update expression index stats you must `ANALYZE users` as well — PostgreSQL refreshes expression index statistics as part of table analysis, scanning the table and evaluating the index expressions inline.

## Common Patterns

**Case-insensitive search**
```sql
CREATE INDEX ON users (lower(email));
SELECT * FROM users WHERE lower(email) = lower('User@Example.com');
```

**Date bucketing**
```sql
CREATE INDEX ON events (date_trunc('month', created_at));
SELECT count(*) FROM events WHERE date_trunc('month', created_at) = '2025-01-01';
```

**JSONB field extraction**
```sql
CREATE INDEX ON documents ((data->>'status'));
SELECT * FROM documents WHERE data->>'status' = 'active';
```
Note the extra parentheses required by the parser when the expression contains `->>`. This is syntactic, not semantic.

**Array unnesting pattern** (use with GIN)
```sql
CREATE INDEX ON products USING gin (string_to_array(tags, ','));
```

**Partial expression index** — combine an expression with a predicate for maximum precision:
```sql
CREATE INDEX ON orders (lower(customer_email))
WHERE status = 'pending';
```

## EXPLAIN Output: Index Cond vs Filter

When the planner successfully matches an expression index, the clause appears as `Index Cond` rather than `Filter`:

```
Index Scan using idx_users_email_lower on users
  Index Cond: (lower(email) = 'user@example.com'::text)
```

If the expression in the query does not match, the planner falls back to a sequential scan, or uses the index with a weaker condition and demotes the clause to `Filter`. This means every candidate row is rechecked:

```
Seq Scan on users
  Filter: (lower(email) = 'user@example.com'::text)
```

A `Filter` on an expression that has an index is a reliable signal of an expression mismatch.

## Pitfalls

**Equivalent but non-identical expressions do not match.** The structural equality check in `match_index_to_operand()` is exact. Common mismatches:

- `upper(email)` vs `lower(email)` — obvious, but easy to mistype in migrations
- `trim(lower(email))` vs `lower(trim(email))` — different tree structures
- `(data->>'field')::int` vs `(data->'field')::text::int` — different cast chains
- Implicit casts inserted by the parser can change the expression tree

**Type coercions introduced by bind parameters.** If a prepared query binds a parameter of type `varchar` and the index expression returns `text`, PostgreSQL may insert a cast node that breaks structural equality. Ensure the query literal or bind parameter type exactly matches the index expression's return type.

**Concurrent build and expression evaluation.** `CREATE INDEX CONCURRENTLY` on an expression index evaluates the expression for every live row. If the expression is expensive (e.g., a complex JSONB traversal), this can be CPU-intensive and slow on large tables.

**Index bloat from volatile-like expressions.** If you mark a function `IMMUTABLE` incorrectly (it accesses a table internally), no index update is triggered when the underlying rows change. The index then contains stale entries. This produces silent wrong results.

## Practical Guidance

- Always run `ANALYZE` after creating an expression index, or it will have no statistics and the planner will use default estimates.
- Use `\d indexname` in psql to inspect the stored expression; compare it character-for-character against query predicates when debugging missed index usage.
- For JSONB field indexes, prefer `(data->>'field')` over casting to a typed expression unless you need range scans — keeps the expression simpler and more likely to match.
- When porting application code that builds query strings, ensure the expression in the application matches the index definition verbatim; a framework that adds implicit `::text` casts may break matching.
- Consider generated columns for complex or frequently-used expressions: they are stored in the heap, have table-level statistics, and eliminate the expression-matching problem entirely.
- Profile `EXPLAIN (ANALYZE, BUFFERS)` with `enable_seqscan=off` to confirm that a potential expression index scan is genuinely cheaper before committing to the index.

## Related Topics

- [[subsystems/indexes/partial-indexes|Partial Indexes]] — combine an expression with a predicate clause to restrict which rows are indexed, a natural complement to expression indexes.
- [[subsystems/planner/index-selection|Index Selection]] — explains how the planner chooses between available indexes, including the structural-equality matching that governs expression index usage.
- [[subsystems/planner/selectivity-estimation|Selectivity Estimation]] — covers how `selfuncs.c` uses per-column statistics; expression indexes feed their own synthetic statistics into this pipeline.
- [[subsystems/planner/statistics|Planner Statistics]] — describes what `pg_statistic` stores and how `ANALYZE` populates it, including the per-expression-column statistics built during table analysis.
- [[subsystems/indexes/btree|B-tree Indexes]] — the default access method used for most expression indexes; operator family and type requirements described here apply directly.
- [[subsystems/generated-columns|Generated Columns]] — a heap-stored alternative to expression indexes that avoids the expression-matching problem and carries table-level statistics.
- [[code-paths/create-index|CREATE INDEX]] — the code path that enforces immutability of index expressions and serialises the expression tree into `pg_index.indexprs`.

---
title: Foreign Keys and Performance
aliases:
  - FK indexes
  - foreign key locking
  - referential integrity performance
tags:
  - theme/concurrency-control
source_files:
  - src/backend/utils/adt/ri_triggers.c
  - src/backend/executor/execIndexing.c
  - src/backend/commands/trigger.c
symbols:
  - RI_FKey_check_ins
  - RI_FKey_restrict_del
  - RI_FKey_cascade_del
---

# Foreign Keys and Performance

Foreign key constraints enforce referential integrity in PostgreSQL but carry
performance implications that are easy to overlook. This article explains the
locking model, the critical importance of indexing FK columns, and operational
techniques for managing FKs on large tables.

## Indexing FK Columns on the Referencing Side

When you define `REFERENCES parent(id)` on a child table, PostgreSQL registers
a set of trigger functions (implemented in `ri_triggers.c`) that fire on
INSERT/UPDATE of the child and on DELETE/UPDATE of the parent.

The constraint-enforcement trigger on the parent side (`RI_FKey_restrict_del`,
`RI_FKey_cascade_del`, etc.) must locate every child row that references the
parent row being deleted or updated. If no index covers the FK column(s) on the
child table, PostgreSQL performs a **sequential scan of the entire child table**
for every such DELETE or UPDATE on the parent. On a child table with millions
of rows this is catastrophic: a single `DELETE FROM parent WHERE id = $1` may
take seconds and hold locks for the duration.

With a B-tree index on the FK column the trigger issues an index scan instead,
turning the check into an O(log N) operation.

**This omission is one of the most common performance mistakes in PostgreSQL
applications.** Tools like `pg_upgrade` and ORMs rarely add these indexes
automatically, so they must be created explicitly.

## Lock Pattern During FK Enforcement

Understanding the locking sequence prevents surprises in concurrent workloads.

| Operation | Lock on parent | Lock on child |
|---|---|---|
| `INSERT INTO child` | `RowShareLock` (brief) | — |
| `UPDATE parent` changing PK/unique key | — | `ShareLock` (brief) |
| `DELETE FROM parent` | — | `ShareLock` (brief) |

The `RowShareLock` taken on the parent during a child INSERT prevents the
parent row from being deleted between the moment the FK value is read and the
moment the child row is written. It is very short-lived and conflicts only with
`AccessExclusiveLock` (e.g., `TRUNCATE`, `DROP TABLE`).

The `ShareLock` on the child during a parent DELETE or UPDATE is taken per
child tuple (row-level lock), not on the whole table, so concurrent reads of
child rows are never blocked.

## FOR NO KEY UPDATE and Reduced FK Contention

PostgreSQL distinguishes between a row update that changes a primary key or
unique key and one that does not. When an UPDATE touches only non-key columns,
PostgreSQL uses the weaker `FOR NO KEY UPDATE` row lock mode instead of the
full `FOR UPDATE` mode.

Because FK enforcement triggers only need to verify that no child row
references the *key* columns, PostgreSQL skips the constraint check entirely
for `FOR NO KEY UPDATE` updates. This means:

- Concurrent `INSERT INTO child` operations are **not blocked** by an in-flight
  parent UPDATE that doesn't touch the key.
- Throughput on write-heavy workloads is significantly improved when parent
  updates are predominantly non-key updates.

To take advantage of this, avoid unnecessarily including PK columns in UPDATE
statements.

## Finding Un-Indexed FK Columns

The following query returns all FK constraints whose referencing columns lack a
covering index. Run it on any database to audit for missing FK indexes.

```sql
SELECT
    c.conname                          AS fk_constraint,
    c.conrelid::regclass               AS child_table,
    array_agg(a.attname ORDER BY x.ordinality) AS fk_columns
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS x(attnum, ordinality)
JOIN pg_attribute a
    ON a.attrelid = c.conrelid
   AND a.attnum   = x.attnum
WHERE c.contype = 'f'
  AND NOT EXISTS (
      SELECT 1
      FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND (i.indkey::int[])[0:cardinality(c.conkey)-1]
              @> c.conkey::int[]
  )
GROUP BY c.conname, c.conrelid
ORDER BY child_table, fk_constraint;
```

For each row returned, create an index:

```sql
CREATE INDEX CONCURRENTLY ON child_table (fk_column);
```

## Cascading Deletes and Index Requirements at Every Level

`ON DELETE CASCADE` causes PostgreSQL to recursively delete child rows, and
then grandchild rows, and so on. The trigger `RI_FKey_cascade_del` fires at
each level. Each level requires an index on the FK column pointing to the level
above. Without it, every cascade step degrades to a sequential scan.

For a three-level hierarchy (orders → line_items → adjustments) a single
`DELETE FROM orders` without indexes at each level performs:

1. Seq scan of `line_items` to find all referencing rows.
2. For each `line_items` row deleted, seq scan of `adjustments`.

The work is **O(N × M)** in the worst case. With indexes at every level it
becomes O(log N + log M) per deleted parent row.

Always index FK columns at every level of a cascade chain.

## Deferrable Constraints

A FK constraint declared `DEFERRABLE INITIALLY DEFERRED` defers enforcement
until `COMMIT` rather than checking it row-by-row at statement end.

```sql
ALTER TABLE child
    ADD CONSTRAINT fk_child_parent
    FOREIGN KEY (parent_id) REFERENCES parent(id)
    DEFERRABLE INITIALLY DEFERRED;
```

Use cases:

- **Bulk loads**: insert rows in any order without satisfying FK relationships
  mid-batch. Only the final state at commit must be consistent.
- **Circular FK references**: two tables that reference each other cannot both
  be populated if constraints are immediate. Deferring one or both breaks the
  deadlock.

The deferred check is implemented via `queueFKConstraint` in `ri_triggers.c`,
which appends the check to a per-transaction queue flushed at pre-commit.

Deferrable constraints still benefit from indexes. The deferred scan at commit
touches the same code paths as an immediate scan.

## NOT VALID + VALIDATE CONSTRAINT

Adding a FK to a large populated table with a standard `ALTER TABLE` takes a
full table scan and holds `ShareRowExclusiveLock` for the duration. This blocks
all writes. The two-phase approach avoids this:

```sql
-- Phase 1: add the constraint without scanning existing rows.
-- Takes only ShareRowExclusiveLock briefly to record the constraint.
ALTER TABLE child
    ADD CONSTRAINT fk_child_parent
    FOREIGN KEY (parent_id) REFERENCES parent(id)
    NOT VALID;

-- Phase 2: validate existing rows.
-- Uses ShareUpdateExclusiveLock — does NOT block concurrent reads or writes.
ALTER TABLE child VALIDATE CONSTRAINT fk_child_parent;
```

After phase 1, new rows are checked immediately. Existing rows are not checked
until phase 2. `VALIDATE CONSTRAINT` uses `ShareUpdateExclusiveLock` (the same
lock level as `CREATE INDEX CONCURRENTLY`), which allows concurrent DML to
proceed.

This is the recommended approach for adding FKs to production tables with
millions of rows.

## Practical Guidance

- Always create a B-tree index on every FK column immediately after creating
  the FK constraint, or use `CREATE INDEX CONCURRENTLY` on existing tables.
- Audit missing FK indexes periodically using the query above; add it to your
  monitoring or post-deploy checklist.
- Prefer `NOT VALID` + `VALIDATE CONSTRAINT` when adding FKs to large tables
  in production.
- For write-heavy parent tables, update non-key columns only to avoid upgrading
  row locks and triggering FK re-validation.
- When using `ON DELETE CASCADE`, verify that indexes exist at every level of
  the hierarchy before enabling cascade on production data.
- Use `DEFERRABLE INITIALLY DEFERRED` for bulk load scripts and circular
  reference situations. Switch back to immediate constraints once the load is
  complete, if you need the extra safety.

## Related Topics

- [[subsystems/indexes/btree|B-tree Indexes]] — the index type used for FK column indexes; explains the B-tree structure that makes FK enforcement checks O(log N).
- [[subsystems/locking/row-locking-patterns|Row Locking Patterns]] — covers the `FOR NO KEY UPDATE` and `FOR UPDATE` row lock modes that govern FK contention on concurrent workloads.
- [[subsystems/locking/lock-contention-and-slow-queries|Lock Contention and Slow Queries]] — diagnoses the lock waits that missing FK indexes cause when parent rows are deleted or updated at scale.
- [[subsystems/transactions/deferrable-constraints|Deferrable Constraints]] — deep dive into how `DEFERRABLE INITIALLY DEFERRED` FK enforcement is queued and flushed at commit.
- [[subsystems/indexes/index-maintenance|Index Maintenance]] — explains `CREATE INDEX CONCURRENTLY` and the `ShareUpdateExclusiveLock` level also used by `VALIDATE CONSTRAINT`.
- [[subsystems/indexes/partial-indexes|Partial Indexes]] — complementary technique for reducing index size on FK columns when only a subset of rows participates in references.
- [[troubleshooting/slow-queries|Slow Queries]] — practical guide for identifying sequential scans caused by missing FK indexes in production query plans.
- [[subsystems/locking/row-level-locking|Row-Level Locking]] — background on PostgreSQL's per-tuple locking model, the mechanism underlying the `ShareLock` and `RowShareLock` patterns FK enforcement takes on child and parent rows.
- [[subsystems/indexes/multicolumn-index-strategies|Multicolumn Index Strategies]] — relevant when a FK constraint spans multiple columns, since the covering index must lead with those columns in the same order to satisfy enforcement lookups.
- [[code-paths/delete|DELETE Code Path]] — walks through how a `DELETE` on a parent table reaches the FK enforcement triggers described above.

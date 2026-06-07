---
title: "Partition Introspection Functions"
aliases:
  - pg_partition_tree
  - pg_partition_ancestors
  - pg_partition_root
  - partition introspection
source_files:
  - src/backend/utils/adt/partitionfuncs.c
symbols:
  - pg_partition_tree
  - pg_partition_root
  - pg_partition_ancestors
  - check_rel_can_be_partition
  - find_all_inheritors
  - get_partition_ancestors
---

PostgreSQL ships three SQL-callable functions — `pg_partition_tree`, `pg_partition_ancestors`, and `pg_partition_root` — that expose the full structure of a [[subsystems/partitioning/overview|partitioning]] hierarchy without requiring hand-written recursive queries. Together they let application code and admin scripts navigate arbitrarily deep partition trees using ordinary `SELECT` statements. The system catalog `pg_inherits` records only immediate parent-child relationships: one row per (parent, child) pair. A three-level hierarchy — a root table, sub-partitioned mid-level tables, and leaf partitions — appears as two disjoint sets of rows. No single query naturally stitches them together. Retrieving the full tree requires a recursive CTE. Computing the depth or root of an arbitrary node requires additional traversal logic. The introspection functions encapsulate this recursion inside C, calling `find_all_inheritors` (for `pg_partition_tree`) or `get_partition_ancestors` (for the other two) from the catalog access layer. Both internal routines walk `pg_inherits` recursively and return an ordered `List` of OIDs. The SQL-callable wrappers then stream that list back as a set-returning result, row by row, without materialising an intermediate table in SQL. A guard function, `check_rel_can_be_partition`, validates each input OID before traversal begins: the relation must exist in the system cache and must either carry the `relispartition` flag or be a relation kind that supports partitioning (`RELKIND_HAS_PARTITIONS`). Relations that fail this check cause the function to return an empty result set rather than an error. This behavior simplifies defensive scripts that pass arbitrary OIDs.

## pg_partition_tree: Full Hierarchy as a Result Set

`pg_partition_tree(regclass)` returns one row per member of the tree rooted at the given relation. The four columns are:

| Column | Type | Meaning |
|---|---|---|
| `relid` | `regclass` | OID of this member |
| `parentrelid` | `regclass` | OID of its immediate parent; `NULL` for the root |
| `isleaf` | `boolean` | `true` when the relation has no further partitions |
| `level` | `integer` | Depth from the root; `0` for the root itself |

The root appears first because `find_all_inheritors` returns parents before their children. For each returned OID the implementation calls `get_partition_ancestors` to build that node's ancestor chain. The immediate parent is the first element of that chain. The implementation computes the level by counting ancestors until it encounters the root OID.

A relation is a leaf when its `relkind` is not a kind that has partitions (`RELKIND_HAS_PARTITIONS` is false). This means it is a plain table or foreign table rather than a partitioned table. Both partitioned tables and leaf partitions appear in the result. The result includes foreign-table partitions as long as they carry the `relispartition` flag.

## pg_partition_ancestors: Walking Up the Tree

`pg_partition_ancestors(regclass)` returns the ancestor chain for a given node, starting with the node itself and ending with the root. The output is a single-column set of `regclass`. The implementation prepends the input relation to the list returned by `get_partition_ancestors`. As a result, the node itself is always the first row, and the root is always the last.

This is the right tool when you have a leaf partition in hand and need to identify which partitioned table it belongs to at each level, or when building a breadcrumb display for a partition management dashboard. Because it includes the input relation, passing the root itself returns a one-row result.

## pg_partition_root: Finding the Top-Level Table

`pg_partition_root(regclass)` returns a single `regclass` value: the OID of the top-most partitioned table in the hierarchy containing the argument. For a relation that is already the root (i.e., `get_partition_ancestors` returns an empty list) the function returns the input unchanged. For any other node it returns the last element of the ancestor list, since `get_partition_ancestors` orders ancestors from nearest to farthest. It returns `NULL` for relations that cannot be part of a partition tree.

A common use is normalisation. Code that receives an arbitrary table name — which might be the root, a mid-level sub-partition, or a leaf — can call `pg_partition_root` once to obtain a canonical identifier. It can then call `pg_partition_tree` on that identifier.

## Practical Patterns

**Finding empty leaf partitions** — useful before archiving or dropping stale time-range partitions:

```sql
SELECT relid
FROM pg_partition_tree('events')
WHERE isleaf
  AND pg_relation_size(relid) = 0;
```

**Generating per-partition VACUUM commands** — handy after a bulk load into a partitioned table:

```sql
SELECT 'VACUUM ANALYZE ' || relid::text || ';'
FROM pg_partition_tree('orders')
WHERE isleaf;
```

**Dropping old monthly partitions** — a pattern for log or metrics tables with time-range partitioning:

```sql
SELECT relid
FROM pg_partition_tree('metrics')
WHERE isleaf
  AND relid::text ~ '_2023_'
ORDER BY relid;
-- then DROP TABLE each result after confirming with pg_relation_size
```

**Debugging a sub-partition hierarchy** — verifying that a leaf is attached where expected:

```sql
SELECT pg_partition_root('metrics_2024_01_east');
-- returns 'metrics', confirming the root

SELECT relid, level
FROM pg_partition_ancestors('metrics_2024_01_east');
-- returns: metrics_2024_01_east (0 relative), metrics_2024_01, metrics
```

**Building a partition management dashboard** — for admin tooling in a web application, joining `pg_partition_tree` with `pg_class` and `pg_stat_user_tables` produces a single-query inventory of all partitions, including their sizes, row counts, and last-autovacuum timestamps. This avoids recursive SQL on the application side.

Because all three functions accept `regclass`, they resolve schema-qualified names, quoted identifiers, and OIDs uniformly. Scripts can pass either a table name string (`'public.events'`) or an integer OID and get consistent results.

## Related Topics

- [[subsystems/partitioning/overview|partitioning]]
- [[subsystems/partitioning/partition-pruning|partition pruning]]
- [[subsystems/partitioning/partitioned-indexes|partitioned indexes]]
- [[subsystems/background/autovacuum|autovacuum]]

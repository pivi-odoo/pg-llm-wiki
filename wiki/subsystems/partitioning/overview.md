---
title: Table Partitioning
aliases:
  - partitioning
  - partition pruning
  - partition routing
source_files:
  - src/backend/partitioning/partbounds.c
  - src/backend/partitioning/partdesc.c
  - src/backend/partitioning/partprune.c
  - src/backend/executor/nodeModifyTable.c
  - src/backend/executor/execPartition.c
  - src/backend/optimizer/path/joinrels.c
symbols:
  - PartitionBoundInfoData
  - PartitionBoundSpec
  - PartitionDesc
  - PartitionDirectoryData
  - GeneratePruningStepsContext
  - PartClauseInfo
  - PruneStepResult
  - ExecFindPartition
  - ExecCrossPartitionUpdate
  - RelationGetPartitionDesc
  - RelationBuildPartitionDesc
  - try_partitionwise_join
  - gen_partprune_steps
---

# Table Partitioning

Partitioning divides a logically single table into a set of physical child tables called *partitions*, each holding a disjoint subset of the rows. The motivation is not just storage organisation: when the [[subsystems/planner/overview|planner]] can prove at plan or execution time that a query touches only a fraction of the partitions, it avoids scanning the rest entirely. At the same time, the executor automatically routes writes to the correct child. Applications see an ordinary table while the database handles the physical layout.

Declarative partitioning, introduced in PostgreSQL 10, differs from the older inheritance-based approach by recording the partition scheme in dedicated catalog tables and wiring the routing and pruning logic directly into the executor and planner. Old-style inheritance partitioning still works, but it has no access to pruning or automatic insert routing.

## The Three Strategies

The strategy controls how a row's partition key value maps to a partition. The right choice depends on the access pattern.

**RANGE** assigns rows whose key falls within a contiguous interval `[lower, upper)` to a partition. It suits time-series data partitioned by month or year, and sequential numeric keys (order IDs, log sequence numbers) where scans are almost always restricted to a recent window. Queries with `WHERE created_at > '2024-01-01'` prune all earlier partitions with no extra work.

**LIST** assigns rows based on membership in an explicit set of discrete values—country codes, status flags, tenant identifiers. Each partition carries exactly the values listed in its `IN (...)` bound. The model is simple but rigid: adding a new country code means either adding a partition or relying on a default partition to absorb unknown values.

**HASH** distributes rows evenly by computing `hash(key) % modulus = remainder`. No range or set relationship exists between a row's value and its partition. The goal is uniform distribution for sharding or parallel throughput, not query selectivity. The planner cannot prune hash partitioning by equality on an expression unless it can compute the exact hash.

The strategy is stored as a single character (`'r'`, `'l'`, `'h'`) in `pg_partitioned_table.partstrat` (`parsenodes.h`).

## Catalog Representation

Four catalog objects together describe a partitioned table hierarchy.

**`pg_class`** has two roles here. The parent table carries `relkind = 'p'` (`RELKIND_PARTITIONED_TABLE`), marking it as a partitioned relation with no storage of its own. Each child partition has `relkind = 'r'` (ordinary) or `'p'` (if it is itself partitioned), plus `relispartition = true` and a non-null `relpartbound` column that stores the partition's bounds as a serialised `PartitionBoundSpec` node.

**`pg_partitioned_table`** exists exactly once per partitioned parent. Its key columns are:

| Column | Meaning |
|---|---|
| `partrelid` | OID of the parent in `pg_class` |
| `partstrat` | Strategy character (`r`/`l`/`h`) |
| `partnatts` | Number of key columns |
| `partdefid` | OID of the default partition, if any |
| `partattrs` | Array of attribute numbers forming the key |
| `partexprs` | Expressions for computed key columns |
| `partclass` | Operator class OIDs for each key column |

**`pg_inherits`** links each partition to its parent with `(inhparent, inhrelid)` pairs, the same mechanism used by classical table inheritance. The partitioning infrastructure scans this catalog (filtered by `inhparent`) when building the partition descriptor.

**`PartitionBoundInfoData`** (`partbounds.h`) is the in-memory summary of all partition bounds for a partitioned relation. `RelationBuildPartitionDesc()` builds it from the individual `PartitionBoundSpec` values and arranges it for fast lookup:

| Field | Meaning |
|---|---|
| `strategy` | `'r'`, `'l'`, or `'h'` |
| `ndatums` | Length of the `datums[]` array |
| `indexes[]` | Maps bound offsets to partition indexes |
| `nindexes` | Length of `indexes[]`—strategy-dependent |
| `null_index` | Partition index that accepts NULL keys, or −1 |
| `default_index` | Partition index for the default partition, or −1 |
| `interleaved_parts` | Bitmap of partitions whose list values interleave |

For RANGE, `nindexes = ndatums + 1`—one slot per interval between bounds, plus sentinels. For HASH, `nindexes` equals the greatest common modulus. `indexes[hash(value) % modulus]` yields the partition index directly.

## The Partition Descriptor and Relcache

The partition descriptor (`PartitionDesc`) is a per-relation cache entry that exposes the bound information alongside the array of child OIDs. `RelationBuildPartitionDesc()` (`partdesc.c`) builds it by reading `pg_inherits` and parsing each child's `relpartbound`. Because DDL (attaching or detaching a partition) invalidates the relcache, PostgreSQL rebuilds descriptors on demand.

A subtlety around `DETACH PARTITION ... CONCURRENTLY` means two descriptors can coexist on a relation: `rd_partdesc` includes all partitions including those being concurrently detached, while `rd_partdesc_nodetached` omits them based on the `pg_inherits.xmin` relative to the active snapshot. `RelationGetPartitionDesc()` (`partdesc.c`) chooses between them automatically.

For executor use, a `PartitionDirectory` (a hash table from OID to descriptor) allows multiple nodes in a single query to share descriptors for the same partitioned relation rather than rebuilding them independently (`partdesc.c`).

## Partition Pruning at Plan Time

Pruning is how partitioning delivers its primary performance benefit: replacing a full Append scan over all partitions with a smaller Append over only those that could contain matching rows. The planner performs this in `partprune.c`. It translates WHERE-clause predicates into *pruning steps* and evaluates them against each partition's bounds.

A pruning step represents either a base test (a comparison of the partition key to some expression via a btree operator) or a Boolean combinator (AND/OR over prior steps). The key internal type is `PartClauseInfo`, which records the key column index (`keyno`), the comparison operator and strategy number, and the expression being compared. The planner matches each qual clause to the partition key via `match_clause_to_partition_key()`, producing one or more `PartitionPruneStep` nodes. The result is a `Bitmapset` of surviving partition indexes.

For RANGE and LIST partitions, the matching amounts to a binary search over the sorted `datums[]` array in `PartitionBoundInfoData` (`get_matching_range_bounds()`, `get_matching_list_bounds()`, `partprune.c`). The planner handles HASH separately: it can only eliminate partitions when an equality condition allows it to compute the exact hash bucket.

Pruning at plan time happens only when the comparison value is a plain constant. The planner folds expressions involving stable or immutable functions first. It cannot use volatile functions for plan-time pruning.

## Runtime Partition Pruning

When a query is parameterised—most importantly, when a prepared statement is executed with different bind parameters—the partition key value is not known when the planner compiles the plan. Pruning cannot happen at plan time, so the planner embeds the pruning steps into the plan tree alongside the Append or MergeAppend node. The executor then evaluates them at startup (for parameters known before scanning begins) or, in some cases, during each rescan.

The distinction among `PARTTARGET_PLANNER`, `PARTTARGET_INITIAL`, and `PARTTARGET_EXEC` in `GeneratePruningStepsContext` (`partprune.c`) controls which phase each step targets. `has_exec_param` being true means at least one step depends on a `PARAM_EXEC` value that changes during execution (e.g., the inner side of a parameterised nested-loop join). When that happens, the executor re-prunes on each rescan. This is particularly valuable for prepared statements with selective WHERE clauses that would otherwise force scans of every partition on each execution.

## Partition Routing on INSERT

An INSERT into a partitioned table never stores a row in the parent—the parent has no storage. The executor must identify the correct leaf partition before writing. This is partition routing. `ExecFindPartition()` (`execPartition.c`) performs it.

The function evaluates the partition key expression against the incoming tuple to produce a Datum (or set of Datums for multi-column keys). Then it searches the `PartitionBoundInfo`, using `partition_bound_bsearch()` for RANGE, a hash lookup for HASH, or a direct search for LIST. If no partition matches, and a default partition exists (`default_index != -1`), the row lands there. If no default exists, the insert fails with a partition constraint violation.

Because partitioned tables can be nested (a partition is itself partitioned), routing recurses through each level until it reaches a leaf. The `PartitionDirectory` avoids rebuilding descriptors at every level of a deep hierarchy.

The `ExecFindPartition()` result is a `ResultRelInfo` pointing to the target leaf partition. The executor opens and caches that `ResultRelInfo` on first use. Subsequent rows with the same destination reuse it without reopening.

## Cross-Partition UPDATE

An ordinary UPDATE that does not change the partition key value happens in-place on the leaf partition, just like a heap update. When the new tuple's key value falls in a different partition, however, the row must physically move. The executor detects this by checking whether the updated slot satisfies the partition constraint of the current leaf and, if not, delegates to `ExecCrossPartitionUpdate()` (`nodeModifyTable.c`).

The mechanism is delete-then-insert: `ExecCrossPartitionUpdate()` deletes the old tuple from its current partition and inserts the new tuple into the root partitioned table. `ExecFindPartition()` then routes it to the correct destination. The complexity lies in concurrency: if another transaction concurrently updated the tuple, `ExecCrossPartitionUpdate()` returns `false`, and the caller re-fetches and retries.

Two important restrictions follow from this design. `INSERT ON CONFLICT DO UPDATE` cannot cause a cross-partition move—the executor rejects it with an error, because the conflict resolution would need to atomically delete from one partition and insert into another while holding the conflict lock. Similarly, `ExecCrossPartitionUpdateForeignKey()` (`nodeModifyTable.c`) must re-check foreign-key constraints that reference the old partition against the new one.

## Partition-Wise Joins and Aggregates

When two partitioned tables share an identical partition scheme (same strategy, same key type, compatible bounds), the planner can join them partition-by-partition rather than building a single join over all rows. The planner joins each matching pair of child partitions independently, producing an Append of join results. This is a *partition-wise join* (`try_partitionwise_join()`, `joinrels.c`).

The benefit is twofold. First, each per-partition join is smaller and may fit in hash join memory that the full join would not. Second, the partition-pair joins are independent and can be parallelised, each running in its own worker.

Partition-wise aggregation extends the idea to GROUP BY: if the partition key is a superset of the grouping columns, the planner can aggregate each partition separately and combine the results, avoiding a global sort or hash over the entire table.

GUCs (`enable_partitionwise_join`, `enable_partitionwise_aggregate`) gate both features because the planner overhead of considering every partition pair grows with the number of partitions, and the benefit evaporates when partitions are small or the join condition does not align with the partition key.

The planner sets `consider_partitionwise_join` on a `RelOptInfo` when it determines the partitioned relation is eligible. `partition_bounds_equal()` (`partbounds.c`) performs the matching. It compares two `PartitionBoundInfo` structures datum-by-datum. Even minor differences in bound types or collations prevent matching.

## Default Partitions

A default partition catches any row whose key does not fall into any explicitly defined partition. `PartitionBoundInfoData.default_index` and `pg_partitioned_table.partdefid` both identify it. Without a default partition, inserting an out-of-range row is an error.

The presence of a default partition complicates partition pruning. Pruning normally excludes partitions whose bounds are disjoint from the query predicate. But a default partition, by definition, holds rows that match none of the explicit bounds. This includes rows that would fall into a gap created by attaching a new partition. For this reason, attaching a new partition to a table that already has a default partition requires a table scan of the default partition to verify that no existing row conflicts with the new partition's bounds. This scan runs under `ACCESS SHARE` on the default partition and `SHARE UPDATE EXCLUSIVE` on the parent.

## Declarative vs. Inheritance-Based Partitioning

Before declarative partitioning existed, the same physical layout was achievable through table inheritance combined with `CHECK` constraints and `BEFORE INSERT` triggers (or rules) for routing. That older pattern still works, but it has none of the infrastructure described in this article. The planner treats inheritance-based partitions as ordinary child tables and cannot prune them based on partition bounds (only via constraint exclusion, which is slower and less precise). Insert routing requires explicit application-level or trigger logic.

Declarative partitioning, recognisable by the presence of a `pg_partitioned_table` row, uses the dedicated infrastructure throughout: PostgreSQL stores bounds as first-class catalog data, the executor routes inserts automatically, the planner prunes with `partprune.c`, and `ExecCrossPartitionUpdate()` handles key-changing updates transparently.

## Related Topics

- [[subsystems/partitioning/partition-pruning|Partition Pruning]] — deep dive into the pruning step generation and evaluation logic that eliminates irrelevant partitions at plan and execution time
- [[subsystems/partitioning/partition-wise-join|Partition-Wise Join]] — how the planner joins two compatibly-partitioned tables partition-by-partition to reduce join input size
- [[subsystems/partitioning/partition-wise-aggregate|Partition-Wise Aggregate]] — aggregating each partition independently before combining results when the partition key covers the grouping columns
- [[subsystems/partitioning/partitioned-indexes|Partitioned Indexes]] — how indexes are created and maintained across a partition hierarchy
- [[subsystems/partitioning/when-and-how|When and How to Partition]] — practical guidance on choosing a strategy, key, and partition count
- [[subsystems/catalog/pg-class|pg_class]] — the relkind and relispartition flags that mark a relation as a partitioned table or a partition
- [[subsystems/planner/constraint-exclusion|Constraint Exclusion]] — the older, inheritance-based mechanism for excluding child tables that declarative partition pruning supersedes
- [[subsystems/planner/overview|Planner Overview]] — how the planner builds Append paths and applies partition pruning
- [[code-paths/insert|INSERT Code Path]] — the full INSERT execution path, including tuple routing
- [[subsystems/storage/buffer-manager|Buffer Manager]] — page-level storage that each partition uses
- [[subsystems/transactions/mvcc|MVCC]] — visibility rules apply per-partition; each child is an independent heap
- [[subsystems/locking/overview|Locking Overview]] — partition attach/detach locking and cross-partition update lock ordering
- [[subsystems/indexes/btree|B-tree Index Internals]] — partitioned indexes and how btree indexes on partitions interact with the parent

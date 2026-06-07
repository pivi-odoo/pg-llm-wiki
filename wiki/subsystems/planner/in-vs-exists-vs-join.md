---
title: IN vs EXISTS vs JOIN Performance
aliases:
  - semijoin-subquery-optimization
  - in-exists-join-planner
tags:
  - theme/query-optimization
  - symptom/slow-query
source_files:
  - src/backend/optimizer/plan/subselect.c
  - src/backend/optimizer/prep/prepjointree.c
  - src/backend/optimizer/path/joinpath.c
symbols:
  - convert_ANY_sublink_to_join
  - convert_EXISTS_sublink_to_join
  - JOIN_SEMI
  - JOIN_ANTI
  - SubLink
---

# IN vs EXISTS vs JOIN Performance

A persistent misconception among PostgreSQL engineers is that `IN`, `EXISTS`, and explicit `JOIN` are semantically equivalent but differ only in performance. The reality is more nuanced. The planner aggressively transforms subquery forms into semijoins, but certain conditions prevent that transformation. Those conditions force materialize-based fallbacks with very different cost profiles. See [[subsystems/planner/subqueries|Subquery Planning and Flattening]] for the conversion mechanics — `convert_ANY_sublink_to_join()`, `convert_EXISTS_sublink_to_join()`, and everything that blocks them (volatile functions, `LIMIT`/`OFFSET`/`DISTINCT`, direct correlation). This page covers the comparative and practical side: which form to write, how to read the resulting plan, and when an explicit `JOIN` is actually the better choice.

## Semijoin and Antijoin Join Types

| Join Type   | SQL Form              | Semantics                                      |
|-------------|----------------------|------------------------------------------------|
| `JOIN_SEMI` | `IN`, `EXISTS`       | Return outer row at most once; stop on match   |
| `JOIN_ANTI` | `NOT IN` (null-safe), `NOT EXISTS` | Return outer row only if no match found |

These are first-class join types processed by `joinpath.c`. The path-generation code in `add_paths_to_joinrel()` considers hash semijoin and nested-loop semijoin strategies. Current PostgreSQL does not support merge semijoin for `JOIN_SEMI`/`JOIN_ANTI` (the executor lacks the stop-early logic for merge join).

## NOT IN and the NULL Trap

`NOT IN` does not transform to `JOIN_ANTI` the way `NOT EXISTS` does. See [[subsystems/planner/subqueries|Subquery Planning and Flattening]] for why the planner cannot apply the same shortcut, and [[sql-features/subquery-patterns|Subquery Patterns]] for the canonical dangerous/safe SQL example. The takeaway for choosing between these three forms: never write `NOT IN` against a subquery column that can be NULL. Default to `NOT EXISTS` whenever there's doubt about nullability.

## Hash Semijoin Execution

When the planner chooses a hash semijoin, the executor builds a hash table from the inner relation, then probes it for each outer row. The critical difference from a regular hash join: once the executor finds a match for an outer row, it **immediately moves to the next outer row** without scanning further. This makes `JOIN_SEMI` cheaper than an inner join followed by `DISTINCT` when the inner relation has high duplication.

```sql
EXPLAIN (ANALYZE, FORMAT TEXT)
SELECT c.id FROM customers c
WHERE c.id IN (SELECT customer_id FROM orders);
```

```
Hash Semi Join  (cost=842.00..1623.50 rows=5000 width=4)
                (actual time=12.341..28.902 rows=4821 loops=1)
  Hash Cond: (c.id = orders.customer_id)
  ->  Seq Scan on customers  (cost=0.00..555.00 rows=20000 ...)
  ->  Hash  (cost=602.00..602.00 rows=19200 ...)
        ->  Seq Scan on orders  (cost=0.00..602.00 rows=19200 ...)
```

An explicit `JOIN` without deduplication would show `Hash Join` (not `Hash Semi Join`) and return duplicate outer rows:

```sql
-- Returns duplicates if customer has multiple orders
SELECT DISTINCT c.id FROM customers c
JOIN orders o ON o.customer_id = c.id;
-- Plan: Hash Join + HashAggregate (more work than semijoin)
```

## When IN/EXISTS Outperform Explicit JOIN

Semijoins beat `JOIN ... GROUP BY` or `JOIN ... DISTINCT` patterns because:

1. The executor stops scanning the inner relation after the first match per outer row — reducing inner-side I/O proportionally to inner duplication ratio.
2. No sort or hash aggregate step is needed to eliminate duplicates from the outer side.
3. For indexed inner relations, a nested-loop semijoin with an index seek terminates after a single index probe per outer row.

The only case where an explicit `JOIN` is strictly equivalent (no deduplication overhead) is when the join key is a unique/primary key on the inner relation. In that case, the planner may choose identical plans.

## Practical Guidance

**Prefer `EXISTS` over `IN` for correlated subqueries.** Both transform to semijoins when flattening succeeds, but `EXISTS` more clearly expresses intent and handles `NULL` on the inner side safely.

**Never use `NOT IN` against nullable columns.** Always use `NOT EXISTS` or add `IS NOT NULL` to the subquery. This is a correctness issue, not just performance.

**Use `EXPLAIN (ANALYZE)` to verify semijoin transformation.** Look for `Hash Semi Join` or `Nested Loop Semi Join` in the plan. If you see `Hash Join` with a subsequent `HashAggregate` or `Unique`, the transformation failed or you wrote an explicit join.

**Add `NOT NULL` constraints where semantically correct.** Constraints help the planner prove that `NOT IN` is safe, potentially enabling `JOIN_ANTI` transformation.

**Avoid blocking constructs in flattened subqueries.** `LIMIT 1` inside an `IN (SELECT ... LIMIT 1)` subquery prevents semijoin transformation and forces a `SubPlan` node. If you need "at least one matching row," write `EXISTS` instead.

**Check for SubPlan nodes in EXPLAIN output.** A `SubPlan` node (not `InitPlan`) in the executor plan means the subquery executes once per outer row — a potential O(N²) operation on large tables.

```sql
-- Diagnosing subplan vs semijoin
EXPLAIN SELECT * FROM large_table
WHERE id IN (SELECT fk FROM other_table WHERE condition);
-- Look for: "Hash Semi Join" (good) vs "SubPlan" (investigate why flattening failed)
```

## Related Topics

- [[subsystems/planner/subqueries|Subquery Planning and Flattening]] — authoritative source for the semijoin/antijoin conversion mechanics: `convert_ANY_sublink_to_join()`, `convert_EXISTS_sublink_to_join()`, the blocking conditions, and SubPlan/InitPlan fallback.
- [[sql-features/subquery-patterns|Subquery Patterns]] — SQL-level patterns for subqueries, including the canonical NOT IN / NULL trap example this page references.
- [[subsystems/planner/join-method-selection|Join Method Selection]] — explains how the planner chooses between hash join, nested-loop, and merge join strategies, including for semijoin and antijoin types.
- [[subsystems/planner/join-ordering|Join Ordering]] — describes how the planner enumerates and selects join orders, which determines the context in which semijoins are placed.
- [[subsystems/planner/optimization-fences|Optimization Fences]] — details constructs like CTEs and LIMIT that prevent subquery flattening and force SubPlan fallback.
- [[subsystems/executor/subplan-nodes|Subplan Nodes]] — documents the executor-side representation of SubPlan and InitPlan nodes that result when semijoin transformation fails.
- [[subsystems/executor/joins|Joins]] — covers hash semijoin and nested-loop semijoin execution mechanics in the executor, including the early-exit behavior for JOIN_SEMI.
- [[subsystems/planner/predicate-pushdown|Predicate Pushdown]] — explains how filter conditions are pushed into subqueries and joins, interacting with the flattening logic described here.

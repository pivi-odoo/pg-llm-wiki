---
title: OR Clause Distribution
aliases:
  - OR clause extraction
  - restriction OR clauses
  - extract_restriction_or_clauses
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/util/orclauses.c
  - src/backend/optimizer/path/indxpath.c
  - src/backend/optimizer/plan/planmain.c
symbols:
  - extract_restriction_or_clauses
  - extract_or_clause
  - consider_new_or_clause
  - is_safe_restriction_clause_for
  - generate_bitmap_or_paths
---

OR clause distribution is a planner transformation that extracts single-relation restriction predicates from join OR expressions. This enables index scans on individual OR branches, which the planner then combines via a BitmapOr plan node. Without this transformation, the executor could only evaluate a join clause like `WHERE (a.x = 42 AND b.y = 43) OR (a.x = 44 AND b.z = 45)` at join time. With this transformation, the planner derives `(a.x = 42 OR a.x = 44)` as a base restriction on `a`. This makes an index scan on `a` possible before the join.

## The Core Transformation

The transformation is a partial conversion to conjunctive normal form (CNF). Given a join OR clause that spans multiple relations, the planner looks for sub-clauses in each OR arm that reference only a single relation. If every arm of the OR yields at least one such sub-clause for a given relation, the planner assembles those sub-clauses into a new restriction OR. It adds the result to that relation's `baserestrictinfo` list.

Consider:

```sql
WHERE (a.x = 42 AND b.y = 43) OR (a.x = 44 AND b.z = 45)
```

The planner synthesises two new restriction clauses. It appends them to `baserestrictinfo`:

```
(a.x = 42 OR a.x = 44)   -- added to rel a
(b.y = 43 OR b.z = 45)   -- added to rel b
```

The planner keeps the original join OR clause intact. These new clauses are redundant with it. The extracted clauses reduce the rows arriving at the join. Critically, they also allow `generate_bitmap_or_paths()` in `indxpath.c` to build a `BitmapOrPath` from the individual arms, if matching indexes exist (see [[subsystems/planner/bitmap-scans]]).

`extract_restriction_or_clauses()` (orclauses.c) drives the whole process. `query_planner()` in `planmain.c` calls it after foreign-key matching and before partition pruning, so the extracted clauses are in place before any path generation begins.

## How Extraction Works

For each base relation, `extract_restriction_or_clauses()` iterates over `rel->joininfo` looking for OR join clauses that the parameterised-path machinery considers movable to that relation. For each candidate it calls `extract_or_clause()`.

`extract_or_clause()` walks the arms of the OR. For each arm (which may itself be an AND of sub-clauses), it calls `is_safe_restriction_clause_for()` on every leaf `RestrictInfo`. This keeps only those clauses whose `clause_relids` match exactly the target relation's `relids`. The planner excludes volatile functions to avoid double evaluation. If no safe clause exists in any arm, extraction fails for that arm, and the function returns `NULL`. The whole OR is unusable for this relation, because every arm must contribute.

When it extracts a usable clause from every arm, `extract_or_clause()` wraps the collected sub-clauses in a fresh `OR` node. It returns that node. The planner builds fresh `RestrictInfo` wrappers for the result, rather than reusing those from the join clause. The code notes that it computes selectivity and other cached fields differently for restriction versus join clauses (orclauses.c line 169).

## Selectivity Accounting and the Redundancy Hack

Because the original join OR logically implies the extracted restriction clause (and the clause is therefore redundant with it), naively adding it would cause `clauselist_selectivity` to count it twice. This would shrink the estimated join size. `consider_new_or_clause()` compensates with an acknowledged hack. It estimates the selectivity of the new restriction clause (`or_selec`). It back-adjusts the cached `norm_selec` of the original join OR clause, so that:

```
join_or_rinfo->norm_selec = orig_selec / or_selec
```

This relies on two facts. `norm_selec` is cached. The same `RestrictInfo` node appears in every join-info list where the join rel could be formed. The comment in the source describes it as a "MAJOR HACK". It notes that the hack breaks for non-linear cases such as outer joins (orclauses.c lines 54–64).

The planner does not add a clause at all if its selectivity exceeds 0.9 — meaning it would filter fewer than 10 % of rows. In that case the overhead of an extra qual evaluation outweighs the benefit (orclauses.c line 292).

## The Cross-Relation Limitation

The transformation only works when every OR arm references the same single relation. The planner cannot distribute a predicate such as:

```sql
WHERE t1.a = 1 OR t2.b = 2
```

It cannot isolate a single arm into a restriction on `t1` alone, because the second arm references `t2` — and symmetrically for `t2`. The entire OR must therefore remain a join clause. The executor evaluates it only after performing the join. `is_safe_restriction_clause_for()` enforces this by requiring `bms_equal(rinfo->clause_relids, rel->relids)` — a strict equality, not a subset test.

## Path Generation from Distributed OR Clauses

After `extract_restriction_or_clauses()` populates `baserestrictinfo`, `create_index_paths()` in `indxpath.c` calls `generate_bitmap_or_paths()` over `rel->baserestrictinfo`. That function treats each OR restriction clause as a candidate `BitmapOrPath`. It requires that every arm of the OR match at least one index. If so, it constructs a `BitmapOrPath` whose children are `BitmapIndexScan` paths, one per arm. At execution time this becomes a [[subsystems/executor/bitmap-and-or]] BitmapOr node. This node unions the tid sets that each branch produces, before fetching heap pages.

If `enable_bitmapscan` is `false`, the planner does not remove bitmap paths, but `cost_bitmap_heap_scan()` (costsize.c line 1013) inflates their startup cost by `disable_cost`. This makes the planner choose other paths. Disabling bitmap scans therefore also suppresses the benefit of OR clause distribution even when the extracted clauses remain on `baserestrictinfo`.

## Nested OR Handling

`extract_or_clause()` recurses into nested OR expressions. This is not merely an optimisation — the function must descend fully to strip all embedded `RestrictInfo` wrappers from the returned expression tree. Returning a tree that still contains `RestrictInfo` nodes at arbitrary depths would produce malformed plan trees downstream (orclauses.c lines 196–203).

`extract_or_clause()` preserves AND/OR flatness: if the only sub-clause extracted from an arm is itself an OR node, it splices that node's args directly into the parent OR's arg list, rather than wrapping one OR inside another.

## Related Topics

- [[subsystems/planner/bitmap-scans]] — how the planner builds BitmapOrPath and BitmapAndPath from distributed OR clauses
- [[subsystems/executor/bitmap-and-or]] — the executor node that evaluates BitmapOr at runtime
- [[subsystems/planner/restrict-info]] — the RestrictInfo node that carries selectivity caches manipulated by this transformation
- [[subsystems/planner/selectivity-estimation]] — how `clauselist_selectivity` interacts with redundant clauses

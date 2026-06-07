---
title: Bipartite Matching in the Planner
aliases:
  - bipartite match
  - Hopcroft-Karp
  - maximum cardinality matching
  - grouping set chains
tags:
  - theme/query-optimization
source_files:
  - src/backend/lib/bipartite_match.c
  - src/include/lib/bipartite_match.h
  - src/backend/optimizer/plan/planner.c
symbols:
  - BipartiteMatchState
  - BipartiteMatch
  - BipartiteMatchFree
  - hk_breadth_search
  - hk_depth_search
  - extract_rollup_sets
---

Maximum bipartite matching gives the PostgreSQL planner a provably optimal way to minimise the number of sort passes needed when executing `GROUPING SETS`, `CUBE`, or `ROLLUP` queries. Without it, the planner would fall back to greedy heuristics that can miss valid pairings and force extra sort operations. With it, Dilworth's theorem guarantees the result is the minimum possible number of aggregate passes even for a twelve-dimension `CUBE` with thousands of grouping sets.

## The Problem: Grouping Sets Need Chaining

A `ROLLUP(a, b, c)` expands into the grouping sets `{a,b,c}`, `{a,b}`, `{a}`, and `{}`. The executor can compute multiple grouping sets in a single aggregate pass if they are arranged into a chain ordered by set inclusion — each set is a subset of the next. This works because a path sorted on `(a, b, c)` already satisfies the prefix requirements of `(a, b)` and `(a)`. Splitting the sets into the fewest such chains minimises the number of Sort + Aggregate passes required.

This is exactly the minimum chain-cover problem on a partially ordered set (poset). By Dilworth's theorem, the minimum number of chains needed to cover a poset equals the maximum antichain size. Computing that minimum cover reduces to finding the maximum cardinality matching on a bipartite graph. The planner exploits this in `extract_rollup_sets()` (`src/backend/optimizer/plan/planner.c`).

## Bipartite Graphs and Maximum Matching

A bipartite graph has two disjoint vertex sets — call them U and V — with edges only between the two sets. A matching is a subset of edges in which no vertex appears more than once. The maximum cardinality matching is the matching with the most edges.

In the grouping-set context, both U and V contain one vertex per distinct grouping set. An edge connects `u_i` to `v_j` when set `i` is a strict subset of set `j` (i.e., set `j` can extend a chain that contains set `i`). The size of the maximum matching tells the planner how many chain links it can create. The matched pairs directly encode which sets belong to the same chain.

After matching, `extract_rollup_sets()` reads `state->pair_uv[u]` and `state->pair_vu[v]` to assign each grouping set to a chain. Two sets `u` and `v` belong to the same chain when `pair_uv[u] = v` or `pair_vu[v] = u`. Unmatched sets each start their own chain, so the total number of chains is `num_sets - state->matching`.

## The Hopcroft-Karp Algorithm

PostgreSQL implements the Hopcroft-Karp algorithm, which finds the maximum matching in O(E × √V) time. The comment in `bipartite_match.c` notes that a 12-dimension `CUBE` produces at most 4096 sets and that planning such a query takes under half a second. For the small graphs that arise in practice, this bound is more than sufficient.

The algorithm alternates between two phases until no augmenting path remains:

**BFS phase (`hk_breadth_search`)** — Starting from all unmatched U vertices simultaneously, a breadth-first search finds the shortest augmenting paths to unmatched V vertices. The BFS records a `distance[]` array that labels each U vertex with its level in the layered graph. The search succeeds (returns `true`) when it reaches at least one unmatched V vertex. `distance[0]` acts as a sentinel holding the length of the shortest augmenting path found, or `HK_INFINITY` when none exists.

**DFS phase (`hk_depth_search`)** — For each unmatched U vertex, a depth-first search follows the layered graph produced by BFS, augmenting along any shortest augmenting path it finds. When augmentation succeeds, the algorithm updates `pair_uv[u]` and `pair_vu[v]` to record the new matching edge. It also increments `state->matching`. Vertices whose paths are exhausted have their distance reset to `HK_INFINITY` so subsequent DFS calls do not revisit them. `check_stack_depth()` guards against deep recursion on large graphs.

Each BFS + DFS round augments the matching by as many edge-disjoint shortest augmenting paths as exist. This is why the outer loop needs at most O(√V) iterations.

## BipartiteMatchState

`BipartiteMatchState` (defined in `src/include/lib/bipartite_match.h`) holds all algorithm state:

| Field | Type | Purpose |
|---|---|---|
| `u_size`, `v_size` | `int` | Number of vertices in each partition |
| `adjacency` | `short **` | Adjacency list: `adjacency[u] = [k, v1, v2, …, vk]` |
| `matching` | `int` | Number of edges in the final matching |
| `pair_uv` | `short *` | `pair_uv[u]` → matched V vertex, or 0 if unmatched |
| `pair_vu` | `short *` | `pair_vu[v]` → matched U vertex, or 0 if unmatched |
| `distance` | `short *` | BFS distance labels for U vertices |
| `queue` | `short *` | BFS queue storage |

Vertices are indexed 1-based; index 0 is reserved as a nil/unmatched sentinel. Using `short` for all arrays caps both partition sizes at `SHRT_MAX - 1` (enforced at the top of `BipartiteMatch()`), which is far beyond any realistic query. Allocation uses `palloc`/`palloc0`, so memory belongs to the caller's [[subsystems/memory/contexts|memory context]]. `BipartiteMatchFree()` releases the arrays but leaves the caller-supplied adjacency list untouched.

## Adjacency List Convention

The adjacency list format is compact and length-prefixed: `adjacency[u][0]` holds the count `k`, and `adjacency[u][1..k]` hold the V-side neighbours. A null pointer means vertex `u` has no edges. `extract_rollup_sets()` constructs this representation by iterating over all pairs of distinct grouping sets and recording a directed edge whenever one is a proper subset of the other (`bms_is_subset()`).

## Complexity in Practice

```
For N distinct grouping sets:
  Vertices: N (each side)
  Edges: at most N² / 2 in the worst case
  Hopcroft-Karp: O(N² × √N) ≈ O(N^2.5)
  At N = 4096: ~10^9 operations in the absolute worst case
```

In practice, grouping-set graphs are sparse (most sets are not subsets of each other), and the √V iteration bound is rarely reached. This keeps planning time well within acceptable limits. The comment in `planner.c` reports under 500 ms for a 12-dimension cube on modest hardware with assertions enabled.

## Relationship to Merge Join Planning

The `bipartite_match` library is a general-purpose utility with no hard dependency on grouping sets. The header comment mentions that maximum matching is also directly applicable to assigning available merge clauses to required pathkeys when planning merge joins. A greedy left-to-right scan of pathkeys can fail to find a valid assignment even when one exists, whereas maximum matching is guaranteed to find it. As of PostgreSQL 16, the only call site is `extract_rollup_sets()`, but the clean API (`BipartiteMatch` / `BipartiteMatchFree`) was deliberately designed to be reusable.

## Related Topics

- [[subsystems/planner/join-ordering]]
- [[subsystems/planner/sort-avoidance]]
- [[subsystems/planner/join-method-selection]]
- [[subsystems/memory/contexts]]

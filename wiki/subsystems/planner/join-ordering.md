---
title: Join Ordering
aliases:
  - join order search
  - DP join search
  - GEQO
tags:
  - theme/query-optimization
  - symptom/slow-query
source_files:
  - src/backend/optimizer/path/joinrels.c
  - src/backend/optimizer/path/allpaths.c
  - src/backend/optimizer/geqo/geqo_main.c
  - src/backend/optimizer/plan/initsplan.c
  - src/backend/optimizer/prep/prepjointree.c
symbols:
  - standard_join_search
  - join_search_one_level
  - make_join_rel
  - join_is_legal
  - RelOptInfo
  - geqo
  - geqo_threshold
  - join_collapse_limit
  - from_collapse_limit
  - set_cheapest
  - populate_joinrel_with_paths
---

# Join Ordering

The number of ways to join N tables grows factorially: five tables have 120 possible orderings, ten tables have over three million, and fifteen tables have over a trillion. Yet PostgreSQL must produce a plan in milliseconds. Join ordering is the planner's answer to this combinatorial explosion — a disciplined search over the space of possible join trees that finds a low-cost plan without exhausting it.

The problem matters because join order determines everything downstream. The first relation processed constrains which indexes are usable, which predicates can be pushed down early, and how much data flows into each subsequent join. A bad ordering can turn a millisecond query into a multi-second one. Getting it right — or close enough — is one of the planner's central responsibilities.

## The Dynamic Programming Algorithm

PostgreSQL's standard join search uses dynamic programming (DP), implemented in (standard_join_search(), allpaths.c). The key insight is memoization: once you have found the cheapest way to join a particular subset of tables, you never need to recalculate it — you just look it up when building larger subsets.

The algorithm works level by level. Level 1 contains the individual base relations. The planner builds level 2 by pairing level-1 items. It builds level 3 by combining a level-2 join with a level-1 item, or by combining two level-2 items (a "bushy" plan). This continues until a single top-level relation encompasses every table in the query.

```
root->join_rel_level[1] = initial_rels        -- base relations
root->join_rel_level[2] = all 2-way joins
root->join_rel_level[3] = all 3-way joins
...
root->join_rel_level[N] = the final join rel
```

At each level, (join_search_one_level(), joinrels.c) enumerates which subsets to combine. It delegates to (make_join_rel(), joinrels.c) to construct or retrieve the `RelOptInfo` for each join and add paths to it. After the planner populates all new join rels at a given level, (set_cheapest(), pathnode.c) scans every path on each new join rel. It records the cheapest ones. Only those cheapest paths survive to serve as inputs to the next level.

The savings from DP come from this pruning. At level 3, when combining a 2-way join with a base relation, the planner only needs the cheapest paths out of the 2-way join, not every possible way to produce those rows. The planner discards the suboptimal 2-way paths. They never appear in any higher-level join. This brings the search cost from factorial down to roughly exponential in practice, though the worst case is still exponential.

## RelOptInfo: the Unit of Search

A `RelOptInfo` (pathnodes.h) represents every node in the DP table — every subset of relations considered at every level. A single `RelOptInfo` serves for both base relations and join relations; the `relids` bitmap distinguishes them: one bit set means a base relation, multiple bits mean a join of those bases.

Key fields for join ordering purposes:

| Field | Purpose |
|---|---|
| `relids` | Bitmap of RT indexes included in this relation/join |
| `rows` | Estimated output row count after applying restriction clauses |
| `pathlist` | All candidate `Path` nodes for producing this relation |
| `cheapest_startup_path` | Unparameterized path with lowest startup cost |
| `cheapest_total_path` | Unparameterized path with lowest total cost (or cheapest minimally-parameterized path if none) |
| `cheapest_parameterized_paths` | Best path for each distinct parameterization; always includes `cheapest_total_path` |
| `joininfo` | Join clauses that reference this rel and at least one other |
| `has_eclass_joins` | True if equivalence-class-derived join conditions exist |

The distinction between `cheapest_startup_path` and `cheapest_total_path` matters when the query uses a `LIMIT`. A plan feeding a small limit can stop early. This makes startup cost more important than total cost. (get_cheapest_fractional_path(), planner.c) interpolates between the two based on the fraction of rows the planner expects to be fetched.

## Bushy Plans and Why They Matter

A "left-deep" plan is one where the inner side of every join is always a base relation — the join tree looks like a chain leaning left. Many optimizers restrict themselves to left-deep trees because they are simpler to enumerate. PostgreSQL does not.

The bushy plan portion of (join_search_one_level(), joinrels.c) explicitly considers joining two multi-relation join rels together:

```c
/* Consider bushy plans: k-rel join combined with (level-k)-rel join */
for (k = 2;; k++)
{
    int other_level = level - k;
    if (k > other_level)
        break;
    /* ... join joinrels[k] with joinrels[other_level] ... */
}
```

Bushy plans can be significantly better than left-deep plans when the query has independent subgraphs — for example, two large tables that filter well against each other, joined via a small bridge table. Forcing left-deep order might require one of the large tables to appear before the other can filter it. A bushy plan can join the two large tables independently with their filters applied first. It can then join the results.

The tradeoff is that bushy plan enumeration adds work: (join_search_one_level()) must consider combinations of intermediate join rels, not just combinations of base rels. PostgreSQL tolerates this cost for small-to-medium join counts. It uses GEQO above a threshold.

## How Join Clauses Drive the Search

The DP algorithm would be unworkable if it tried every possible combination at every level. The key optimization is using join clauses to restrict which pairs are even considered.

Every `RelOptInfo` accumulates join clauses in its `joininfo` list during query initialization. When (join_search_one_level()) iterates at a given level, it first asks: does this join rel have join clauses or equivalence-class-based join conditions? If yes, it only attempts joins with rels that share a clause — handled by (make_rels_by_clause_joins(), joinrels.c). Only if a rel has no applicable clauses does the planner fall back to Cartesian products via (make_rels_by_clauseless_joins(), joinrels.c).

The `has_eclass_joins` flag on `RelOptInfo` is a secondary signal: even when `joininfo` is empty, an equivalence class may connect two rels. An equality like `a.x = b.x` creates an `EquivalenceClass` that covers both `a` and `b`. The planner then knows they can be joined on that condition without an explicit `RestrictInfo` in either `joininfo` list.

Before the planner creates any join, (join_is_legal(), joinrels.c) validates it against the query's outer join constraints. Outer joins impose ordering requirements: the planner cannot join the inner side of a LEFT JOIN to tables outside the LEFT JOIN before the LEFT JOIN itself completes. `join_is_legal()` checks every `SpecialJoinInfo` node to ensure the proposed combination respects these constraints. It returns false for illegal pairings, so the planner never generates invalid plans.

## Parameterized Paths

A parameterized path is one that cannot produce rows on its own — it requires values from an outer relation to function. The canonical example is an index scan on the inner table of a nested loop join: `WHERE inner.fk = outer.pk` becomes a correlated lookup where each row from the outer side supplies the key for an index lookup on the inner side.

The planner tracks parameterized paths separately from unparameterized ones. The `ppilist` on a `RelOptInfo` holds `ParamPathInfo` nodes, one per distinct parameterization, each recording the outer rels needed and the expected row count when those rels' values are provided. The `cheapest_parameterized_paths` list keeps the lowest-cost path for each such parameterization.

When the planner later considers a nested loop join, it looks in `cheapest_parameterized_paths` for an inner path parameterized exactly by the outer rel being joined. If such a path exists and its cost estimate is attractive, the planner builds a nested loop using it. The efficiency gain can be dramatic: instead of scanning the entire inner table once and filtering, the plan performs one index lookup per outer row, each lookup being nearly free.

The planner can use parameterized paths only in nested loop joins. Hash joins and merge joins must materialize the inner relation completely, so they cannot benefit from per-row parameterization. This asymmetry is why the planner sometimes chooses nested loop plans over hash or merge plans when a highly selective index is available on the inner side.

There is a subtlety involving `PlaceHolderVar` expressions: if a placeholder must be evaluated at a join involving both the proposed outer and some third relation, a nested loop parameterized only by the outer rel cannot correctly supply that placeholder's value. (have_dangerous_phv(), joinrels.c) detects this case. It causes (join_is_legal()) to reject the join. This prevents the creation of plans the executor cannot handle.

## Controlling the Search: join_collapse_limit and from_collapse_limit

PostgreSQL exposes two GUCs that limit how much join reordering the planner performs.

`from_collapse_limit` controls how many items the planner allows in a flat FROM list before it stops collapsing subqueries into the outer query. When a subquery appears in a FROM clause, the planner can "pull it up" and merge its tables into the surrounding join search. This gives the optimizer more freedom. If doing so would push the FROM list past `from_collapse_limit` items, the subquery stays separate. The planner plans it as its own subproblem.

`join_collapse_limit` controls explicit JOIN syntax. When a query has `A JOIN B JOIN C ...`, the planner normally flattens this into a set of rels. It reorders them freely. If the number of joined relations would exceed `join_collapse_limit`, the planner stops flattening. It preserves the explicit join order given in the SQL text. The joins then become subproblems that the planner solves independently, in the order the user wrote them.

Setting either limit to 1 means the planner honors the query's written order exactly — useful for manual tuning or when the planner's estimates are known to be poor. The default for both is 8, matching the default GEQO threshold. Below 8 relations, the planner searches freely. Above 12, GEQO takes over.

## GEQO: Genetic Optimization for Large Join Counts

Above `geqo_threshold` relations (default 12), the factorial search space becomes too large even for dynamic programming. The planner switches to GEQO, the Genetic Query Optimizer. GEQO sacrifices optimality guarantees for bounded planning time.

GEQO frames join ordering as a variant of the Traveling Salesman Problem. It encodes each join order as a permutation of relation identifiers (a "chromosome"). The cost of a query plan is the "fitness" of that chromosome. The genetic algorithm (geqo(), geqo_main.c) proceeds through three phases repeated for many generations:

1. **Selection** — The algorithm chooses two parent chromosomes from the current population, using a linear bias toward fitter individuals (cheaper plans).
2. **Recombination** — The algorithm crosses the parents to produce a child chromosome. PostgreSQL defaults to Edge Recombination Crossover (ERX). ERX tries to preserve adjacency relationships: if parent A joins tables X and Y consecutively and parent B does too, the child is likely to as well.
3. **Replacement** — The algorithm evaluates the child's fitness by actually building a join tree and costing it. If the child is fitter than the worst individual in the current pool, it replaces that individual.

The algorithm seeds the pool with random join orderings, then sorts it by fitness before the generational loop begins. The number of relations and the `geqo_effort` parameter determine pool size and generation count. By default, pool size is roughly `2^(N+1)`, constrained between `10 * effort` and `50 * effort`. The number of generations equals the pool size.

The key trade-off is that GEQO may miss the globally optimal plan. The genetic search is stochastic: two runs with different `geqo_seed` values can produce different plans. Neither is guaranteed to be optimal. For many large joins this is acceptable. The optimal plan is unknowable in reasonable time anyway. GEQO reliably finds good plans.

## The Final Selection

After the DP search (or GEQO) completes, the top-level `RelOptInfo` holds all paths found for the complete join. (set_cheapest(), pathnode.c) has already selected `cheapest_startup_path` and `cheapest_total_path` at every intermediate level. The top-level `cheapest_total_path` — or, for queries with a small limit, the result of (get_cheapest_fractional_path()) — becomes the plan tree that (create_plan(), createplan.c) converts into executable nodes.

The DP algorithm guarantees that by the time any join rel is built, every subrel it depends on already has its cheapest path computed. This means the final plan selection is not a separate global optimization step — it is the natural output of the level-by-level construction, where pruning at each level propagates upward.

## Controlling and Diagnosing Join Order

The DP algorithm is only as good as its row estimates. Statistics can go stale. Correlated columns can cause estimate errors that compound across join levels. Either way, the planner can pick a join order that is badly wrong for the actual data. The error compounds because each join's estimated output row count becomes the input cardinality for the next join's cost model. A factor-of-ten underestimate at join two becomes a factor-of-hundred underestimate at join three. By then, the planner may have already committed to a nested loop that is efficient for tens of rows but catastrophic for millions.

The primary diagnostic is `EXPLAIN ANALYZE`. When the planner has chosen a bad join order, the output typically shows a large discrepancy between estimated and actual rows at an early join node:

```sql
-> Hash Join  (cost=... rows=12 ...) (actual rows=84201 ...)
     Hash Cond: (orders.customer_id = customers.id)
```

An estimate of 12 rows with 84,201 actual rows means the planner thought it was building a tiny intermediate result. Everything above that node — the join strategy chosen, whether to use a nested loop or a hash join, which indexes to probe — depended on that estimate of 12 rows. If the actual intermediate relation is enormous, a nested loop that was cheap in the model becomes a full cross-product in practice.

### Forcing join order with join_collapse_limit

When the planner's estimates are wrong and you know the correct join order, `join_collapse_limit = 1` is the most direct override. Setting it to 1 tells the planner to treat the explicit JOIN syntax as an ordered constraint: the planner will join the relations left to right in exactly the order written, with no reordering attempted. The planner still chooses join strategies (hash join vs. nested loop vs. merge join). It still evaluates parameterized paths. But it does not search for a better permutation.

To use this in practice: write the joins in the order you know to be efficient — driving table first, most selective joins early — then run the query with `join_collapse_limit = 1`:

```sql
SET join_collapse_limit = 1;

SELECT *
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN regions r ON r.id = c.region_id
JOIN promotions p ON p.id = o.promotion_id;
```

The setting takes effect per-session and per-transaction, so you can confine it to a single query by wrapping it in a transaction or using `SET LOCAL`. It does not affect other sessions or persist across connections.

`from_collapse_limit = 1` provides the analogous control for subqueries in a FROM clause. Normally the planner pulls subqueries up into the outer join search. This gives it more relations to reorder freely. With `from_collapse_limit = 1`, each subquery stays as a separate subproblem. The planner plans it independently, in the written nesting order. This is useful when a subquery is intentionally isolating a join result — for instance, pre-aggregating before a join to reduce cardinality. Without the limit, the planner keeps undoing that structure in its search.

The two limits work at different syntactic levels. `join_collapse_limit` applies when the planner decides whether to flatten an explicit JOIN tree into a flat list of relations (`deconstruct_recurse()`, `initsplan.c`). If flattening would produce more relations than the limit, the planner stops flattening. It creates a sub-joinlist instead. `from_collapse_limit` applies when the planner decides whether to inline a subquery's FROM list into the outer FROM list: if the resulting outer FROM list would exceed the limit, the subquery stays separate.

### Reading join order from EXPLAIN

The join tree chosen by the planner is directly visible in `EXPLAIN` output. The innermost node executes first. Each join operator names its two inputs. For a hash join, the top-level `Hash Join` node has two children — the outer side (the relation driving the loop, listed first) and the `Hash` node wrapping the inner side (the relation whose rows are loaded into the hash table). A typical efficient hash join has a smaller relation on the inner (hashed) side:

```
Hash Join
  -> Seq Scan on orders         <- outer, large
  -> Hash
       -> Seq Scan on regions   <- inner, small: hashed into memory
```

If the plan has this reversed — a small table as the outer driver and a large table being hashed — it is a sign the planner got the cardinality wrong. It believed the large table was small. Similarly, a nested loop with an unexpectedly large outer side is a strong signal that the upstream row estimate was wrong.

Nested loop plans are the most sensitive to bad row estimates. A plan that expects to probe an index 12 times is cheap. A plan that probes it 84,000 times is expensive in proportion. When `EXPLAIN ANALYZE` shows a nested loop outer side with actual rows far exceeding estimated rows, the planner chose the nested loop for a world that doesn't exist.

### GEQO threshold and planning time

For queries with many joins — complex reporting queries, ORM-generated queries that join a dozen tables — planning time can become significant even before a plan is executed. The DP algorithm is exponential in the worst case. Doubling the number of relations can more than double planning time.

Two knobs trade plan quality for planning speed. `geqo_threshold` (default 12) determines when the planner switches from DP to GEQO. Lowering it to, say, 8 means GEQO takes over earlier. This caps the DP search to smaller join counts and reduces planning time, at the cost of potentially worse plans for 8–11 table joins. Raising it above 12 forces DP to run longer on large join counts. This may find better plans but takes more time.

`join_collapse_limit` is a more aggressive lever. When set below the number of joins in a query, the planner stops searching for a better order. It trusts the written order. For a 20-table query where the written join order is known-good — because a developer has tuned it, or an ORM generates joins in a known-correct order — setting `join_collapse_limit = 8` reduces the set of relations exposed to DP to groups of at most 8. This eliminates most of the combinatorial search. Planning time drops substantially. If the written order is reasonable, the plan remains just as good.

### When there is no join hint syntax

PostgreSQL has no native syntax for join hints, analogous to Oracle's `/*+ LEADING(a b c) */` or SQL Server's query hints. The available approaches for controlling join order when the planner makes poor choices are:

**Write order plus join_collapse_limit = 1.** The most portable approach. Write the FROM clause and JOIN chain in the desired order, then set `join_collapse_limit = 1` for the query. No extension required; works on any PostgreSQL installation.

**CTE materialization barriers.** The planner plans a CTE declared with `MATERIALIZED` as a separate subproblem. It materializes the CTE's result before the outer query runs. This prevents the planner from pulling the CTE's tables into the outer join search and reordering them. It is not a join order hint in the precise sense — the planner still chooses order within the CTE and within the outer query separately — but it can enforce that a particular intermediate result is computed first:

```sql
WITH filtered_orders AS MATERIALIZED (
    SELECT * FROM orders WHERE status = 'complete'
)
SELECT * FROM filtered_orders fo
JOIN customers c ON c.id = fo.customer_id;
```

**pg_hint_plan.** The `pg_hint_plan` extension (available from PGDG and commonly pre-installed in managed services) adds Oracle-style hint comments that the planner reads and obeys. The `Leading` hint specifies join order directly:

```sql
/*+ Leading(o c r) HashJoin(o c) */
SELECT * FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN regions r ON r.id = c.region_id;
```

`pg_hint_plan` is the most expressive option for join tuning without rewriting SQL. It is an extension, though. It introduces a maintenance dependency. It is absent from some managed PostgreSQL environments.

## Related Topics

- [[subsystems/planner/geqo|GEQO]] — the genetic query optimizer that replaces dynamic programming when join counts exceed `geqo_threshold`
- [[subsystems/planner/cost-model|Cost Model]] — the cost functions used to evaluate and prune candidate join paths at each DP level
- [[subsystems/planner/equivalence-classes|Equivalence Classes]] — how equality conditions across relations are used to drive clause-based join enumeration
- [[subsystems/planner/join-method-selection|Join Method Selection]] — how the planner chooses between hash, merge, and nested loop joins once an ordering is fixed
- [[subsystems/planner/selectivity-estimation|Selectivity Estimation]] — how predicate selectivity is estimated to produce the row counts that guide join ordering
- [[subsystems/planner/stale-statistics-and-bad-plans|Stale Statistics and Bad Plans]] — how outdated statistics cause compounding cardinality errors across join levels
- [[subsystems/planner/restrict-info|RestrictInfo]] — the clause representation stored in `joininfo` lists that controls which join pairs the planner considers
- [[subsystems/planner/overview|Planner Overview]] — the overall planner architecture and planning stages.
- [[subsystems/planner/statistics|Planner Statistics]] — how row estimates that drive join cost decisions are computed.
- [[subsystems/indexes/btree|B-tree Index Internals]] — the index structures that parameterized paths rely on.
- [[subsystems/locking/overview|Lock Manager]] — lock acquisition ordering, which is influenced by the join order.
- [[architecture/overview|Architecture Overview]] — where the planner fits in the full query execution pipeline.

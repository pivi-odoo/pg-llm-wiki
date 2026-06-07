---
title: Subquery Planning and Flattening
aliases:
  - subquery flattening
  - subplan
  - initplan
  - pull_up_subqueries
  - semi-join conversion
tags:
  - symptom/slow-query
  - theme/query-optimization
source_files:
  - src/backend/optimizer/prep/prepjointree.c
  - src/backend/optimizer/plan/subselect.c
  - src/include/optimizer/prep.h
symbols:
  - pull_up_subqueries
  - pull_up_sublinks
  - pull_up_simple_subquery
  - is_simple_subquery
  - make_subplan
  - build_subplan
  - convert_ANY_sublink_to_join
  - convert_EXISTS_sublink_to_join
  - SubPlan
  - InitPlan
  - SubLink
---

# Subquery Planning and Flattening

Every subquery in a SQL statement forces the planner to decide between two fundamentally different execution strategies: flatten the subquery into the outer query so that the combined join space can be optimised as a whole, or leave it as a separate plan that the executor runs independently. The choice matters enormously for performance. A flattened subquery gives the planner visibility into the full join graph. Conditions can be pushed across the former subquery boundary. The join ordering algorithm can mix outer and inner relations freely. A separate plan is an opaque unit. The planner can optimise it in isolation, but cannot move predicates in or out of it. If the subquery is correlated, it must also be re-executed for every row of the outer query.

The preprocessing pipeline in `prepjointree.c` and `subselect.c` works hard to flatten whatever it legally can before the cost-based planner takes over.

## The Two Strategies

A subquery that survives into execution becomes either a **SubPlan** or an **InitPlan** node (`SubPlan` struct, `subselect.c`). The difference is whether the subquery is correlated with its outer query.

A **SubPlan** carries outer-query columns via `PARAM_EXEC` parameters listed in `parParam`. The executor evaluates it once per outer row (for subqueries in WHERE or JOIN conditions) or once per output row (for subqueries in the SELECT list). Even when a SubPlan is cheap to run, re-running it thousands of times on a large outer relation is rarely optimal.

An **InitPlan** has an empty `parParam` list — no outer-column references. The executor evaluates it exactly once before the outer query begins. It stores the result in an executor parameter slot. Any number of outer-query nodes can read that cached result at zero additional cost. The distinction matters even for EXISTS subqueries. An uncorrelated `EXISTS (SELECT ...)` becomes an InitPlan, and the executor checks it once. A correlated `EXISTS (SELECT ... WHERE t.id = outer.id)` becomes a SubPlan, and the executor checks it per row.

The build logic in `build_subplan()` checks `splan->parParam == NIL` to decide which case applies. For InitPlans, it records the plan on `root->init_plans` rather than embedding it in the expression tree.

## Subquery Flattening via pull_up_subqueries

The primary flattening pass is `pull_up_subqueries()` (`prepjointree.c`). It walks the query's join tree looking for range-table entries of kind `RTE_SUBQUERY`. When it finds one whose subquery is simple enough, it calls `pull_up_simple_subquery()` to fold the inner query into the parent.

The simplicity test lives in `is_simple_subquery()`. A subquery can be pulled up only if it lacks all of the following:

| Disqualifying feature | Reason |
|---|---|
| `hasAggs` / `groupClause` / `groupingSets` / `havingQual` | Aggregation defines group boundaries the planner cannot cross |
| `hasWindowFuncs` | Window functions depend on partition/order relative to the subquery result |
| `hasTargetSRFs` | Set-returning functions in the target list expand row counts unpredictably |
| `sortClause` / `distinctClause` | ORDER BY / DISTINCT impose ordering the parent might violate |
| `limitOffset` / `limitCount` | LIMIT/OFFSET restrict the row set in a way that a join cannot replicate |
| `setOperations` | UNION/INTERSECT/EXCEPT (except simple UNION ALL, handled separately) |
| `cteList` | WITH clauses are not yet inlineable in this path |
| `hasForUpdate` | Explicit locking must apply at the subquery's semantic level |
| `security_barrier` on the RTE | Scattering the subquery's Vars into the parent would allow the parent to filter rows before security quals run |

The planner straightforwardly merges a subquery that passes all these checks. `OffsetVarNodes` renumbers the subquery's range-table entries, and `CombineRangeTables` appends them to the parent's range table. `perform_pullup_replace_vars()` replaces every Var in the parent that referenced the now-vanished subquery RTE with the corresponding expression from the subquery's target list (`pullup_replace_vars_context`). The subquery's FROM/WHERE structure replaces the subquery's slot in the parent's join tree. From that point on, the planner sees a single flat query that it can optimise without any subquery boundary.

The recursive nature of the pass is deliberate: `pull_up_simple_subquery()` runs `pull_up_sublinks()` and `pull_up_subqueries()` on the child before merging, so the planner flattens nested subqueries from the inside out.

## Semi-join and Anti-join Conversion

A separate, earlier pass, `pull_up_sublinks()`, handles EXISTS, NOT EXISTS, IN, and ANY subqueries in WHERE clauses. It converts them into explicit join nodes before the main `pull_up_subqueries` pass runs.

`pull_up_sublinks()` walks the qual tree looking for `SubLink` nodes. When it finds a convertible case, it adds a new `JoinExpr` to the join tree. It also removes the SubLink from the qual, replacing it with a constant TRUE (the join's existence carries the condition).

**EXISTS**: `convert_EXISTS_sublink_to_join()` promotes the subquery to a range-table entry and inserts a semi-join (`JOIN_SEMI`). The planner can then implement it as a hash semi-join, merge semi-join, or nested loop, choosing based on cost. The key property is that the semi-join stops as soon as it finds one matching row — the same early-exit semantics as EXISTS.

**NOT EXISTS**: the planner calls the same function with the `under_not` flag set, producing an anti-join (`JOIN_ANTI`). The anti-join returns outer rows for which no inner match exists.

**IN / ANY**: `convert_ANY_sublink_to_join()` promotes the subquery and creates a semi-join. The testexpr (the comparison expression from the IN/= ANY clause) becomes the join condition. The function refuses to convert if the sub-select references any outer-query Var at level 1 (direct correlation), because that would produce incorrect results when the semi-join semantics are applied before the correlation is resolved. Conversion only applies at the top level of a WHERE or JOIN/ON clause. An IN inside an OR cannot be converted this way, because the three-valued logic differs.

For uncorrelated ANY subqueries that survive as SubPlans rather than joins, `build_subplan()` additionally checks whether the subplan result fits in `hash_mem`. If it does, it sets `splan->useHashTable = true`, allowing the executor to build a hash table over the subplan output once and then probe it for each outer row — essentially a hash semi-join without the full join conversion.

For a practical decision guide — when `EXISTS`, `IN`, or an explicit `JOIN` produces the fastest plan, and how to recognise a successful (or blocked) semijoin conversion in `EXPLAIN` output — see [[subsystems/planner/in-vs-exists-vs-join]].

## Lateral Subqueries

A LATERAL subquery — one whose FROM entry carries the `lateral` flag — can reference columns from earlier items in the same FROM clause. This reference is legal and deliberate, but it places constraints on flattening.

`pull_up_simple_subquery()` can still pull up a LATERAL subquery in many cases. The `is_simple_subquery()` check for LATERAL subqueries adds an additional condition via `jointree_contains_lateral_outer_refs()`: if the subquery's WHERE or JOIN/ON clauses contain lateral references to relations outside an enclosing outer join, the planner refuses pullup. Pulling up would require moving those quals above the outer join boundary, changing semantics.

When a LATERAL subquery cannot be flattened, it participates in path generation as a **parameterized path**. The planner treats the lateral references as parameters that must be supplied by the outer side of a nested loop. The executor evaluates the inner side fresh for each outer row that provides new parameter values — effectively the same as a correlated subplan, but with the full path and cost machinery rather than a SubPlan node.

## Correlated Scalar Subqueries

A scalar subquery in the SELECT list (e.g., `SELECT (SELECT name FROM users WHERE users.id = orders.user_id)`) or in a WHERE clause that returns a single value cannot be flattened: it references outer-query columns and produces one output value per outer row. These become SubPlan nodes with `subLinkType = EXPR_SUBLINK` (or `EXISTS_SUBLINK`, `ANY_SUBLINK`, etc.).

Because these SubPlans carry `parParam` entries linking them to the outer query, the executor must re-run the subplan for each outer row. A few strategies can mitigate the per-row cost:

- If the subplan's top node already materializes output (hash aggregate, sort, etc.), no extra buffering is needed.
- Otherwise, if `enable_material` is set and `parParam` is empty, the planner wraps the subplan in a Material node so repeated scans read from memory rather than re-executing the whole plan.
- If a correlated subplan happens to be used only once, the planner just re-runs it unconditionally.

The planning entry point for all SubLink → SubPlan conversion is `make_subplan()` in `subselect.c`. It calls `subquery_planner()` recursively on the subquery, selects the best path from the subroot, and calls `create_plan()` to materialise the plan. It then hands everything to `build_subplan()` to decide InitPlan vs SubPlan and wire up the parameter references.

## Processing Order

The planner runs these transformations in a fixed sequence before cost-based optimisation begins:

```
replace_empty_jointree      -- ensure every FROM has at least one RTE
pull_up_sublinks            -- EXISTS/IN/ANY → semi-join / anti-join
preprocess_function_rtes    -- inline set-returning functions
pull_up_subqueries          -- simple subqueries → merged join tree
flatten_simple_union_all    -- UNION ALL → append relation
(expression preprocessing)  -- SubLinks remaining → SubPlan / InitPlan
reduce_outer_joins          -- eliminate provably-unnecessary outer joins
remove_useless_result_rtes  -- clean up dummy RTEs
```

After `pull_up_subqueries` runs, the join tree contains no `RTE_SUBQUERY` entries that could be flattened. `SS_process_sublinks()` converts any SubLinks that still appear in expressions (because they could not be converted to joins) into SubPlan or InitPlan nodes during this subsequent pass. This pass is part of expression preprocessing in `subquery_planner()`.

## Key Data Structures

**SubPlan** node fields (used both for SubPlan and InitPlan in the plan tree):

| Field | Meaning |
|---|---|
| `subLinkType` | Type of sublink: EXISTS, EXPR, ANY, ALL, ARRAY, ROWCOMPARE, MULTIEXPR |
| `plan_id` | Index into `PlannedStmt.subplans` list |
| `parParam` | List of PARAM_EXEC parameter IDs supplied by the outer query (empty for InitPlan) |
| `args` | Outer-query expressions that supply the `parParam` values |
| `setParam` | List of PARAM_EXEC IDs set by this subplan (used by consumers) |
| `testexpr` | Comparison expression for ANY/ALL/ROWCOMPARE subplans |
| `useHashTable` | Whether the executor should hash the subplan output for probing |
| `unknownEqFalse` | For IN tests: treat unknown (NULL) comparisons as false |

**pullup_replace_vars_context** (internal to `prepjointree.c`):

| Field | Meaning |
|---|---|
| `targetlist` | The subquery's output expressions, used as replacements for Var references |
| `target_rte` | The RTE being dissolved |
| `relids` | Set of relids within the subquery (needed for PlaceHolderVar creation in LATERAL cases) |
| `wrap_non_vars` | Whether non-Var output items must be wrapped in PlaceHolderVars |
| `rv_cache` | Cache of already-built PHV wrappers to avoid duplicates |

## Relationship to Other Planner Phases

The flattening passes described here run entirely within `subquery_planner()` before `query_planner()` is called. By the time the join-ordering algorithm in `[[subsystems/planner/join-ordering]]` sees the query, the join tree contains only base relations and join nodes — no subquery wrappers. This is what gives the planner its ability to consider join orderings that cross what were subquery boundaries in the original SQL.

The `nodeSubplan.c` code in the executor executes subplans that survive flattening. See `[[subsystems/executor/overview]]` for how parameter slots are allocated and filled at runtime. The cost model accounts for SubPlan re-execution cost in `cost_subplan()` (`optimizer/path/costsize.c`). This informs the planner's decision between alternative formulations when both a join conversion and a SubPlan path are available (as happens for correlated EXISTS, where `make_subplan()` can generate both an `AlternativeSubPlan`).

## When Flattening Fails: Reading EXPLAIN and Rewriting

The sections above describe what the planner tries to do. In practice, some subqueries resist every transformation and execute as SubPlan nodes. Knowing how to spot them in EXPLAIN output and how to rewrite them manually is an essential skill for PostgreSQL query tuning.

### Spotting a SubPlan in EXPLAIN output

A SubPlan node appears in `EXPLAIN (ANALYZE, BUFFERS)` output indented beneath the plan node that invokes it, labeled with the name assigned by `build_subplan()`:

```
Seq Scan on orders o  (cost=... rows=... width=...)
  Filter: (SubPlan 1)
  SubPlan 1
    ->  Aggregate  (cost=... rows=1 width=...)
          ->  Seq Scan on items i  (cost=... rows=... width=...)
                Filter: (order_id = o.id)
```

The most revealing number is `loops=N` in the ANALYZE output — it records how many times the executor ran that node. For a SubPlan, `loops` equals the number of outer rows that triggered it. A SubPlan with `loops=50000` on a 50,000-row outer scan is executing the inner query once per outer row. The total cost is roughly `50000 × (inner plan cost)`. That multiplication is the performance problem.

An **InitPlan**, by contrast, appears above the main plan with `loops=1` regardless of outer cardinality. It is cheap. If EXPLAIN shows `InitPlan 1 (returns $0)` with `loops=1`, no action is needed.

### Correlated aggregate subquery → pre-aggregated LEFT JOIN

The most common SubPlan pattern is a correlated aggregate in the SELECT list: a subquery that counts or sums rows in a related table, correlated to the outer row via a foreign-key condition.

```sql
-- Before: SubPlan re-executed once per order row
SELECT o.id,
       (SELECT count(*) FROM items i WHERE i.order_id = o.id) AS cnt
FROM orders o;
```

The planner cannot flatten this because the subquery has an aggregate (`count(*)`). `is_simple_subquery()` rejects it (`hasAggs` is true). Every outer row triggers a fresh execution of the inner aggregate scan.

The manual rewrite pre-aggregates the inner table independently and then joins the result:

```sql
-- After: single pass over items, then one join
SELECT o.id, coalesce(c.cnt, 0) AS cnt
FROM orders o
LEFT JOIN (
    SELECT order_id, count(*) AS cnt
    FROM items
    GROUP BY order_id
) c ON c.order_id = o.id;
```

The LEFT JOIN is necessary rather than an inner join to preserve orders with zero items (where the inner subquery would have returned zero, and a coalesce of NULL from the outer join returns the same). The planner can now choose a hash join or merge join between `orders` and the pre-aggregated result, executing the group-by exactly once. For large outer relations this is typically orders of magnitude faster.

The same pattern applies to any aggregate function: `sum`, `max`, `min`, `avg`. The key structural move is always the same — extract the correlated aggregate into a derived table that groups by the join key, then LEFT JOIN and coalesce.

### EXISTS and the OR barrier

The planner invokes `convert_EXISTS_sublink_to_join()` only when `pull_up_sublinks_qual_recurse()` encounters the SubLink at a position it can reach by descending through AND nodes. The recursion stops at OR nodes (`prepjointree.c`: "Stop if not an AND"). An EXISTS inside an OR branch therefore stays as a SubPlan:

```sql
-- EXISTS inside OR: stays as SubPlan, re-executed per outer row
WHERE a.status = 'active'
   OR EXISTS (SELECT 1 FROM b WHERE b.fk = a.id)
```

There is no mechanical way to preserve exact OR semantics while converting the EXISTS to a semi-join in general, because the semi-join would need to be evaluated before the OR short-circuits. The practical fix depends on the query's intent. One approach is to split the two cases with UNION ALL:

```sql
SELECT * FROM a WHERE a.status = 'active'
UNION ALL
SELECT * FROM a WHERE a.status <> 'active'
  AND EXISTS (SELECT 1 FROM b WHERE b.fk = a.id);
```

This moves the EXISTS to a top-level WHERE clause where the conversion can succeed. The two branches together cover the same logical condition as the original OR. The UNION ALL approach does require care around duplicates. Duplicates can appear if `status = 'active'` rows also match the EXISTS condition.

The same restriction blocks EXISTS inside a CASE expression — `pull_up_sublinks_qual_recurse()` does not descend into CASE arms.

### IN (subquery) and what blocks hash semi-join

`convert_ANY_sublink_to_join()` almost always converts an uncorrelated `IN (subquery)` — one where the subquery does not reference any outer-query column — to a semi-join. The function checks `contain_vars_of_level(subselect, 1)` and refuses if the subquery has direct outer references. For a truly uncorrelated subquery, conversion succeeds. The planner then produces a hash semi-join or merge semi-join.

If EXPLAIN shows a SubPlan with `useHashTable` in effect (visible as `SubPlan ... (hashed)` in some output contexts), the semi-join conversion was blocked, but the hash optimization still applies: the executor materializes the subplan output into a hash table once and then probes it for each outer row. This is better than a plain SubPlan but still worse than a full semi-join, because the outer query cannot push predicates through the subplan boundary.

Features that block `convert_ANY_sublink_to_join()` entirely:

- A direct outer-column reference inside the subquery (the subquery is correlated).
- A volatile function in the `testexpr` — `contain_volatile_functions()` returns true, and the planner refuses the conversion to avoid changing how many times the volatile expression evaluates.

For the correlated case there is no automatic rescue: restructure the query manually instead (e.g., using the pre-aggregated LEFT JOIN pattern above). For the volatile case, if the volatile call is incidental and can be moved outside the subquery, removing it restores convertibility.

### Uncorrelated scalar subquery: leave it alone

A scalar subquery with no outer-column references — `WHERE price > (SELECT avg(list_price) FROM catalog)` — becomes an InitPlan. `build_subplan()` detects the empty `parParam` list and marks it as an InitPlan, evaluated exactly once before the outer query begins. The executor caches its result in a PARAM_EXEC slot and reads it at zero cost for every outer row. EXPLAIN shows it as `InitPlan 1 (returns $0)` with `loops=1`.

These are already optimal. Rewriting them into joins or CTEs adds complexity without benefit. It may even force repeated evaluation if the rewrite introduces a lateral reference.

### LATERAL as a deliberate correlated join

LATERAL subqueries in the FROM clause are a first-class language feature for expressing correlated joins. The planner treats them differently from SubPlan expressions. A LATERAL subquery participates in path generation as a parameterized path — the planner considers it as the inner side of a nested loop and can evaluate access methods (including index scans on the inner side) normally. The planner, by contrast, handles a correlated subquery in the SELECT list entirely outside the join-ordering machinery.

```sql
SELECT o.id, c.total
FROM orders o,
LATERAL (SELECT sum(amount) AS total FROM items WHERE order_id = o.id) c;
```

This is semantically equivalent to the correlated scalar subquery version. Because it is in the FROM clause, though, the planner can use an index on `items.order_id`. It can also choose between a nested loop (re-evaluating the LATERAL for each outer row with a good index) or flattening it further, if conditions allow. For queries where an index lookup per outer row is genuinely the best strategy — such as a lookup into a small inner table with high selectivity — LATERAL is often the right form to write explicitly.

### NOT IN and the NULL trap

`WHERE col NOT IN (SELECT ...)` translates to `WHERE col <> ALL(SELECT ...)` in the planner's internal representation. SQL three-valued logic means that if the subquery returns any NULL value, the `col <> NULL` comparison yields NULL (not TRUE). The entire NOT IN condition then yields NULL rather than TRUE for those rows. The result is that the outer query returns no rows at all whenever the subquery can produce a NULL — a correctness problem that masquerades as a performance problem or vice versa. See [[sql-features/subquery-patterns|Subquery Patterns]] for the canonical dangerous/safe SQL side by side.

The `build_subplan()` code records this via `unknownEqFalse`: for top-level IN tests it sets this to true to treat NULLs as non-matches. This matches the SQL semantics for IN. For NOT IN the planner cannot safely apply that shortcut without changing semantics.

Both the `NOT EXISTS` and `LEFT JOIN ... IS NULL` rewrites avoid the NULL ambiguity inherent in NOT IN. `convert_EXISTS_sublink_to_join()` converts `NOT EXISTS` to an anti-join (with `under_not = true`). It applies the same semi-join optimisation path as EXISTS. The `LEFT JOIN` / `IS NULL` anti-join pattern is equivalent and sometimes easier for the planner to optimise through predicate pushdown.

## Related Topics

- [[subsystems/executor/subplan-nodes|SubPlan Executor Nodes]] — how the executor evaluates SubPlan and InitPlan nodes at runtime, including parameter slot allocation and per-row re-execution.
- [[subsystems/planner/join-ordering|Join Ordering]] — the join-ordering algorithm that operates on the flattened join tree produced after subquery pullup; subquery flattening is a prerequisite for this stage.
- [[subsystems/planner/lateral-joins|Lateral Joins]] — deep dive into parameterized paths and the planner's treatment of LATERAL subqueries in FROM clauses.
- [[subsystems/planner/in-vs-exists-vs-join|IN vs EXISTS vs JOIN]] — practical comparison of the three formulations and when the planner converts each to semi-join or anti-join paths.
- [[subsystems/planner/ctes|CTEs in the Planner]] — how WITH clauses interact with subquery flattening and when CTEs act as optimisation fences preventing pullup.
- [[sql-features/subquery-patterns|Subquery Patterns]] — SQL-level patterns for expressing correlated lookups, lateral joins, and anti-joins with notes on planner behavior for each.
- [[subsystems/planner/predicate-pushdown|Predicate Pushdown]] — how quals are pushed into and across subquery and join boundaries once the join tree is flattened.

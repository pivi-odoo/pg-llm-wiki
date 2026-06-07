---
title: PARAM_EXEC Parameters and PlaceHolderVars
aliases:
  - PARAM_EXEC
  - paramExecTypes
  - PlaceHolderVar
  - PlannerParamItem
  - NestLoopParam
  - placeholder expression
  - ph_eval_at
  - phrels
source_files:
  - src/backend/optimizer/util/paramassign.c
  - src/backend/optimizer/util/placeholder.c
  - src/include/nodes/pathnodes.h
symbols:
  - PARAM_EXEC
  - PARAM_EXTERN
  - PlaceHolderVar
  - PlaceHolderInfo
  - PlannerParamItem
  - NestLoopParam
  - PlannerInfo
  - assign_param_for_var
  - replace_outer_var
  - replace_nestloop_param_var
  - generate_new_exec_param
  - assign_special_exec_param
  - make_placeholder_expr
  - find_placeholder_info
  - add_placeholders_to_joinrel
  - paramExecTypes
  - plan_params
  - curOuterParams
  - ph_eval_at
  - phrels
  - phnullingrels
---

The planner uses two interrelated mechanisms to move expression values across plan node boundaries: `PARAM_EXEC` integer slots that give subplans and NestLoop nodes a shared channel for passing values at runtime, and `PlaceHolderVar` wrapper nodes that pin expressions to a specific join level so that outer joins cannot silently change their nullability. Both mechanisms are resolved entirely at planning time; by the time the executor starts, `PlaceHolderVar` nodes are gone and every `PARAM_EXEC` slot has a fixed integer ID in the shared parameter array.

## PARAM_EXEC Slots and the Parameter Array

Every plan produced by the planner comes with a flat array of executor parameter slots. At runtime, the executor keeps one `ParamExecData` array per query, indexed by an integer ID. A plan node that produces a value writes into the array; a plan node that consumes a value reads from it by evaluating a `Param` node whose `paramkind` is `PARAM_EXEC` and whose `paramid` indexes into the array.

This is distinct from `PARAM_EXTERN` parameters. `PARAM_EXTERN` slots hold values supplied by the client application — the `$1`, `$2`, ... bind parameters in a prepared statement. Their values arrive from outside the plan and are never written by plan nodes. `PARAM_EXEC` slots, by contrast, are purely internal: the planner assigns them, and only plan nodes write and read them during execution.

The planner manages a global list, `root->glob->paramExecTypes`, that records the data type OID of every slot allocated for the current plan. Slot allocation is permanent: once a slot is appended to the list it keeps its index for the lifetime of the plan. The slot number is literally the list length at the moment of allocation — `list_length(root->glob->paramExecTypes)` before appending the new OID. Because the list is global across all query levels, there is a single flat namespace of slot IDs and no risk of two parts of the plan tree fighting over the same integer.

### What Uses PARAM_EXEC Slots

Three distinct situations cause the planner to allocate `PARAM_EXEC` slots:

**Subquery outer references.** When a subquery references a column from an outer query level — a correlated reference — the planner cannot simply pass a `Var` node through the plan boundary. The column value must be injected into the subplan from outside. The planner replaces each such `Var` with a `Param` node (via `replace_outer_var`) and records the required mapping in `root->plan_params` at the outer query level. `plan_params` is a list of `PlannerParamItem` structs, each holding the original expression and the slot ID assigned to it. After the subquery's plan is built, the outer query level knows which column values it must push into which slots before the subplan executes.

**NestLoop parameterization.** In a nested-loop join, the inner side may need a column value from the current outer row — for instance, when the inner side carries an index scan parameterized on the join key. These dependencies are tracked in `root->curOuterParams`, a list of `NestLoopParam` structs. Each `NestLoopParam` records a slot ID and the outer-side expression (a `Var` or `PlaceHolderVar`) whose value must be loaded into that slot. When `create_plan.c` constructs a `NestLoop` plan node, it uses `identify_current_nestloop_params` to collect the relevant `NestLoopParam` entries from `curOuterParams` and attach them to the `NestLoop` node. The executor then loads those slots with each fresh outer-row value before rescanning the inner plan.

**SubPlan outputs.** When a subplan produces a value consumed by its parent (an InitPlan storing a scalar result, or a SubPlan feeding an aggregate), `generate_new_exec_param` allocates a fresh slot without creating a `PlannerParamItem`. No deduplication is needed here because the slot is single-purpose: the subplan writes it exactly once (per execution unit). The parent reads it.

There are also special-purpose slots allocated by `assign_special_exec_param`, which stores `InvalidOid` as the type to mark slots that carry no actual value — they are used for change-signaling between a recursive union node and its worktable scan, and within the `EvalPlanQual` mechanism.

### Deduplication Within a Scope

The planner reuses an existing slot rather than allocating a new one when the same `Var` (or `PlaceHolderVar`) is referenced multiple times within the same subplan or nestloop scope. `assign_param_for_var` scans `root->plan_params` at the appropriate query level before allocating, and returns the existing `paramId` if a matching entry is already present. The same deduplication logic applies to `curOuterParams` in `replace_nestloop_param_var`.

The scope of deduplication is intentionally narrow. Once the planner finishes processing a subplan and resets `plan_params` to empty, subsequent references to the same `Var` will get fresh slots. The same applies once a NestLoop node is created and its `NestLoopParam` entries are removed from `curOuterParams`. The code comments note that an earlier attempt to avoid allocating duplicate slots across scopes caused latent bugs due to overlapping parameter lifetimes, and was abandoned.

## PlaceHolderVar: Pinning Expressions to a Join Level

When a subquery is pulled up into the outer query by `pull_up_subqueries`, expressions from the subquery's target list are substituted for `Var` references in the outer query. Most expressions can be placed wherever the planner finds it convenient to evaluate them — pushed down to a base relation scan, or evaluated at any join node along the way. But some expressions involve outer joins in a way that makes early evaluation incorrect.

Consider an expression derived from a subquery that is on the nullable side of a LEFT JOIN. If the join produces a null-extended row, the expression should yield NULL — not the value that the subquery would have computed before the join. Evaluating the expression below the join and then passing the pre-computed value upward gives the wrong answer.

A `PlaceHolderVar` node wraps such an expression and carries metadata that prevents the planner from moving the computation below the wrong join. It has four key fields:

- `phexpr`: the wrapped expression itself.
- `phrels`: the set of relids that syntactically enclose the expression's origin — the "source" set in the join tree. This determines the lowest join level at which the expression is semantically well-defined.
- `phnullingrels`: the set of outer-join relids that can null the result of this expression. Equivalent to `varnullingrels` on a plain `Var`.
- `phid`: a planner-run-unique integer ID. Two `PlaceHolderVar` nodes with the same `phid` represent the same logical expression. Comparison ignores `phexpr` to handle cases where the contained expression has been rewritten differently in different copies.

### PlaceHolderInfo: The Central Metadata Record

For every distinct `PlaceHolderVar` expression that is actually referenced in the plan tree, the planner creates exactly one `PlaceHolderInfo` node in `root->placeholder_list`. The `PlaceHolderInfo` holds the information needed for join-order decisions:

- `ph_eval_at`: the lowest join level at which the expression can be correctly evaluated. Initialized as the intersection of `phrels` with the set of relations actually referenced inside `phexpr`. If the expression contains no relation references (e.g., a constant wrapped in a PHV), `ph_eval_at` falls back to `phrels` itself. The planner may later widen this set to avoid evaluating the expression before a required join is performed.
- `ph_lateral`: relids referenced inside `phexpr` that lie outside `phrels` — lateral references from a subquery's inner expression to outer query levels. These are tracked separately because they must be available when the expression is evaluated but are not part of the expression's "home" join level.
- `ph_needed`: the highest join level at which the expression's value is consumed. This is analogous to `attr_needed` for base-relation columns. It drives where the expression appears in join rel target lists.

The `PlaceHolderInfo` is created lazily by `find_placeholder_info` the first time a given `phid` is encountered during planning. If `placeholdersFrozen` is already true, creating a new info is an error — the set of PHVs must be stable before join-order planning begins.

### Where Placeholders Are Evaluated

During join path enumeration, `add_placeholders_to_joinrel` checks each `PlaceHolderInfo` whenever a new join relation is formed. If `ph_eval_at` is a subset of the new join relation's relids — meaning all required inputs are now available — and `ph_needed` extends above this join — meaning something higher will consume the value — then the `PlaceHolderVar` is added to the join relation's target list. This is the point at which the expression is committed to being evaluated.

If `ph_eval_at` is a subset of a single base relation's relid set, `add_placeholders_to_base_rels` handles the earlier case: the expression can be computed at scan time and carried upward as an attribute-like column.

Once the best plan is selected and `create_plan.c` constructs the final plan tree, `PlaceHolderVar` nodes in target lists are replaced: either by the actual expression (if evaluated at the current level) or by a `Var` or `Param` referencing the result computed lower in the tree. At execution time no `PlaceHolderVar` nodes exist; they are entirely a planning-time construct.

The underlying need for placeholders comes from the semantics of outer joins. A plain inner join cannot produce a null-extended row from either input, so expressions can be freely pushed down or pulled up. A LEFT JOIN's right side may produce no matching row, and all columns from that side become NULL in the output. An expression that reads one of those columns must be evaluated after the join, not before. Otherwise it will compute a non-null result from actual column values that the join semantics require to appear as NULL.

Subquery pullup is the primary source of `PlaceHolderVar` creation. When `pull_up_simple_subquery` substitutes a subquery's target expressions into the outer query, it wraps expressions in `PlaceHolderVar` nodes when the subquery was on the nullable side of an outer join (`wrap_non_vars` flag in `pullup_replace_vars_context`). The wrapper prevents those expressions from floating below the outer join during subsequent path enumeration.

LATERAL subqueries generate `PlaceHolderVar` nodes for a different reason: an expression inside a LATERAL subquery may reference both local relations and outer lateral references. The planner wraps such expressions so that `ph_lateral` correctly captures the lateral dependency. This lets the join-ordering logic know which outer relations must be available before the LATERAL subquery can be evaluated.

## Interaction Between the Two Mechanisms

`PlaceHolderVar` nodes and `PARAM_EXEC` slots interact when a PHV must be passed across a plan boundary. If a `PlaceHolderVar` from an outer query level must be supplied to a subplan, `replace_outer_placeholdervar` allocates a `PARAM_EXEC` slot for it and records a `PlannerParamItem` just as `replace_outer_var` does for plain `Var` nodes. Similarly, if a NestLoop must pass a `PlaceHolderVar` value to its inner side, `replace_nestloop_param_placeholdervar` allocates a slot and records a `NestLoopParam` entry.

In both cases the underlying mechanism is identical to the `Var` path: a `PARAM_EXEC` slot carries the evaluated value at runtime. The plan node responsible for computing the PHV expression writes the result into that slot before the consumer executes.

## Related Topics

- [[subsystems/planner/subqueries|Subquery Planning and Flattening]] — how subqueries are pulled up and how SubPlan/InitPlan nodes are created
- [[subsystems/planner/lateral-joins|Lateral Joins]] — how LATERAL subqueries generate parameterized paths and PHVs
- [[subsystems/planner/join-ordering|Join Ordering]] — how the planner enumerates join orders subject to PHV and lateral constraints
- [[subsystems/planner/outer-join-promotion|Outer Join Promotion and Simplification]] — when outer joins can be simplified, reducing the need for PHVs
- [[subsystems/executor/overview|Executor Overview]] — how the parameter array is initialized and how plan nodes read and write PARAM_EXEC slots at runtime

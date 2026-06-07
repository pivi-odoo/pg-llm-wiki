---
title: "Target List and Var Utilities"
aliases:
  - target list
  - tlist
  - PathTarget
  - pull_varnos
  - pull_var_clause
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/util/tlist.c
  - src/backend/optimizer/util/var.c
symbols:
  - TargetEntry
  - PathTarget
  - tlist_member
  - add_to_flat_tlist
  - get_tlist_exprs
  - tlist_same_exprs
  - apply_tlist_labeling
  - get_sortgroupref_tle
  - make_pathtarget_from_tlist
  - make_tlist_from_pathtarget
  - copy_pathtarget
  - add_column_to_pathtarget
  - add_new_column_to_pathtarget
  - split_pathtarget_at_srfs
  - pull_varnos
  - pull_varattnos
  - pull_vars_of_level
  - contain_var_clause
  - contain_vars_of_level
  - pull_var_clause
  - flatten_join_alias_vars
---

The target list is the ordered list of expressions a plan node must compute — the row shape it produces. Two parallel representations exist inside the planner: the executor-facing `List` of `TargetEntry` nodes used throughout query trees and plan nodes, and the leaner `PathTarget` used during path generation. Alongside these, `var.c` provides the analysis primitives — walking expression trees to find which relations and columns a clause references — that the rest of the planner depends on for join ordering, parameterisation, and alias resolution.

## TargetEntry and the tlist model

A `TargetEntry` wraps one expression with its output-column metadata: `resno` (1-based column position), `resname` (column alias for display), `ressortgroupref` (non-zero if this column participates in ORDER BY, GROUP BY, or DISTINCT), `resorigtbl`/`resorigcol` (original base-table identity for the pg wire protocol), and the `resjunk` flag marking columns that are computed for internal use but not returned to the client. System columns used for sort keys or for RETURNING evaluation are the common case for junk columns.

Searching a tlist uses structural equality by default (`tlist_member()`, `tlist.c`). The variant `tlist_member_match_var()` relaxes this to match only on `varno`, `varattno`, `varlevelsup`, and `vartype`. This handles cases where a set-returning function has been inlined and the planner ends up with more type information than when the original `Var` was created. In particular, the `typmod` of the new expression may differ from the old Var's typmod, but the semantics are identical.

The `resjunk` / non-junk split is a recurring distinction. `get_tlist_exprs()` strips `TargetEntry` wrappers and returns raw expressions, respecting an `includeJunk` flag. `count_nonjunk_tlist_entries()` returns the user-visible column count. `tlist_same_exprs()` tests whether two tlists carry the same expression trees by position, deliberately ignoring all label fields. This is the correct predicate for deciding whether a non-projection-capable plan node (e.g., a scan whose output is already the right shape) can have a new tlist jammed into it without adding a `Result` projection node.

`apply_tlist_labeling()` (`tlist.c`) copies only the metadata fields from one tlist onto another that carries the same expressions. `createplan.c` uses this when it decides to reuse an existing tlist for a non-projection node but needs to restore the output labeling that the planner had computed.

## SortGroupRef bookkeeping

ORDER BY, GROUP BY, DISTINCT, and window function partitioning all need to refer to specific tlist expressions. Rather than duplicating expression trees, the planner assigns each such expression a non-zero `ressortgroupref` index. `SortGroupClause` nodes carry the matching index in their `tleSortGroupRef` field. This indirection survives expression equality failures (two syntactically equal expressions can be the same sort key or different ones depending on their role). It also survives the label-stripping that `tlist_same_exprs()` intentionally performs.

The lookup functions `get_sortgroupref_tle()`, `get_sortgroupclause_tle()`, `get_sortgroupclause_expr()`, and `get_sortgrouplist_exprs()` (all in `tlist.c`) traverse a targetlist by `ressortgroupref` value. Downstream consumers of GROUP BY and ORDER BY — hash aggregation, sort nodes, merge join key extraction — use `extract_grouping_cols()`, `extract_grouping_ops()`, and `extract_grouping_collations()` to convert the `SortGroupClause` list into plain arrays of `AttrNumber`, operator OIDs, and collation OIDs suitable for the executor.

The planner chooses between sort-based and hash-based grouping strategies. `grouping_is_sortable()` checks whether every `SortGroupClause` has a valid sort operator. `grouping_is_hashable()` checks whether all clauses were marked hashable by the parser. Both checks scan the clause list once and return a boolean — the parser does the heavy lifting of setting these flags when the query is analyzed.

## PathTarget: a leaner representation for path generation

During path enumeration, the full `TargetEntry` decoration is unnecessary overhead. `PathTarget` (`pathnodes.h`) stores only what path generation needs:

| Field | Purpose |
|---|---|
| `exprs` | `List *` of bare expressions (no `TargetEntry` wrappers) |
| `sortgrouprefs` | Parallel `Index[]` array; 0 for expressions with no sort/group role |
| `cost` | Estimated evaluation cost of all expressions |
| `width` | Estimated average output row width in bytes |
| `has_volatile_expr` | Tristate: `VOLATILITY_UNKNOWN`, `VOLATILITY_NOVOLATILE`, or `VOLATILITY_VOLATILE` |

`make_pathtarget_from_tlist()` and `make_tlist_from_pathtarget()` convert between the two representations. The conversion is not free-of-information-loss in general: `resname`, `resorigtbl`, `resorigcol`, and `resjunk` have no counterpart in `PathTarget`. This is acceptable because paths carry only what is needed for cost estimation and plan shape. `createplan.c` re-applies the full labeling from the original query's tlist via `apply_tlist_labeling()` once it has chosen the winning plan.

`add_column_to_pathtarget()` appends one expression, allocating or resizing `sortgrouprefs` only if a non-zero ref is supplied. When the caller adds a column without a sortgroupref to a target that has no sortgrouprefs yet, neither allocation nor copying occurs. Adding to a target resets `has_volatile_expr` from `VOLATILITY_NOVOLATILE` to `VOLATILITY_UNKNOWN` conservatively — only `contain_volatile_functions()` can confirm the new state.

`copy_pathtarget()` performs a shallow copy: it duplicates the `exprs` list but shares the underlying expression trees. This is the intended behaviour since path generation builds many targets that mostly reference the same underlying column expressions.

## SRF splitting and multi-level projection

Set-returning functions (SRFs) impose a constraint the executor enforces: an SRF must appear at the top level of a `ProjectSet` node's target list. A deeply nested expression like `x + srf1(srf2(y))` cannot be evaluated in a single step. `split_pathtarget_at_srfs()` (`tlist.c`) analyses a `PathTarget` for SRF nesting. It decomposes the target into a sequence of targets, lowest first, each satisfying the "SRFs only at top level" invariant.

The decomposition tracks nesting depth: an SRF at depth 1 wraps no other SRF. An SRF at depth 2 has another SRF among its arguments. For `x + srf1(srf2(y + z))`, the levels are:

```
Level 0 (SRF-free):          x, y, z
Level 1 (depth-1 SRFs):      x, srf2(y + z)
Level 2 (depth-2 SRFs / top): x + srf1(srf2(y + z))
```

After `setrefs.c` substitutes `Var` references for already-computed subexpressions, each level becomes a valid `ProjectSet` (or plain `Result` for SRF-free levels) target list. The outputs of `split_pathtarget_at_srfs()` are two parallel lists: the `PathTarget` for each level and a boolean indicating whether that level actually evaluates any SRFs. The function inserts an extra SRF-free level whenever the outermost expression at maximum SRF depth is not itself an SRF call — meaning the final scalar combination of SRF results needs its own projection step.

The function treats expressions that already appear in `input_target` as opaque Var-like atoms regardless of whether they contain SRFs, preventing redundant re-expansion of SRFs already computed by a lower plan node.

## Var analysis: pulling and testing

`var.c` provides a family of tree-walker functions that answer questions about which relations and columns an expression references. These are fundamental to join ordering, clause placement, and parameterised path construction.

`pull_varnos()` returns a `Relids` bitmapset of every relation (by RT index) referenced in an expression at the current query level. It includes `varnullingrels` from `Var` nodes and `phnullingrels` from `PlaceHolderVar` nodes — the outer-join relids that can null those variables. The planner needs these to correctly enforce outer-join semantics when considering clause placement. For `PlaceHolderVar` nodes, if a `PlaceHolderInfo` is available, `pull_varnos_walker()` uses `ph_eval_at` (the computed evaluation point) rather than `phrels` (the syntactic scope). The fallback to `phrels` exists for early-planning phases before `PlaceHolderInfo` is populated.

`pull_varattnos()` accumulates a `Bitmapset` of attribute numbers referenced by a specific relation (`varno`) within an expression. The function offsets attribute numbers by `FirstLowInvalidHeapAttributeNumber` so that system attributes (e.g., `ctid`) can be represented. The planner uses this when constructing column lists for index-only scan feasibility and for partial-index predicate checks.

`pull_var_clause()` (`var.c`) is the workhorse for post-sublink-reduction analysis. It collects all `Var`-like leaves from an expression, but flag bits control its behaviour for the four "complex leaf" types — `Aggref`, `GroupingFunc`, `WindowFunc`, and `PlaceHolderVar`:

| Flag pair | Effect |
|---|---|
| `PVC_INCLUDE_AGGREGATES` / `PVC_RECURSE_AGGREGATES` | Treat `Aggref` as an opaque leaf, or descend into its arguments |
| `PVC_INCLUDE_WINDOWFUNCS` / `PVC_RECURSE_WINDOWFUNCS` | Same for `WindowFunc` |
| `PVC_INCLUDE_PLACEHOLDERS` / `PVC_RECURSE_PLACEHOLDERS` | Same for `PlaceHolderVar` |

Specifying neither flag in a pair causes `pull_var_clause_walker()` to throw an error if it encounters the corresponding node type — a deliberate safety check asserting that the caller does not expect to encounter it at this planning stage.

`contain_var_clause()` is a fast boolean test for whether any current-level `Var` exists in an expression. The planner uses it for constant-expression detection and for deciding whether a clause can be pushed below a subquery boundary. `contain_vars_of_level()` generalises this to a specific query level. This matters when checking whether a subquery's WHERE clause has lateral references to an outer level.

## Join alias flattening

Before the planner builds join paths, it must replace `Var` nodes that reference join RTE outputs with references to the underlying base-relation columns. `flatten_join_alias_vars()` (`var.c`) performs this substitution using an expression mutator traversal. The function expands whole-row join Vars (where `varattno = InvalidAttrNumber`) into `RowExpr` nodes enumerating all non-dropped output columns.

The subtle part is preserving `varnullingrels`: when an outer join can null the variable, that information must be carried through to the replacement expression. If the replacement is a plain `Var`, `PlaceHolderVar`, or a chain of implicit coercions (the forms the parser puts into join alias lists), `adjust_standard_join_alias_expression()` threads the `varnullingrels` bits into the existing nulling fields. When the replacement is an arbitrary expression (possible when subselects have been flattened into join alias lists), and when a `PlannerInfo *root` is available, the function creates a new `PlaceHolderVar` wrapper to carry the nulling information. Without a root — in the parser's pre-planning usage of this function to expand join aliases before GROUP BY validation — the function cannot create that wrapper. `flatten_join_alias_vars()` then only supports the "standard" alias expression forms.

## Related Topics

- [[subsystems/planner/overview|Planner Overview]] — how target lists fit into the broader planning pipeline and upper-rel stages

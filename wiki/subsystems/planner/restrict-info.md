---
title: RestrictInfo — Clause Metadata in the PostgreSQL Planner
aliases:
  - RestrictInfo
  - restriction clause
  - join clause wrapper
  - clause metadata cache
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/util/restrictinfo.c
  - src/include/nodes/pathnodes.h
symbols:
  - RestrictInfo
  - make_restrictinfo
  - make_restrictinfo_internal
  - commute_restrictinfo
  - join_clause_is_movable_to
  - join_clause_is_movable_into
  - restriction_is_securely_promotable
  - extract_actual_clauses
  - RINFO_IS_PUSHED_DOWN
---

Every WHERE predicate and JOIN/ON condition in a query reaches the planner as a plain expression tree, but the optimizer needs far more than the expression itself to make good decisions. The planner immediately wraps each clause in a `RestrictInfo` struct, which serves as a durable cache for every expensive-to-derive property of that clause — selectivity estimates, applicable index operators, security classification, and the sets of relations the clause touches. Because a single clause can be considered by dozens of path alternatives across multiple join orderings, storing all derived metadata in one place avoids redundant computation and keeps path evaluation fast.

## What a RestrictInfo Contains

The `RestrictInfo` struct holds the original clause in its `clause` field and then layers on several categories of derived state.

**Relid sets** are computed once at construction time and form the backbone of clause placement decisions:

- `clause_relids` — the bitmask of rangetable indexes (base relation OIDs represented as RT numbers, plus any outer-join relids carried in `varnullingrels`) that actually appear in the clause expression.
- `required_relids` — the minimum set of relids that must be available before the clause can be evaluated. This is usually identical to `clause_relids`, but for certain outer-join ON clauses, the planner deliberately widens it to include the OJ's relid. This forces evaluation of the clause at the join node rather than below it. It also prevents the planner from applying predicates before the null-extension that the outer join produces.
- `outer_relids` — for outer-join clauses only, the set of relids on the outer (preserved) side of the join. The planner cannot push a clause into a parameterized scan of any relation in this set, because doing so would suppress rows that should instead emerge as null-extended.
- `incompatible_relids` — outer-join relids above which the clause must not be evaluated; used primarily to prevent multiple clones of the same clause from all being applied at the same join level.
- `left_relids` / `right_relids` — for binary operator clauses, the relids referenced by the left and right operands respectively.

**Selectivity and cost caches** start at their sentinel value of `-1`. The planner fills them in on first demand. `norm_selec` holds the selectivity under inner-join semantics. `outer_selec` holds the equivalent for outer-join evaluation. The `eval_cost` field caches the `QualCost` of evaluating the expression so that cost models can sum clause costs without re-traversing the expression tree. The planner similarly lazy-fills the merge-join and hash-join caches (`scansel_cache`, `left_bucketsize`, `right_bucketsize`, `left_mcvfreq`, `right_mcvfreq`) as it considers each join method.

**Behavioral flags** encode properties that affect where and whether the clause can be used:

- `is_pushed_down` — true if the clause originated from WHERE or INNER JOIN (always), or if an outer-join ON clause has been demoted to a filter predicate because it is degenerate (references only the nullable side). The `RINFO_IS_PUSHED_DOWN` macro extends this test to catch clauses whose `required_relids` exceed the current join's scope. This can happen when the planner considers parameterized paths.
- `can_join` — true when the clause is a binary operator whose left and right relid sets are disjoint and non-empty, meaning it syntactically resembles a join condition and might be usable as a merge-join or hash-join clause.
- `pseudoconstant` — true when the clause references no Vars from the current query level and no volatile functions. The planner can hoist such clauses into a `gating Result` node that evaluates them once rather than per tuple.
- `leakproof` — set for clauses at security level > 0 that are known not to leak information through side channels. The planner may evaluate leakproof clauses before security-barrier views' filter conditions, but it cannot do so for non-leakproof ones.
- `has_clone` / `is_clone` — when outer-join identity 3 requires generating multiple variants of the same predicate with different nulling-rel bitmasks, all variants share an `rinfo_serial` and are marked to ensure only one is applied per plan.

## Restriction Clauses versus Join Clauses

The distinction between a restriction clause and a join clause is not a property of the node type — both are `RestrictInfo` structs — but of how many base relations the clause references.

A clause whose `clause_relids` (after stripping outer-join relids) resolves to exactly one base relation is a **restriction clause**. The planner places it in the `baserestrictinfo` list of the corresponding `RelOptInfo`. The executor applies it during the scan of that relation, either as an index qual or as a filter in the scan node's `qual` list. Index selection considers each `RestrictInfo` in `baserestrictinfo` to see whether its operator, combined with the indexed column, falls within a supported operator family.

A clause whose `clause_relids` spans two or more base relations is a **join clause**. The planner distributes it into the `joininfo` list of every `RelOptInfo` that covers a strict subset of the referenced relations. These lists drive join-tree construction: when the planner is building a join of relations A and B, it consults `joininfo` lists from both A and B to discover what clauses become applicable at that join node. The planner has not yet applied the clause. It remains pending until the planner forms a join rel that encompasses all its referenced relations.

This two-tier distribution means each `RestrictInfo` has a well-defined evaluation point. The `required_relids` field makes this point precise: the planner cannot apply a clause until all relations in `required_relids` are in scope.

## Avoiding Redundant Computation

A key motivation for wrapping clauses in `RestrictInfo` rather than using bare expression nodes is that the planner evaluates many candidate paths. A query with five tables might explore hundreds of join orderings. Each path evaluation touches every clause that applies to a given join. Without caching, each path evaluation would re-derive selectivity, re-check for merge-join operators, and re-examine index applicability from scratch.

The planner initializes all expensive-to-compute fields in `RestrictInfo` to sentinel values (`-1` for costs and selectivities, `NIL` for operator lists, `InvalidOid` for operator OIDs) and fills them in lazily on first access. Once filled, they remain valid for the entire planning session, because they depend only on the clause expression and the statistics in `pg_statistic`. Neither changes during planning.

The `rinfo_serial` field ensures that a commuted copy inherits the original's selectivity and cost cache. `commute_restrictinfo` produces this copy when an index requires the Var of a binary operator clause on the left side. Commutation does not change the clause's semantics, so the cached values stay valid.

## Role in Index and Join Method Selection

**Index selection** inspects `baserestrictinfo` clauses for each base relation. For each `RestrictInfo` that passes the initial `can_join = false` check (or where `clause_relids` is a singleton), the index AM's match function tests whether the clause's operator is a member of the index's operator family. The planner gathers matching clauses into `IndexClause` nodes that reference the original `RestrictInfo`. This lets the planner reuse the cached selectivity when it estimates index scan cost. The `norm_selec` value on the `RestrictInfo` directly informs how many tuples the planner expects the index to return.

**Join method selection** for merge joins uses `mergeopfamilies` (a list of operator family OIDs) to confirm that the clause's operator supports sorted merge joins, then uses `scansel_cache` to cache per-sort-ordering selectivities computed by `mergejoinscansel`. For hash joins, `hashjoinoperator` records whether a valid hash operator exists. `left_bucketsize` / `right_bucketsize` inform the hash table sizing calculation that underlies hash join cost estimation.

The planner consults the `outer_relids` field when it considers parameterized paths. `join_clause_is_movable_to` checks that the target base relation is not in `outer_relids` before it allows the planner to move a join clause "down" into a parameterized index scan. Similarly, `join_clause_is_movable_into` uses `clause_relids` and `outer_relids` to verify that a proposed evaluation point is consistent with the clause's semantics.

## Security Level and Evaluation Order

The `security_level` field encodes the trust level of the source from which the clause arrived. Clauses from user-supplied WHERE conditions come in at level 0. Clauses injected by row-level security policies or security-barrier views carry higher levels. The rule is that the planner cannot evaluate a higher-numbered clause before a lower-numbered clause unless the higher one is leakproof. `restriction_is_securely_promotable` enforces this rule: the planner may not push an index qual from a high-security-level `RestrictInfo` in front of base restriction filters from lower security levels, unless the qual is leakproof. This prevents an index condition from inadvertently revealing information about rows that a security filter should have hidden first.

## OR Clauses and Sub-RestrictInfos

Flat clauses map cleanly to a single `RestrictInfo`, but OR clauses need special treatment for OR-indexscan paths. When `make_restrictinfo` encounters a top-level OR clause, it calls `make_sub_restrictinfos` to build a modified copy of the OR expression. In this copy, `make_sub_restrictinfos` wraps each non-AND sub-clause in its own `RestrictInfo`. The planner stores the result in the `orclause` field of the outer `RestrictInfo`. The outer node's `clause` field retains the original OR expression for general filter evaluation, while `orclause` exposes individual sub-clauses with their own cached metadata for the OR-indexscan machinery to match against index operator families independently.

## Related Topics

- [[subsystems/planner/index-selection|Index selection]]
- [[subsystems/planner/join-method-selection|Join method selection]]
- [[subsystems/planner/selectivity-estimation|Selectivity estimation]]
- [[subsystems/planner/equivalence-classes|Equivalence classes]]
- [[subsystems/planner/predicate-pushdown|Predicate pushdown]]
- [[subsystems/planner/join-ordering|Join ordering]]
- [[subsystems/planner/overview|Planner overview]]

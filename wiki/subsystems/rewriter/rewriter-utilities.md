---
title: "Rewriter Support and Rule Removal"
aliases:
  - rewriteSupport
  - rewriteRemove
  - rule catalog maintenance
tags:
  - theme/caching
source_files:
  - src/backend/rewrite/rewriteSupport.c
  - src/backend/rewrite/rewriteRemove.c
  - src/include/rewrite/rewriteSupport.h
  - src/include/catalog/pg_rewrite.h
symbols:
  - SetRelationRuleStatus
  - RemoveRewriteRuleById
  - IsDefinedRewriteRule
  - get_rewrite_oid
  - ViewSelectRuleName
---

The rule system relies on two layers of metadata: the `pg_rewrite` system catalog that persists rule definitions, and the `relhasrules` flag in `pg_class` that tells the rewriter at a glance whether a relation has any rules to consult. The support and removal routines maintain the consistency between these two layers. They also ensure that every other backend learns about changes through the shared-invalidation mechanism.

## The `pg_rewrite` catalog and its indexes

Every rewrite rule — including the hidden ON SELECT rule that backs a view — occupies one row in `pg_rewrite` (OID 2618, defined in `src/include/catalog/pg_rewrite.h`). PostgreSQL keeps the catalog small. The fixed-width fields (`ev_class`, `ev_type`, `ev_enabled`, `is_instead`) live in the heap tuple. The two variable-length `pg_node_tree` columns (`ev_qual` and `ev_action`) are eligible for [[subsystems/storage/toast|TOAST]] compression, because rule bodies can be large.

Two unique indexes enforce the catalog's key structure:

| Index | Columns | Purpose |
|---|---|---|
| `pg_rewrite_oid_index` (OID 2692) | `oid` | Primary key lookup by rule OID |
| `pg_rewrite_rel_rulename_index` (OID 2693) | `ev_class, rulename` | Uniqueness within a relation; also the syscache key |

Rule names are unique only per relation, not globally. The `RULERELNAME` syscache entry — keyed on `(ev_class, rulename)` — is the natural lookup for existence checks and OID resolution (`IsDefinedRewriteRule()` and `get_rewrite_oid()`, `rewriteSupport.c`).

## The `ViewSelectRuleName` convention

The macro `ViewSelectRuleName` is defined as the string `"_RETURN"` (`rewriteSupport.h:18`). Every view's ON SELECT INSTEAD rule must carry this name. PostgreSQL enforces the naming convention when a view is created. The view-expansion logic in the [[subsystems/rewriter/overview|rewriter]] relies on it: when `fireRIRrules()` finds an RTE pointing at a view, it fetches the cached rule with this fixed name rather than iterating all rules on the relation. Nothing in `rewriteSupport.c` enforces the name itself — it is simply the shared constant that both `DefineView()` and the rule-firing code use to stay in agreement.

## Tracking whether a relation has rules

The boolean `pg_class.relhasrules` is a fast-path signal. Before the rewriter does any work, it checks this flag through the relcache. If it is false, the rewriter attempts no rule lookup. `SetRelationRuleStatus()` (`rewriteSupport.c`) is the single function responsible for keeping this flag correct after a rule is created or dropped.

The function opens `pg_class` under `RowExclusiveLock`, fetches the tuple, and updates it only when the flag value actually needs to change. The critical detail is what happens when no change is needed: even when `relhasrules` already has the desired value, the function still calls `CacheInvalidateRelcacheByTuple()` unconditionally:

```c
/* rewriteSupport.c:79 */
else
{
    /* no need to change tuple, but force relcache rebuild anyway */
    CacheInvalidateRelcacheByTuple(tuple);
}
```

This is deliberate. The callers of `SetRelationRuleStatus()` invoke it after any DDL that modifies the rule set. A relcache flush must propagate to all backends, regardless of whether the flag byte itself changed. Without this forced invalidation, a backend that already cached the relation's `RuleLock` would keep using the stale rule set.

## Removing a rule

`RemoveRewriteRuleById()` (`rewriteRemove.c`) is the low-level deletion routine. The dependency manager invokes it when a rule is dropped — either because `DROP RULE` was issued directly, or because a view (and its hidden `_RETURN` rule) is being dropped as a dependent of another object.

The function takes the following steps:

1. Opens `pg_rewrite` under `RowExclusiveLock` and locates the row by OID using `pg_rewrite_oid_index`.
2. Reads `ev_class` from the tuple to identify the owning relation, then acquires `AccessExclusiveLock` on that relation. The comment explicitly notes that a weaker lock would suffice for non-SELECT rules, but the function always takes `AccessExclusiveLock` to guarantee that no query is mid-execution against a relation whose rule set is about to change.
3. Rejects deletion if the owning relation is a system catalog and `allowSystemTableMods` is false.
4. Deletes the `pg_rewrite` tuple via `CatalogTupleDelete()`.
5. Calls `CacheInvalidateRelcache()` on the event relation so every backend rebuilds its `RuleLock` for that table.
6. Releases the `pg_rewrite` lock but retains the `AccessExclusiveLock` on the event relation until the transaction commits (`table_close(event_relation, NoLock)`).

The lock-retention pattern is standard PostgreSQL DDL practice: holding the lock until commit ensures that no other transaction can see the relation in a partially-updated state between the catalog delete and the transaction's visibility boundary.

## Shared-invalidation as the consistency mechanism

Both `SetRelationRuleStatus()` and `RemoveRewriteRuleById()` ultimately rely on the shared-invalidation (SI) subsystem to propagate changes. Neither function directly modifies any other backend's memory. Instead, they emit an SI invalidation message. Every backend, including the one that issued the DDL, processes this message at the next safe opportunity, discarding its cached `RuleLock` and `pg_class` tuple for the affected relation.

This design means the two catalog files contain no locking of their own beyond what is needed to safely update the catalog tuples. The SI infrastructure handles the multi-backend coherence problem uniformly, the same way it handles any other catalog change.

## Existence checks and OID resolution

`IsDefinedRewriteRule()` (`rewriteSupport.c`) performs a `SearchSysCacheExists2` against the `RULERELNAME` cache. PostgreSQL uses it before creating a rule to detect a name collision, and before replacing one (the `OR REPLACE` path in `DefineQueryRewrite()`).

`get_rewrite_oid()` (`rewriteSupport.c`) resolves a `(relation OID, rule name)` pair to the rule's OID. The `missing_ok` flag controls error vs. `InvalidOid` on a miss, following the standard pattern used throughout PostgreSQL catalog lookup functions. The returned OID is what the dependency manager and `DROP RULE` processing pass to `RemoveRewriteRuleById()`.

## Related Topics

- [[subsystems/rewriter/overview|Query Rewriter Overview]] — how the rewriter uses the rule catalog at query time
- [[subsystems/rewriter/updatable-views|Updatable Views]] — the DDL path that calls `SetRelationRuleStatus()` and registers the `_RETURN` rule
- [[subsystems/rewriter/rules-vs-triggers|Rules vs Triggers]] — when rule-based rewriting is the right tool vs. trigger-based logic

---
title: "DROP Commands"
aliases:
  - DROP processing
  - dropcmds.c
  - RemoveObjects
  - performMultipleDeletions
  - DROP CASCADE internals
source_files:
  - src/backend/commands/dropcmds.c
  - src/backend/catalog/dependency.c
  - src/backend/catalog/objectaddress.c
  - src/include/catalog/dependency.h
  - src/include/catalog/objectaddress.h
symbols:
  - RemoveObjects
  - performDeletion
  - performMultipleDeletions
  - findDependentObjects
  - reportDependentObjects
  - deleteObjectsInList
  - deleteOneObject
  - doDeletion
  - AcquireDeletionLock
  - ObjectAddresses
  - ObjectAddressExtra
  - PERFORM_DELETION_INTERNAL
  - PERFORM_DELETION_CONCURRENTLY
  - PERFORM_DELETION_QUIETLY
  - does_not_exist_skipping
  - get_object_address
  - check_object_ownership
  - IsPinnedObject
---

Most PostgreSQL `DROP` commands share a single dispatch function in `src/backend/commands/dropcmds.c`. Whether the user writes `DROP FUNCTION`, `DROP TYPE`, `DROP OPERATOR CLASS`, or `DROP SCHEMA`, the same `RemoveObjects()` entry point resolves names, checks privileges, and hands the identified objects to the dependency engine in `src/backend/catalog/dependency.c`. The dependency engine then determines the full closure of objects that must be deleted, enforces RESTRICT or CASCADE semantics, and performs the physical catalog removals in safe order. Understanding this two-layer design — dispatch plus dependency traversal — explains why DROP behaves consistently across dozens of object types. It also explains why DROP's failure modes look the way they do.

## What RemoveObjects Handles and What It Doesn't

`RemoveObjects()` is not a handler for every `DROP` statement. Relations, indexes, sequences, views, materialized views, roles, databases, tablespaces, and subscriptions all have their own entry points because they require specialized pre-deletion logic (storage teardown for relations, role revocation, concurrent index semantics, and so on). `RemoveObjects()` covers everything else: functions, procedures, aggregates, operators, casts, types, schemas, languages, text search configurations, foreign data wrappers, foreign servers, event triggers, rules, triggers, policies, statistics objects, transforms, and more.

The split is not arbitrary. Objects handled by `RemoveObjects()` share a common pattern: their deletion consists primarily of removing catalog rows, cleaning up `pg_depend` entries, and processing cascades. Relations and roles require considerably more: physical file removal, cleanup of the lock table, or cluster-wide effects that a generic catalog sweep cannot safely handle.

## Name Resolution and Locking

The first thing `RemoveObjects()` does is allocate an `ObjectAddresses` list. It then iterates over every name in the `DropStmt.objects` list. For each name it calls `get_object_address()` (`src/backend/catalog/objectaddress.c`), passing `AccessExclusiveLock` as the required lock mode. That function translates the parsed name into an `ObjectAddress` triple of `(classId, objectId, objectSubId)`. The name may be schema-qualified, may include lists of argument types for functions and operators, or may name a sub-object like a trigger. See [[subsystems/catalog/object-addressing]] for how name resolution handles retry loops and invalidation races.

`RemoveObjects()` acquires `AccessExclusiveLock` on every object before adding it to the deletion list. This blocks all concurrent access for the duration of the transaction. It prevents new dependencies from forming on an object that is about to disappear. The dependency engine also holds a lock on the open `pg_depend` relation. It acquires `RowExclusiveLock` once, when it opens the relation. It keeps the lock open through the entire traversal to avoid repeated open/close overhead.

## Privilege Checks

After resolving a name, `RemoveObjects()` calls `check_object_ownership()` to verify that the current role owns the object or has superuser privileges. `check_object_ownership()` checks ownership against the object's own entry in its catalog (e.g., `pg_proc.proowner` for functions, `pg_type.typowner` for types). For sub-objects like triggers and rules, it checks ownership against the owning relation in `pg_class` instead, because sub-objects have no independent owner field.

Namespace membership is a secondary check: if the object is in a temporary schema (`isTempNamespace()`), `RemoveObjects()` updates `MyXactFlags` to record that the current transaction has accessed a temp namespace. This flag ensures cleanup happens if the transaction aborts.

## The Batch-Then-Delete Pattern

`RemoveObjects()` does not process each object independently. It collects all `ObjectAddress` values into the list first, then calls `performMultipleDeletions()` once with the entire list. The comment in the source explains the reason: "This avoids unnecessary DROP RESTRICT errors if there are dependencies between them."

Consider `DROP FUNCTION f, AGGREGATE a` where aggregate `a` internally depends on function `f`. If `f` were processed first in isolation, `findDependentObjects()` would encounter `a` as a dependent of `f`. It would then refuse to drop `f` under RESTRICT, because `findDependentObjects()` had not yet identified `a` as a co-deletion target. By passing both objects as a single batch, the engine sees that `a` is in the `pendingObjects` list when it processes `f`. It then recognizes that the INTERNAL dependency is already satisfied and proceeds without error. The batch acts as a declaration of intent that suppresses spurious RESTRICT violations among the listed objects themselves.

This design also means the engine deletes the entire set in a single transaction with a single set of catalog locks — there is no window where some objects are deleted and others are not.

## IF EXISTS and Missing Object Handling

When `stmt->missing_ok` is true and `get_object_address()` returns an invalid OID, `RemoveObjects()` calls the static helper `does_not_exist_skipping()` to emit a NOTICE and skip the object instead of raising an error.

The NOTICE message is more specific than a simple "[type] does not exist". Three internal helpers check whether the problem is the schema rather than the object:

- `schema_does_not_exist_skipping()` calls `LookupNamespaceNoError()` on the schema portion of the qualified name. If the schema itself is missing, the message says "schema X does not exist, skipping" rather than "function X does not exist, skipping".
- `owningrel_does_not_exist_skipping()` handles triggers, rules, and policies. These are always qualified as `relation.name`. If the relation (or its schema) is missing, the message reflects that.
- `type_in_list_does_not_exist_skipping()` handles functions, aggregates, operators, casts, and transforms that include type names as arguments. If a type listed in the argument signature is unknown, the message names that type rather than the target function.

This layered checking produces diagnostically useful NOTICE messages for complex qualified names. It avoids confusion when a nested name component is the actual missing piece.

## Function vs. Aggregate Distinction

`DROP FUNCTION` in `RemoveObjects()` includes a guard against accidentally dropping aggregates. After resolving the function OID, `RemoveObjects()` calls `get_func_prokind()`. If it returns `PROKIND_AGGREGATE`, the command fails with an error and a hint to use `DROP AGGREGATE` instead. Internally, aggregates share rows in `pg_proc` with ordinary functions, so the prohibition is a deliberate historical convention rather than a structural necessity. The same check does not run in the reverse direction: `DROP AGGREGATE` does not need to reject procedures or plain functions because the parser routes those to different codes for each object type.

## How the Dependency Engine Deletes Objects

After `RemoveObjects()` builds its address list, control passes to `performMultipleDeletions()` in `dependency.c`. [[subsystems/catalog/pg-depend]] documents the full mechanics of dependency traversal. That article explains how `findDependentObjects()` walks `pg_depend` in both directions, how INTERNAL dependencies redirect drops to owning objects, how the engine achieves deterministic CASCADE ordering by sorting dependents before recursion, and how it handles TOCTOU races with `systable_recheck_tuple()`.

From the DROP command perspective, two behavioral outcomes matter:

**RESTRICT mode**: any object reachable via a NORMAL dependency from the target causes an error listing what would need to be dropped. The dependency engine silently includes objects reachable only via AUTO, INTERNAL, PARTITION, or EXTENSION dependencies in the deletion; it considers them implementation details of the target, not independent dependents. This is why `DROP TABLE` removes indexes and statistics automatically, but fails when a view references the table.

**CASCADE mode**: the dependency engine adds all reachable objects to the deletion set. It emits a NOTICE for each object reached via a NORMAL dependency, so the user knows what was dropped. PostgreSQL caps the NOTICE output at 100 entries on the client; the full list always goes to the server log.

## Object-Type-Specific Deletion Dispatch

Once the engine builds the full deletion set and satisfies the behavior mode, `deleteObjectsInList()` iterates the list in topological order (dependents before their dependencies) and calls `deleteOneObject()` for each entry. That function invokes the `OAT_DROP` object access hook (see [[subsystems/event-triggers]] for how event triggers integrate here). It then calls `doDeletion()`. `doDeletion()` dispatches by `ObjectClass` to a type-specific removal function.

The dispatch covers the full range of catalog types:

| Object class | Removal action |
|---|---|
| Relations (`OCLASS_CLASS`) | `heap_drop_with_catalog()` for tables; `index_drop()` for indexes; `RemoveAttributeById()` for column sub-objects |
| Functions / procedures | `RemoveFunctionById()` |
| Types | `RemoveTypeById()` |
| Constraints | `RemoveConstraintById()` |
| Column defaults | `RemoveAttrDefaultById()` |
| Operators, operator classes/families | `RemoveOperatorById()`, `DropObjectById()` |
| Triggers, rules, policies | `RemoveTriggerById()`, `RemoveRewriteRuleById()`, `RemovePolicyById()` |
| Extensions | `RemoveExtensionById()` |
| Text search objects, FDWs, foreign servers, event triggers, transforms, etc. | `DropObjectById()` (generic tuple deletion via syscache or OID index scan) |

After the type-specific removal, `deleteOneObject()` deletes the object's outgoing `pg_depend` rows, cleans `pg_shdepend`, and removes comments and security labels. It then calls `CommandCounterIncrement()`. This ensures the next deletion in the list sees a catalog snapshot that reflects the current deletion. This increment is what makes chained cascades work correctly — each step sees the catalog state left by all previous steps.

`doDeletion()` explicitly excludes global objects — roles, databases, tablespaces, subscriptions. Attempting to reach them through the dependency engine raises an immediate error. They have their own dedicated drop paths (`DROP ROLE` calls `DropRole()` directly) because their effects are cluster-wide and cannot be cleanly handled inside a per-database transaction.

## Pinned Objects

Certain built-in catalog objects cannot be dropped under any circumstances. `IsPinnedObject()` in `src/backend/catalog/catalog.c` identifies these by OID: any object with an OID below `FirstUnpinnedObjectId` is pinned, with a small number of exceptions (the `public` schema, databases, and large objects). Pinned objects include built-in types, operators, access methods, and the tables of the system catalog themselves.

Pinned objects have no `pg_depend` rows recording their dependents — the OID check short-circuits the traversal before the engine attempts any scan. When `findDependentObjects()` encounters a pinned object as the target, it raises an immediate error: "cannot drop [object] because it is required by the database system." No amount of CASCADE or superuser privilege overrides this check.

## Concurrent Drop and the Lock Modes

Most DROP operations use `AccessExclusiveLock` on the target object. This lock blocks all other access for the transaction. The exception is `DROP INDEX CONCURRENTLY`; the engine signals this case with the `PERFORM_DELETION_CONCURRENTLY` flag. In that case, `AcquireDeletionLock()` uses `ShareUpdateExclusiveLock` instead of `AccessExclusiveLock` on the relation. `deleteOneObject()` also closes and reopens the `pg_depend` relation around the `doDeletion()` call. This roundtrip is necessary because concurrent index drop internally commits the current transaction. It is the only case in the dependency engine where a sub-operation commits mid-way through the deletion list.

The `PERFORM_DELETION_CONCURRENT_LOCK` flag covers a related scenario used by `REINDEX CONCURRENTLY`: it uses the same lock mode as concurrent index drop but does not commit mid-transaction.

## The PERFORM_DELETION_* Flags

`performDeletion()` and `performMultipleDeletions()` accept a bitmask that controls engine behavior:

| Flag | Effect |
|---|---|
| `PERFORM_DELETION_INTERNAL` | Suppresses event trigger notifications and relaxes some permission checks; used for cascade deletions triggered internally rather than by a user command |
| `PERFORM_DELETION_CONCURRENTLY` | Concurrent drop semantics (indexes only); uses lighter lock and commits mid-transaction |
| `PERFORM_DELETION_QUIETLY` | Reduces NOTICE messages to DEBUG2 level; used for temp schema cleanup on session exit |
| `PERFORM_DELETION_SKIP_ORIGINAL` | Deletes dependents but not the original target objects themselves; used when the caller will handle the originals separately |
| `PERFORM_DELETION_SKIP_EXTENSIONS` | Does not delete extension objects; used when cleaning up temp objects to avoid accidentally uninstalling extensions |
| `PERFORM_DELETION_CONCURRENT_LOCK` | Uses concurrent lock mode without committing mid-transaction; used by `REINDEX CONCURRENTLY` |

These flags propagate through the entire recursive call chain, so a cascade triggered by an internal operation carries the `PERFORM_DELETION_INTERNAL` flag all the way down to `deleteOneObject()`.

## Related Topics

- [[subsystems/catalog/pg-depend]]
- [[subsystems/catalog/object-addressing]]
- [[subsystems/event-triggers]]
- [[code-paths/alter-table]]
- [[subsystems/extensions/overview]]

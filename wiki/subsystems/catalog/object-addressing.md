---
title: "Object Addressing and Access Hooks"
aliases:
  - ObjectAddress
  - object access hooks
  - InvokeObjectPostCreateHook
tags:
  - theme/extensibility
source_files:
  - src/backend/catalog/objectaddress.c
  - src/backend/catalog/objectaccess.c
  - src/include/catalog/objectaddress.h
  - src/include/catalog/objectaccess.h
symbols:
  - ObjectAddress
  - ObjectPropertyType
  - get_object_address
  - check_object_ownership
  - get_object_namespace
  - getObjectDescription
  - getObjectTypeDescription
  - getObjectIdentity
  - getObjectIdentityParts
  - object_access_hook
  - ObjectAccessType
  - RunObjectPostCreateHook
  - RunObjectDropHook
  - RunObjectPostAlterHook
  - RunNamespaceSearchHook
  - RunFunctionExecuteHook
  - InvokeObjectPostCreateHook
  - InvokeObjectDropHook
---

PostgreSQL operates on dozens of distinct catalog object types — tables, functions, types, operators, triggers, publications, and many more. It needs a uniform way to identify any of them, resolve SQL names to runtime identifiers, and notify security or audit extensions of catalog-level events. `objectaddress.c` handles identification and name resolution: it provides the `ObjectAddress` type and the name-resolution machinery. `objectaccess.c` handles notification: it provides a plugin hook called at key lifecycle moments for every object.

## The ObjectAddress Type

An `ObjectAddress` is a three-field struct (`objectaddress.h`):

```c
typedef struct ObjectAddress
{
    Oid   classId;      /* OID of the catalog (a pg_class row OID) */
    Oid   objectId;     /* OID of the object row within that catalog */
    int32 objectSubId;  /* sub-object: column attnum, or 0 for whole objects */
} ObjectAddress;
```

The `classId` is the OID of the system catalog that owns the object — for example, `ProcedureRelationId` (1255) for a function in `pg_proc`, or `RelationRelationId` (1259) for a relation in `pg_class`. Together, `(classId, objectId)` uniquely identifies any database object. The `objectSubId` adds one level of granularity for objects that live inside another: table columns use the `attnum` as the sub-ID. Column defaults (`pg_attrdef`) reference the column's attnum on the referenced side. All other objects use `objectSubId = 0`.

The convenience macros `ObjectAddressSet` and `ObjectAddressSubSet` fill the struct fields in a single statement. The sentinel `InvalidObjectAddress` has all fields set to `InvalidOid`/`0`. `get_object_address()` returns it when `missing_ok = true` and the object does not exist.

This compact representation is used everywhere the dependency system, the DROP machinery, and event triggers need to refer to catalog objects. The `pg_depend` and `pg_shdepend` tables are built entirely around the same `(classid, objid, objsubid)` triple, so `ObjectAddress` and those catalogs speak exactly the same language — see [[subsystems/catalog/pg-depend|pg_depend and dependency tracking]].

## The ObjectProperty Table

`objectaddress.c` maintains a static array named `ObjectProperty[]` with one entry per addressable object class. Each `ObjectPropertyType` row records the catalog OID, the OID of its primary-key index, the syscache IDs for OID-based and name-based lookups, the attribute numbers for the OID, name, namespace, owner, and ACL columns, and a flag indicating whether `(namespace, name)` is sufficient for a unique lookup.

This table drives a family of generic introspection functions (`get_object_oid_index()`, `get_object_catcache_oid()`, `get_object_attnum_owner()`, and so on) that let the rest of the codebase retrieve catalog metadata about any object type without a long `switch` statement. The dependency code uses these helpers to look up ownership and namespace without knowing the specific catalog schema.

Not every catalog column is populated for every object class. Access methods and roles have no namespace column (`attnum_namespace = InvalidAttrNumber`). Objects like operator family members (`pg_amop`, `pg_amproc`) have no name or owner column. The infrastructure handles `InvalidAttrNumber` gracefully rather than requiring callers to special-case these classes.

## Resolving Names to Addresses

DDL commands receive object names from the parser as `ObjectType`/`Node *` pairs. `get_object_address()` (`objectaddress.c`) translates a parsed name into a locked `ObjectAddress`. The function signature is:

```c
ObjectAddress get_object_address(ObjectType objtype, Node *object,
                                 Relation *relp, LOCKMODE lockmode,
                                 bool missing_ok);
```

The `lockmode` is applied to the identified object before the function returns. For relation-typed objects (`OBJECT_TABLE`, `OBJECT_INDEX`, etc.), `get_object_address()` acquires the lock by opening the relation via `relation_openrv_extended()`. That call handles the lock internally and also returns the open `Relation` in `*relp`. For all other object types, `get_object_address()` calls either `LockDatabaseObject()` or `LockSharedObject()` depending on whether the object lives in a per-database or cluster-wide catalog.

The function contains a retry loop to handle a race: the name lookup and the lock acquisition are two separate steps, so a concurrent DDL operation could rename or drop the object between them. After locking, the code checks whether any shared invalidation messages arrived since it began (`SharedInvalidMessageCounter`). If the counter advanced and no relation was opened, the function re-executes the lookup to verify the OID is still the same. Opening a relation would already have serialized the DDL, so the check only applies when none was opened. If the OID changed, it releases the first lock and re-locks the new OID. This loop converges quickly in practice.

`get_object_address_relobject()` handles sub-object resolution for triggers, rules, policies, and constraints. It opens the parent relation with `AccessShareLock` — just enough to freeze out concurrent DDL on the parent — then looks up the sub-object OID by name. Attributes and column defaults follow a similar pattern, but with their own helpers that also validate the attnum against the relation's tuple descriptor.

## Describing Objects for Messages and Events

Three functions produce human-readable strings from an `ObjectAddress`:

- `getObjectDescription()` returns a phrase like `"table public.orders"` or `"function myfunc(integer)"` suitable for error messages.
- `getObjectTypeDescription()` returns just the type label, e.g. `"table"` or `"function"`.
- `getObjectIdentityParts()` (also available as `getObjectIdentity()`) returns a fully-qualified identifier suitable for logging and for round-tripping back into `get_object_address()`. It also populates `objname` and `objargs` output lists that can reconstruct the address without re-parsing.

`pg_identify_object()` and `pg_identify_object_as_address()` use these functions. Both are SQL-callable wrappers that expose object information to event trigger functions and replication tooling. The `ObjectTypeMap[]` static array provides the reverse mapping from the string type labels back to `ObjectType` enum values via `read_objtype_from_string()`. Event trigger decoding uses this function.

The object description functions navigate every catalog variant through a large `switch` on `getObjectClass()`, which maps `classId` to an `ObjectClass` enum. This design keeps the per-type knowledge in one place rather than scattered across callers.

## Ownership Checking

`check_object_ownership()` verifies that a given role owns the addressed object. `ALTER`, `DROP`, and `COMMENT` commands call it after `get_object_address()` has identified and locked the target. The function again uses a `switch` on `ObjectType` because the check varies:

- For relation-owned sub-objects (columns, triggers, rules, policies, table constraints), the check is against the relation's owner in `pg_class`, not the sub-object itself.
- For types, domains, and type attributes, the check is against the type's owner in `pg_type`.
- For domain constraints, ownership falls back to the owning domain type.
- For large objects, setting `lo_compat_privileges = on` bypasses the check.

This mirrors the dependency model: sub-objects are legally owned by whoever owns their parent, so ownership checks ascend to the parent rather than expecting a separate owner entry.

## Object Access Hooks

`objectaccess.c` provides a global hook point for security and audit extensions. The hook is a function pointer:

```c
extern PGDLLIMPORT object_access_hook_type object_access_hook;
```

where the hook type is:

```c
typedef void (*object_access_hook_type)(ObjectAccessType access,
                                        Oid classId,
                                        Oid objectId,
                                        int subId,
                                        void *arg);
```

A second hook, `object_access_hook_str`, has the same shape but passes the object name as a `const char *` instead of an OID, for use in cases where the object name is available but an OID has not yet been assigned.

### Access Event Types

| Event | Constant | Fired when |
|---|---|---|
| `OAT_POST_CREATE` | `OAT_POST_CREATE` | After object is created and catalog rows are committed. The command counter may not yet be incremented; use `SnapshotSelf` to see the new row. |
| `OAT_DROP` | `OAT_DROP` | Just before object deletion inside `deleteOneObject()`. |
| `OAT_POST_ALTER` | `OAT_POST_ALTER` | Just after object is altered, before command counter increment. The old version is visible via MVCC; `SnapshotSelf` shows the new version. |
| `OAT_NAMESPACE_SEARCH` | `OAT_NAMESPACE_SEARCH` | Before name lookup in a schema; equivalent to `USAGE` permission check. The hook can deny access by setting `result = false` in its argument struct. |
| `OAT_FUNCTION_EXECUTE` | `OAT_FUNCTION_EXECUTE` | Before function execution; equivalent to `EXECUTE` permission check. |
| `OAT_TRUNCATE` | `OAT_TRUNCATE` | Just before a relation is truncated; equivalent to `TRUNCATE` permission check. |

Each event type has a corresponding argument struct (`ObjectAccessPostCreate`, `ObjectAccessDrop`, `ObjectAccessPostAlter`, `ObjectAccessNamespaceSearch`) that carries additional context. For `OAT_DROP`, the `dropflags` field mirrors the `PERFORM_DELETION_*` flags from `dependency.h`, letting the hook distinguish user-initiated drops from internal cascades. For `OAT_POST_ALTER`, `auxiliary_id` is used when the catalog row is identified by two OIDs (for `pg_inherits`, `pg_db_role_setting`, or `pg_user_mapping`).

### Hook Invocation Pattern

Core code never calls the hook functions directly. Instead it uses macros defined in `objectaccess.h`:

```c
InvokeObjectPostCreateHook(classId, objectId, subId)
InvokeObjectDropHookArg(classId, objectId, subId, dropflags)
InvokeObjectPostAlterHook(classId, objectId, subId)
InvokeNamespaceSearchHook(objectId, ereport_on_violation)
InvokeFunctionExecuteHook(objectId)
InvokeObjectTruncateHook(objectId)
```

Each macro checks `object_access_hook != NULL` before calling through. This zero-cost check means the hook machinery adds no overhead when no extension has registered a hook. That is the common case in production.

The `OAT_NAMESPACE_SEARCH` macro has a slightly different form: it returns `true` when no hook is installed, preserving the default-allow semantics. It calls the hook only when one is registered:

```c
#define InvokeNamespaceSearchHook(objectId, ereport_on_violation)   \
    (!object_access_hook                                            \
     ? true                                                         \
     : RunNamespaceSearchHook((objectId), (ereport_on_violation)))
```

A hook implementation that wants to deny namespace access sets `ns_arg.result = false` and must never set it to `true`. This ensures that when multiple hooks are chained through successive assignments to `object_access_hook`, access is granted only if all hooks agree.

### Use by Extensions and Security Labels

The access hook is the foundation for extensions that need to enforce custom authorization policies or produce audit logs. `sepgsep` (SE-PostgreSQL) and `pg_audit` are the canonical examples. An extension installs its hook by saving the previous value of `object_access_hook` and restoring it during unload. This forms a chain if multiple hooks are active. PostgreSQL itself does not chain hooks; it is the extension's responsibility to call the previous hook in the chain.

The `OAT_POST_CREATE` event with `is_internal = true` signals that the object was created as a side effect of a user operation (e.g., a [[subsystems/storage/toast|TOAST]] table created for a user table, or an index created due to a type change). Extensions can use this flag to suppress audit records for internal catalog maintenance that the user did not directly request.

## Related Topics

- [[subsystems/catalog/pg-depend|pg_depend and dependency tracking]]
- [[subsystems/catalog/core-catalogs|Core System Catalogs]]

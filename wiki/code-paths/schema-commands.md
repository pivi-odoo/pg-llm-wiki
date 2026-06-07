---
title: "Schema DDL Commands"
aliases:
  - "CREATE SCHEMA"
  - "ALTER SCHEMA"
  - "schemacmds.c"
  - "NamespaceCreate"
  - "schema namespace"
source_files:
  - src/backend/commands/schemacmds.c
  - src/include/commands/schemacmds.h
  - src/include/catalog/pg_namespace.h
  - src/include/nodes/parsenodes.h
  - src/backend/parser/parse_utilcmd.c
symbols:
  - CreateSchemaCommand
  - RenameSchema
  - AlterSchemaOwner
  - AlterSchemaOwner_oid
  - AlterSchemaOwner_internal
  - NamespaceCreate
  - CreateSchemaStmt
  - CreateSchemaStmtContext
  - FormData_pg_namespace
---

A schema in PostgreSQL is a named namespace within a single database. It groups objects — tables, views, sequences, functions, types — under a common prefix, so that `sales.orders` and `inventory.orders` can coexist without conflict. Schemas are distinct from roles. A schema has an owner role and an ACL. The role itself has no corresponding schema unless one is explicitly created. The implementation lives in `src/backend/commands/schemacmds.c`, with the catalog record in `pg_namespace` (relation OID 2615).

## The pg_namespace Catalog

Every schema maps to a row in `pg_namespace`. The catalog has three meaningful columns: `nspname` (the schema name, unique across the database), `nspowner` (OID of the owning role, foreign-keyed to `pg_authid`), and `nspacl` (a nullable `aclitem[]` encoding per-grantee privileges). Two unique indexes enforce uniqueness by name and by OID. When `nspacl` is null, `acldefault()` determines the default access for the namespace type.

All schema DDL reads or writes `pg_namespace` with `RowExclusiveLock`. There are no weaker lock modes: even `ALTER SCHEMA RENAME` takes a row-exclusive lock because it modifies the index-covered `nspname` column in place.

## Creating a Schema

`CREATE SCHEMA` is the entry point for `CreateSchemaCommand()`. The command has three optional components: a name, an owner (`AUTHORIZATION role`), and a body containing object definitions. Any of these can be absent.

### Ownership and naming defaults

If the command specifies no owner, the current user owns the schema. If the command specifies no name, the owner's role name becomes the schema name. This is the mechanism behind the `"$user"` convention in the default [[subsystems/catalog/schema-search-path|search_path]]. For example, running `CREATE SCHEMA AUTHORIZATION alice` without a name creates a schema called `alice`. `CreateSchemaCommand` looks up the name from `pg_authid` at creation time via `SearchSysCache1(AUTHOID, ...)`.

### Privilege model

`CREATE SCHEMA` requires `CREATE` privilege on the *database*, not on any existing schema. This is intentional and unlike most other CREATE commands. Schemas are top-level namespace containers, so the gating privilege lives at the database level. The privilege check uses `object_aclcheck(DatabaseRelationId, MyDatabaseId, saved_uid, ACL_CREATE)` against the *current* user, not the target owner.

A second check, `check_can_set_role(saved_uid, owner_uid)`, ensures the current user can "become" the desired owner role. This prevents privilege escalation via ownership giveaway — you cannot create a schema and donate it to a superuser role you do not belong to.

`IsReservedName()` rejects names starting with `pg_` unless the server was started with `allowSystemTableMods`, an internal testing flag. This applies to both `CREATE SCHEMA` and `ALTER SCHEMA RENAME`.

### Identity switch and search_path scoping

When the schema owner differs from the current user, `CreateSchemaCommand` calls `SetUserIdAndSecContext(owner_uid, save_sec_context | SECURITY_LOCAL_USERID_CHANGE)` before executing any sub-commands. This ensures every object created in the schema body is owned by the intended owner rather than by whoever ran the `CREATE SCHEMA` statement. `CreateSchemaCommand` restores the identity unconditionally at the end of the function.

The function also temporarily prepends the new schema to `search_path` so that unqualified names in the body resolve to the correct schema. `CreateSchemaCommand` does this with `set_config_option("search_path", ...)` using `GUC_ACTION_SAVE` semantics. This pushes the change onto a GUC nest level opened by `NewGUCNestLevel()`. `AtEOXact_GUC(true, save_nestlevel)` automatically rolls back the change at the end of the function, even on error. See [[subsystems/guc]] for the nest level and GUC stack mechanics.

### Sub-command execution order

`CREATE SCHEMA` permits a body of object definitions:

```sql
CREATE SCHEMA sales
  CREATE TABLE orders (id serial, amount numeric)
  CREATE VIEW pending AS SELECT * FROM orders WHERE amount > 0;
```

The parser collects these as a `List` of raw parsetrees in `CreateSchemaStmt.schemaElts`. Before execution, `transformCreateSchemaStmtElements()` (`parse_utilcmd.c`) buckets them by type. It then concatenates them in dependency-safe order: sequences first, then tables, views, indexes, triggers, and finally grants. This ordering guarantees that a view referencing a table in the same `CREATE SCHEMA` statement will find the table already created.

`CreateSchemaCommand` dispatches sub-commands via `ProcessUtility()` with `PROCESS_UTILITY_SUBCOMMAND` — the flag that tells the utility dispatcher these are not top-level statements. Crucially, there is no call to `parse_analyze_*()` or the query rewriter for these sub-commands. The grammar only permits utility statements (not DML) in a schema body, so raw parsetree execution is safe and avoids the overhead of a full analysis pass. `CreateSchemaCommand` calls `CommandCounterIncrement()` after each sub-command so that subsequent ones can see the objects just created within the same transaction.

`CreateSchemaCommand` fires the [[subsystems/event-triggers|event trigger]] for the schema itself (`EventTriggerCollectSimpleCommand()`) before the sub-commands execute. This preserves the invariant that the schema's event fires before any contained object's event.

### IF NOT EXISTS

When `if_not_exists` is set and the schema already exists, `CreateSchemaCommand` emits a NOTICE and returns `InvalidOid` without executing sub-commands. Inside an extension script this has an additional guard: `checkMembershipInCurrentExtension()` verifies that the pre-existing schema is already owned by the current extension. This prevents an extension script from silently co-opting a schema it does not own. Co-opting an unowned schema would be a correctness hazard during `CREATE EXTENSION IF NOT EXISTS` or extension upgrades.

## Renaming a Schema

`ALTER SCHEMA name RENAME TO newname` is handled by `RenameSchema()`. The old schema row is fetched with `SearchSysCacheCopy1(NAMESPACENAME, ...)` under `RowExclusiveLock`. After checking that the new name is not already taken, that the current user owns the schema, and that the current user has `CREATE` on the database, the `nspname` field is updated in place via `namestrcpy()` and `CatalogTupleUpdate()`. The name uniqueness index is updated automatically as part of the catalog update.

The reserved-name check (`IsReservedName`) applies to the *new* name, not the old one. You cannot rename an existing schema to `pg_anything` even if you own it.

## Changing Schema Ownership

`ALTER SCHEMA name OWNER TO newowner` is handled by `AlterSchemaOwner()` (lookup by name) and `AlterSchemaOwner_oid()` (lookup by OID). Both delegate to the static `AlterSchemaOwner_internal()`. The OID variant is called by internal paths such as `DROP OWNED` and `REASSIGN OWNED`, which operate on sets of objects without having names readily available.

The internal implementation has a deliberate no-op path: if the new owner is the same as the current owner, the function returns immediately without touching the catalog. This idempotency is important for dump/restore. `pg_dump` emits `ALTER SCHEMA OWNER TO` unconditionally. Replaying a dump should not fail or produce spurious catalog churn when the owner is already correct.

When the owner changes, `AlterSchemaOwner_internal` updates two catalog fields together. `aclnewowner()` patches `nspacl` to remap any ACL entries that referenced the old owner to the new owner. This transfers old-owner privileges rather than losing them. `AlterSchemaOwner_internal` then updates `nspowner` to the new owner OID. Finally, `changeDependencyOnOwner()` updates the ownership dependency record in [[subsystems/catalog/pg-depend|pg_depend]].

The privilege model mirrors `CREATE SCHEMA`: the check is `CREATE` on the database by the *current user*, plus `check_can_set_role(GetUserId(), newOwnerId)`. The new owner is not consulted.

## DROP SCHEMA

`DROP SCHEMA` is not in `schemacmds.c`; it goes through the generic object-drop machinery in `dependency.c`. The critical distinction is between `RESTRICT` (the default) and `CASCADE`.

With `RESTRICT`, the drop fails if any object depends on the schema — if even one table exists inside it. `performDeletion()` enforces this by scanning `pg_depend` for dependents of the namespace object. With `CASCADE`, `performDeletion()` traverses the dependency graph. It drops all contained objects recursively in reverse-dependency order. Because schemas are namespace containers, practically every object inside the schema has a normal dependency (`DEPENDENCY_NORMAL`) on the schema row. `CASCADE` therefore drops all tables, views, sequences, functions, and types in the schema before removing the `pg_namespace` row itself.

PostgreSQL permits dropping the `public` schema. Dropping it removes the namespace entirely, including the ACL that governs it. Recreating it requires `CREATE SCHEMA public` followed by appropriate privilege grants.

## Schema Security and the public Schema

Prior to PostgreSQL 15, `initdb` created the `public` schema and granted `CREATE` privilege to `PUBLIC` (all roles). This meant any authenticated user could create objects — including functions and operators — in the `public` schema. That made the schema a vector for trojan-horse attacks against superusers who later run queries there. Starting in PostgreSQL 15, `initdb` revokes `CREATE` on `public` from `PUBLIC` by default. The `public` schema still exists and is still in the default `search_path`. However, object creation requires an explicit grant.

Schemas serve as a trust boundary in multi-tenant deployments. The schema's `USAGE` privilege protects objects inside a schema. Without `USAGE`, a role cannot resolve unqualified names to objects inside the schema, even if it has `SELECT` on a specific table. Granting `USAGE` on a schema without granting privileges on individual tables does nothing useful. Conversely, granting table privileges without `USAGE` on the schema means the role must use fully qualified names. Transparent access requires both privileges.

## Multi-Tenant Patterns

The canonical multi-tenant pattern puts one tenant's data in one schema: `CREATE SCHEMA tenant_42` with tables `tenant_42.orders`, `tenant_42.customers`. Application connections set `search_path = tenant_42` at session start (or via a role's `ALTER ROLE ... SET search_path`). This lets queries use unqualified names. It also means the query planner sees only the tenant's tables.

The [[subsystems/catalog/schema-search-path]] article covers the resolution mechanics in detail. This includes the `recomputeNamespacePath()` call that turns the `search_path` GUC string into a list of namespace OIDs. It also covers how `$user` expansion and missing-schema skipping work at runtime.

Row-level security ([[subsystems/row-level-security]]) is an alternative that keeps all tenants in a single schema and enforces isolation via policies. This approach has different tradeoffs around schema evolution and query plan sharing.

## Related Topics

- [[subsystems/catalog/schema-search-path]] — how `search_path` resolves unqualified names at runtime
- [[subsystems/catalog/pg-depend]] — dependency tracking and DROP CASCADE internals
- [[subsystems/guc]] — GUC nest levels and `GUC_ACTION_SAVE` semantics
- [[subsystems/event-triggers]] — event trigger collection and ordering during DDL
- [[subsystems/roles-privileges]] — ACL model, `aclnewowner`, privilege checking
- [[subsystems/catalog/ddl-locking]] — lock modes used during schema DDL

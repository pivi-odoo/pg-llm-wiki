---
title: "pg_namespace: The Schema Registry"
aliases:
  - pg_namespace
  - schema catalog
  - namespace catalog
source_files:
  - src/backend/catalog/pg_namespace.c
  - src/include/catalog/pg_namespace.h
  - src/include/catalog/pg_namespace.dat
  - src/backend/catalog/namespace.c
symbols:
  - NamespaceCreate
  - FormData_pg_namespace
  - PG_CATALOG_NAMESPACE
  - PG_TOAST_NAMESPACE
  - PG_PUBLIC_NAMESPACE
  - InitTempTableNamespace
  - RemoveTempRelations
---

`pg_namespace` is the system catalog that serves as PostgreSQL's schema registry. Every named database object — table, view, function, type, operator, and more — belongs to exactly one namespace. A foreign key into `pg_namespace` records that namespace. The catalog is tiny (three user-visible columns plus an OID), but it sits at the root of every name-resolution lookup in the system.

## The catalog structure

`FormData_pg_namespace` (`src/include/catalog/pg_namespace.h`) has four columns:

| Column | Type | Description |
|---|---|---|
| `oid` | `Oid` | Unique identifier, used as a foreign key from every object catalog |
| `nspname` | `name` | The schema name; unique across the database, enforced by `pg_namespace_nspname_index` |
| `nspowner` | `Oid` → `pg_authid` | The role that owns the schema and holds `CREATE` privilege by default |
| `nspacl` | `aclitem[]` | Access control list; `NULL` means the owner has full control, no other grants |

Two unique indexes enforce the uniqueness constraints: one on `oid` (`pg_namespace_oid_index`, OID 2685) and one on `nspname` (`pg_namespace_nspname_index`, OID 2684). The syscache entries `NAMESPACEOID` and `NAMESPACENAME` are backed by these indexes respectively, making point lookups by either key O(1) for the common case.

## How object catalogs reference namespaces

Every namespace-scoped object catalog carries a column that is a foreign key into `pg_namespace`. Name uniqueness within a schema is enforced per-catalog by a unique index on the `(name, namespace)` pair — for example, `pg_class` has a unique index on `(relname, relnamespace)`. This means two tables in different schemas can share the same name. That is the entire point of the schema mechanism.

| Catalog | Namespace column |
|---|---|
| `pg_class` | `relnamespace` |
| `pg_proc` | `pronamespace` |
| `pg_type` | `typnamespace` |
| `pg_operator` | `oprnamespace` |
| `pg_opclass` | `opcnamespace` |
| `pg_opfamily` | `opfnamespace` |
| `pg_collation` | `collnamespace` |
| `pg_conversion` | `connamespace` |

All namespace-qualified object lookups that do not already know the OID start with a syscache hit on `NAMESPACENAME` to resolve the schema name to an OID, then a second syscache hit on the object catalog keyed by `(name, namespaceOid)`.

## Creating a schema: NamespaceCreate

`NamespaceCreate()` (`src/backend/catalog/pg_namespace.c`) is the single low-level function that `CREATE SCHEMA` ultimately calls. It rejects duplicate names via `SearchSysCacheExists1(NAMESPACENAME, ...)`, computes the default ACL from the owner's `ALTER DEFAULT PRIVILEGES` settings, allocates a new OID with `GetNewOidWithIndex`, inserts the tuple with `CatalogTupleInsert`, and records dependency edges on the owner role and any roles mentioned in the ACL.

The `isTemp` parameter is the one meaningful behavioral branch in the function. When it is true:

- The default-ACL computation is skipped entirely (`nspacl` is stored as `NULL`). Temporary schemas are owned by the bootstrap superuser and deliberately have no grants — sessions that need the temp namespace skip the ACL check themselves, while other backends cannot peek at another session's temp tables.
- `recordDependencyOnCurrentExtension` is not called. This prevents a `CREATE TEMP TABLE` executed inside an extension script from registering the transient temp schema as a member of that extension.

Permanent schemas follow neither exemption: they inherit default ACLs. The system also records them in `pg_extension_membership` if created while an extension is being installed.

## Built-in namespaces

Three namespaces are bootstrapped at `initdb` time and exist in every database. Their OIDs and symbols are defined in `src/include/catalog/pg_namespace.dat`:

| OID | Symbol | Name | Purpose |
|---|---|---|---|
| 11 | `PG_CATALOG_NAMESPACE` | `pg_catalog` | All system catalogs and built-in types, functions, and operators |
| 99 | `PG_TOAST_NAMESPACE` | `pg_toast` | [[subsystems/storage/toast|TOAST]] tables and their indexes |
| 2200 | `PG_PUBLIC_NAMESPACE` | `public` | Default schema for user objects; owned by the `pg_database_owner` pseudo-role |

`pg_catalog` is special in name resolution: the resolver always prepends it to the active search path, even when absent from the explicit `search_path` GUC value, unless the user deliberately positions it elsewhere. This ensures built-in operators and functions are always reachable by unqualified name.

`pg_toast` is visible to users but effectively read-only. Its tables are named `pg_toast_<reloid>`. `pg_class.reltoastrelid` links them to their parent table. The `pg_toast_temp_NNN` schemas follow the same naming convention for the TOAST tables belonging to temporary relations.

`public` is not hardwired into name resolution code. It appears in the default `search_path` value (`"$user", public`) purely because the GUC default says so. Removing it from the path makes `public` inaccessible by unqualified name, just like any other schema.

## Temporary schemas

Each backend session that creates at least one temporary table gets its own schema named `pg_temp_NNN`, where `NNN` is the backend's process number (`MyProcNumber`). `InitTempTableNamespace()` (`src/backend/catalog/namespace.c`) creates the schema lazily, on the first `CREATE TEMP TABLE` in the session; it calls `NamespaceCreate(namespaceName, BOOTSTRAP_SUPERUSERID, true)`. If a schema with that name already exists (left over from a crashed predecessor session with the same process number), `InitTempTableNamespace()` first calls `RemoveTempRelations()` to clean out the stale contents before reusing the schema row.

Temporary schemas bypass two mechanisms that apply to regular schemas:

- **ACL checks.** The bootstrap superuser owns the temp schema for a session, with no ACL grants. The name-resolution code in `namespace.c`, however, skips `ACL_USAGE` checks when iterating the session's own `pg_temp_NNN`. Other backends cannot reach that schema by name at all, because the `pg_temp` alias resolves only to the calling session's namespace.
- **Extension membership.** The `isTemp` flag passed to `NamespaceCreate` suppresses `recordDependencyOnCurrentExtension`, so no dependency edge from the temp schema to the currently-installing extension is ever written.

PostgreSQL prohibits parallel workers from creating temporary tables (`InitTempTableNamespace` returns an error if `IsParallelWorker()` is true) and similarly blocks hot-standby sessions.

At session end, an exit callback registered when the temp namespace was first committed calls `RemoveTempRelations()` to drop all objects inside the schema. PostgreSQL reuses the `pg_namespace` row itself rather than deleting it, so the next session that inherits the same process number can reclaim it.

## Relationship to search_path

`pg_namespace` stores the ground truth. [[subsystems/catalog/schema-search-path|search_path]] is a session-level ordered list of schema names. The name resolver converts this list to a list of namespace OIDs and walks it when resolving unqualified identifiers. When `foo` appears in a query without a schema qualifier, the resolver iterates the active OID list and for each entry checks whether an object of the right type named `foo` exists in the corresponding catalog with a matching namespace OID. The `pg_namespace` catalog is the source for that OID — resolution starts here even when the caller only has a string name.

The special alias `pg_temp` in a `search_path` string resolves at runtime to whatever `pg_temp_NNN` OID belongs to the current session. If no temp namespace has been initialized yet, the resolver silently skips the alias (or, if it is the first entry in the path, defers it until a temp object is actually created).

## Multi-tenant schema isolation

The typical multi-tenant pattern — one schema per tenant, a shared application role — maps directly onto `pg_namespace`. Each tenant's schema is a separate row with its own `nspacl`. Granting `USAGE` on the schema allows the tenant role to see objects inside it; `CREATE` on the schema allows adding new objects.

```sql
-- Provision a tenant
CREATE SCHEMA tenant_42 AUTHORIZATION app_role;
GRANT USAGE ON SCHEMA tenant_42 TO tenant_42_role;
GRANT CREATE ON SCHEMA tenant_42 TO tenant_42_role;

-- Confine a session to one tenant
ALTER ROLE tenant_42_role SET search_path = tenant_42;
```

Because the `relnamespace` OID in `pg_class` scopes object names (and the equivalent namespace columns do so in `pg_proc`, `pg_type`, and others), there is no collision risk between tenant schemas even when they contain tables with identical names. The schema boundary is the OID — not the string name — so renaming a schema does not break any in-flight queries that already resolved names to OIDs.

ORM frameworks that manage schema migrations (Flyway, Alembic, Liquibase) rely on this scoping: the migration history table lives in the same namespace as the application tables, isolated from other environments by the OID boundary. Row-level security can then add a second layer of isolation within a shared schema, but schema-level isolation using `pg_namespace` is coarser-grained and requires no changes to application queries.

## See also

- [[subsystems/catalog/schema-search-path]] — search_path resolution algorithm and `recomputeNamespacePath`
- [[subsystems/catalog/core-catalogs]] — overview of `pg_class`, `pg_proc`, `pg_type`, and `pg_attribute`
- [[subsystems/catalog/pg-depend]] — how `DROP SCHEMA CASCADE` propagates via dependency tracking
- [[subsystems/storage/toast]] — the `pg_toast` namespace and out-of-line storage
- [[subsystems/catalog/object-addressing]] — `ObjectAddress` and how namespaced objects are identified globally
- [[subsystems/catalog/syscache]] — the syscache layer that backs `NAMESPACEOID` and `NAMESPACENAME` lookups

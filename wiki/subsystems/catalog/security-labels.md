---
title: "Security Labels"
aliases:
  - security label
  - SECURITY LABEL
  - pg_seclabel
  - pg_shseclabel
  - sepgsql
  - mandatory access control
  - MAC
tags:
  - theme/extensibility
source_files:
  - src/backend/commands/seclabel.c
  - src/include/commands/seclabel.h
  - src/include/catalog/pg_seclabel.h
  - src/include/catalog/pg_shseclabel.h
symbols:
  - ExecSecLabelStmt
  - GetSecurityLabel
  - SetSecurityLabel
  - DeleteSecurityLabel
  - DeleteSharedSecurityLabel
  - register_label_provider
  - check_object_relabel_type
  - LabelProvider
---

Security labels are arbitrary string annotations that extensions attach to database objects — tables, columns, schemas, roles, tablespaces, and more. They are PostgreSQL's extension point for external mandatory access control (MAC) frameworks such as SELinux (via the `sepgsql` contrib module) and AppArmor. Unlike SQL `GRANT` and `REVOKE`, security labels carry no semantics inside the core engine; they are opaque strings, and the registered provider that issued them entirely defines their meaning.

## What a Security Label Is

A security label is a `(provider, label)` pair attached to a database object. The `provider` field is a free-form text name identifying the MAC system — `"selinux"` for `sepgsql`, for example — while `label` is the MAC policy string, typically something like `"system_u:object_r:sepgsql_table_t:s0"`. PostgreSQL stores and retrieves labels on request but enforces nothing about their content. The provider extension handles all validation and access-control enforcement.

This design keeps the core engine decoupled from any particular MAC policy language. Different MAC systems can coexist in the same cluster, each registering under a distinct provider name. A given object can hold labels from multiple providers simultaneously.

The `SECURITY LABEL FOR provider ON object IS 'label'` command stores an annotation; passing `NULL` instead of a label string removes it. The command verifies that the target object is of a supported type (tables, views, columns, schemas, sequences, functions, roles, tablespaces, languages, and a handful of other catalog-level objects are supported; indexes, rules, and operators are not — `SecLabelSupportsObjectType()` in `seclabel.c` is the authoritative list). It also requires that the caller own the target object.

## Catalog Storage

Labels on database-local objects are stored in `pg_seclabel` (`src/include/catalog/pg_seclabel.h`). Each row carries five fields:

| Column | Type | Meaning |
|---|---|---|
| `objoid` | `oid` | OID of the labeled object |
| `classoid` | `oid` | OID of the system catalog that contains the object (e.g. `pg_class` for a table) |
| `objsubid` | `int4` | Column number within the relation, or 0 for the relation itself |
| `provider` | `text` | Name of the MAC provider |
| `label` | `text` | The label string |

The combination `(objoid, classoid, objsubid, provider)` is unique, enforced by the primary-key index `pg_seclabel_object_index`. Together, `(classoid, objoid, objsubid)` is an [[subsystems/catalog/object-addressing|object address]], the same three-field key used throughout the dependency tracking and DDL machinery.

Cluster-wide shared objects — roles (`pg_authid`), databases (`pg_database`), tablespaces (`pg_tablespace`), and similar entries that live in the global catalog rather than a per-database catalog — use a parallel catalog, `pg_shseclabel` (`src/include/catalog/pg_shseclabel.h`). Its schema is identical, except that it omits `objsubid`, since shared objects have no sub-components. It also carries `BKI_SHARED_RELATION`, which makes it visible across all databases in the cluster. `GetSecurityLabel()` and `SetSecurityLabel()` in `seclabel.c` route between the two catalogs based on whether `IsSharedRelation(object->classId)` is true.

When a labeled object is dropped, `DeleteSecurityLabel()` removes all labels for that object from `pg_seclabel`. The shared-object variant `DeleteSharedSecurityLabel()` does the same for `pg_shseclabel`. The standard dependency-drop machinery calls these cleanup functions, so labels are never orphaned.

## Provider Registration

An extension that implements a MAC system registers itself at load time by calling `register_label_provider(provider_name, hook)` (`seclabel.c`). This appends a `LabelProvider` entry — a `(name, callback)` pair — to the process-local `label_provider_list`. The list lives in `TopMemoryContext` so it persists for the life of the backend.

```c
typedef void (*check_object_relabel_type) (const ObjectAddress *object,
                                           const char *seclabel);
```

The callback receives the `ObjectAddress` of the target and the proposed label string. Its job is to validate the label: if the string is not a syntactically valid MAC label for that provider's policy language, the callback should `ereport(ERROR)` to reject the assignment. It may also perform additional enforcement — checking that the current user has the MAC permission to assign that label to that object — before returning. If the callback returns normally, PostgreSQL accepts and stores the label.

Because the provider list is process-local, each backend that loads the extension registers its own callback. The cluster loads extensions intended for use with security labels via `shared_preload_libraries` or `session_preload_libraries`, so that every backend that connects has the provider available.

If a user issues `SECURITY LABEL` without a provider name and exactly one provider is registered, PostgreSQL uses that provider implicitly. If multiple providers are loaded, the command requires the provider name (`ExecSecLabelStmt()`, `seclabel.c`).

## Command Execution Flow

`ExecSecLabelStmt()` (`seclabel.c`) handles `SECURITY LABEL` statements end-to-end:

1. It locates the provider in `label_provider_list` by name, or selects it implicitly if only one is loaded.
2. It checks the object type against `SecLabelSupportsObjectType()`.
3. `get_object_address()` resolves the object name to an `ObjectAddress` and acquires `ShareUpdateExclusiveLock` on it.
4. `check_object_ownership()` verifies the current user owns the target.
5. It invokes the provider callback — the only validation step that knows about the label's meaning.
6. `SetSecurityLabel()` writes or updates the catalog row, or deletes it if the label is NULL.

`ExecSecLabelStmt()` holds the lock on the target object until commit, which prevents concurrent DDL from racing with label assignment.

## Relationship to Other Access-Control Mechanisms

Security labels are deliberately separate from the SQL privilege system. [[subsystems/roles-privileges|Roles and privileges]] control what SQL operations a role can perform on an object. [[subsystems/row-level-security|Row-level security]] restricts which rows of a table a role can read or write. Security labels, by contrast, attach metadata. An external MAC framework reads this metadata to enforce policies that exist outside SQL. For instance, an SELinux policy might control whether a given OS process — the PostgreSQL backend acting on behalf of a user — can read data with a particular MAC label.

The practical effect is that the two systems layer together. A user may hold `SELECT` on a table, but the MAC framework may deny access because the table's security label does not permit reads by that process's SELinux context. The provider extension enforces the MAC check, typically through hooks that intercept executor operations, not through the catalog lookup in `seclabel.c` itself.

Row-level labels are a separate matter: a non-zero `objsubid` in `pg_seclabel` identifies a column, not a row. Some MAC extensions support per-row labeling through other mechanisms, distinct from `pg_seclabel`. The standard `pg_seclabel` catalog is an object-level store; row-level MAC labeling requires additional infrastructure outside the scope of the core security label system.

## Use Cases

The primary use case is integration with SELinux via the `sepgsql` contrib extension. `sepgsql` registers itself as a provider named `"selinux"`, validates labels against the system SELinux policy on assignment, and hooks into executor operations to enforce MAC checks whenever labeled objects are accessed. In this configuration, PostgreSQL becomes a mandatory access control enforcement point: SELinux denials can prevent data access even when SQL privileges would otherwise allow it.

More broadly, security labels provide a hook point for any compliance or auditing system that needs to annotate database objects with policy metadata. A compliance tool can attach classification labels (`"sensitivity:high"`, `"pii:true"`) to columns, then read them back through `pg_seclabel` or via `obj_description()`-style queries. This can drive downstream enforcement or reporting, without needing custom catalog tables.

## Related Topics

- [[subsystems/row-level-security|row-level security]]
- [[subsystems/roles-privileges|roles and privileges]]
- [[subsystems/extensions/hooks|extension hooks]]
- [[subsystems/catalog/object-addressing|object addressing]]

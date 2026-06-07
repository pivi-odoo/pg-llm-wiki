---
title: Roles and Privileges
aliases:
  - ACL
  - Access Control
  - Permissions
tags:
  - symptom/auth-failure
source_files:
  - src/backend/catalog/aclchk.c
  - src/backend/utils/adt/acl.c
  - src/include/utils/acl.h
  - src/include/nodes/parsenodes.h
symbols:
  - AclItem
  - AclMode
  - Acl
  - aclmask
  - aclupdate
  - acldefault
  - has_privs_of_role
  - roles_is_member_of
  - pg_class_aclmask
  - object_aclmask
  - pg_attribute_aclmask
  - object_aclcheck
  - pg_class_aclcheck
  - ExecuteGrantStmt
  - ExecAlterDefaultPrivilegesStmt
  - RoleRecurseType
---

# Roles and Privileges

PostgreSQL's security model is built around a single, unified concept: the role. There are no separate "user" and "group" types in the catalog. A role that has the `LOGIN` attribute can authenticate and open a session; a role without it serves as a named collection of privileges. This unification means the same `GRANT role TO role` mechanism both assigns privileges to users and composes groups, eliminating a class of conceptual overhead that afflicts systems with distinct user and group objects.

All authorization decisions reduce to two questions: what privileges does an object's ACL grant to a role, and which roles does the current user effectively hold? The answer to the second question feeds directly into the first, making role membership the primary lever for privilege management.

## The Unified Role Model

Every principal is stored as a row in `pg_authid`. The `rolcanlogin` flag distinguishes a login role from a group role. Both role types live in the same catalog and participate in the same membership graph. Roles are identified internally by OID; names exist only for human readability. See [[subsystems/auth/role-management|Role Management]] for the full `pg_authid` attribute reference (`rolsuper`, `rolinherit`, `rolcreaterole`, and the rest) and the DDL that sets them.

## Role Membership and Inheritance

Role membership is recorded in `pg_auth_members`. This catalog maps a member OID to a role OID and carries three option flags — `admin_option`, `inherit_option`, and `set_option`. Their meaning and the distinction between passively-inherited and `SET ROLE`-gated privileges are covered in the Role Management article linked above, alongside the `pg_authid` attribute table.

The internal function `roles_is_member_of()` (acl.c) builds the transitive closure of memberships by breadth-first expansion of `pg_auth_members`, using three traversal modes encoded in `RoleRecurseType`:

```c
enum RoleRecurseType
{
    ROLERECURSE_MEMBERS = 0,  /* recurse unconditionally */
    ROLERECURSE_PRIVS = 1,    /* recurse through inheritable grants */
    ROLERECURSE_SETROLE = 2   /* recurse through grants with set_option */
};
```

The resulting list is cached per session. Syscache callbacks on `pg_auth_members` and `pg_authid` invalidate it when membership changes. `has_privs_of_role()` uses `ROLERECURSE_PRIVS` to ask "does this user automatically hold the target role's privileges?" `member_can_set_role()` uses `ROLERECURSE_SETROLE` to ask "can this user switch to the target role?"

## The ACL Data Type

Every securable object stores its permission list as an array of `AclItem` values, typed as `aclitem[]` in SQL and as `Acl *` (a standard PostgreSQL array) in C. A null ACL column means no explicit grants have been made, so the engine applies a computed default via `acldefault()`.

The core struct is compact by design:

```c
typedef struct AclItem
{
    Oid    ai_grantee;  /* role being granted privileges (0 = PUBLIC) */
    Oid    ai_grantor;  /* role that issued the grant */
    AclMode ai_privs;  /* 64-bit bitmask: low 32 = privs, high 32 = grant options */
} AclItem;
```

`AclMode` is a `uint64`. The lower 32 bits hold the privilege bits; the upper 32 hold the corresponding grant option bits. `GRANT ... WITH GRANT OPTION` sets a bit in both halves. Macros `ACLITEM_GET_PRIVS` and `ACLITEM_GET_GOPTIONS` extract each half.

The special grantee OID `ACL_ID_PUBLIC` (value 0) represents the `PUBLIC` pseudo-role. An entry with `ai_grantee == 0` grants privileges to every role in the system. Grant options cannot be assigned to `PUBLIC`. The code explicitly rejects this, because dropping the granting user later would otherwise create an unresolvable dependency.

ACL arrays are ordered so that grants with grant options appear before grants that depend on them. This ordering matters for `pg_dump`, which must emit `GRANT` statements in dependency order to reconstruct privileges correctly.

**PostgreSQL 18:** `pg_get_acl(classid oid, objid oid, objsubid int)` returns the ACL for any database object as a structured result set, accepting the object's catalog OID, object OID, and sub-object ID (0 for the object itself, column number for attributes). This makes programmatic privilege inspection straightforward without manually joining catalog-specific ACL columns.

### Privilege Bit Assignments

Privilege bits are defined in `src/include/nodes/parsenodes.h`:

| Constant | Bit | Character | Applies to |
|---|---|---|---|
| `ACL_INSERT` | 1<<0 | `a` | tables, columns |
| `ACL_SELECT` | 1<<1 | `r` | tables, columns, sequences |
| `ACL_UPDATE` | 1<<2 | `w` | tables, columns, sequences |
| `ACL_DELETE` | 1<<3 | `d` | tables |
| `ACL_TRUNCATE` | 1<<4 | `D` | tables |
| `ACL_REFERENCES` | 1<<5 | `x` | tables, columns |
| `ACL_TRIGGER` | 1<<6 | `t` | tables |
| `ACL_EXECUTE` | 1<<7 | `X` | functions, procedures |
| `ACL_USAGE` | 1<<8 | `U` | schemas, sequences, types, FDWs, foreign servers, languages |
| `ACL_CREATE` | 1<<9 | `C` | schemas, databases, tablespaces |
| `ACL_CREATE_TEMP` | 1<<10 | `T` | databases |
| `ACL_CONNECT` | 1<<11 | `c` | databases |
| `ACL_SET` | 1<<12 | `s` | configuration parameters |
| `ACL_ALTER_SYSTEM` | 1<<13 | `A` | configuration parameters |
| `ACL_MAINTAIN` | 1<<14 | `m` | tables (PostgreSQL 17+) |

The one-character codes appear in the text representation of ACLs. For example, `alice=arwdDxt/bob` means alice has all table privileges granted by bob.

**PostgreSQL 17:** The `MAINTAIN` privilege (`m`) allows non-superusers to run `VACUUM`, `ANALYZE`, `REINDEX`, `REFRESH MATERIALIZED VIEW`, `CLUSTER`, and `LOCK TABLE` on a specific table. Previously these operations required ownership or superuser. The predefined role `pg_maintain` grants `MAINTAIN` on all tables in the cluster, providing a convenient bundle for DBA-tier accounts. These accounts need routine maintenance access without broader write privileges.

### Where ACLs Are Stored

ACL columns exist throughout the catalog:

| Catalog | Column | Object type |
|---|---|---|
| `pg_class` | `relacl` | tables, views, sequences, etc. |
| `pg_proc` | `proacl` | functions and procedures |
| `pg_namespace` | `nspacl` | schemas |
| `pg_database` | `datacl` | databases |
| `pg_type` | `typacl` | types |
| `pg_tablespace` | `spcacl` | tablespaces |
| `pg_foreign_data_wrapper` | `fdwacl` | foreign-data wrappers |
| `pg_foreign_server` | `srvacl` | foreign servers |
| `pg_language` | `lanacl` | procedural languages |
| `pg_attribute` | `attacl` | individual columns |
| `pg_parameter_acl` | `paracl` | configuration parameters (GUCs) |

A null ACL triggers `acldefault()` on each lookup. The defaults include some public access: databases grant `CONNECT` and `CREATE TEMP` to `PUBLIC`, functions grant `EXECUTE` to `PUBLIC`, and types grant `USAGE` to `PUBLIC`. These defaults exist for backward compatibility and can be revoked explicitly.

## How Privilege Checks Work

Every privilege check goes through `aclmask()` (acl.c), which returns the subset of a requested privilege mask that the specified role actually holds according to an ACL array:

1. If the role is the object owner, `aclmask()` treats all grant options as satisfied without scanning the ACL.
2. `aclmask()` scans the ACL linearly for entries where `ai_grantee` is either `ACL_ID_PUBLIC` or the role's own OID. It ORs matching bits into the result.
3. A second pass checks remaining unsatisfied bits against entries for other roles. It calls `has_privs_of_role()` to test whether the subject role inherits from the ACL entry's grantee. This two-pass structure avoids calling `has_privs_of_role()` (which is moderately expensive) when direct or public grants already satisfy the request.

Higher-level functions build on `aclmask()`:

- `pg_class_aclmask()` / `pg_class_aclcheck()` — fetch `pg_class.relacl`, then call `aclmask()`. After the ACL check, these functions additionally test membership in the predefined `pg_read_all_data` and `pg_write_all_data` roles.
- `object_aclmask()` — generic path for databases, functions, schemas, tablespaces, foreign servers, and types; fetches the ACL from whichever catalog owns the object.
- `pg_attribute_aclmask()` — fetches `pg_attribute.attacl`; importantly, it does not consider table-level privileges. The caller is responsible for ORing table-level and column-level masks together when needed.

All these functions return `AclResult` — either `ACLCHECK_OK`, `ACLCHECK_NO_PRIV`, or `ACLCHECK_NOT_OWNER` — after comparing the `aclmask()` result against the required bits.

The superuser short-circuit appears throughout: `object_aclmask()` returns the full requested mask immediately if `superuser_arg(roleid)` is true, without reading the ACL. The one exception is row-level security, which applies to superusers only when `FORCE ROW SECURITY` is set on the table.

## Column-Level Privileges

Column-level grants (`GRANT SELECT (col) ON table`) store privileges in `pg_attribute.attacl` as an `aclitem[]`. They extend, but do not replace, table-level grants. When the executor checks column access, it unions the result of `pg_class_aclmask()` (table level) with `pg_attribute_aclmask()` (column level). A role with `SELECT` on the table can read every column. A role with `SELECT` on a specific column can read just that column, even without table-level access.

When a table-level privilege is revoked, `ExecGrant_Attribute()` (aclchk.c) iterates over all columns and updates their per-column ACLs. A column ACL of NULL means "no column-specific grants" — the default for columns is explicitly no privileges, in contrast to tables where the owner implicitly holds all rights.

## Schema Privileges

Schemas act as namespaces, and their privileges have a gating effect. `USAGE` on a schema is a prerequisite for accessing any object within it by unqualified name. Without `USAGE`, a role cannot look up objects in the schema even if it holds `SELECT` on a specific table inside. `CREATE` on a schema allows creating new objects there.

This two-level structure (schema + object) means that granting object-level privileges without schema `USAGE` is effectively useless for most practical access. The common pattern for fine-grained access control is: grant `CONNECT` on the database, `USAGE` on the schema, then specific object privileges.

## Default Privileges

`ALTER DEFAULT PRIVILEGES` pre-configures ACLs that apply automatically to objects a specified role creates in the future. The scope can optionally be limited to a specific schema. The configuration is stored in `pg_default_acl`, indexed by owning role, schema, and object type.

When an object is created, `get_user_default_acl()` (acl.c) looks up any matching `pg_default_acl` entries. It merges them into the initial ACL via `aclmerge()`. The resulting ACL is stored in the object's catalog column from the moment of creation. Default privileges only affect future objects; existing objects are not retroactively modified.

The struct driving `ExecAlterDefaultPrivilegesStmt()` in aclchk.c:

```c
typedef struct
{
    Oid         roleid;      /* owning role */
    Oid         nspid;       /* namespace, or InvalidOid if none */
    bool        is_grant;
    ObjectType  objtype;
    bool        all_privs;
    AclMode     privileges;
    List       *grantees;
    bool        grant_option;
    DropBehavior behavior;
} InternalDefaultACL;
```

## Predefined Roles

PostgreSQL 14 introduced a set of built-in roles that provide commonly needed privilege bundles without requiring superuser:

| Role | Purpose |
|---|---|
| `pg_read_all_data` | SELECT on all tables, views, and sequences; USAGE on all schemas |
| `pg_write_all_data` | INSERT, UPDATE, DELETE on all tables and views; USAGE on all sequences |
| `pg_read_all_settings` | Read all GUC parameters, including normally hidden ones |
| `pg_read_all_stats` | Read all statistics views |
| `pg_stat_scan_tables` | Execute monitoring functions that scan tables |
| `pg_monitor` | Read/execute various monitoring views and functions |
| `pg_signal_backend` | Send signals (cancel, terminate) to other backends |
| `pg_read_server_files` | Read server-side files via COPY and file functions |
| `pg_write_server_files` | Write server-side files via COPY |
| `pg_execute_server_program` | Execute programs on the server via COPY |
| `pg_checkpoint` | Issue CHECKPOINT |
| `pg_database_owner` | Implicitly owned by the current database's owner |
| `pg_maintain` | MAINTAIN on all tables (PostgreSQL 17+) |
| `pg_signal_autovacuum_worker` | Send signals to [[subsystems/background/autovacuum|autovacuum]] worker processes (PostgreSQL 18+) |

`pg_read_all_data` and `pg_write_all_data` are not enforced solely through ACL entries on individual objects. Instead, `pg_class_aclmask_ext()` checks membership in these roles after the normal ACL scan. It then sets the corresponding privilege bits directly. This means they override even explicit `REVOKE` statements on individual relations — a deliberate design choice for administrative convenience.

**PostgreSQL 17:** The `pg_maintain` predefined role grants the `MAINTAIN` privilege on all tables cluster-wide. Members can run `VACUUM`, `ANALYZE`, `REINDEX`, `REFRESH MATERIALIZED VIEW`, `CLUSTER`, and `LOCK TABLE` on any table without requiring object ownership or superuser status. Like `pg_read_all_data`, membership is checked in `pg_class_aclmask_ext()` after the standard ACL scan.

**PostgreSQL 18:** The `pg_signal_autovacuum_worker` predefined role allows members to send signals to autovacuum worker processes without superuser rights. This fills a gap left by `pg_signal_backend`, which covers regular backends but not autovacuum workers, enabling operations teams to manage runaway autovacuum jobs from monitoring accounts.

## The Superuser Exception

A superuser (`rolsuper = true`) bypasses ACL checks for nearly all operations. In `object_aclmask()` and `pg_class_aclmask_ext()`, the first substantive check is `superuser_arg(roleid)`, which returns the full requested mask immediately if true.

The two exceptions are:

1. **System catalog modifications** — even superusers cannot write to system catalogs via ordinary DML; the code in `pg_class_aclmask_ext()` strips write bits from the mask for system relations unless the caller is a superuser (and even then, only with `pg_authid.rolsuper`).

2. **Row-level security with `FORCE ROW SECURITY`** — when a table has `FORCE ROW SECURITY` enabled, the owning role's sessions are still subject to RLS policies. Superusers are subject to RLS only if they also have `BYPASSRLS` disabled for that session, or the table explicitly forces it.

## GRANT and REVOKE Implementation

`ExecuteGrantStmt()` (aclchk.c) is the entry point for all `GRANT` and `REVOKE` statements. It resolves object names to OIDs. It then determines the effective grantor via `select_best_grantor()`, which finds the role in the current user's membership graph that holds the necessary grant options. Finally, it calls type-specific handlers such as `ExecGrant_Relation()` for tables or `ExecGrant_common()` for most other object types.

The actual ACL modification goes through `merge_acl_with_grant()`, which calls `aclupdate()` for each grantee. `aclupdate()` searches the existing ACL array for a matching `(grantee, grantor)` pair and either updates it in-place or appends a new entry. When grant options are removed, `recursive_revoke()` cascades the revocation to any privileges that were sub-granted based on the now-removed grant option.

`updateAclDependencies()` maintains dependency tracking, recording shared dependencies from ACL members back to the roles they reference. This ensures that dropping a role triggers appropriate revocation of grants that reference it.

## Related Topics

- [[subsystems/auth/role-management|Role Management]] — covers the DDL layer for creating, altering, and dropping roles, complementing the ACL enforcement described here
- [[subsystems/row-level-security|Row-Level Security]] — extends the privilege model with per-row policies that apply after ACL checks pass
- [[subsystems/catalog/parameter-acl|Parameter ACL]] — details the `pg_parameter_acl` catalog that stores GUC-level privilege entries referenced in the ACL column table above
- [[subsystems/catalog/core-catalogs|Core Catalogs]] — covers `pg_authid`, `pg_auth_members`, and the other system catalogs that back the role and ACL data structures
- [[subsystems/catalog/pg-namespace|pg_namespace]] — explains schema namespace storage, directly relevant to schema-level USAGE and CREATE privilege checks
- [[subsystems/auth/pg-hba-conf|pg_hba.conf]] — governs connection-level authentication that precedes the ACL and role privilege checks described here
- [[subsystems/guc|GUC]] — configuration parameters include ACL-controlled settings (ACL_SET, ACL_ALTER_SYSTEM) listed in the privilege bit table
- [[architecture/overview|Architecture Overview]] — overall system architecture and catalog layout
- [[subsystems/catalog/syscache|Catalog Caches]] — how catalog caches speed up repeated ACL lookups
- [[subsystems/transactions/mvcc|MVCC]] — visibility rules that interact with object ownership
- [[subsystems/storage/heap|Heap Storage]] — where catalog rows including ACL columns are stored
- [[subsystems/executor/overview|Executor]] — where privilege checks are triggered during query execution

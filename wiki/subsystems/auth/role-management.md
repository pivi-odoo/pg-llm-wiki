---
title: "Role Management: CREATE, ALTER, DROP ROLE"
aliases:
  - CREATE ROLE
  - CREATE USER
  - ALTER ROLE
  - DROP ROLE
  - role attributes
  - pg_authid
source_files:
  - src/backend/commands/user.c
  - src/include/catalog/pg_authid.h
  - src/include/catalog/pg_auth_members.h
symbols:
  - CreateRole
  - AlterRole
  - DropRole
  - RenameRole
  - AddRoleMems
  - DelRoleMems
  - GrantRoleOptions
  - RevokeRoleGrantAction
---

PostgreSQL's role system is the mechanism that creates, configures, and connects every database principal, whether a human user or a service account. Every subject that can log in, own objects, or hold privileges is a role. PostgreSQL stores every attribute that governs what a role may do in a single catalog row.

## The Unified Role Concept

PostgreSQL has no distinct "user" and "group" types. Both are roles. The `CREATE USER` and `CREATE GROUP` statements are historical aliases for `CREATE ROLE`. The only difference is that `CREATE USER` sets `LOGIN` by default; `CREATE ROLE` does not. All three write the same catalog row in `pg_authid` (`CreateRole()`, `user.c`).

PostgreSQL reserves role names starting with `pg_` for system use. It rejects them at creation time. It folds names to lowercase like all identifiers, unless they are double-quoted.

## Role Attributes

Every role is a row in the shared catalog `pg_authid`. The privilege-related columns are boolean flags:

| `pg_authid` column | Attribute in SQL | Meaning |
|---|---|---|
| `rolsuper` | `SUPERUSER` | Bypasses all permission checks except RLS with `FORCE ROW SECURITY` |
| `rolinherit` | `INHERIT` | Automatically hold privileges from roles this role is a member of |
| `rolcreaterole` | `CREATEROLE` | May create, alter, and drop roles below its privilege level |
| `rolcreatedb` | `CREATEDB` | May create databases |
| `rolcanlogin` | `LOGIN` | May initiate a client connection |
| `rolreplication` | `REPLICATION` | May initiate streaming replication and create/drop replication slots |
| `rolbypassrls` | `BYPASSRLS` | Exempt from row-level security policies |
| `rolconnlimit` | `CONNECTION LIMIT` | Maximum concurrent sessions (-1 means no limit) |
| `rolpassword` | `PASSWORD` | Hashed password for password authentication |
| `rolvaliduntil` | `VALID UNTIL` | Timestamp after which the password is no longer accepted |

`CreateRole()` applies a permission hierarchy: a non-superuser with `CREATEROLE` can create roles, but not ones with `SUPERUSER`, `REPLICATION`, `BYPASSRLS`, or `CREATEDB` attributes it doesn't itself hold. Superusers can create roles with any attributes.

## Password Storage

PostgreSQL never stores passwords in plaintext. `CreateRole()` calls `encrypt_password()` to hash the password before inserting it into `rolpassword`. The default hash method since PostgreSQL 14 is SCRAM-SHA-256, controlled by the `password_encryption` GUC.

PostgreSQL treats an empty string the same as no password: libpq does not transmit empty passwords, so storing one would create a login that appears to have password authentication. Such a login would never authenticate successfully. PostgreSQL clears the password column instead of storing the empty string, giving consistent behavior across all clients.

Extensions can intercept password changes via the `check_password_hook` (`user.c`). This hook receives the plaintext password, the role name, the hash type, and the expiry timestamp before PostgreSQL stores the password. It can raise an error to enforce policy rules.

## Role Membership

PostgreSQL stores role membership in `pg_auth_members`. Each row records that a role is a member of another role. Each row also carries three option flags:

| `pg_auth_members` option | SQL keyword | Effect |
|---|---|---|
| `admin_option` | `WITH ADMIN OPTION` | The member may grant this role to other roles |
| `inherit_option` | `WITH INHERIT TRUE` | Member automatically holds the parent role's privileges |
| `set_option` | `WITH SET TRUE` | Member may use `SET ROLE` to assume the parent role's identity |

`GRANT role TO role` calls `AddRoleMems()` (`user.c`), which inserts or updates a row in `pg_auth_members`. `REVOKE role FROM role` calls `DelRoleMems()`, which may cascade via `plan_recursive_revoke()` to downstream grants that depended on the revoked grant option.

When `inherit_option` is true, the member passively holds all privileges of the parent role without any session-level action. When `inherit_option` is false but `set_option` is true, the member gains the privileges only after the session executes `SET ROLE parent_role`. This is the mechanism for requiring an explicit privilege escalation step — useful for audit trails or for separating normal-operation and elevated-operation identities.

## createrole_self_grant

When a non-superuser with `CREATEROLE` creates a new role, PostgreSQL automatically grants them `ADMIN OPTION` on that role (recorded with the bootstrap superuser as grantor). This lets the creator administer the role they just created without being a superuser.

The `createrole_self_grant` GUC can extend this automatic grant to also include `INHERIT` and/or `SET` options. The default value is an empty string (no extra options). Setting it to `'set'`, `'inherit'`, or `'set, inherit'` applies the options immediately when the non-superuser creates a role, saving an explicit `GRANT` step. This has no security implications because the creator already holds `ADMIN OPTION` and can make the same grant manually.

## Altering and Dropping Roles

`ALTER ROLE name [RENAME TO | SET option | RESET option | attribute ...]` routes to `AlterRole()` or `RenameRole()` depending on the clause. Attribute changes update `pg_authid` in place.

`ALTER ROLE name IN DATABASE dbname SET guc_name = value` records a per-database GUC override in `pg_db_role_setting`. PostgreSQL applies this setting at connection time when the role connects to that specific database, after `postgresql.conf` and before any session-level `SET`.

`DROP ROLE name` calls `DropRole()`. `DropRole()` checks that the role owns no objects and that no privileges reference it, then deletes the `pg_authid` row and all `pg_auth_members` entries. Attempting to drop a role that owns objects raises an error; the owner must reassign (`REASSIGN OWNED BY`) or drop (`DROP OWNED BY`) the objects first.

## Related Topics

- [[subsystems/roles-privileges|Roles and Privileges (GRANT, ACL system)]]
- [[subsystems/auth/overview|Authentication Overview (pg_hba.conf)]]
- [[subsystems/row-level-security|Row-Level Security]]
- [[subsystems/catalog/core-catalogs|Core System Catalogs]]

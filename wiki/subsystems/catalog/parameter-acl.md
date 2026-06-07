---
title: "Parameter ACL: GRANT SET ON PARAMETER"
aliases:
  - pg_parameter_acl
  - GRANT SET ON PARAMETER
  - parameter ACL
source_files:
  - src/backend/catalog/pg_parameter_acl.c
  - src/include/catalog/pg_parameter_acl.h
  - src/backend/utils/misc/guc.c
  - src/backend/catalog/aclchk.c
symbols:
  - ParameterAclCreate
  - ParameterAclLookup
  - pg_parameter_aclcheck
  - pg_parameter_aclmask
  - convert_GUC_name_for_parameter_acl
  - check_GUC_name_for_parameter_acl
---

PostgreSQL 15 introduced `GRANT SET ON PARAMETER` as a way to let non-superusers change specific GUC variables that were previously restricted to superusers. Before this feature, any parameter with a `PGC_SUSET` context was off-limits to ordinary roles; the only workarounds were granting superuser status or writing a `SECURITY DEFINER` wrapper. The `pg_parameter_acl` catalog records which parameters have delegated SET or ALTER SYSTEM privileges. The GUC machinery consults it whenever a non-superuser attempts to change a restricted variable.

## The pg_parameter_acl Catalog

`pg_parameter_acl` (OID 6243) is a shared catalog — it lives in the global tablespace and is visible across all databases in the cluster. Its schema is minimal:

| Column | Type | Description |
|--------|------|-------------|
| `oid` | `oid` | Row identifier |
| `parname` | `text` | Canonical GUC name |
| `paracl` | `aclitem[]` | Access control list (NULL means no grants yet) |

The catalog is shared (`BKI_SHARED_RELATION`) because GUC privileges apply cluster-wide. A role granted `SET` on `work_mem` holds that privilege in every database, just as role membership does.

The `parname` column stores a canonicalized form of the GUC name: lowercase and with any legacy alias translated to the current name. `convert_GUC_name_for_parameter_acl()` (`guc.c`) handles this normalization, applying `map_old_guc_names[]` for renamed parameters and lowercasing the result to match GUC lookup semantics. `ParameterAclCreate()` creates a row in `pg_parameter_acl` the first time any privilege is granted for a parameter. The `paracl` column starts as NULL; the standard `GRANT` machinery populates it.

`check_GUC_name_for_parameter_acl()` prevents inserting useless rows: a name must either match an existing GUC (`find_option()` succeeds) or be a valid custom GUC name (`extension.varname` form). Unknown names that satisfy neither are rejected at `GRANT` time with `ERRCODE_INVALID_NAME`.

## Privilege Bits

Parameters support two distinct privilege bits, defined in `acl.h`:

- `ACL_SET` (`'s'`) — allows `SET param = value` in a session and `ALTER ROLE … SET param` to persist the setting.
- `ACL_ALTER_SYSTEM` (`'A'`) — allows `ALTER SYSTEM SET param`, which writes to `postgresql.auto.conf`.

`ACL_ALL_RIGHTS_PARAMETER_ACL` is the union of both bits. A typical delegation grants only `ACL_SET`; granting `ACL_ALTER_SYSTEM` is more powerful because it affects all future connections.

The grantor of a parameter privilege is always recorded as the bootstrap superuser OID, matching the convention used for other shared-object ACLs.

## Enforcement in the GUC Machinery

The permission check lives in `set_config_option()` (`guc.c`), the central function for applying any GUC change. When a non-superuser attempts to set a variable whose context is `PGC_SUSET`, the code falls into:

```c
case PGC_SUSET:
    if (context == PGC_USERSET || context == PGC_BACKEND)
    {
        aclresult = pg_parameter_aclcheck(name, srole, ACL_SET);
        if (aclresult != ACLCHECK_OK)
            ereport(elevel, ...  "permission denied to set parameter \"%s\"");
    }
```

`PGC_SU_BACKEND` parameters (set only during connection startup) go through the same check at the `PGC_BACKEND` context level. `PGC_USERSET` parameters need no ACL check — any role can set them.

`pg_parameter_aclcheck()` (`aclchk.c`) canonicalizes the name, looks up the row in `pg_parameter_acl` via the `PARAMETERACLNAME` syscache, and calls `aclmask()` against the `paracl` column. Superusers short-circuit immediately and always receive `ACL_NO_RIGHTS`-free access. If no row exists for the parameter, the function returns `ACL_NO_RIGHTS` for all non-superusers — the catalog's absence is the denial, not a negative entry.

The `check_GUC_name_for_set` path used by `ALTER ROLE SET` and `ALTER DATABASE SET` also calls `pg_parameter_aclcheck()` to decide whether a non-superuser may record a persistent GUC override (`guc.c`, around line 6604). This is what makes `ALTER ROLE … SET` work for a role that holds `ACL_SET`: the validation function grants permission at `PGC_USERSET` level so the value can be recorded in `pg_db_role_setting`.

The system checks `ALTER SYSTEM SET` separately using `ACL_ALTER_SYSTEM` before touching `postgresql.auto.conf`. The post-alter hook fires with `ParameterAclRelationId` even for parameters that have no `pg_parameter_acl` row, to give extensions a consistent hook point.

## Granting and Revoking

```sql
-- Allow app_role to set search_path in sessions and via ALTER ROLE
GRANT SET ON PARAMETER search_path TO app_role;

-- Allow dba_role to also write search_path to auto.conf
GRANT ALTER SYSTEM ON PARAMETER search_path TO dba_role;

-- Revoke the session-level privilege
REVOKE SET ON PARAMETER search_path FROM app_role;
```

`GRANT ALL ON PARAMETER` grants both `SET` and `ALTER SYSTEM`. `WITH GRANT OPTION` is not supported for parameter ACLs.

After `REVOKE` removes all privileges from a parameter, the `pg_parameter_acl` row remains with a non-NULL empty ACL rather than being deleted. This preserves the object address and avoids complications with dependency tracking.

## Interaction with ALTER ROLE SET

`ALTER ROLE role SET param TO value` persists a GUC override in `pg_db_role_setting`, applied at login. For `PGC_SUSET` parameters this was superuser-only before PostgreSQL 15. With a `GRANT SET ON PARAMETER param TO role` in place, the role may now issue this command for itself. The internal path in `guc.c` runs `check_GUC_name_for_set()`. This grants the operation at `PGC_USERSET` level when `pg_parameter_aclcheck` returns `ACLCHECK_OK`, allowing the value to pass validation and be stored.

This combination is the recommended pattern for multi-tenant deployments: grant `SET` on `statement_timeout`, `work_mem`, or `search_path` to application roles so they can manage their own session behavior without superuser contact.

## Name Canonicalization and Legacy Names

GUC names are case-insensitive in SQL but stored lowercase in `pg_parameter_acl`. `convert_GUC_name_for_parameter_acl()` also applies the `map_old_guc_names[]` substitution table, which maps deprecated parameter names to their current equivalents. This means a `GRANT SET ON PARAMETER` issued with an old name is stored under the new canonical name. It will match correctly when the GUC machinery enforces the check.

## Dependency Tracking

`pg_parameter_acl` rows participate in the dependency system as object type `OBJECT_PARAMETER_ACL`. Dropping a role that holds privileges on a parameter causes the standard ACL pruning to run — the `aclitem[]` entries for that role are removed. There is no cascade behavior: the parameter ACL row itself is not dropped when grantees are removed.

## Use Cases

The primary motivation for this feature is multi-tenant and security-hardened deployments:

- **`search_path` hardening**: granting `SET` on `search_path` to specific application roles prevents schema-injection attacks via `search_path` manipulation while still letting those roles configure their own path. See [[subsystems/catalog/schema-search-path|search_path]].
- **Resource control without superuser**: application roles can be permitted to adjust `work_mem`, `statement_timeout`, or `lock_timeout` for their sessions without holding any elevated privilege.
- **Reduced superuser surface**: connection poolers and middleware that previously needed superuser access to set `application_name` or similar parameters can be granted only the specific privileges they need.

## Related Topics

- [[subsystems/catalog/per-role-settings|per-role settings]] — `pg_db_role_setting` and how `ALTER ROLE SET` persists GUC overrides
- [[subsystems/auth/role-management|role management]] — role creation, attributes, and membership
- [[subsystems/catalog/schema-search-path|search_path]] — a common target for parameter ACL delegation

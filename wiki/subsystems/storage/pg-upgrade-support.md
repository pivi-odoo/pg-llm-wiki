---
title: "pg_upgrade Support Functions"
aliases:
  - binary_upgrade support
  - pg_upgrade OID transplanting
  - binary_upgrade GUC
source_files:
  - src/backend/utils/adt/pg_upgrade_support.c
  - src/include/catalog/binary_upgrade.h
symbols:
  - binary_upgrade_set_next_heap_pg_class_oid
  - binary_upgrade_set_next_heap_relfilenode
  - binary_upgrade_set_next_toast_pg_class_oid
  - binary_upgrade_set_next_toast_relfilenode
  - binary_upgrade_set_next_index_pg_class_oid
  - binary_upgrade_set_next_index_relfilenode
  - binary_upgrade_set_next_pg_type_oid
  - binary_upgrade_set_next_array_pg_type_oid
  - binary_upgrade_set_next_multirange_pg_type_oid
  - binary_upgrade_set_next_multirange_array_pg_type_oid
  - binary_upgrade_set_next_pg_enum_oid
  - binary_upgrade_set_next_pg_authid_oid
  - binary_upgrade_set_next_pg_tablespace_oid
  - binary_upgrade_set_record_init_privs
  - binary_upgrade_set_missing_value
  - binary_upgrade_create_empty_extension
  - CHECK_IS_BINARY_UPGRADE
---

`pg_upgrade_support.c` exposes a set of SQL-callable functions that `pg_upgrade` uses to transplant OIDs, storage filenodes, and other catalog identifiers from an old cluster into a freshly initialised new cluster. The `binary_upgrade` GUC guards every function in the file; calling any of them on an ordinary running server raises an error immediately. The functions exist because OIDs weave together PostgreSQL's [[subsystems/catalog/core-catalogs]] entirely. Any mismatch between the OIDs a catalog row was written with, and the OIDs the new cluster assigns, would silently corrupt cross-catalog references.

## The binary_upgrade GUC

`pg_upgrade` spawns the new-cluster postmaster with `-c binary_upgrade=on` before it issues any DDL. This sets the C-level global `IsBinaryUpgrade` to `true`. Every function in `pg_upgrade_support.c` begins with the `CHECK_IS_BINARY_UPGRADE` macro, which issues `ereport(ERROR, ERRCODE_CANT_CHANGE_RUNTIME_PARAM, ...)` if `IsBinaryUpgrade` is false. The GUC is not listed in `postgresql.conf` and has no effect on normal operations; it exists purely as a mode switch that enables the otherwise-inaccessible transplant API.

Because the functions set backend-global variables, they are inherently session-scoped and single-use: the next `CREATE TABLE`, `CREATE INDEX`, or `CREATE TYPE` consumes the override the moment it runs. After that the variable reverts to `InvalidOid` (or the equivalent zero value) and ordinary OID allocation resumes.

The PostgreSQL system [[subsystems/catalog/core-catalogs]] expresses all foreign-key-like relationships as raw `Oid` values, not as names. A row in `pg_attribute` identifies its table by storing the `pg_class.oid` of that table. A row in `pg_depend` stores the OIDs of both the dependent and referenced objects. If `pg_upgrade` allowed the new cluster to assign fresh OIDs to each recreated object, every one of those cross-references would break. Preservation is therefore not an optimisation but a correctness requirement: the new cluster must end up with exactly the same OID assignments that the old cluster had.

The same logic applies to storage filenodes. A relation's heap files live in `$PGDATA/base/<dboid>/<relfilenode>`. `pg_upgrade` hard-links (or copies) the old data files into the new cluster directory under those same numeric names. If the new `CREATE TABLE` picked a different relfilenode the backend would look for the data at the wrong path.

## Relation and filenode overrides

Before issuing a `CREATE TABLE` for a migrated relation, `pg_upgrade` calls:

- `binary_upgrade_set_next_heap_pg_class_oid(oid)` — preloads `binary_upgrade_next_heap_pg_class_oid` so that the next heap's `pg_class` row is inserted with that exact OID.
- `binary_upgrade_set_next_heap_relfilenode(relfilenumber)` — preloads `binary_upgrade_next_heap_pg_class_relfilenumber` so that the on-disk storage path matches the old cluster.

If the table has a [[subsystems/storage/toast]] table, two additional calls fix its `pg_class` OID and relfilenode via `binary_upgrade_set_next_toast_pg_class_oid` and `binary_upgrade_set_next_toast_relfilenode`.

Indexes follow the same pattern through `binary_upgrade_set_next_index_pg_class_oid` and `binary_upgrade_set_next_index_relfilenode`. All six functions write into the corresponding `binary_upgrade_next_*` globals declared in `catalog/binary_upgrade.h` and consumed by the code paths that create heaps and indexes.

```
pg_upgrade (client side)
  │
  ├─ binary_upgrade_set_next_heap_pg_class_oid(old_oid)
  ├─ binary_upgrade_set_next_heap_relfilenode(old_filenode)
  ├─ [optional] binary_upgrade_set_next_toast_pg_class_oid(old_toast_oid)
  ├─ [optional] binary_upgrade_set_next_toast_relfilenode(old_toast_filenode)
  └─ CREATE TABLE ...
       └─ heap creation code reads binary_upgrade_next_heap_pg_class_oid
          and binary_upgrade_next_heap_pg_class_relfilenumber,
          then resets them to InvalidOid
```

## Type, enum, and role OID overrides

The catalog also references types, array types, multirange types, enum labels, and roles by OID throughout. The corresponding override functions write into a parallel set of globals:

| Function | Global written |
|---|---|
| `binary_upgrade_set_next_pg_type_oid` | `binary_upgrade_next_pg_type_oid` |
| `binary_upgrade_set_next_array_pg_type_oid` | `binary_upgrade_next_array_pg_type_oid` |
| `binary_upgrade_set_next_multirange_pg_type_oid` | `binary_upgrade_next_mrng_pg_type_oid` |
| `binary_upgrade_set_next_multirange_array_pg_type_oid` | `binary_upgrade_next_mrng_array_pg_type_oid` |
| `binary_upgrade_set_next_pg_enum_oid` | `binary_upgrade_next_pg_enum_oid` |
| `binary_upgrade_set_next_pg_authid_oid` | `binary_upgrade_next_pg_authid_oid` |
| `binary_upgrade_set_next_pg_tablespace_oid` | `binary_upgrade_next_pg_tablespace_oid` |

`pg_upgrade` sets each one immediately before the DDL statement that creates the corresponding object, and the catalog insertion code consumes it once.

## Initial privileges

`binary_upgrade_set_record_init_privs(bool)` sets `binary_upgrade_record_init_privs`. When `true`, subsequent catalog inserts tag the new rows as having "initial privileges" — the baseline ACL state that `initdb` establishes for the database. This prevents `pg_upgrade` from re-granting default privileges on system catalog objects that already had the correct ACL in the source cluster. `pg_upgrade` toggles the flag around blocks of DDL that restore built-in catalog permissions.

## Missing column defaults

`binary_upgrade_set_missing_value(table_oid, attname, value)` calls `SetAttrMissing()` to restore a column's "missing value" — the default returned for rows predating an `ALTER TABLE ... ADD COLUMN ... DEFAULT` that used the fast-path optimisation. Without this function, rows in the old cluster that had never been rewritten would silently return `NULL` for that column after upgrade.

## Extension metadata

`binary_upgrade_create_empty_extension` calls `InsertExtensionTuple` directly, registering an extension in `pg_extension` without running its SQL script. This gives `pg_upgrade` control over exactly which catalog rows it attributes to an extension, while it recreates the member objects one by one with their original OIDs.

Setting a next-OID override bypasses the normal OID counter and duplicate-OID detection. If a caller preloads an OID that is already in use by another catalog row, the subsequent insert creates a duplicate primary key. The catalog code does not check for this at insertion time. The resulting corruption is silent and may only surface much later as confusing errors or wrong query results. The `CHECK_IS_BINARY_UPGRADE` guard and the requirement to pass `-c binary_upgrade=on` at postmaster start are the only barriers preventing misuse — there is no additional runtime validation that the supplied OID is safe.

## Related Topics

- [[subsystems/catalog/core-catalogs]]
- [[subsystems/storage/toast]]
- [[subsystems/storage/visibility-map]]

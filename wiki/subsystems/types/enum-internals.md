---
title: "Enum Type Internals"
aliases:
  - "CREATE TYPE AS ENUM"
  - "pg_enum"
source_files:
  - src/backend/utils/adt/enum.c
  - src/backend/catalog/pg_enum.c
  - src/backend/commands/typecmds.c
  - src/include/catalog/pg_enum.h
symbols:
  - enum_in
  - enum_out
  - enum_cmp_internal
  - compare_values_of_enum
  - AddEnumLabel
  - RenumberEnumType
  - check_safe_enum_use
---

# Enum Type Internals

An enum type is a fixed, ordered set of string labels. Every label is a row in the `pg_enum` catalog. Enum values are physically stored and compared as OIDs, not as strings. This design keeps storage compact and comparison cheap while leaving the label text available for display.

## The pg_enum catalog

Each row of `pg_enum` represents one label of one enum type and has three columns relevant to the implementation:

| Column | Type | Purpose |
|--------|------|---------|
| `oid` | `oid` | The datum value used when storing or passing this enum value |
| `enumtypid` | `oid` | The `pg_type` OID of the enum type this label belongs to |
| `enumsortorder` | `float4` | The ordering position of this label within the enum |
| `enumlabel` | `name` | The text label as declared in `CREATE TYPE` or `ALTER TYPE ADD VALUE` |

When a column of an enum type stores a value, PostgreSQL writes the OID of the corresponding `pg_enum` row to the heap page, not the label string. `enum_in()` (`enum.c`) translates a text label to an OID by doing a syscache lookup on `(enumtypid, enumlabel)`. `enum_out()` translates back by looking up the OID in the `ENUMOID` syscache.

## Ordering and comparison

`enumsortorder`, a 32-bit float, determines the order of enum labels. At type creation time, labels receive sort orders 1.0, 2.0, 3.0, and so on — integers spaced to leave room for later additions.

Comparison functions (`enum_lt`, `enum_le`, `enum_eq`, etc.) ultimately call `enum_cmp_internal()`. This function contains an important fast path: if both OID arguments are even numbers, their numeric OID order always matches their `enumsortorder` order. In that case, the comparison is a direct integer comparison without any catalog access. This holds because `CREATE TYPE AS ENUM` assigns OIDs from the system sequence. The sequence allocates even OIDs to `pg_enum` rows created together in a single catalog write. The typcache calls `compare_values_of_enum()` only when either OID is odd. This happens for enum values added via `ALTER TYPE ADD VALUE`, described below.

## ALTER TYPE ADD VALUE and sort order bisection

`ALTER TYPE ADD VALUE` inserts a new `pg_enum` row with a sort order chosen to place the new label before or after a specified neighbor. `AddEnumLabel()` (`pg_enum.c`) bisects the `enumsortorder` range between the neighbor and its adjacent label, assigning the midpoint as the new row's sort order.

Because `enumsortorder` is a float4 (32-bit), repeated bisection eventually exhausts floating-point precision: the midpoint equals one of its neighbors. When this happens, `RenumberEnumType()` rewrites the sort orders of all existing `pg_enum` rows for that type. It redistributes them as evenly spaced integers, leaving room for future additions. This renumbering is a catalog update. It takes a lock on `pg_enum`.

New values added by `ALTER TYPE ADD VALUE` receive odd OIDs, because they are inserted individually rather than in a batch. This is why `enum_cmp_internal()` cannot safely use the fast comparison path for them. The odd-OID signal tells `enum_cmp_internal()` to fall back to sorting by `enumsortorder` via the typcache.

## Uncommitted enum values and index safety

A subtle constraint governs enum values added in an open transaction: PostgreSQL cannot safely use an uncommitted `pg_enum` row in an index. If the row were indexed and the transaction then rolled back, the index would reference an OID that no longer exists in `pg_enum`. This would corrupt comparisons.

`check_safe_enum_use()` (`enum.c`) enforces this restriction. `enum_in()` and the range function `enum_range()` call it before returning any enum OID to SQL. The check inspects the [[subsystems/transactions/hint-bits|hint bits]] of the `pg_enum` heap tuple: if `HEAP_XMIN_COMMITTED` is set, the row is safe. Otherwise, it checks whether the row was created by the same transaction that created the enum type itself (using `EnumUncommitted()` on the OID). If the row is from a different uncommitted transaction, the function raises an error.

Enum values created as part of `CREATE TYPE AS ENUM` are exempt, because the type and all its initial labels share a transaction. Any index on an enum column would also be new. It would disappear on rollback. Values added later with `ALTER TYPE ADD VALUE` do not get this exemption. They are restricted until the adding transaction commits.

## Type cache integration

The typcache maintains an `enum_cmp_cache` per enum type — a sorted array of `(OID, enumsortorder)` pairs loaded from `pg_enum`. `compare_values_of_enum()` binary-searches this array to find sort orders for two OIDs. Then it compares the sort orders as floats. Syscache invalidation invalidates the cache when `pg_enum` changes, causing a rebuild on next use.

## See also

- [[subsystems/catalog/core-catalogs]] — pg_type typtype values and the catalog system
- [[subsystems/types/domains]] — another constraint-bearing type built atop a base type
- [[subsystems/transactions/mvcc]] — how uncommitted catalog rows interact with snapshots

---
title: "Collation Catalog (pg_collation)"
aliases:
  - collation
  - pg_collation
  - ICU collation
  - libc collation
  - collisdeterministic
source_files:
  - src/backend/catalog/pg_collation.c
  - src/include/catalog/pg_collation.h
symbols:
  - FormData_pg_collation
  - CollationCreate
  - COLLPROVIDER_DEFAULT
  - COLLPROVIDER_ICU
  - COLLPROVIDER_LIBC
---

A collation defines the rules by which PostgreSQL orders strings and folds case — it governs `ORDER BY` results, index key ordering, `LIKE` pattern matching sensitivity, and equality semantics for text columns. Every text value in the system carries an implicit or explicit collation OID that the planner and executor use to select comparison functions. Getting this wrong silently produces incorrect query results or prevents index use entirely.

## What the catalog stores

`pg_collation` (`FormData_pg_collation`, `pg_collation.h`) has one row per named collation per encoding. The key fields are:

| Column | Type | Meaning |
|---|---|---|
| `collname` | `name` | SQL-visible name, unique within `(collencoding, collnamespace)` |
| `collnamespace` | `oid` | Owning schema; built-in collations live in `pg_catalog` |
| `collprovider` | `char` | `'d'` default, `'c'` libc, `'i'` ICU, `'b'` builtin |
| `collisdeterministic` | `bool` | Whether equal strings must be byte-identical (see below) |
| `collencoding` | `int32` | Encoding this collation applies to; `-1` means any encoding |
| `collcollate` | `text` | `LC_COLLATE` locale string (libc provider only) |
| `collctype` | `text` | `LC_CTYPE` locale string (libc provider only) |
| `colliculocale` | `text` | ICU locale ID (ICU provider only) |
| `collicurules` | `text` | Optional ICU tailoring rules |
| `collversion` | `text` | Snapshot of the provider's collation version at creation time |

The unique index `pg_collation_name_enc_nsp_index` covers `(collname, collencoding, collnamespace)`. A separate any-encoding collation (`collencoding = -1`) cannot share a name with an encoding-specific one in the same namespace; `CollationCreate()` checks both directions under a `ShareRowExclusiveLock` to close the race window.

## Provider types and their tradeoffs

The `collprovider` column determines which library supplies comparison and case-folding routines at runtime.

**libc (`'c'`)** delegates directly to the C library's `strcoll` / `strxfrm`. Coverage is broad because every POSIX system ships locales. However, locale definitions vary between OS versions and distributions. PostgreSQL splits the effective locale across two fields. `collcollate` controls sort order. `collctype` controls character classification (`isalpha`, `toupper`, etc.). These can differ — for example, `en_US.UTF-8` collate with `C` ctype is a common combination that gives ASCII character classification with locale-aware ordering.

**ICU (`'i'`)** uses the International Components for Unicode library. A BCP-47 locale tag stored in `colliculocale` identifies ICU collations. They may also carry additional tailoring in `collicurules`. ICU provides richer Unicode support (multi-level weighting, language-specific rules) and consistent behaviour across platforms. It is the recommended provider for new deployments dealing with non-ASCII data.

**Default (`'d'`)** is a pseudo-provider that means "inherit the database's collation". The special collation `pg_catalog."default"` always has provider `'d'`. Columns declared without `COLLATE` use it. Its actual behaviour depends on what the database was created with.

**Builtin (`'b'`)** covers the special `C` and `POSIX` collations that rely on raw byte comparison rather than any locale library. They are fast and fully portable but order by Unicode code point rather than linguistic rules.

## Determinism and its consequences

`collisdeterministic` is the most operationally significant boolean in the catalog. When `true` (the default for all libc and builtin collations), PostgreSQL guarantees that two strings compare equal if and only if they are byte-for-byte identical after normalization. When `false`, linguistically equivalent strings may be "equal" even if they differ in case, accents, or normalization form. This setting is only valid for ICU collations.

Non-deterministic collations enable true case-insensitive and accent-insensitive search without function wrappers:

```sql
CREATE COLLATION case_insensitive (
    provider = icu,
    locale = 'und-u-ks-level2',
    deterministic = false
);

ALTER TABLE products ALTER COLUMN name TYPE text COLLATE case_insensitive;
```

The cost is significant: **PostgreSQL disallows non-deterministic collations in unique indexes** (two "equal" strings that differ in bytes would violate uniqueness at the storage level). **It also restricts pattern operators like `LIKE` and `~`**, because pattern matching is inherently byte-oriented. The planner enforces this: it will use an index built on a non-deterministic collation for range scans and `ORDER BY` but not for equality deduplication.

## Collation version tracking

`collversion` captures a version string from the provider at the time the collation is created or imported. ICU exposes a version via `ucol_getVersion()`; libc exposes one via `strverscmp` on the locale version string where available.

When PostgreSQL uses a collation to build an index, it records the version in `pg_index.indcollation` alongside a snapshot in `pg_depend`. On each subsequent database open, PostgreSQL compares the current provider version against the stored snapshot. If they differ, the system emits a warning:

```
WARNING: collation "en-US-x-icu" has version mismatch
DETAIL: The collation in the database was created using version 153.120, but the
operating system provides version 153.122.
HINT: Rebuild all objects affected by this collation and then use ALTER COLLATION
... REFRESH VERSION, or build PostgreSQL with the right library version.
```

This mechanism exists because a libc or ICU upgrade can silently reorder strings. That reordering corrupts B-tree indexes that were built under the old ordering. The correct remediation is to `REINDEX` affected indexes and then `ALTER COLLATION ... REFRESH VERSION` to update `collversion` in `pg_collation`. `pg_import_system_collations()` also refreshes versions in bulk when called via `ALTER DATABASE ... REFRESH COLLATION VERSION`.

## How collations flow to indexes

`pg_attribute.attcollation` stores a column's collation. When PostgreSQL creates an index on that column, it copies the collation OID into the index's operator class descriptor. This determines which comparison function the executor calls during both inserts and scans.

The planner will only use an index for a query predicate when the collation in the predicate matches the collation the index was built with. A query like `WHERE name = 'foo' COLLATE "C"` will not use an index built with `COLLATE "en-US-x-icu"`, even if the column default collation is ICU. This means mixing collations across predicates and DDL can silently fall back to sequential scans. `EXPLAIN` will show the mismatch if `collation` fields in the operator node differ from the index's collation.

Expressions can also carry collations. When computing `lower(col)` or `col || ' suffix'`, PostgreSQL derives a result collation using collation-precedence rules: explicit `COLLATE` clauses win, then column collations, then the database default. Conflicts between two implicit collations (for example, concatenating two columns with different collations) raise an error at parse time.

## Importing system collations

`initdb` and `ALTER DATABASE ... REFRESH COLLATION VERSION` both call `pg_import_system_collations()` (`pg_collation.c`). It enumerates collations from both libc (via `locale -a` output parsing or `nl_langinfo` iteration) and ICU (via `uloc_countAvailable()`) and calls `CollationCreate()` for each one with `if_not_exists = true` / `quiet = true`, so re-running is idempotent. This is how the hundreds of locale-named collations visible in `pg_catalog` arrive there without manual DDL.

## See also

- [[subsystems/catalog/core-catalogs]] — how system catalogs are bootstrapped and indexed
- [[subsystems/catalog/syscache]] — collation lookups go through `COLLNAMEENCNSP` and `COLLOID` cache entries
- [[subsystems/indexes/btree|btree index internals]] — how collation OIDs wire into comparison functions at the index level
- [[subsystems/types/variable-length-types|text and encoding]] — encoding and collation interaction for multibyte character sets

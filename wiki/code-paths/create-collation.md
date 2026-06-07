---
title: "CREATE COLLATION / ALTER COLLATION"
aliases:
  - CREATE COLLATION
  - ALTER COLLATION
  - collation commands
  - DefineCollation
  - AlterCollation
  - pg_import_system_collations
  - collation provider
  - ICU collation
source_files:
  - src/backend/commands/collationcmds.c
  - src/include/commands/collationcmds.h
  - src/include/catalog/pg_collation.h
symbols:
  - DefineCollation
  - AlterCollation
  - IsThereCollationInNamespace
  - pg_import_system_collations
  - pg_collation_actual_version
  - create_collation_from_locale
---

A collation defines the rules for sorting and comparing character strings — which characters are considered equal (case folding, accent stripping) and in what order they sort. PostgreSQL supports two collation providers: `libc`, which delegates to the operating system locale library, and `icu`, which uses the ICU library for richer Unicode-aware rules. `src/backend/commands/collationcmds.c` implements the collation management commands — `CREATE COLLATION`, `ALTER COLLATION`, and the helper function `pg_import_system_collations()`.

## CREATE COLLATION

`DefineCollation()` is the implementation of `CREATE COLLATION`. It accepts either a `FROM` clause (copy an existing collation) or an explicit set of locale parameters.

### Providers

**`libc` provider** (the default) uses the OS locale machinery. Two parameters determine its behavior:

- `LC_COLLATE` — controls sort order.
- `LC_CTYPE` — controls character classification (e.g. `isupper()`).

You can set both from a single `LOCALE` option, or specify them separately with `LC_COLLATE` and `LC_CTYPE`. `CREATE COLLATION` sets the encoding of the collation to the current database encoding. `check_encoding_locale_matches()` then verifies compatibility.

**`icu` provider** requires ICU to be compiled in. It uses a single `LOCALE` option, interpreted as a BCP 47 language tag or a POSIX-style locale name. PostgreSQL canonicalises the provided locale to a BCP 47 tag via `icu_language_tag()`. It emits a `NOTICE` if the canonical form differs from the input. ICU collations use encoding `-1` (encoding-independent). This means one ICU collation works for all encodings, as long as the database encoding is ICU-compatible.

### Nondeterministic Collations

Setting `DETERMINISTIC = false` creates a *nondeterministic* collation. In a nondeterministic collation, two strings that compare equal may still be physically distinct. For example, some German locale rules consider `ü` and `ue` equal. Case-insensitive rules consider `A` and `a` equal. This enables case-insensitive or accent-insensitive matching through the normal equality operator `=` without `ILIKE`.

PostgreSQL currently supports nondeterministic collations only with the ICU provider. The libc provider's locale facilities do not expose a way to separate comparison rules from string identity.

```sql
-- Case-insensitive, accent-insensitive collation using ICU
CREATE COLLATION ci_ai (
    PROVIDER = icu,
    LOCALE = 'und-u-ks-level1',  -- UCA sensitivity: base letters only
    DETERMINISTIC = false
);

-- Use it on a column
CREATE TABLE users (
    username text COLLATE ci_ai
);
```

### ICU Rules

The `RULES` option (ICU provider only) specifies additional tailoring rules in ICU rule syntax, allowing fine-grained customisation beyond what a locale tag expresses:

```sql
CREATE COLLATION numeric_sort (
    PROVIDER = icu,
    LOCALE = 'en',
    RULES = '& 9 < 10 < 11 < 12'  -- sort multi-digit numbers numerically
);
```

### Copying a Collation

`CREATE COLLATION name FROM existing_collation` copies all parameters of the source. `CREATE COLLATION` cannot copy the `default` collation. It uses `COLLPROVIDER_DEFAULT`, a sentinel for "use the database's collation". Creating a second entry with the same provider would break catalog lookups that special-case `DEFAULT_COLLATION_OID`.

### Collation Versioning

Each collation records a *version string* in `pg_collation.collversion`. This string captures the locale library version at creation time. If the ICU or libc library is upgraded and the sort order changes, the stored version will no longer match the current version returned by `pg_collation_actual_version()`. PostgreSQL detects this mismatch and warns that indexes built with the old collation may be corrupt.

```sql
-- Check for version mismatches
SELECT collname,
       collversion AS stored_version,
       pg_collation_actual_version(oid) AS current_version
FROM pg_collation
WHERE collversion IS NOT NULL
  AND collversion <> pg_collation_actual_version(oid);
```

## ALTER COLLATION REFRESH VERSION

`AlterCollation()` implements `ALTER COLLATION name REFRESH VERSION`. It reads the current version from the locale library and updates `pg_collation.collversion`. It emits a `NOTICE` either way, whether the version changed or not. This command does not fix any existing indexes. After upgrading the locale library, indexes on collatable columns may need to be rebuilt with `REINDEX`.

## pg_import_system_collations

`pg_import_system_collations(schema oid)` is a superuser-only function that populates `pg_collation` from the operating system's available locales. On Linux/macOS it runs `locale -a` through a pipe and creates one collation per locale. On Windows it calls `EnumSystemLocalesEx()`. It enumerates ICU locales via `uloc_countAvailable()`.

For libc locales, `pg_import_system_collations()` imports encoding-tagged variants like `en_US.utf8` under their full name. It also creates a shorter alias without the encoding tag (`en_US`) for convenience. It sorts aliases lexicographically before insertion. If multiple locale names would produce the same alias, this ensures a deterministic winner.

`initdb` calls the function when it initialises a new cluster. An administrator can call it again later to pick up newly installed locales.

## Related Topics

- [[subsystems/types/locale|Locale and Collation Internals]]
- [[subsystems/catalog/core-catalogs|Core System Catalogs]] — `pg_collation` table
- [[code-paths/create-index|CREATE INDEX]] — collations affect index sort order and correctness

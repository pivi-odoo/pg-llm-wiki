---
title: "name and \"char\" Types"
aliases:
  - name type
  - '"char" type'
  - NAMEDATALEN
  - single-byte char
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/name.c
  - src/backend/utils/adt/char.c
symbols:
  - namein
  - nameout
  - namecmp
  - namestrcpy
  - namestrcmp
  - charin
  - charout
  - chartoi4
  - i4tochar
  - NAMEDATALEN
---

PostgreSQL ships two narrow character types that are internal to the catalog machinery. Application developers encounter them whenever they query system tables: `name`, a fixed-width 64-byte string for object identifiers, and `"char"` (always written with double quotes to distinguish it from SQL's `CHAR`), a single-byte flag value. Neither type is intended for general application data, but understanding how they work explains several surprising behaviors — silent name truncation, flag comparisons with character literals, and the absence of collation overhead in catalog scans.

## The name Type and NAMEDATALEN

Every object that lives in the system catalog — tables, columns, indexes, functions, roles — has an identifier stored as `name`. The type is defined as a fixed-length, null-terminated byte array of exactly `NAMEDATALEN` bytes (64 bytes in standard builds, giving 63 usable characters plus the null terminator).

The fixed width is a deliberate storage trade-off. Catalog columns like `pg_class.relname` and `pg_attribute.attname` are accessed on virtually every query. If they were `text`, every read would require varlena header decoding and potentially [[subsystems/storage/toast|TOAST]] decompression. With `name`, catalog rows have predictable sizes. Code can extract the values with a simple pointer offset — no header stripping, no detoasting.

The kernel constant `NAMEDATALEN` is deliberately symbolic rather than a hard-coded literal. The source file `name.c` comments this explicitly: "DO NOT use hard-coded constants anywhere, always use NAMEDATALEN." A custom PostgreSQL build can increase it at compile time. But all downstream tools (libpq, pg_dump, JDBC drivers) must be recompiled to match. This is why the default of 64 bytes has been stable for decades.

## Silent Truncation on Input

The `namein` input function applies `pg_mbcliplen` to clip the input at `NAMEDATALEN - 1` bytes while respecting multibyte character boundaries. It does not raise an error — it silently discards excess bytes. This is the mechanism behind a well-known PostgreSQL behavior: `CREATE TABLE` with a name longer than 63 characters succeeds, but the stored name is shorter than what was specified. The binary receive path (`namerecv`) takes the opposite stance: it raises `ERRCODE_NAME_TOO_LONG` rather than silently truncating, because binary protocol callers must handle this themselves.

The practical implication is that two long names differing only after the 63rd byte are indistinguishable in the catalog. PostgreSQL emits a `NOTICE` at the DDL layer (in the parser/analyzer, not in `namein` itself) when this happens. But client settings can suppress the notice. So applications that generate identifiers programmatically must enforce their own length limits.

## Comparison and Collation

Name comparisons use C collation by default. The fast path in `namecmp` checks for `C_COLLATION_OID` and falls directly through to `strncmp`, avoiding locale machinery entirely. This is why catalog scans are fast even on databases configured with non-C locales: the catalog itself always compares names byte-for-byte.

When `name` values are compared in user queries with a non-C collation explicitly specified, the comparison delegates to the `varstr_cmp` infrastructure, the same path used by `text`. This is unusual and rarely intentional. Most code that works with `name` values relies on C-collation semantics.

The in-memory layout is always zero-padded to `NAMEDATALEN` bytes, allocated with `palloc0`. This means `memcmp` over the full 64 bytes is a valid equality test (used internally), while string operations terminate at the first null byte.

## The "char" Type

`"char"` (type OID 18 in `pg_type`) is a single signed byte stored without any header. On disk it occupies exactly one byte — there is no varlena wrapper, no alignment padding beyond what the surrounding tuple requires. This makes it the smallest possible catalog flag type.

The type is used throughout the system catalogs for enumeration columns:

- `pg_class.relkind` — `'r'` for ordinary table, `'i'` for index, `'v'` for view, `'S'` for sequence, and so on
- `pg_class.relpersistence` — `'p'` permanent, `'u'` unlogged, `'t'` temporary
- `pg_attribute.attidentity` — `'a'` always, `'d'` by default, or zero for no identity
- `pg_attribute.attgenerated` — `'s'` for stored generated columns

Querying these columns uses single-quoted character literals: `WHERE relkind = 'r'`. PostgreSQL implicitly casts the literal to `"char"` for the comparison.

## "char" vs char(1) and text

The distinction between `"char"` and the SQL-standard `char(1)` (`bpchar`) is significant:

| Property | `"char"` | `char(1)` / `bpchar` | `text` |
|---|---|---|---|
| Storage | 1 byte, no header | varlena with header (4+ bytes) | varlena with header |
| Collation | none | yes | yes |
| SQL standard | no | yes | no (but widely supported) |
| Blank-padding | no | yes | no |
| Intended use | catalog flags | user data | user data |

`"char"` has no collation because it is not a text type in the linguistic sense — it is a compact enumeration tag. Ordering comparisons treat the stored byte as `uint8` (unsigned), while integer conversion treats it as `int8` (signed). The source documents this asymmetry as a deliberate backwards-compatibility choice, not an oversight.

The input function `charin` accepts either a single character or a four-character octal escape (`\ooo`) for non-printable byte values. `charout` displays bytes with the high bit set (0x80–0xFF) in octal escape notation, matching the traditional bytea escape format.

## Where Developers Encounter These Types

```mermaid
graph TD
    A["System catalog column<br/>(pg_class, pg_attribute, ...)"] --> B{"Column purpose"}
    B -->|"Object name<br/>(relname, attname, nspname)"| C["name type<br/>64 bytes fixed-width"]
    B -->|"Flag / kind<br/>(relkind, relpersistence)"| D["\"char\" type<br/>1 byte, no header"]
    C --> E["Silent truncation at 63 chars<br/>C-collation comparisons"]
    D --> F["Compared with char literals<br/>relkind = 'r'"]
```

The most common surprise with `name` is the silent truncation. Code that dynamically constructs identifier names — migration tools, ORM schema generators, testing frameworks — must validate lengths before sending DDL. PostgreSQL will accept and truncate without error at the SQL level. Only the `NOTICE` message (if client_min_messages allows it) reveals what happened.

The most common surprise with `"char"` is the type mismatch when comparing catalog columns to `text` or `varchar` values. A query like `WHERE relkind = 'r'::text` will fail or produce unexpected results because `"char"` and `text` are not directly comparable without a cast. The correct form is either the bare literal `'r'` (which the parser coerces) or an explicit `'r'::"char"` cast.

The `nameconcatoid` function illustrates how PostgreSQL itself handles the truncation problem internally: when constructing `information_schema` `specific_name` values, it concatenates a name with an OID suffix and clips the name portion (not the OID) to ensure the suffix always fits. This is a pattern worth borrowing in application code that must fit identifiers into catalog-compatible lengths.

## Related Topics

- [[subsystems/types/base-types]]
- [[subsystems/types/variable-length-types]]
- [[subsystems/catalog/core-catalogs|System Catalog Overview]]
- [[subsystems/storage/toast]]

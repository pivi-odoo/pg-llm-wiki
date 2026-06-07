---
title: "OID Reference Types (regproc, regtype, regclass)"
aliases:
  - regproc
  - regtype
  - regclass
  - regprocedure
  - regoper
  - regoperator
  - regrole
  - regnamespace
  - regcollation
  - regdictionary
  - regconfig
source_files:
  - src/backend/utils/adt/regproc.c
symbols:
  - regprocin
  - regprocout
  - regprocedurein
  - regclassin
  - regclassout
  - regtypein
  - regtypeout
  - regrolein
  - regnamespacein
  - to_regclass
  - to_regtype
  - to_regproc
  - format_procedure_extended
  - format_operator_extended
  - parseNameAndArgTypes
  - stringToQualifiedNameList
---

The `reg*` family of types — `regproc`, `regprocedure`, `regtype`, `regclass`, `regoper`, `regoperator`, `regrole`, `regnamespace`, `regcollation`, `regconfig`, and `regdictionary` — are all binary-compatible with `Oid`, differing only in their text I/O routines. They exist so that PostgreSQL can write and display OID values as human-readable catalog names rather than raw numbers. This makes system catalog queries and extension code dramatically more readable. On input, each type resolves a name against the relevant catalog using the session's `search_path`. On output, it renders the OID back as the shortest unambiguous name visible in the current `search_path`.

## OID Wrappers With Name-Aware I/O

Every `reg*` type stores a plain 32-bit OID in exactly the same way `oid` does. The binary send/receive functions (`regprocrecv`, `regtyperecv`, etc.) simply delegate to `oidrecv`/`oidsend` (regproc.c). No additional representation exists on disk or in memory. The distinction is entirely in the text-format input and output functions.

This design means that `regclass` columns in system catalogs (like `pg_trigger.tgrelid` exposed through views, or the argument to `nextval`) are physically identical to an `Oid` column. The type system just routes I/O through different functions. This gives developers name-based access without paying any storage cost.

## Input: Name Resolution and Overload Disambiguation

All input functions follow the same three-step fallback:

1. If the input string is `-` (or `0` for `regoper`/`regoperator`), return `InvalidOid` (OID 0).
2. If the string is all digits, convert it directly to an OID via `oidin()`.
3. Otherwise, treat it as a possibly schema-qualified name and resolve it against the relevant catalog.

The third step is where the types diverge. `regclassin()` calls `RangeVarGetRelid()` with `NoLock` — it deliberately avoids locking because the caller may not have permissions on the relation and only needs the OID. `regtypein()` invokes the full type parser (`parseTypeString()`). This means it understands multi-keyword type names like `double precision` and array syntax like `integer[]`. It discards any typmod (regproc.c). `regprocin()` uses `FuncnameGetCandidates()`, but requires that exactly one function match the unqualified name. If the name is overloaded, it raises `ERRCODE_AMBIGUOUS_FUNCTION`.

`regprocedure` is the unambiguous sibling of `regproc`. Its input format is `funcname(argtype, argtype, ...)`. It calls `parseNameAndArgTypes()` to split the string at the first unquoted `(`. It then parses the argument type list via `parseTypeString()` for each element, and matches against `FuncnameGetCandidates()` by exact argument OID comparison (regproc.c). This makes it the correct choice anywhere overloaded functions must be referenced unambiguously.

`regoperator` works analogously for operators, accepting `+(integer,integer)` style syntax. Unary operators use `NONE` for the missing operand, as in `-(NONE,integer)`.

During bootstrap processing, all `reg*` input functions require numeric OID strings. The name-resolution machinery depends on syscaches and the namespace system, which are not yet initialized at bootstrap time.

## Output: Search-Path-Sensitive Qualification

The output functions share a common invariant: qualify the name if and only if the corresponding input function would not find it uniquely using the current `search_path`. This is not a formatting choice but a roundtrip correctness guarantee.

For `regprocout()`, the output calls `FuncnameGetCandidates()` with the bare function name and checks whether exactly one result comes back with the same OID. If not — either because another function with the same name exists in an earlier schema, or the function's schema is not in `search_path` — it prepends the schema name using `quote_qualified_identifier()` (regproc.c). `regclassout()` uses `RelationIsVisible()` for the same purpose.

When no catalog row exists for the OID (the object was dropped after the OID was stored), every output function falls back to printing the raw numeric OID. This matches the input convention: input functions always accept numeric OIDs, so the value remains valid even when the referenced object is gone.

`format_procedure_extended()` and `format_operator_extended()` expose the regprocedure/regoperator formatting logic to other backend modules via flags:

| Flag | Effect |
|---|---|
| `FORMAT_PROC_INVALID_AS_NULL` | Return NULL instead of numeric OID for dangling references |
| `FORMAT_PROC_FORCE_QUALIFY` | Always schema-qualify, ignore `search_path` |
| `FORMAT_OPERATOR_INVALID_AS_NULL` | Same as above for operators |
| `FORMAT_OPERATOR_FORCE_QUALIFY` | Always schema-qualify operators |

DDL deparsing and `pg_dump`-style output use these flags where the target `search_path` is unknown.

## Safe Lookup: the `to_reg*` Functions

Every `reg*` type has a corresponding `to_reg*()` SQL function (`to_regclass`, `to_regtype`, `to_regproc`, etc.) that returns NULL on lookup failure instead of raising an error. Internally these use `DirectInputFunctionCallSafe()` with an `ErrorSaveContext` node to trap errors (regproc.c). This is the correct approach in extension code that needs to test whether an object exists:

```sql
SELECT to_regclass('myschema.mytable') IS NOT NULL;
```

The casting approach `'myschema.mytable'::regclass` raises an error when the table does not exist, which forces the use of exception handlers. `to_regclass()` avoids that cost.

## The Full Family

Each type maps to a distinct catalog:

| Type | Catalog | Sentinel for InvalidOid | Disambiguation |
|---|---|---|---|
| `regproc` | `pg_proc` | `-` | Requires unique match by name |
| `regprocedure` | `pg_proc` | `-` | Name + argument type list |
| `regoper` | `pg_operator` | `0` | Requires unique match by name |
| `regoperator` | `pg_operator` | `0` | Name + operand types (`NONE` for unary) |
| `regclass` | `pg_class` | `-` | Name via `RangeVarGetRelid` |
| `regtype` | `pg_type` | `-` | Full type parser including array syntax |
| `regcollation` | `pg_collation` | `-` | Name via `get_collation_oid` |
| `regconfig` | `pg_ts_config` | `-` | Name via `get_ts_config_oid` |
| `regdictionary` | `pg_ts_dict` | `-` | Name via `get_ts_dict_oid` |
| `regrole` | `pg_authid` | `-` | Single-component name; no schema |
| `regnamespace` | `pg_namespace` | `-` | Single-component name |

`regrole` and `regnamespace` explicitly reject schema-qualified names (more than one dot-separated component) because roles and schemas are global and not themselves namespaced. The input functions call `list_length(names) != 1` and raise `ERRCODE_INVALID_NAME` if more components are present (regproc.c).

## Implicit Cast: text to regclass

`text_regclass()` implements an implicit cast from `text` to `regclass`. It exists specifically to support legacy call forms of `nextval(text)`, `currval(text)`, and similar sequence functions that predate the `regclass` type. Unlike the normal `regclassin()` path, it does not go through `ErrorSaveContext` and always throws on failure. New code should use `regclass` literals directly rather than relying on this implicit cast.

## Catalog Dependency

Because `reg*` types resolve names at input time, the OID stored in a column is stable even if the object is later renamed. PostgreSQL resolved the old name when it stored the value, and renaming the object does not change its OID. However, dropping the object leaves a dangling OID. The dependency tracking system (which records object dependencies in `pg_depend`) does not automatically know about `reg*` values stored in user tables, so extension authors who store `regclass` or `regtype` values should manually record dependencies or document the constraint.

The [[subsystems/catalog/core-catalogs|core system catalogs]] (`pg_proc`, `pg_class`, `pg_type`, and their siblings) are the actual resolution targets. Every output function uses syscache lookups (`SearchSysCache1(PROCOID, ...)`, `SearchSysCache1(RELOID, ...)`, etc.) to translate OIDs back to names efficiently.

## Related Topics

- [[subsystems/catalog/core-catalogs|Core System Catalogs]] — pg_proc, pg_class, pg_type, pg_operator and their indexes
- [[subsystems/catalog/syscache|Syscache]] — per-backend hash cache used by all reg* I/O functions for OID-to-name resolution
- [[subsystems/catalog/relcache|Relcache]] — relation descriptor cache; regclass resolution avoids locking via RangeVarGetRelid

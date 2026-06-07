---
title: "Type Resolution in the Analyzer"
aliases:
  - parse_type
  - TypeName lookup
  - type modifier resolution
source_files:
  - src/backend/parser/parse_type.c
symbols:
  - LookupTypeNameExtended
  - LookupTypeName
  - typenameType
  - typenameTypeId
  - typenameTypeIdAndMod
  - typenameTypeMod
  - LookupTypeNameOid
  - GetColumnDefCollation
  - LookupCollation
  - typeStringToTypeName
  - parseTypeString
  - TypeName
---

Type resolution in the analyzer is the process of converting a `TypeName` parse-tree node — a raw list of schema-qualified name strings or a `%TYPE` reference — into a live `pg_type` syscache tuple plus a resolved type modifier (`typmod`). Every DDL statement that names a data type, every `CAST`, every column definition, and every parameter declaration passes through this machinery before the analyzer can attach OID and typmod information to query nodes.

## The TypeName Node and Its Three Forms

The `TypeName` struct (defined in `src/include/nodes/parsenodes.h`) can represent a type in three distinct ways. `LookupTypeNameExtended()` handles each differently:

**Pre-cooked OID.** When the backend constructs a `TypeName` internally — for example, when expanding a rule or building an implicit cast — it sets `names = NIL`. It stores the type OID directly in `typeOid`. The lookup path is a single `SearchSysCache1(TYPEOID, ...)` call with no name resolution at all.

**Normal name reference.** The common case: `names` is a list of string `Value` nodes (one element for an unqualified name, two for `schema.type`). If a schema is given, `LookupExplicitNamespace()` resolves the schema name to an OID. `LookupTypeNameExtended()` then locates the type via the `TYPENAMENSP` syscache entry (keyed on `typname` + `typnamespace`). If no schema is given, `TypenameGetTypidExtended()` walks the `search_path` one namespace at a time, calling `GetSysCacheOid2(TYPENAMENSP, ...)` until it finds a match or exhausts the path. This mirrors the same search-path logic used for functions and operators.

**`%TYPE` reference.** When `pct_type` is true, the name list encodes `relation.column` (or `schema.relation.column`). The analyzer calls `RangeVarGetRelid()` and then `get_attnum()` / `get_atttype()` to resolve the column's type. The analyzer emits a `NOTICE` with the resolved concrete type name. It then replaces the `%TYPE` form with an ordinary OID going forward. The analyzer takes no lock on the relation at this point. As a result, a concurrent `ALTER TABLE ... ALTER COLUMN TYPE` can cause the lookup to return a stale type — a known limitation noted in the code.

After any of these three paths produces a type OID, `LookupTypeNameExtended()` checks whether `typeName->arrayBounds` is non-nil. If so, it calls `get_array_type()` to substitute the array type OID, reflecting SQL's `integer[]` syntax.

## Shell Types and the typisdefined Guard

A **shell type** is a forward-declared type — created by `CREATE TYPE name` before the full `CREATE TYPE name AS ...` definition — whose `pg_type` row exists but has `typisdefined = false`. The lower-level function `LookupTypeNameExtended()` returns such a tuple without complaint, relying on callers to check `typisdefined` themselves. The higher-level wrappers enforce this contract:

- `typenameType()` raises `ERRCODE_UNDEFINED_OBJECT` with the message "type is only a shell" if `typisdefined` is false.
- `LookupTypeNameOid()` returns the OID even for shells — intentionally, since some DDL contexts need to reference not-yet-defined types.
- `typenameTypeId()` and `typenameTypeIdAndMod()` both call through `typenameType()` and therefore reject shells.

The rule of thumb: code that needs a fully usable type (expression analysis, column definitions) should call `typenameType()` or `typenameTypeId()`. Code that is building catalog entries and merely needs to record the OID may call `LookupTypeNameOid()` and accept the risk.

## Type Modifier Resolution

A `typmod` is a 32-bit integer stored alongside a type OID that carries type-specific precision or length constraints — `varchar(100)` encodes `100 + VARHDRSZ` as its typmod, `numeric(10,2)` encodes scale and precision in a packed form, and types without constraints use `-1`.

When a `TypeName` carries modifier expressions in its `typmods` list (the raw grammar output for the parenthesized arguments in `varchar(100)`), `typenameTypeMod()` converts them to an internal value through a two-step process:

1. `typenameTypeMod()` serialises the modifier expressions to C strings and assembles them into a `cstring[]` array. The grammar restricts these expressions to integer constants, float constants, string literals, and bare identifiers.
2. `typenameTypeMod()` passes that array to the type's `typmodin` function (stored in `pg_type.typmodin`). `typmodin` interprets the strings and returns the packed `int32` modifier.

If the type has no `typmodin` function (`typmodin = InvalidOid`), any attempt to supply modifiers raises an error. This is how the system enforces that `int4(3)` is not legal SQL even though `integer` is a valid type name.

The `typmod` value flows from `typenameTypeMod()` back up through `LookupTypeNameExtended()` into its `typmod_p` output parameter. Callers like `typenameTypeIdAndMod()` then embed the typmod in `Const`, `Var`, or column-definition nodes so that downstream operators and coercions can enforce length limits at runtime.

## Collation Resolution for Column Definitions

`GetColumnDefCollation()` resolves the effective collation for a column definition, layering three sources:

| Priority | Source |
|---|---|
| Highest | Explicit `COLLATE clause` on the column (`LookupCollation()` resolves by name) |
| Middle | Pre-cooked `collOid` already attached to the `ColumnDef` (from inherited or altered columns) |
| Lowest | The type's own default collation (`get_typcollation(typeOid)`) |

If `GetColumnDefCollation()` finds a collation but the type's `typcollation` is zero (meaning the type is not collatable, e.g., `integer`), it raises `ERRCODE_DATATYPE_MISMATCH`. This is the error path behind "collations are not supported by type integer".

## Parsing Type Names from Strings

`typeStringToTypeName()` handles the case where a type name arrives as a plain SQL string — for example, in `format_type()` reverse lookups, catalog entries stored as text, or extension code that builds type references dynamically. It invokes `raw_parser()` with the `RAW_PARSE_TYPE_NAME` mode, which runs the grammar in a restricted mode that accepts only a type name expression. `typeStringToTypeName()` returns the resulting `TypeName` node. It explicitly rejects the `SETOF` modifier since that concept does not belong outside function return-type contexts.

`parseTypeString()` chains `typeStringToTypeName()` with a full `LookupTypeName()` call to produce both an OID and a typmod from a string. It can optionally propagate errors through an `ErrorSaveContext` rather than throwing, giving callers soft-error semantics.

## Syscache Lifecycle

All functions in `parse_type.c` that return a `Type` (a typedef for `HeapTuple`) acquire a syscache reference via `SearchSysCache1(TYPEOID, ...)`. Callers are responsible for calling `ReleaseSysCache()` on the returned tuple when done. The comments in `LookupTypeNameExtended()` call this out explicitly: forgetting to release will pin the cache entry and cause memory to grow unboundedly under DDL-heavy workloads. The convenience functions `typenameTypeId()` and `typenameTypeIdAndMod()` release the cache internally and return only the scalar values, eliminating this concern for the common case. PostgreSQL draws these tuples from [[subsystems/catalog/core-catalogs|pg_type]], the central type catalog.

## Key Function Reference

| Function | Returns | Notes |
|---|---|---|
| `LookupTypeNameExtended()` | `Type` (HeapTuple) | Core lookup; caller must check `typisdefined` and call `ReleaseSysCache` |
| `LookupTypeName()` | `Type` | Thin wrapper for `LookupTypeNameExtended` with `temp_ok=true` |
| `typenameType()` | `Type` | Asserts `typisdefined`; raises error for missing or shell types |
| `typenameTypeId()` | `Oid` | Releases cache internally; most common call site |
| `typenameTypeIdAndMod()` | void (out params) | Returns OID + typmod; releases cache |
| `LookupTypeNameOid()` | `Oid` | Accepts shells; used in DDL contexts |
| `typenameTypeMod()` | `int32` | Calls `typmodin` function; static, used internally |
| `GetColumnDefCollation()` | `Oid` | Resolves effective collation for a `ColumnDef` |
| `typeStringToTypeName()` | `TypeName *` | Parses SQL type string via `raw_parser` |
| `parseTypeString()` | `bool` | Full string → OID + typmod; soft-error capable |

## Related Topics

- [[subsystems/parser/semantic-analysis|Semantic analysis]] — the broader analyzer pipeline that calls these functions when processing column definitions, casts, and parameter types
- [[subsystems/catalog/core-catalogs|Core system catalogs]] — `pg_type` schema and syscache access patterns used by all lookup functions here

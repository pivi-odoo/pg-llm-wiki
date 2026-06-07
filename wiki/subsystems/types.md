---
title: "Type System"
aliases:
  - "PostgreSQL Types"
  - "pg_type"
  - "Type Input/Output"
  - "Type Coercion"
  - "Operator Classes"
  - "TypeCacheEntry"
tags:
  - theme/caching
source_files:
  - src/backend/utils/cache/typcache.c
  - src/backend/utils/adt/
  - src/backend/parser/parse_coerce.c
  - src/backend/catalog/pg_type.c
  - src/include/utils/typcache.h
  - src/include/catalog/pg_type.h
  - src/include/catalog/pg_cast.h
  - src/include/catalog/pg_opfamily.h
symbols:
  - TypeCacheEntry
  - lookup_type_cache
  - pg_type
  - pg_cast
  - pg_opfamily
  - pg_opclass
  - pg_operator
  - pg_proc
  - coerce_to_target_type
  - find_coercion_pathway
  - InputFunctionCall
  - OutputFunctionCall
  - FunctionCallInvoke
---

# Type System

Every value in PostgreSQL has a type identified by an OID in `pg_type`. The type system defines how values are parsed from text, serialised to text, stored on disk, compared, hashed, and coerced. It is the foundation on which operators, indexes, and extensions build.

## pg_type catalog

| Column | Type | Purpose |
|---|---|---|
| `oid` | `oid` | Type OID — the universal identifier used everywhere |
| `typname` | `name` | Type name (e.g. `int4`, `text`, `float8`) |
| `typnamespace` | `oid` | Schema OID |
| `typlen` | `int2` | Fixed byte length; -1 = variable-length (varlena); -2 = C string |
| `typbyval` | `bool` | If true, Datums are passed by value (fits in a pointer); otherwise by reference |
| `typtype` | `char` | `b`=base, `c`=composite, `d`=domain, `e`=enum, `p`=pseudo, `r`=range, `m`=multirange |
| `typcategory` | `char` | Implicit coercion category: `N`=numeric, `S`=string, `D`=datetime, `A`=array, etc. |
| `typispreferred` | `bool` | Preferred target within its category for implicit coercion |
| `typinput` | `regproc` | Input function: `cstring → Datum` |
| `typoutput` | `regproc` | Output function: `Datum → cstring` |
| `typreceive` | `regproc` | Binary input function: `bytea → Datum` |
| `typsend` | `regproc` | Binary output function: `Datum → bytea` |
| `typmodin` | `regproc` | Type modifier input (e.g. `varchar(n)` → int array) |
| `typmodout` | `regproc` | Type modifier output (int array → display string) |
| `typanalyze` | `regproc` | Custom ANALYZE function; 0 = use default |
| `typalign` | `char` | Alignment: `c`=1, `s`=2, `i`=4, `d`=8 bytes |
| `typstorage` | `char` | TOAST strategy: `p`=plain, `e`=external, `m`=main, `x`=extended |
| `typnotnull` | `bool` | Domain NOT NULL constraint |
| `typbasetype` | `oid` | For domains: OID of the base type |
| `typndims` | `int4` | For array types: number of dimensions (usually 0 = unconstrained) |
| `typelem` | `oid` | For array types: OID of element type |
| `typarray` | `oid` | OID of the array type for this type; 0 if none |

## Datum representation

All values in the executor flow as `Datum` (a `uintptr_t`). Whether a `Datum` holds a value or a pointer depends on `typbyval` and `typlen`:

| `typbyval` | `typlen` | Datum holds |
|---|---|---|
| `true` | 1, 2, 4 (or 8 on 64-bit) | The value itself, in the least significant bytes |
| `false` | > 0 | Pointer to a fixed-size palloc'd or stack-allocated struct |
| `false` | -1 | Pointer to a `varlena` struct (4-byte or 1-byte header + data) |
| `false` | -2 | Pointer to a null-terminated C string |

The `GETARG_*` and `RETURN_*` macros in `fmgr.h` hide this encoding from function authors.

## Input / output functions

Every base type has a pair of I/O functions that convert between the text wire format and the internal `Datum` representation.

### Input function

Called by `InputFunctionCall(flinfo, str, typioparam, typmod)` (`fmgr.c`):

```c
/* Example: int4in */
Datum int4in(PG_FUNCTION_ARGS)
{
    char *inputText = PG_GETARG_CSTRING(0);
    long result = strtol(inputText, &endptr, 10);
    /* validate, then: */
    PG_RETURN_INT32((int32) result);
}
```

Input functions are called when:
- A literal constant in SQL is cast to a type: `'42'::integer`
- A string column is read from a CSV file (`COPY FROM`)
- A client sends a parameter in text format (extended query protocol)

### Output function

Called by `OutputFunctionCall(flinfo, val)`:

```c
/* Example: int4out */
Datum int4out(PG_FUNCTION_ARGS)
{
    int32 arg = PG_GETARG_INT32(0);
    char *result = palloc(12);
    snprintf(result, 12, "%d", arg);
    PG_RETURN_CSTRING(result);
}
```

### Binary I/O (receive / send)

`typreceive` / `typsend` convert between the internal format and a binary wire format. They are used when the client negotiates binary parameter or result encoding (libpq `PQexecParams` with `resultFormat=1`). Binary I/O avoids the text serialisation round-trip, which is significant for large numeric arrays.

## Type modifiers

`typmodin` and `typmodout` support length/precision constraints like `VARCHAR(50)` or `NUMERIC(10,2)`. PostgreSQL stores the modifier as an `int4` or small `int4[]` alongside the column's type OID in `pg_attribute.atttypmod`. The input function receives `typmod` as its third argument and enforces the constraint.

## Type categories and coercion

### Implicit vs explicit coercion

PostgreSQL distinguishes three coercion contexts:
- **Implicit**: allowed without any cast syntax. `1 + 1.5` implicitly promotes `int4` to `float8`.
- **Assignment**: applied silently when inserting or updating. `INSERT INTO t(int_col) VALUES (1.9)` truncates via assignment cast.
- **Explicit**: requires `CAST(x AS type)` or `x::type`.

### pg_cast

Every allowed type conversion has a row in `pg_cast`:

| Column | Meaning |
|---|---|
| `castsource` | Source type OID |
| `casttarget` | Target type OID |
| `castfunc` | Conversion function OID (0 = binary compatible) |
| `castcontext` | `i`=implicit, `a`=assignment, `e`=explicit-only |
| `castmethod` | `f`=function, `i`=I/O coercion, `b`=binary compatible |

`find_coercion_pathway()` (`parse_coerce.c`) queries `pg_cast` to find the conversion. `coerce_to_target_type()` wraps the conversion in a `FuncExpr` or `RelabelType` node depending on the method.

### Binary-compatible casts

A cast with `castmethod = 'b'` and `castfunc = 0` means the internal representation is identical. `int2` and `int4` are NOT binary-compatible (different widths), but `varchar` and `text` are. Binary-compatible casts produce a `RelabelType` node in the plan, which has zero runtime cost.

### Implicit promotion for operators

When the parser resolves an operator (e.g., `+`), it calls `oper()` → `OpernameGetOprid()` → `oper_select_candidate()`. If the argument types don't exactly match any `pg_operator` row, the parser tries implicit coercions. The `typcategory` and `typispreferred` flags guide the choice: numeric types prefer `float8`; string types prefer `text`.

## Operator classes and families

Index access methods require types to support comparison and/or hashing. This is encoded in:

- **`pg_opfamily`**: a named set of semantically compatible operators and support functions. Example: `integer_ops` for B-tree on integer types.
- **`pg_opclass`**: binds an access method to a type using a specific operator family. `int4_ops` for `btree` on `int4`.
- **`pg_amop`**: operators in a family (e.g., `<`, `<=`, `=`, `>=`, `>` for btree).
- **`pg_amproc`**: support functions (e.g., comparison function, hash function).

An operator class for btree must provide support function 1 (`compare`): a function returning negative/zero/positive. For hash indexes, support function 1 (`hashfunc`) must return `int4`. `lookup_type_cache()` looks these up.

## TypeCacheEntry

`lookup_type_cache(typeOid, flags)` (`typcache.c`) returns a `TypeCacheEntry` — a backend-local cache of frequently needed type metadata:

```c
typedef struct TypeCacheEntry
{
    Oid     type_id;
    int16   typlen;
    bool    typbyval;
    char    typalign;
    char    typstorage;
    char    typtype;
    Oid     typrelid;       /* for composite types */
    Oid     typelem;        /* for array types */
    Oid     typarray;

    /* Comparison and hashing support (loaded on demand) */
    Oid     btree_opf;      /* default btree operator family OID */
    Oid     btree_opintype;
    Oid     hash_opf;
    Oid     hash_opintype;

    /* Equality, less-than, comparison functions */
    FmgrInfo eq_opr_finfo;
    FmgrInfo lt_opr_finfo;
    FmgrInfo cmp_proc_finfo;
    FmgrInfo hash_proc_finfo;
    FmgrInfo hash_extended_proc_finfo;
    ...
} TypeCacheEntry;
```

`flags` is a bitmask (`TYPECACHE_EQ_OPR`, `TYPECACHE_CMP_PROC`, `TYPECACHE_HASH_PROC`, …). It controls which fields are populated on first access. Executor nodes use `TypeCacheEntry` heavily to avoid repeated catalog lookups for comparison functions.

## Composite types

A composite type (`typtype = 'c'`) represents a row structure. Each relation (table, view, sequence) automatically has a corresponding composite type. Composite types store their column definitions in `pg_attribute` (keyed by `typrelid`). `TypeCacheEntry.tupDesc` holds the `TupleDesc` for the composite type, loaded on demand.

## Domain types

A domain (`typtype = 'd'`) is a named alias for a base type with optional constraints (`NOT NULL`, `CHECK`). `typbasetype` points to the underlying type. `domain_check()` checks domains at assignment time. It evaluates the domain's `CHECK` constraints stored in `pg_constraint`.

## Enum types

An enum (`typtype = 'e'`) has its values listed in `pg_enum`. Enum comparison uses the `enumsortorder` float4 field from `pg_enum` for ordering. Adding a new enum value with `ALTER TYPE ... ADD VALUE` inserts a new `pg_enum` row without rewriting tables.

## Range and multirange types

A range type (`typtype = 'r'`) is defined by its element type, a canonical function, a subtype difference function, and a btree operator class for the element type. `RangeTypeInfo` (in `typcache.c`) caches this metadata. Multirange types (`typtype = 'm'`, PG 14+) wrap a sorted array of non-overlapping range values.

## See also

- [[subsystems/catalog/core-catalogs]] — pg_type, pg_cast, pg_opfamily, pg_opclass schema
- [[subsystems/parser/semantic-analysis]] — where type resolution and coercion decisions are made
- [[subsystems/planner/statistics]] — type-specific statistics and histogram bounds
- [[subsystems/indexes/index-am]] — how operator classes connect types to index access methods
- [[subsystems/storage/toast]] — varlena storage and the TOAST mechanism for large values

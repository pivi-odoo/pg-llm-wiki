---
title: "Base Types (CREATE TYPE)"
aliases:
  - "custom base types"
  - "user-defined types"
  - "shell types"
tags:
  - theme/extensibility
source_files:
  - src/backend/commands/typecmds.c
  - src/include/catalog/pg_type.h
symbols:
  - DefineType
  - TypeCreate
  - TypeShellMake
---

# Base Types (CREATE TYPE)

A base type is a fundamental type. C code defines its internal representation. Unlike composite types, domains, enums, and ranges — which are built on existing types — a base type specifies its own memory layout, I/O functions, and storage properties. Defining one is primarily an extension author's task. It also requires superuser privilege.

## The shell type bootstrap

Creating a base type requires I/O functions whose SQL declarations reference the type name. But the type does not exist yet when PostgreSQL creates those functions. PostgreSQL resolves this chicken-and-egg problem with a *shell type*: a placeholder `pg_type` row with `typisdefined = false`. This shell type lets an author declare functions before the type is fully defined.

The sequence for defining a base type is always:

```sql
CREATE TYPE mytype;                       -- create the shell
CREATE FUNCTION mytype_in(cstring) RETURNS mytype ...;
CREATE FUNCTION mytype_out(mytype) RETURNS cstring ...;
CREATE TYPE mytype (                       -- fill in the definition
    INPUT  = mytype_in,
    OUTPUT = mytype_out,
    INTERNALLENGTH = 4
);
```

`CREATE TYPE mytype` with no parameters calls `TypeShellMake()`, which inserts a `pg_type` row with all fields at their null or zero defaults except `typisdefined = false`. The full `CREATE TYPE ... (...)` call to `DefineType()` finds this shell row. It fills in the row and sets `typisdefined = true`. Sometimes no shell exists when PostgreSQL reaches the full definition. In that case, `DefineType()` creates the shell implicitly before filling it in.

## Memory layout parameters

The most important parameters describe how values of the type are represented in memory and on disk.

**`INTERNALLENGTH`**: The byte length of one value in memory. Set to a positive integer for fixed-width types, to `-1` for variable-length types (varlena), or to `-2` for null-terminated C strings. Fixed-width types have their entire value in the datum. Variable-length types begin with a 4-byte length header.

**`PASSEDBYVALUE`**: If true, values fit in a `Datum` (at most 8 bytes on 64-bit platforms). PostgreSQL copies them directly into function argument slots. Types with `INTERNALLENGTH > sizeof(Datum)` cannot use pass-by-value. PostgreSQL passes most small fixed-width types (`int4`, `float8`) by value.

**`ALIGNMENT`**: The byte alignment required when the value is stored on a heap page. Values are `'c'` (1 byte), `'s'` (2 bytes), `'i'` (4 bytes, the default), or `'d'` (8 bytes). The alignment must match what the C representation requires. A misaligned datum causes undefined behaviour on architectures that enforce alignment.

**`STORAGE`**: Controls TOAST behaviour for variable-length types.
- `PLAIN` — no TOAST; the value is always stored inline. Required for fixed-width types.
- `EXTENDED` — the default for varlena; the value may be compressed or moved out-of-line to the TOAST table.
- `EXTERNAL` — may be stored out-of-line but not compressed.
- `MAIN` — stored inline if possible, compressed before moving out-of-line.

## I/O and conversion functions

Every base type must have at least `INPUT` and `OUTPUT` functions.

**`INPUT`** (`cstring → mytype`): Takes the text representation as a null-terminated C string and returns the internal representation. The function receives two additional hidden arguments: the type's OID (for polymorphic types) and a `typmod` value. If the input is invalid, it should call `ereport(ERROR, ...)`.

**`OUTPUT`** (`mytype → cstring`): Takes the internal representation and returns a palloc'd null-terminated string.

**`RECEIVE`** (`internal → mytype`) and **`SEND`** (`mytype → bytea`): Optional binary I/O functions used by the binary copy protocol and `COPY BINARY`. Without these, binary transfer falls back to text I/O.

**`TYPMOD_IN`** (`cstring[] → int4`) and **`TYPMOD_OUT`** (`int4 → cstring`): Required only if the type supports a type modifier — a compile-time parameter like the precision in `numeric(10, 2)`. `TYPMOD_IN` converts the list of modifier values to a packed integer. `TYPMOD_OUT` reverses this for display. PostgreSQL stores the packed `typmod` integer in `pg_attribute.atttypmod` for typed columns.

**`ANALYZE`**: An optional custom statistics-collection function called by `ANALYZE` instead of the default. If omitted, `ANALYZE` computes a histogram and most-common-values using the type's comparison and equality operators.

**`SUBSCRIPT`**: An optional custom subscript handler function, used to implement non-array subscripting (e.g., `jsonb` field access via subscript syntax). PostgreSQL 14 added this option.

## Category, delimiter, and default value

**`CATEGORY`** assigns the type to a category that influences implicit casting priority. The built-in categories include `'S'` (string), `'N'` (numeric), `'D'` (datetime), `'U'` (user-defined, the default). The query planner uses category to choose among competing implicit casts.

**`PREFERRED`** (`true`/`false`): If true, this type is preferred within its category for implicit casts. An extension author should mark at most one type per category as preferred. Only built-in types like `float8` use the value `true`.

**`DELIMITER`**: The single ASCII character used to separate values in arrays of this type when rendered as text. Defaults to comma.

**`DEFAULT`**: The default value for columns of this type that have no explicit default. Stored as a text string that the INPUT function converts.

**`ELEMENT`**: If set, this type is an array of the specified element type. Setting `ELEMENT = mybasetype` makes the new type behave as an array whose elements are `mybasetype`. In practice, PostgreSQL creates array types automatically — extension authors rarely need to set this manually.

**`COLLATABLE`**: If true, the type supports collations. The I/O functions and operators must respect the collation OID passed through `PG_GET_COLLATION()`. Most scalar types are not collatable. Text-like types are.

## The LIKE clause

The `LIKE othertype` clause copies `INTERNALLENGTH`, `PASSEDBYVALUE`, `ALIGNMENT`, and `STORAGE` from another type. This is a shortcut for creating a type with the same physical layout as an existing one but different I/O semantics or operators. The author does not need to know the exact numeric values.

## Catalog effects

`DefineType()` calls `TypeCreate()` to insert the `pg_type` row. Simultaneously, PostgreSQL creates an implicit array type named `_mytype` with its own `pg_type` row. This array type uses the array I/O functions. It references `mytype` as its `typelem`. PostgreSQL allocates the array OID first, so it can store it in the base type's `typarray` column. This makes the reverse link available to the type cache.

After `CREATE TYPE` completes, the type is usable but has no operators or index support. To make it fully functional, the extension author typically also creates:
- Equality and ordering operators, registered in an operator class for B-tree and hash access methods.
- Aggregate functions that operate on the type.
- Cast functions to and from related types.

## See also

- [[subsystems/types/domains]] — a type built on an existing base type with added constraints
- [[subsystems/types/enum-internals]] — a simpler user-defined type form
- [[subsystems/storage/toast]] — how EXTENDED and EXTERNAL storage work for varlena types
- [[subsystems/catalog/core-catalogs]] — pg_type fields and typisdefined

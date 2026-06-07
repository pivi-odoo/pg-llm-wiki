---
title: "Composite Types"
aliases:
  - "Row Types"
  - "Record Types"
  - "RECORD"
source_files:
  - src/backend/commands/typecmds.c
  - src/backend/utils/adt/rowtypes.c
  - src/backend/utils/cache/typcache.c
  - src/include/catalog/pg_type.h
symbols:
  - DefineCompositeType
  - lookup_rowtype_tupdesc
  - BlessTupleDesc
  - record_in
  - record_out
---

# Composite Types

A composite type is a structured type whose values are ordered sequences of named, typed fields — in other words, a row. PostgreSQL has three overlapping forms of composite type: named composite types created with `CREATE TYPE ... AS (...)`, the implicit row type that every table or view carries, and anonymous record types whose structure is known only at runtime.

## Named composite types and table row types

Every table in PostgreSQL has a corresponding composite type in `pg_type`. When you create a table, the system first allocates a `pg_type` entry with `typtype = 'c'`. It then creates the table. The `typrelid` column in that `pg_type` row points to the table's `pg_class` entry. The system stores the column definitions as `pg_attribute` rows on that same `pg_class` entry. The composite type and the table share physical catalog storage.

`CREATE TYPE foo AS (x int, y text)` works the same way. `DefineCompositeType()` (`typecmds.c`) calls `DefineRelation()` with `RELKIND_COMPOSITE_TYPE`, creating a `pg_class` entry with no heap pages. It then records the `pg_type` entry. A standalone composite type behaves identically to a table's row type at the catalog level. The difference is purely in whether heap storage and indexes exist.

This sharing has consequences. `ALTER TABLE` changes that affect column layout — adding, dropping, or reordering columns — also change the composite type visible to functions and queries that reference it. `ALTER TYPE ... ADD ATTRIBUTE` on a named composite type is permitted. It modifies the backing `pg_class` entry the same way. You cannot `ALTER TYPE ... ADD ATTRIBUTE` on a table's row type directly. Use `ALTER TABLE` instead.

## The type cache and TupleDesc

Any code that needs to work with a composite value at runtime goes through `lookup_rowtype_tupdesc()` (`typcache.c`). This function returns a `TupleDesc` — a descriptor of the field names, types, and attrnums. For named composite types, PostgreSQL derives the `TupleDesc` from the `pg_attribute` rows of the backing `pg_class` entry. It caches the `TupleDesc` in the type cache entry keyed by `pg_type` OID. `syscache` invalidation messages invalidate the cache when the table or composite type definition changes.

For anonymous record types the lookup is different (see below).

## Anonymous record types

SQL functions that return `RECORD` or PL/pgSQL functions that use `RECORD` variables deal with composite values whose column structure is unknown at planning time. PostgreSQL assigns these a special type OID of `RECORDOID`. It distinguishes different structures using a runtime typmod — a small integer allocated at execution time.

PostgreSQL maintains the typmod-to-`TupleDesc` mapping in two places: `RecordCacheArray`, a session-local array indexed by typmod, and a `SharedRecordTypmodRegistry` in shared memory. The registry is used when records cross session boundaries, for example through parallel workers or logical replication. When a function like `json_to_record()` or a SRF constructs a row with a known-at-runtime structure, it calls `BlessTupleDesc()` (`funcapi.c`) on its `TupleDesc`. `BlessTupleDesc()` searches for an existing typmod with the same column layout, or allocates a new one. It stamps the `TupleDesc` with the typmod. It registers the descriptor in both the local cache and the shared registry. Subsequent lookups with `lookup_rowtype_tupdesc(RECORDOID, typmod)` find the descriptor by typmod index without touching the system catalog.

This design means anonymous record types are cheap to create. But they cannot be stored persistently: a typmod is a session-local integer, not a durable catalog OID. PostgreSQL rejects an attempt to store a `RECORD`-typed column in a table at parse time.

## I/O and text representation

The text format of a composite value is a parenthesized, comma-separated list of field values: `(42,"hello world",)` — the trailing comma represents a NULL third field. `record_in()` and `record_out()` in `rowtypes.c` implement the generic I/O for any composite type. They look up the column types via `lookup_rowtype_tupdesc()`. Then they iterate columns and call each column's own input or output function. Quoted strings and backslash escaping inside composite literals follow the same rules as array literals.

The binary send/receive path (`record_send`, `record_recv`) encodes each field as a 4-byte OID, a 4-byte length, and then the column's binary representation, allowing type OID verification on receive.

## Composite values in expressions

SQL allows composite construction with `ROW(expr, ...)` or a bare `(expr, expr)` — the parser treats the parenthesized form as a `RowExpr` node. PostgreSQL performs composite comparison (`=`, `<>`, `<`, `>`) field-by-field in column order, using each field's own comparison operators. This requires the composite type to have a valid comparison strategy in its type cache entry.

Accessing a field of a composite value uses the `FieldSelect` executor node (`nodeAgg.c`, `execExpr.c`). This node takes the composite `Datum` and deforms it with `heap_deform_tuple()` or `heap_getattr()`. It then returns the requested attribute.

PostgreSQL passes composite values by reference as a `HeapTuple`-formatted in-memory blob. If the tuple is larger than a threshold, it may be toasted. But individual fields are not independently toastable through the composite type layer. TOAST operates on the entire composite datum as a unit, unless the field itself is a varlena stored out-of-line in its own TOAST table. This exception only applies to table columns, not standalone composite values.

## Composite types and polymorphism

Composite types interact with PostgreSQL's polymorphism through the `anyelement` and `anycompatible` pseudo-types. But there is no `anycomposite` pseudo-type in the core type system. Functions that accept or return `RECORD` effectively accept any composite type. A `OUT` parameter list or the `AS` clause of a `SELECT` using the function provides the actual column structure.

## See also

- [[subsystems/types/range-types]] — another structured type family with similar catalog layout
- [[subsystems/storage/toast]] — how large composite datums are stored
- [[subsystems/catalog/core-catalogs]] — pg_type, pg_class, pg_attribute relationships
- [[code-paths/create-table]] — how table creation produces the implicit row type

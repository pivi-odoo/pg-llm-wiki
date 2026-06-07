---
title: "OID Type"
aliases:
  - oid
  - object identifier
  - oidvector
  - OID type internals
source_files:
  - src/backend/utils/adt/oid.c
  - src/include/postgres_ext.h
  - src/include/catalog/pg_type.h
symbols:
  - oidin
  - oidout
  - oidrecv
  - oidsend
  - buildoidvector
  - check_valid_oidvector
  - oidvectorin
  - oidvectorout
  - oidparse
  - oid_cmp
---

Every database object in PostgreSQL — tables, functions, types, operators, roles — is identified by an Object Identifier (OID), a 32-bit unsigned integer. OIDs thread through the system catalogs as foreign keys: `pg_class.oid` identifies a table, `pg_proc.oid` a function, `pg_type.oid` a type. Understanding OIDs is essential for reading catalog queries, interpreting psql output, and debugging schema-related problems.

## Representation

At the C level, `Oid` is `typedef unsigned int` (32-bit). The SQL type `oid` maps to this C type. The I/O functions in `src/backend/utils/adt/oid.c` parse and format OIDs as unsigned decimal integers:

- `oidin` accepts a string of decimal digits and calls `uint32in_subr()`, which rejects negative values and values above `UINT32_MAX`.
- `oidout` formats with `snprintf("%u", ...)`, always producing an unsigned decimal string.
- Binary wire format is 4 bytes in network byte order (`pq_sendint32`).

Because OIDs are unsigned, values from 2³¹ through 2³²−1 are valid OIDs. PostgreSQL uses this range for user objects after OID wraparound. The parser handles it by accepting `Float` AST nodes (large OID literals that overflow `int4`) in `oidparse()`.

## Assignment and Uniqueness

OIDs within a single database cluster are assigned from a cluster-wide counter stored in the control file. The counter is monotonically increasing and wraps around at 2³² back to `FirstNormalObjectId` (16384). PostgreSQL detects wraparound and rechecks for conflicts before assigning a recycled OID.

System objects (built-in types, operators, functions) have hard-coded OIDs below `FirstNormalObjectId`. User-created objects receive OIDs from the counter. OIDs are cluster-unique at any given moment but are not globally unique across clusters or time. Two different databases could have a table with OID 12345. After a `DROP TABLE`, the OID may eventually be reused.

## Catalog Columns

OIDs surface in catalog queries in several common patterns:

```sql
-- Get the OID for a table by name
SELECT oid FROM pg_class WHERE relname = 'orders';

-- Use the regclass cast — shorter and avoids schema-qualification issues
SELECT 'orders'::regclass::oid;

-- Join catalogs using OID as foreign key
SELECT a.attname, t.typname
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
WHERE a.attrelid = 'orders'::regclass
  AND a.attnum > 0;
```

The `reg*` family of types (`regclass`, `regtype`, `regproc`, `regoperator`, etc.) are OIDs with special I/O functions that resolve names at input time and display symbolic names at output time. They are the idiomatic way to reference catalog objects by name in queries.

## oidvector

`oidvector` is a specialised fixed-format array of OIDs used in several system catalog columns, notably `pg_proc.proargtypes` (function argument types) and `pg_index.indkey` (index column numbers). It is not a general-purpose `oid[]` array — it has strict structural requirements:

- One-dimensional
- Zero-based indexing (lower bound 0)
- No NULL elements
- Element type must be `oidoid`

`check_valid_oidvector()` enforces these constraints. It is called whenever an `oidvector` arrives as a SQL parameter. The restriction exists because C code in the catalog access layer accesses `oidvector.values[]` directly as a C array. Any deviation from the expected layout would cause incorrect reads or crashes.

A general `oid[]` can be cast to `oidvector` at the SQL level. `check_valid_oidvector()` guards against such casts producing a structurally invalid value.

## InvalidOid

The constant `InvalidOid` is 0. PostgreSQL uses it as a sentinel meaning "no object" or "not applicable" throughout the codebase — analogous to a null pointer. Catalog columns that represent optional references (e.g. `pg_class.reltoastrelid` when there is no [[subsystems/storage/toast|TOAST]] table) store `InvalidOid`. SQL-visible, `InvalidOid` appears as `0` when cast to `oid`.

```sql
-- Find tables without a TOAST relation
SELECT relname FROM pg_class
WHERE relkind = 'r' AND reltoastrelid = 0;
```

## Related Topics

- [[subsystems/catalog/core-catalogs|Core System Catalogs]] — where OIDs are used as primary keys
- [[subsystems/catalog/syscache|System Catalog Cache (syscache)]] — OID-keyed caches for fast lookups
- [[subsystems/types/regproc-types|Reg* Object Reference Types]] — OID aliases with name resolution

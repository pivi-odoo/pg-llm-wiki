---
title: "Miscellaneous Utility Functions (misc.c)"
aliases:
  - pg_sleep
  - current_database
  - current_query
  - pg_typeof
  - parse_ident
  - pg_input_is_valid
  - num_nulls
  - misc functions
source_files:
  - src/backend/utils/adt/misc.c
  - src/include/utils/builtins.h
symbols:
  - current_database
  - current_query
  - pg_sleep
  - pg_typeof
  - pg_collation_for
  - pg_num_nulls
  - pg_num_nonnulls
  - parse_ident
  - pg_input_is_valid
  - pg_input_error_info
  - pg_relation_is_updatable
  - pg_column_is_updatable
  - pg_tablespace_databases
  - pg_tablespace_location
  - pg_get_keywords
  - pg_get_replica_identity_index
  - any_value_transfn
---

`src/backend/utils/adt/misc.c` is a catch-all for SQL-callable utility functions that don't belong to a specific data type. The functions it contains are useful for introspection, diagnostics, and defensive programming in application queries and stored procedures.

## Session and Query Context

**`current_database()`** returns the name of the current database by resolving `MyDatabaseId` through the catalog cache. It reflects the database the backend connected to. It cannot change during a session.

**`current_query()`** returns the text of the currently executing statement by reading `debug_query_string`, the global variable that the command dispatcher keeps updated. Inside a trigger or function it returns the outer statement's text, not the function body. Returns NULL when called outside a query context (e.g. from a background worker with no active statement).

**`pg_sleep(seconds float8)`** pauses the current backend for at least the specified duration. The implementation uses `WaitLatch()` rather than `sleep()` or `select()` to ensure the backend wakes up promptly if a query cancel or postmaster death signal arrives. Because `WaitLatch` has a maximum sleep of `INT_MAX` milliseconds, durations longer than about 24 days loop internally.

## Type Introspection

**`pg_typeof(anyelement)`** returns the `regtype` OID of its argument without evaluating it. Useful for diagnosing implicit type coercions:

```sql
SELECT pg_typeof(1),           -- integer
       pg_typeof(1.0),         -- numeric
       pg_typeof('x'::text),   -- text
       pg_typeof(now());       -- timestamp with time zone
```

**`pg_collation_for(anyelement)`** returns the name of the collation in effect for an expression (`"COLLATE FOR"` in standard SQL). Returns NULL if the type is not collatable or has no explicit collation.

## Null Counting

**`num_nulls(VARIADIC "any")`** and **`num_nonnulls(VARIADIC "any")`** count NULL and non-NULL arguments in a variadic list. They handle both the variadic-array calling convention (when all args are bundled into an `anyarray`) and the separate-argument convention, making them reliable across different call sites. Both return NULL when called with a null variadic array argument, consistent with other variadic functions.

```sql
-- Validate that a row has at most one null among key fields
SELECT * FROM orders
WHERE num_nulls(customer_id, product_id, quantity) > 0;
```

## Identifier Parsing

**`parse_ident(text, strict boolean DEFAULT true)`** splits a qualified SQL identifier (e.g. `"public"."orders"`) into an array of its components, handling double-quote escaping and case folding:

```sql
SELECT parse_ident('public.orders');     -- {public,orders}
SELECT parse_ident('"My Schema".tbl');  -- {"My Schema",tbl}
```

In strict mode (the default), any trailing non-identifier characters cause an error. In non-strict mode, parsing stops at the first unrecognisable character and returns the partial result. Unquoted components are lowercased to match PostgreSQL's case-folding rules. Quoted components are preserved verbatim.

## Input Validation

**`pg_input_is_valid(text, regtype)`** and **`pg_input_error_info(text, regtype)`** test whether a string is a valid input for a given type without raising an error. They rely on the datatype's input function supporting the *soft error* mechanism (`errsave`/`ereturn`). Types that use `elog(ERROR, ...)` directly will still raise an error even through these functions.

```sql
-- Validate user-supplied date strings before inserting
SELECT col, pg_input_is_valid(col, 'date') AS ok,
       (pg_input_error_info(col, 'date')).message AS error
FROM (VALUES ('2024-01-15'), ('not-a-date'), ('2024-02-30')) AS t(col);
```

`pg_input_error_info` returns a composite row of `(message, detail, hint, sql_error_code)`. Both functions cache type I/O information on the `FmgrInfo` across repeated calls when the type argument is constant.

## Updatability Checks

**`pg_relation_is_updatable(regclass, include_triggers boolean)`** returns a bitmask indicating which DML operations (`INSERT`, `UPDATE`, `DELETE`) are supported on a relation. Views are updatable only if they have an unconditional rule or instead-of trigger for each operation. **`pg_column_is_updatable(regclass, int2, boolean)`** narrows this to a specific column.

These functions back the `information_schema.columns.is_updatable` column and the updatability information in client tools.

## Tablespace Inspection

**`pg_tablespace_databases(oid)`** lists the OIDs of databases that have objects in a tablespace, by reading the filesystem directory directly rather than querying the catalog. **`pg_tablespace_location(oid)`** returns the filesystem path of a tablespace by reading the symbolic link in `pg_tblspc/`.

## Grammar and Catalog Metadata

**`pg_get_keywords()`** returns all SQL grammar keywords with their reservation category (`U`=unreserved, `C`=column-name, `T`=type/function-name, `R`=reserved) and whether they can be used as bare labels without `AS`. Useful for building SQL editors and query parsers.

**`pg_get_catalog_foreign_keys()`** returns the foreign key relationships between system catalog tables (from the static `sys_fk_relationships[]` array in `catalog/system_fk_info.h`). These are logical constraints that the catalog enforces through code rather than physical `pg_constraint` entries.

**`pg_get_replica_identity_index(regclass)`** returns the OID of the replica identity index for a table — the index used to identify rows in logical replication. Returns NULL if the table uses the full-row identity mode or has no replica identity.

## ANY_VALUE Aggregate

**`any_value_transfn()`** is the transition function for the `ANY_VALUE` aggregate, which returns an arbitrary non-null value from a group. The implementation simply returns the first argument unchanged — the aggregate framework guarantees the function is not called for null inputs, so the first non-null value seen becomes the result.

## Related Topics

- [[subsystems/types/base-types|Base Types]] — scalar type internals
- [[subsystems/parser/semantic-analysis|Semantic Analysis]] — where expression types are resolved
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]] — `current_query()` source

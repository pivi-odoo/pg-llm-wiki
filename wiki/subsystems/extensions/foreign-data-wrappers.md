---
title: "Foreign Data Wrappers (DDL and Catalog)"
aliases:
  - FDW
  - foreign data wrapper
  - CREATE FOREIGN TABLE
  - CREATE SERVER
  - IMPORT FOREIGN SCHEMA
  - foreign server
  - user mapping
source_files:
  - src/backend/commands/foreigncmds.c
  - src/include/foreign/foreign.h
  - src/include/foreign/fdwapi.h
  - src/include/catalog/pg_foreign_data_wrapper.h
  - src/include/catalog/pg_foreign_server.h
  - src/include/catalog/pg_foreign_table.h
  - src/include/catalog/pg_user_mapping.h
symbols:
  - CreateForeignDataWrapper
  - AlterForeignDataWrapper
  - CreateForeignServer
  - AlterForeignServer
  - CreateUserMapping
  - AlterUserMapping
  - RemoveUserMapping
  - CreateForeignTable
  - ImportForeignSchema
  - optionListToArray
---

Foreign Data Wrappers (FDWs) let PostgreSQL query external data sources — other databases, REST APIs, CSV files, or anything a shared library can reach — using ordinary SQL. The DDL layer in `foreigncmds.c` handles the catalog side: creating and altering foreign-data wrapper objects, servers, user mappings, foreign tables, and bulk schema imports. PostgreSQL delegates actual data access to a callback-based API defined in `fdwapi.h`, which each FDW shared library implements.

## Object Hierarchy

Four catalog tables participate in the FDW system:

| Catalog | Object | Purpose |
|---|---|---|
| `pg_foreign_data_wrapper` | FDW | Names the shared library and its handler/validator functions |
| `pg_foreign_server` | Server | A specific external endpoint using a given FDW; holds connection options |
| `pg_user_mapping` | User mapping | Maps a PostgreSQL role to credentials for a specific server |
| `pg_foreign_table` | Foreign table | A relation backed by external data; references a server |

Creating a foreign table requires first creating a server, which requires first creating a foreign-data wrapper. User mappings are optional but needed when the FDW requires per-user credentials (most database FDWs do).

## Options: the text[] Encoding

All four catalog tables store their configuration as a `text[]` column where each element is a `"key=value"` string. `optionListToArray()` converts the `DefElem` list from the parser into this format. `optionListToArray()` validates that key names do not contain `=` (which would make `"a=b=c"` ambiguous). PostgreSQL then stores the array verbatim in the catalog without further transformation.

Validation of option names and values is not PostgreSQL's responsibility — it belongs to the FDW. If the FDW registered a validator function (`VALIDATOR funcname` in `CREATE FOREIGN DATA WRAPPER`), PostgreSQL calls that function with the options list before storing them. The validator typically uses `libpq`-style option checking to reject unknown or malformed keys.

## Handler and Validator Functions

`CREATE FOREIGN DATA WRAPPER` accepts two optional function references:

- **`HANDLER`**: a function returning `fdw_handler` (an internal pseudotype) that provides the FDW's execution callbacks. Without a handler, users can define the FDW, but they cannot query foreign tables.
- **`VALIDATOR`**: a function that accepts a `text[]` of options and a catalog OID, called at DDL time to validate options before PostgreSQL commits them to the catalog.

Both functions must be owned by a superuser. Ordinary users cannot install FDW handlers, even when granted `USAGE` on an FDW. The handler function runs with the privileges of the backend and can call arbitrary C code.

## Privileges

- Creating a foreign-data wrapper requires superuser privilege.
- Creating a foreign server requires `USAGE` privilege on the FDW.
- Creating a user mapping for another role requires superuser privilege. Users can create their own mappings if they have `USAGE` on the server.
- Creating a foreign table requires `CREATE` on the schema and `USAGE` on the server.

`GRANT USAGE ON FOREIGN SERVER` is the normal way to let non-superusers access an external data source.

## IMPORT FOREIGN SCHEMA

`IMPORT FOREIGN SCHEMA remote_schema FROM SERVER srv INTO local_schema` bulk-imports table definitions from an external schema. PostgreSQL calls the FDW's `ImportForeignSchema` callback, which is responsible for returning a list of `CREATE FOREIGN TABLE` statements as strings. PostgreSQL then executes those statements one by one in a subtransaction per table, wrapping each in an error callback that reports the failing table name.

PostgreSQL passes the `LIMIT TO (table_list)` and `EXCEPT (table_list)` clauses to the FDW as lists. The FDW is responsible for filtering on them. PostgreSQL does not enforce them — they are hints to the callback.

## ALTER and ownership transfers

`ALTER FOREIGN DATA WRAPPER`, `ALTER SERVER`, and `ALTER USER MAPPING` update individual options. Options in the `OPTIONS (...)` clause can be `ADD`, `SET`, or `DROP` on a per-key basis. The DDL layer merges the requested changes with the existing `text[]` and re-validates the result. `ALTER SERVER OWNER TO` transfers ownership, but requires the new owner to have `USAGE` on the FDW. PostgreSQL blocks the ownership transfer if that condition is not met.

## Related Topics

- [[subsystems/extensions/overview|Extensions Overview]] — the broader extension system that FDWs participate in
- [[subsystems/catalog/core-catalogs|System Catalog]] — the catalog tables that store FDW metadata
- [[code-paths/create-table|CREATE TABLE]] — foreign tables share much of the same DDL path

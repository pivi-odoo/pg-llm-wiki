---
title: Pseudo-Types
aliases:
  - pseudo type
  - pseudotype
  - polymorphic types
  - anyelement
  - anyarray
  - cstring pseudo-type
  - internal pseudo-type
source_files:
  - src/backend/utils/adt/pseudotypes.c
  - src/include/catalog/pg_type.dat
symbols:
  - cstring_in
  - cstring_out
  - void_in
  - void_out
  - anyarray_out
  - PSEUDOTYPE_DUMMY_INPUT_FUNC
  - PSEUDOTYPE_DUMMY_IO_FUNCS
---

Pseudo-types are `pg_type` catalog entries with `typtype = 'p'` and `typcategory = 'P'`. They exist solely to give the parser and type-checker a name to use when declaring function signatures. They cannot be used as table column types, and they carry no storage semantics of their own. PostgreSQL's function manager (`fmgr`) uses OIDs from `pg_type` to represent argument and result types uniformly, including for functions whose arguments or results do not correspond to any storable SQL type. Rather than special-casing these in the parser and planner, the system gives each special kind its own OID in `pg_type` with `typtype = 'p'`. The catalog entry is real enough to satisfy foreign key constraints inside `pg_proc.proargtypes`. The type is marked, though, so that `CREATE TABLE` and similar DDL reject it. Each pseudo-type must still supply `typinput` and `typoutput` functions. Because no actual I/O is legal for most of them, the `PSEUDOTYPE_DUMMY_INPUT_FUNC` and `PSEUDOTYPE_DUMMY_IO_FUNCS` macros in `pseudotypes.c` generate those functions. The generated functions unconditionally raise `ERRCODE_FEATURE_NOT_SUPPORTED`. Pseudo-types divide cleanly into two families. C-interface types describe how internal C functions exchange data with the function manager. Polymorphic types let SQL-level functions declare that two or more arguments or return values must resolve to the same concrete type at call time.

## C-interface pseudo-types

These types model the ABI contract between a C function and the function manager. They appear in `pg_proc` signatures but are never passed as SQL values between queries.

**`cstring`** (OID 2275) represents a null-terminated C string — the raw `char *` that every `_in` and `_out` I/O function receives and returns. It is a borderline case: unlike other C-interface pseudo-types, it has working I/O functions (`cstring_in`, `cstring_out`, `cstring_recv`, `cstring_send`). PostgreSQL even supports it in record constructors and arrays. The source comments note that it might eventually be promoted to a regular base type. Its `typlen = -2` signals the null-terminator convention rather than the 4-byte varlena length word used by text.

**`internal`** (OID 2281) stands for an arbitrary C pointer. Functions that need to pass opaque state between themselves — such as the aggregate transition-function / final-function pair — declare `internal` as the intermediate type. The system will not call such a function from SQL unless at least one other argument is a genuine type. This restriction prevents SQL-level users from constructing a forged pointer.

**`opaque`** was a legacy catch-all used before proper pseudo-types were introduced; it has been removed from modern PostgreSQL and now only appears in very old dump files.

**`trigger`** (OID 2279) and **`event_trigger`** (OID 3838) are the declared return types of row-level trigger functions and event trigger functions respectively. The SPI machinery recognises these OIDs and treats the return value specially. The actual `HeapTuple` is passed through a `TriggerData` struct, not as a SQL datum.

**`void`** (OID 2278) marks functions that produce no useful return value. It is one of the few pseudo-types with working I/O: `void_in` accepts any input and discards it, and `void_out` returns an empty string. This lets `SELECT function_returning_void(...)` execute without error. PL function handlers use this so they can return `VOID` without special-casing.

**`language_handler`**, **`fdw_handler`**, **`index_am_handler`**, **`table_am_handler`**, and **`tsm_handler`** follow the same pattern as `trigger`. Each marks the return type of a specific category of handler function. The calling infrastructure dispatches on the OID rather than inspecting the return value as a SQL datum.

## Polymorphic pseudo-types

Polymorphic pseudo-types let a single SQL function definition cover an open-ended family of concrete types. The type-checker enforces a resolution rule at parse time: within one function call, all arguments (and the return type) that belong to the same polymorphic family must resolve to the same concrete type. The two families are independent of each other.

### The `anyelement` family

`anyelement` (OID 2283) stands for any single base type. `anyarray` (OID 2277) stands for any array type. The element type of that array must match any `anyelement` in the same signature. `anynonarray` (OID 2776) further constrains the resolved type to be a non-array type. `anyenum` constrains it to an enum type.

`anyrange` (OID 3831) stands for any range type; its element type must match `anyelement` if both appear. `anymultirange` (OID 4537) is the corresponding multirange constraint.

Example: `array_append(anyarray, anyelement)` — the second argument must be an element of the array type supplied as the first.

### The `anycompatible` family

`anycompatible` (OID 5077), `anycompatiblearray` (OID 5078), `anycompatiblenonarray` (OID 5079), `anycompatiblerange` (OID 5080), and `anycompatiblemultirange` (OID 4538) behave like their `any*` counterparts, but they apply implicit coercion. The planner finds the common supertype of all supplied arguments rather than requiring an exact type match. This family was added in PostgreSQL 13 to let functions like `greatest()` and `least()` accept mixed-but-compatible argument types without writing a separate overload for every combination.

The two families do not interact: an `anyelement` argument and an `anycompatible` argument in the same function resolve independently.

### `record` and `record[]`

`record` (OID 2249) is a composite pseudo-type representing an anonymous row — a row whose column list is not fixed in the catalog. Functions that return a `record` must be called with an `AS` column-definition list so the parser can assign names and types to the individual fields. `_record` (OID 2287) is the corresponding pseudo-type for arrays of anonymous records. Both carry `typcategory = 'P'`, so they share the prohibition on column use. They have fully working I/O functions (`record_in`, `record_out`), though, because anonymous composites can appear as transient values in `SELECT` results.

## Column-use restriction

The DDL layer checks `pg_type.typtype` before accepting a type in `CREATE TABLE` or `ALTER TABLE`. Any type with `typtype = 'p'` is rejected outright. This check exists because pseudo-types either represent C-level values with no defined on-disk format (`internal`, `trigger`) or represent constraints on type variables rather than concrete storage types (`anyelement`, `anycompatible`). Storing such a value in a heap tuple would be meaningless or unsafe.

## Related Topics

- [[subsystems/types/base-types|Base types]]
- [[subsystems/types/composite-types|Composite types]]
- [[subsystems/types/range-types|Range types]]
- [[subsystems/types/array-internals|Array internals]]
- [[subsystems/types/enum-internals|Enum internals]]

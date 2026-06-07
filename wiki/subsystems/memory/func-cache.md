---
title: "Function Call Cache"
aliases:
  - funccache
  - CachedFunction
  - CachedFunctionHashKey
  - cached_function_compile
  - function cache
tags:
  - theme/caching
source_files:
  - src/backend/utils/cache/funccache.c
  - src/include/utils/funccache.h
symbols:
  - CachedFunction
  - CachedFunctionHashKey
  - CachedFunctionHashEntry
  - cached_function_compile
  - cfunc_hashtable_lookup
  - cfunc_hashtable_insert
  - cfunc_hashtable_delete
  - cfunc_resolve_polymorphic_argtypes
  - compute_function_hashkey
  - delete_function
  - CachedFunctionCompileCallback
  - CachedFunctionDeleteCallback
---

The function call cache (`funccache.c`) stores per-backend compiled representations of SQL-language and PL/pgSQL functions so that each function body needs to be parsed, analysed, and compiled only once per calling context rather than on every invocation. The cache sits between the raw `pg_proc` catalog row and the per-call `FmgrInfo` lookup. This avoids repeated [[subsystems/catalog/syscache|syscache]] reads and re-compilation for functions that are called many times within a session. PostgreSQL 18 refactored this machinery into a shared module so that multiple procedural languages can reuse the same hash-table infrastructure.

## What the cache stores

Each entry in the function call cache represents a function compiled for a specific combination of calling context, resolved argument types, and result type. The stable identity of an entry — its hash key — is captured in `CachedFunctionHashKey` (`funccache.h`):

| Field | Purpose |
|---|---|
| `funcOid` | OID of the function in `pg_proc` |
| `isTrigger` / `isEventTrigger` | whether the function is called as a DML or event trigger |
| `trigOid` | for DML triggers, the OID of the specific trigger; zero otherwise |
| `inputCollation` | collation passed by the caller; affects plan generation for collation-sensitive expressions |
| `nargs` | number of input arguments (matches `pronargs`) |
| `argtypes[FUNC_MAX_ARGS]` | resolved input argument types, with any polymorphic types replaced by the concrete types supplied at the call site |
| `callResultType` | for functions returning composite, the actual result `TupleDesc`; null for scalar returns |
| `cacheEntrySize` | the language-specific size of the full cache struct; distinguishes entries from different language handlers |

The `CachedFunction` struct itself holds only the fields that `funccache.c` manages directly:

| Field | Purpose |
|---|---|
| `fn_hashkey` | back-pointer to the entry in the hash table |
| `fn_xmin` | transaction ID of the `pg_proc` row at compile time |
| `fn_tid` | item pointer of the `pg_proc` row at compile time |
| `dcallback` | language-specific callback to free language-owned subsidiary data |
| `use_count` | number of active invocations; prevents premature deletion |

Language handlers — SQL functions, PL/pgSQL, and others — allocate a larger struct that embeds `CachedFunction` at the start and appends their own fields (parse trees, execution plans, variable layouts, and so on) immediately after. The `cacheEntrySize` field in the hash key ensures that two languages that both happen to use `funccache.c` cannot share a cache entry, even if the function OID and argument types match. This matters when `CREATE OR REPLACE FUNCTION` switches the implementation language.

A single function OID does not uniquely determine what a compiled function entry must contain, because polymorphic functions resolve to different concrete types at each call site. A function declared as `f(anyelement) RETURNS anyelement` may be called with `integer` from one query and `text` from another. The compiled representation — including the expression trees and any JIT-compiled code — must reflect the actual types it will manipulate. Separate cache entries for each resolved type combination allow each compiled copy to be type-specific. Trigger functions have an additional dimension: the same function OID may serve multiple trigger definitions attached to different tables with different row types or different transition table names. Including the trigger OID in the hash key guarantees a distinct cache entry per trigger, so each compiled copy can embed the correct `TupleDesc` for the target relation. The `callResultType` component addresses a subtler case. A function declared to return `record` relies on a column definition list at the call site to establish its output row type. A function returning a named composite type could see its definition change via `ALTER TABLE`. Including the result `TupleDesc` in the hash key means that a query with a different column definition list gets a fresh cache entry rather than silently using a stale type layout.

## Lifecycle of a cache entry

The entry point for all cache interactions is `cached_function_compile()`. Callers — typically a language handler's call or compile function — pass the current `FunctionCallInfo`, their previously cached pointer (from `fn_extra` or equivalent), a compile callback, a delete callback, and the language-specific entry size.

On first call or after invalidation, `cached_function_compile()` fetches the function's `pg_proc` row via `SearchSysCache1(PROCOID, ...)`. It then calls `compute_function_hashkey()` to resolve polymorphic argument types and build the hash key. `cfunc_resolve_polymorphic_argtypes()` writes the resolved types into `hashkey.argtypes`. It calls the standard `resolve_polymorphic_argtypes()` and additionally treats `RECORD`-typed input arguments as if they were polymorphic. The actual type from the call expression replaces `RECORDOID`, so each named composite type passed to such an argument gets a distinct compiled entry.

`cached_function_compile()` then uses the hash key to probe `cfunc_hashtable`, a static backend-local `HTAB` in [[subsystems/memory/contexts|TopMemoryContext]]. On a hit, `cached_function_compile()` validates the cached entry by comparing `fn_xmin` and `fn_tid` against the current `pg_proc` tuple header. This pair uniquely identifies the exact version of the catalog row that was current at compile time. If the row has since been updated by `CREATE OR REPLACE FUNCTION` or `ALTER FUNCTION`, the tuple's `xmin` or `ctid` will have changed. The entry is then stale.

When no valid entry exists, `cached_function_compile()` allocates a new `CachedFunction` struct (or the full language-specific struct of `cacheEntrySize` bytes) in `TopMemoryContext`. It then invokes the language-specific compile callback to fill it. After the callback returns, `cached_function_compile()` records `fn_xmin` and `fn_tid`. It inserts the entry into the hash table. A `PG_TRY` block wraps the allocation and callback. This frees a freshly allocated struct if compilation fails, avoiding a leak in `TopMemoryContext`.

`funccache.c` itself does not manage the `use_count` field. Callers are expected to increment it before entering the body of the function and decrement it when execution finishes. This count guards against a race where someone updates `pg_proc` while the function is actively executing. If `use_count > 0`, `funccache.c` removes the stale entry from the hash table but preserves its storage. This lets the active invocation complete safely. `funccache.c` then simply leaks the orphaned entry — an acceptable tradeoff given that the scenario requires a recursive function call at the moment an `ALTER FUNCTION` commits.

## Invalidation

`funccache.c` invalidates cache entries by version-checking, not by syscache callbacks. There is no registration of a `CacheRegisterSyscacheCallback` for `PROCOID`. Instead, `cached_function_compile()` re-reads the `pg_proc` tuple on every call. It compares the current tuple's `xmin` and `ctid` against the stored values. This is efficient for two reasons. The lookup is already needed to compute the hash key for a new entry. Re-reading `pg_proc` costs only a syscache probe — itself cheap after the first call.

When the version check fails, `delete_function()` removes the entry from the hash table. If `use_count` is zero, it also invokes the language-specific `dcallback` to free the language-owned data (parse trees, plan trees, allocated contexts). `delete_function()` never frees the `CachedFunction` struct itself if there are outstanding `fn_extra` pointers from `FmgrInfo` structs. Those pointers remain valid. They will find the entry gone from the hash on their next call, triggering a recompile.

## Relationship to FmgrInfo and the generic plan cache

The function call cache is distinct from two other caching layers that PostgreSQL callers encounter.

`FmgrInfo` (`fmgr.h`) is the per-call-site function lookup struct that records the OID, the calling convention, and a single `fn_extra` pointer for language-specific state. `FmgrInfo` is cheap and frequently re-created. It holds no compiled representation itself. The function call cache sits one level above: `fn_extra` typically points to the `CachedFunction`-derived struct managed here, so that the language handler can skip re-lookup on repeated calls to the same `FmgrInfo`.

The [[subsystems/planner/generic-plans|generic plan cache]] (`plancache.c`) is concerned with caching complete query execution plans for parameterised statements, including decisions about whether to use a generic plan or a custom plan. It operates at the SQL statement level. `PREPARE` / `EXECUTE` and PL/pgSQL's internal statement caching use it. The function call cache operates at the function body level: it caches the compiled representation of the function itself (its parse tree or bytecode), not the execution plan for any individual SQL statement within the function. A PL/pgSQL function that contains `SELECT` statements will use both caches. The function call cache stores the compiled PL/pgSQL body, while each embedded SQL statement may have its own entry in the generic plan cache.

## Related Topics

- [[subsystems/catalog/syscache|Catalog Caches (syscache / catcache)]]
- [[subsystems/memory/contexts|Memory Contexts]]
- [[subsystems/planner/generic-plans|Generic Plan Cache]]
- [[subsystems/executor/sql-language-functions|SQL-Language Functions]]
- [[subsystems/executor/jit-llvm|JIT Compilation (LLVM)]]

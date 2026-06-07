---
title: "Fast-Path Function Call Protocol"
aliases:
  - "Fast-Path Protocol"
  - "PQfn"
  - "Fastpath"
tags:
  - theme/wire-protocol
source_files:
  - src/backend/tcop/fastpath.c
  - src/include/tcop/fastpath.h
symbols:
  - HandleFunctionRequest
  - fetch_fp_info
  - parse_fcall_arguments
  - SendFunctionResult
  - fp_info
---

# Fast-Path Function Call Protocol

The fast-path protocol is a [[subsystems/wire-protocol|wire-protocol]] message type (`'F'`) that lets a client invoke a PostgreSQL function directly by OID, bypassing the SQL parser, planner, and executor entirely. A single round trip from client to server is sufficient: the server calls `HandleFunctionRequest()` (fastpath.c), which resolves the function via `fmgr_info()` and dispatches straight to `FunctionCallInvoke()`. The mechanism predates the extended query protocol and survives today primarily as the implementation path for large-object operations.

## Protocol mechanics

The client sends a single `'F'` message whose body contains, in order:

| Field | Size | Meaning |
|---|---|---|
| Function OID | 4 bytes | Which function to call |
| Number of argument format codes | 2 bytes | 0, 1, or N |
| Argument format codes | 2 bytes each | 0 = text, 1 = binary |
| Number of arguments | 2 bytes | Must match `pg_proc.pronargs` |
| Per-argument: length | 4 bytes | -1 signals a NULL argument |
| Per-argument: bytes | length bytes | Argument payload |
| Result format code | 2 bytes | 0 = text, 1 = binary |

`parse_fcall_arguments()` reads this layout from the `StringInfo` buffer that `postgres.c` handed in. The format-code array is flexible: if `numAFormats` is 0 all arguments default to text; if it is 1 the single code applies to every argument; if it equals `nargs` each argument has its own code. Any other combination raises `ERRCODE_PROTOCOL_VIOLATION`.

The server reply is a `'V'` (function result) message: a 4-byte length followed by the payload, or `–1` for a NULL result. `SendFunctionResult()` applies the same text/binary distinction to the output side.

## Input and output conversion

Format code 0 (text) means the argument bytes are a client-encoded text string. `parse_fcall_arguments()` calls `pg_client_to_server()` to convert the encoding. It then passes the result to the type's input function via `OidInputFunctionCall()`. On the output side, `SendFunctionResult()` calls `OidOutputFunctionCall()`. It sends the resulting C string.

Format code 1 (binary) means the bytes are the type's internal wire representation. For input, the fast-path handler uses `OidReceiveFunctionCall()` instead. For output, `OidSendFunctionCall()` produces a `bytea` whose contents are sent verbatim. The binary path is faster and lossless, which is why large-object clients prefer it for bulk data transfer.

One limitation the source notes explicitly: the collation passed to `InitFunctionCallInfoData()` is always `InvalidOid`, so collation-sensitive functions cannot be called meaningfully via fast-path.

## Security checks

Because the client supplies a raw function OID rather than a qualified SQL name, fast-path calls bypass the normal name-resolution security machinery. `HandleFunctionRequest()` therefore performs two explicit ACL checks before invoking anything:

1. `object_aclcheck(NamespaceRelationId, fip->namespace, GetUserId(), ACL_USAGE)` — the calling user must have `USAGE` on the function's schema.
2. `object_aclcheck(ProcedureRelationId, fid, GetUserId(), ACL_EXECUTE)` — the calling user must have `EXECUTE` on the function itself.

Both checks raise an `aclcheck_error` on failure. `HandleFunctionRequest()` also calls `InvokeNamespaceSearchHook` and `InvokeFunctionExecuteHook` to allow extensions (for example, security policy plugins) to intervene.

Additionally, `fetch_fp_info()` rejects any `pg_proc` entry that is not a plain function (`prokind != PROKIND_FUNCTION`) or that returns a set (`proretset`). Procedures, aggregate functions, and window functions cannot be called via fast-path.

## The fp_info struct and catalog lookups

`fetch_fp_info()` populates a stack-allocated `struct fp_info` with the information needed to call the function:

```
struct fp_info {
    Oid      funcid;
    FmgrInfo flinfo;          /* filled by fmgr_info() */
    Oid      namespace;
    Oid      rettype;
    Oid      argtypes[FUNC_MAX_ARGS];
    char     fname[NAMEDATALEN];
};
```

The struct is allocated fresh on every call. An earlier version attempted to cache it across calls within the same transaction. The code comment notes this was "utterly useless," because `postgres.c` executes each fast-path call as a separate transaction command. As a result, the cached data could never be reused. The current approach simply repeats the `SearchSysCache1(PROCOID, ...)` and `fmgr_info()` lookups unconditionally.

## Strict-function null handling

If the resolved function is declared `STRICT` (`fn_strict` in the `FmgrInfo`), `HandleFunctionRequest()` inspects every argument for `isnull`. If any argument is NULL, `HandleFunctionRequest()` does not call the function and immediately returns the result as NULL. This mirrors the behaviour of `ExecMakeFunctionResultSet` in the executor. It is required for correctness: strict functions must not run with NULL inputs.

## Historical role

Before the extended query protocol existed (PostgreSQL 7.4 and earlier), libpq used fast-path calls heavily for internal bookkeeping. The most common uses were:

- **Type OID to name resolution** — drivers would call `pg_catalog.typname` lookups by OID to decode `RowDescription` type codes.
- **Large object I/O** — `lo_open`, `lo_close`, `lo_read`, `lo_write`, `lo_lseek`, `lo_tell`, `lo_truncate`, and `lo_creat` each have a corresponding server function. The libpq `lo_*` API wrappers have always driven these via `PQfn()` (the C-library name for a fast-path call). Most drivers that need large objects still do.

The `NOTES` comment in fastpath.c describes the entire file as "the server side of PQfn," reflecting this origin. The extended query protocol (Parse/Bind/Execute) is strictly more capable. It accepts arbitrary SQL expressions and parameterised queries. It supports server-side prepared statements that survive across many bind cycles. It integrates naturally with the full type system and collation handling. It also provides well-defined error recovery via `Sync` messages, a mechanism that fast-path lacks entirely. For these reasons, modern drivers use the extended protocol for almost everything. They reserve `PQfn()` / fast-path for the large-object interface and a handful of legacy operations. The wire-protocol documentation notes fast-path as a historical curiosity. The source comment calls it "cruft."

## Logging and observability

`HandleFunctionRequest()` respects `log_statement = all`: if that setting is active it logs `"fastpath function call: \"<name>\" (OID <oid>)"` before invoking the function. Duration logging via `check_log_duration()` runs at the end of the call, subject to `log_min_duration_statement` the same way ordinary queries are.

Fast-path calls are not visible through `pg_stat_activity` as a query string (there is no SQL text). They do appear as active sessions, however. Their duration can appear in the PostgreSQL log.

## Related Topics

- [[subsystems/wire-protocol]]
- [[architecture/client-connection]]
- [[subsystems/storage/toast]]

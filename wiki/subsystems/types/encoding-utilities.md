---
title: "Multibyte Encoding Utilities"
aliases:
  - mbutils
  - encoding conversion
  - client encoding
source_files:
  - src/backend/utils/mb/mbutils.c
symbols:
  - pg_client_to_server
  - pg_server_to_client
  - pg_any_to_server
  - pg_server_to_any
  - pg_do_encoding_conversion
  - pg_do_encoding_conversion_buf
  - pg_unicode_to_server
  - pg_verify_mbstr
  - pg_verify_mbstr_len
  - pg_mblen_cstr
  - pg_mblen_range
  - pg_mblen_with_len
  - pg_mbcliplen
  - pg_mbcharcliplen
  - PrepareClientEncoding
  - SetClientEncoding
  - InitializeClientEncoding
  - ConvProcInfo
---

The multibyte encoding utilities in `mbutils.c` form the backbone of PostgreSQL's runtime encoding system. They manage three distinct encodings simultaneously — the database (server) encoding, the client encoding, and the message encoding. They also provide the string-level functions that convert between them. Every text value entering or leaving the backend passes through this layer.

## Three Encodings, One Backend

PostgreSQL tracks three encoding identities in process-local state:

- `DatabaseEncoding` — the encoding in which text data is stored on disk.
- `ClientEncoding` — the encoding the current client expects.
- `MessageEncoding` — the encoding used for `gettext`-generated error messages. It may differ from the database encoding under `SQL_ASCII` databases, or on platforms where `LC_CTYPE` disagrees with the database collation.

Each is stored as a pointer into the read-only `pg_enc2name_tbl[]` array, so lookups are a pointer dereference with no memory allocation (mbutils.c).

## Conversion Function Caching

PostgreSQL implements encoding conversion via user-facing conversion functions stored in `pg_conversion`. Looking up and loading these functions from the catalogs requires a live transaction. The backend solves this by caching fmgr lookup results in `ConvProcInfo` structs held in a singly-linked list (`ConvProcList`) allocated in `TopMemoryContext`. Each entry pairs a `(server_encoding, client_encoding)` combination with two `FmgrInfo` pointers: one for client-to-server and one for server-to-client.

```c
typedef struct ConvProcInfo {
    int         s_encoding;
    int         c_encoding;
    FmgrInfo    to_server_info;
    FmgrInfo    to_client_info;
} ConvProcInfo;
```

The list is never freed, because conversion functions must remain available even after transaction rollback. The backend needs to re-establish the previous encoding without touching the catalogs. The two-phase setup (`PrepareClientEncoding` / `SetClientEncoding`) exists precisely to support this. `PrepareClientEncoding()` adds a new `ConvProcInfo` while inside a transaction. `SetClientEncoding()` atomically switches the active function pointers to the newly cached entry, cleaning up duplicates.

During backend startup, no transaction is active. The database encoding may not be known yet either. `SetClientEncoding()` therefore defers the actual load, storing the requested encoding in `pending_client_encoding`. `InitializeClientEncoding()`, called from `InitPostgres()` once the database is open, commits the deferred request. It also resolves `Utf8ToServerConvProc` — a separate cached pointer used when converting individual Unicode code points. This pointer is set once and never changes within a session.

## The Return-Original-Pointer Contract

All string conversion functions share an important API invariant documented at the top of the file: if the function performs no conversion, it returns the **original source pointer unchanged** — no palloc, no copy. The function palloc's the converted result only when conversion actually happens. Callers that pass non-null-terminated strings *must* test `result == src` before using `strlen()` on the result.

When the source and destination encodings are identical, the functions skip validation entirely. They return `src` as-is. The rationale is that PostgreSQL already validated data resident in the server, at ingress. The flip side is that `pg_any_to_server()` always validates, even when encodings match. It handles externally-supplied data, where validity cannot be assumed.

PostgreSQL treats `SQL_ASCII` as a wildcard: it considers any byte sequence valid in it. When the database encoding is `SQL_ASCII` and the client sends a non-ASCII-safe encoding, the server rejects bytes with the high bit set rather than attempting an impossible conversion (`pg_any_to_server()`, mbutils.c).

## Memory Allocation for Conversions

Encoding conversions can expand strings. PostgreSQL defines `MAX_CONVERSION_GROWTH` as the worst-case byte expansion factor. The conversion functions allocate `len * MAX_CONVERSION_GROWTH + 1` bytes using `MemoryContextAllocHuge()` to avoid hitting `MaxAllocSize` limits on the initial allocation. For large inputs (over one million bytes), the function `repalloc`s the result down to its actual size after conversion, since the overallocation could itself exceed `MaxAllocSize` for very large strings.

`pg_do_encoding_conversion()` requires an active transaction because it looks up the conversion function in `pg_conversion` on every call. The fast path `perform_default_encoding_conversion()` uses the pre-cached `FmgrInfo` pointers, and therefore works outside transactions. `pg_client_to_server()` and `pg_server_to_client()` use it when the target encoding matches the current client encoding.

A second variant, `pg_do_encoding_conversion_buf()`, writes into a caller-supplied buffer instead of palloc'ing. It clips the input to fit the worst-case expansion. Contexts where dynamic allocation is inconvenient use it — for example, incremental protocol message parsing.

## Character Length and Clipping

The `pg_wchar_table[]` dispatch table (defined in `pg_wchar.h`) is the central abstraction for encoding-specific operations. Each entry holds function pointers for multibyte length, display length, verification, and wchar conversion. `mbutils.c` wraps these in several length-and-clip helpers:

| Function | Unit of limit | Notes |
|---|---|---|
| `pg_mblen_cstr()` | — | Returns byte length of one character; validates continuation bytes |
| `pg_mblen_range()` | pointer range | Raises error if character extends past `end` |
| `pg_mblen_with_len()` | byte count | Raises error if character exceeds `limit` bytes |
| `pg_mblen_unbounded()` | — | No bounds check; caller must have pre-validated the string |
| `pg_mbcliplen()` | bytes | Returns byte count not exceeding `limit` bytes, never splits a character |
| `pg_mbcharcliplen()` | characters | Returns byte count of at most `limit` characters |
| `pg_mbstrlen()` | — | Count of characters in a null-terminated string |

The bounded variants (`pg_mblen_cstr`, `pg_mblen_range`, `pg_mblen_with_len`) call `report_invalid_encoding_db()` on overflow. This function formats the offending byte sequence as hex for the error message. The unbounded `pg_mblen_unbounded()` (historically `pg_mblen()`) is safe only after the string has been validated. Its earlier name is deprecated and will be removed.

All single-byte encoding paths short-circuit with `strlen()` or simple arithmetic, since `pg_database_encoding_max_length() == 1` makes character and byte counts identical.

## String Validation

`pg_verify_mbstr()` delegates to the encoding-specific `mbverifystr` function pointer. It fires `report_invalid_encoding()` on failure. The `noError` flag converts the error into a `false` return for callers that need to probe validity without raising an exception.

`pg_verify_mbstr_len()` is slower but necessary when the character count of the string is needed alongside validation. It walks the string character by character using `mbverifychar`. It fast-paths ASCII bytes (high bit clear) to avoid the overhead of the full verifier on common content.

## Unicode Code Point Conversion

`pg_unicode_to_server()` converts a single Unicode code point (`pg_wchar`) to the server encoding. It handles ASCII code points (U+0000–U+007F) trivially. For a UTF-8 database, `unicode_to_utf8()` encodes the point directly. For all other encodings, the function uses the session-level `Utf8ToServerConvProc` pointer. It first encodes the code point as UTF-8. Then it calls the UTF-8-to-server conversion function. If that pointer is NULL (no conversion path exists), the function raises an error. `pg_unicode_to_server_noerror()` follows the same logic but returns `false` rather than erroring. It uses the `noError` parameter to the conversion function. It also checks that the entire input was consumed.

The parser uses this path when processing Unicode escape sequences like `U&'\0041'` in string literals. It intentionally avoids catalog access, relying on the pre-cached function pointer set during `InitializeClientEncoding()`.

## Character Incrementing

`pg_database_encoding_character_incrementer()` returns an encoding-specific function that advances a character to the next valid codepoint. `make_greater_string()`, in the statistics machinery, uses this to compute upper bounds for range predicates. UTF-8 and EUC-JP have dedicated incrementers that respect their byte-range constraints and surrogate pair exclusions. All other encodings fall back to `pg_generic_charinc()`. This function simply increments the last byte until the verifier accepts the result.

## Related Topics

- [[subsystems/memory/contexts|memory context]] — conversion results are allocated in `CurrentMemoryContext`; the conversion function cache lives in `TopMemoryContext`

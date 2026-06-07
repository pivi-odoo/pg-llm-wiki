---
title: "JSON Type Internals"
aliases:
  - json type
  - json.c
  - text json
source_files:
  - src/backend/utils/adt/json.c
symbols:
  - json_in
  - json_out
  - datum_to_json
  - json_categorize_type
  - JsonTypeCategory
  - JsonAggState
  - JsonUniqueParsingState
  - JsonUniqueBuilderState
  - JsonUniqueHashEntry
  - escape_json
  - json_validate
  - json_build_object_worker
  - json_build_array_worker
  - JsonEncodeDateTime
  - array_to_json_internal
  - composite_to_json
---

The `json` type stores JSON text exactly as the user supplied it — whitespace, key ordering, and duplicate keys are all preserved through a round-trip. Its on-disk representation is a plain `text` varlena. The only work PostgreSQL does at write time is lexical validation. This is the defining contract that separates `json` from [[subsystems/jsonb|JSONB]]: `json` optimises for write fidelity, `jsonb` for read performance.

## Storage and Validation

`json_in()` converts a C string to a `text` datum. It then immediately runs the lexer over it via `makeJsonLexContext` and `pg_parse_json_or_errsave`. If the input is not syntactically valid JSON, `json_in()` raises the error at input time. It never stores the value. If valid, `json_in()` saves the bytes verbatim — no normalisation, no re-encoding, no deduplication. `json_out()` is therefore trivially `TextDatumGetCString` with no transformation at all (`json.c`).

Because `json` is just `text` underneath, it inherits [[subsystems/storage/toast|TOAST]] compression and out-of-line storage automatically. There is no binary-level difference between a `json` column and a `text` column from the storage manager's perspective. The type OID carried in the system catalog is the sole distinction.

Binary protocol transfer (`json_send`, `json_recv`) sends and receives the raw bytes as a `text` wire value, with re-validation on receipt to guard against corrupt data arriving over the protocol (`json.c`).

## Type Categorisation for Serialisation

Converting arbitrary PostgreSQL values to JSON is central to functions like `to_json()`, `row_to_json()`, `array_to_json()`, and the aggregate functions. The key design decision is to classify each input type once, cache the result, and then reuse it for every row in a set-returning or aggregate context.

`json_categorize_type()` maps a type OID to a `JsonTypeCategory` tag and, where relevant, the OID of the output or cast function to call (`json.c`):

| `JsonTypeCategory` | Source types | Serialisation behaviour |
|--------------------|-------------|------------------------|
| `JSONTYPE_NULL` | SQL NULL | Emits literal `null` |
| `JSONTYPE_BOOL` | `bool` | Emits `true` or `false` (unquoted) |
| `JSONTYPE_NUMERIC` | `int2`, `int4`, `int8`, `float4`, `float8`, `numeric` | Emits unquoted number if valid JSON number, else quoted string |
| `JSONTYPE_DATE` | `date` | ISO 8601 date string via `JsonEncodeDateTime()` |
| `JSONTYPE_TIMESTAMP` | `timestamp` | ISO 8601 timestamp string |
| `JSONTYPE_TIMESTAMPTZ` | `timestamptz` | ISO 8601 with timezone offset |
| `JSONTYPE_JSON` | `json`, `jsonb` | Output function result pasted verbatim (already valid JSON) |
| `JSONTYPE_ARRAY` | Any array type | Recursive via `array_to_json_internal()` |
| `JSONTYPE_COMPOSITE` | Any row/record type | Recursive via `composite_to_json()` |
| `JSONTYPE_CAST` | User-defined type with explicit cast to `json` | Calls the cast function |
| `JSONTYPE_OTHER` | Any remaining built-in type | Output function result, then `escape_json()` |

The categorisation walks through a domain's base type first. This means PostgreSQL handles domain types identically to their base. For user-defined types with no explicit cast to `json`, `json_categorize_type()` serialises the value via the type's text output function, then string-escapes it. The result is a JSON string containing whatever the type's `typoutput` function produces.

## Datetime Serialisation

Datetimes require special handling because `DateStyle`, a session-level GUC, controls the output of `date_out`, `timestamp_out`, and `timestamptz_out`. JSON demands ISO 8601 regardless of that setting. `JsonEncodeDateTime()` bypasses the output functions entirely, calling the low-level `EncodeDateTime` and `EncodeDateOnly` routines directly with `USE_XSD_DATES` hardcoded (`json.c`). This makes the JSON output stable across `DateStyle` changes. For `timestamptz`, an optional `tzp` pointer allows the caller to specify a UTC offset explicitly — used by jsonpath datetime comparisons — rather than reading the session timezone.

This is why `to_json_is_immutable()` returns `false` for all datetime categories: the function result depends on the session timezone even though `JsonEncodeDateTime` itself normalises the format.

## Composite and Array Recursion

`composite_to_json()` reflects a row type into a JSON object. It calls `lookup_rowtype_tupdesc` to get the `TupleDesc`, skips dropped columns, escapes each attribute name with `escape_json()`, and recursively calls `datum_to_json()` for each attribute value. Because it calls `json_categorize_type` inside the per-attribute loop, it handles rows with heterogeneous column types correctly. But it pays the categorisation cost per row per column, rather than once per query. Callers that know the type in advance (like the aggregates) cache the category externally.

`array_to_json_internal()` deconstructs a PostgreSQL array datum via `deconstruct_array`, categorises the element type once, and then walks the multi-dimensional structure recursively through `array_dim_to_json()`. Each dimension maps to a nested JSON array. Multi-dimensional PostgreSQL arrays become nested JSON arrays with identical nesting depth.

## String Escaping

`escape_json()` is the single function responsible for producing valid JSON string literals from arbitrary text. It wraps the content in double quotes. It replaces control characters, backslashes, and double-quote characters with their JSON escape sequences. `escape_json()` emits characters below U+0020 that do not have a named escape sequence as `\uXXXX`. PostgreSQL calls the function for all string values, all object keys (including composite attribute names), and all `JSONTYPE_OTHER` values (`json.c`).

## Key Uniqueness Checking

The `json` type preserves duplicate keys. It stores them as-is and passes them back to the client unchanged. However, several builder functions offer optional duplicate-key detection for contexts where uniqueness is required.

The mechanism uses a flat hash table (`HTAB`) keyed on `(object_id, key_string)` pairs (`JsonUniqueHashEntry`). During parsing, `JsonUniqueParsingState` maintains a stack of `JsonUniqueStackEntry` nodes, one per open object. Each object receives a unique integer ID from an incrementing counter so that keys in sibling or nested objects do not collide with each other in the flat hash. When `json_unique_object_field_start()` finds a duplicate key, it clears the `unique` flag and frees the entire stack. Further key events become no-ops, since uniqueness is already violated.

During building (e.g., `json_build_object_worker` with `unique_keys = true`), `JsonUniqueBuilderState` tracks the same hash table. A subtle edge case arises when `absent_on_null` is also true: the builder must still check the key for uniqueness, even though it suppresses the value. The builder allocates a throwaway `StringInfo` (`skipped_keys`) on demand, to materialise the key text into the hash table without writing it to the output buffer (`json.c`).

`json_validate()` exposes both lexical validation and key-uniqueness checking as a single entry point used by the `json_is_valid()` SQL function. It runs the standard parser with a custom `JsonSemAction` that hooks into object-start, object-field-start, and object-end events.

## Aggregate Functions

`json_agg` and `json_object_agg` accumulate JSON text incrementally into a `StringInfo` buffer held in the aggregate's [[subsystems/memory/contexts|memory context]]. The transition state is `JsonAggState`. This struct carries the buffer, the pre-computed type category and output function OID for keys and values, and optionally a `JsonUniqueBuilderState` for unique-key enforcement.

The transition functions come in four variants covering the cross-product of two options:

- `absent_on_null`: skip rows where the value (or for `json_object_agg`, the value argument) is NULL, rather than emitting a JSON `null`.
- `unique_keys` (object aggregate only): raise an error on duplicate keys rather than silently overwriting.

The final function for both aggregates is a single `pfree`-free operation: it calls `catenate_stringinfo_string()` to append the closing `]` or ` }` to the accumulated buffer in a fresh palloc, without mutating the transition state. PostgreSQL requires this because aggregate final functions must not modify their state. The planner may call the final function multiple times in certain execution contexts.

## Immutability Inference

The planner uses `to_json_is_immutable()` to determine whether a `to_json(x)` expression can be treated as immutable, enabling constant-folding and stable index expressions. The function returns `true` only for `bool`, `json`/`jsonb` pass-through, and numeric/cast types whose output function is declared `IMMUTABLE`. Datetime types always return `false` because timezone-sensitivity makes them stable at best. Arrays and composites conservatively return `false` pending deeper analysis of element types (`json.c`).

## Key Structures

| Structure | Role |
|-----------|------|
| `JsonTypeCategory` | Enum classifying a PostgreSQL type for JSON serialisation |
| `JsonAggState` | Aggregate transition state: output buffer + type info + optional uniqueness check |
| `JsonUniqueParsingState` | Uniqueness check state during JSON parsing: hash table + object-id stack |
| `JsonUniqueBuilderState` | Uniqueness check state during JSON construction: hash table + throwaway buffer |
| `JsonUniqueHashEntry` | Hash table key: `(object_id, key, key_len)` triple |

## Related Topics

- [[subsystems/jsonb|JSONB Storage and Indexing]] — binary JSON type with parsed on-disk format, GIN indexing, and containment operators

---
title: "Boolean Type Internals"
aliases:
  - bool
  - boolean
  - boolin
  - boolout
  - bool_and
  - bool_or
  - EVERY
source_files:
  - src/backend/utils/adt/bool.c
  - src/include/utils/builtins.h
symbols:
  - boolin
  - boolout
  - boolrecv
  - boolsend
  - booltext
  - parse_bool
  - parse_bool_with_len
  - booland_statefunc
  - boolor_statefunc
  - bool_accum
  - bool_accum_inv
  - bool_alltrue
  - bool_anytrue
  - BoolAggState
---

PostgreSQL's `boolean` type is a one-bit value stored as a single byte. Its apparent simplicity conceals several design decisions that affect how applications interact with it: a deliberately wide input vocabulary, two different text output formats depending on how the value is cast, and a two-state aggregate that tracks both total count and true count to support sliding-window operations.

## Input: Wide Vocabulary

`boolin()` accepts more string representations than most applications expect. The full list is: `true`, `false`, `yes`, `no`, `on`, `off`, `1`, `0` — and any unambiguous prefix thereof. `boolin()` strips leading and trailing whitespace before matching. The matching is case-insensitive throughout. This vocabulary comes from PostgreSQL's GUC system, where administrators have historically configured booleans using `on`/`off` and `yes`/`no` in addition to `true`/`false`.

`parse_bool_with_len()` shares its parsing logic with GUC. It switches on the first character. Then it does a full case-insensitive comparison of the rest. There is a small subtlety around `on` and `off`: both start with `'o'`. So the code uses `max(len, 2)` as the comparison length. This ensures the code rejects `'o'` alone, even though it is a prefix of `on`.

Rejected inputs raise `ERRCODE_INVALID_TEXT_REPRESENTATION` with the message "invalid input syntax for type boolean".

## Output: Two Different Text Representations

`boolout()` produces `'t'` or `'f'` — single-character strings used by the binary protocol and by `COPY` text format. `booltext()`, the cast function for `boolean → text`, produces `'true'` or `'false'` to follow the SQL standard. Applications that stringify booleans for display, JSON, or API responses get the full words. Wire protocol and storage use the abbreviated form.

This split surprises developers who call `bool_col::text` expecting `'t'` and get `'true'` instead. The distinction is intentional: the SQL standard specifies `true`/`false` for the text representation of the SQL boolean type, while `'t'`/`'f'` is the legacy PostgreSQL storage format.

## Binary Protocol

The binary receive function `boolrecv()` reads one byte. It treats any nonzero value as `true`. The send function `boolsend()` writes `1` for true. It writes `0` for false. JDBC and other drivers that use binary encoding send and receive exactly one byte.

## Boolean Aggregates

PostgreSQL implements four aggregates: `bool_and` / `EVERY` (all non-null values must be true), and `bool_or` / `ANY` (at least one non-null value must be true).

The simple forms (`booland_statefunc`, `boolor_statefunc`) process one row at a time and return a running boolean. They cannot support the inverse transition function needed by moving-aggregate mode (sliding windows). Moving-aggregate mode uses `BoolAggState` instead, which counts the total number of non-null rows (`aggcount`) and the number that are true (`aggtrue`). The inverse transition function `bool_accum_inv()` decrements both counters, allowing rows to leave the window. The finalizer functions — `bool_alltrue()` for `bool_and` and `bool_anytrue()` for `bool_or` — check the counters. They return NULL when `aggcount` is zero (an empty set).

```c
typedef struct BoolAggState {
    int64 aggcount;   /* non-null rows seen */
    int64 aggtrue;    /* of those, how many are true */
} BoolAggState;
```

The NULL-returning behavior when the aggregate sees no non-null input matches SQL semantics: `bool_and()` over an empty or all-NULL set is NULL, not `true`.

## Related Topics

- [[subsystems/types/base-types|Base Types]] — how PostgreSQL's type system defines input/output functions
- [[subsystems/types/numeric-scalar-types|Numeric Scalar Types]] — the parallel implementation for integer and float types

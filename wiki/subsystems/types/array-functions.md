---
title: "Array User Functions"
aliases:
  - array_append
  - array_prepend
  - array_cat
  - array_agg
  - array_position
  - array_positions
  - array_remove
  - array_replace
  - array_shuffle
  - array_sample
source_files:
  - src/backend/utils/adt/array_userfuncs.c
symbols:
  - array_append
  - array_prepend
  - array_cat
  - array_agg_transfn
  - array_agg_finalfn
  - array_agg_combine
  - array_position_common
  - array_positions
  - array_shuffle
  - array_sample
  - fetch_array_arg_replace_nulls
---

PostgreSQL implements its user-visible array manipulation functions — appending, searching, aggregating, sampling — in `array_userfuncs.c`. They sit above the lower-level array storage machinery (`arrayfuncs.c`) and are the functions SQL queries invoke directly. Most of them share a common optimization: operating on the *expanded* array representation to avoid repeated detoasting.

## The Expanded Array Optimization

Arrays stored in table columns are in a compact varlena format. Calling `array_append` in a loop would normally detoast and re-toast the array on every call. `fetch_array_arg_replace_nulls()` avoids this by upgrading the input to an `ExpandedArrayHeader` — a deconstructed, in-memory form that can be modified in place and passed between calls without serialization. Functions that both accept and return an array of the same type propagate this expanded form. As a result, a chain of `array_append` calls in a PL/pgSQL loop pays the detoasting cost only once.

If the input array argument is NULL, `fetch_array_arg_replace_nulls()` substitutes an empty array of the appropriate element type rather than propagating NULL. This is the expected behavior for functions like `array_append` when called with a freshly-initialized NULL accumulator.

## Constructors

`array_append(arr anyarray, elem anyelement)` adds one element at the end and returns the new array. `array_prepend(elem anyelement, arr anyarray)` adds at the front. The `||` operator calls both when one operand is an array and the other is a scalar element.

`array_cat(arr1 anyarray, arr2 anyarray)` concatenates two arrays. For one-dimensional arrays this is straightforward concatenation. For multi-dimensional arrays, the second array's dimensions must match the trailing dimensions of the first — the function concatenates along the outermost axis. `||` between two arrays calls `array_cat`.

All three functions call `array_set_element()` or `array_concat()` from `arrayfuncs.c` on the expanded header and return the result in expanded form when possible.

## Aggregation: array_agg

`array_agg(expr)` collects values from multiple rows into an array. The aggregate state is managed by three functions:

- `array_agg_transfn()` — called once per input row; appends the current value to an `ArrayBuildState` accumulator.
- `array_agg_finalfn()` — called once at the end; converts the accumulator to a finished `ArrayType`.
- `array_agg_combine()` — used in parallel aggregation to merge partial results from workers.

For parallel execution, the partial array must travel between processes. `array_agg_serialize()` and `array_agg_deserialize()` convert the accumulator to and from a binary format over the inter-process channel to make this possible.

`array_agg(array_col)` — aggregating array-valued expressions — calls a separate family of functions (`array_agg_array_transfn`, etc.). These concatenate arrays rather than appending elements, building a higher-dimensional result. This is how `SELECT array_agg(ARRAY[a, b]) FROM t` produces a two-dimensional array.

`ORDER BY` within `array_agg` is supported through the standard aggregate ordering mechanism and does not affect the function implementation itself.

## Searching: array_position and array_positions

`array_position(arr anyarray, elem anyelement [, start int])` returns the 1-based index of the first occurrence of `elem` in `arr`, or NULL if not found. The optional `start` parameter begins the search at that index. `array_position_start()` is a separate SQL-callable entry point that passes the start argument to the shared `array_position_common()` implementation.

`array_positions(arr anyarray, elem anyelement)` returns an `integer[]` of all matching indices.

Both functions use the element type's equality operator (`=`) via the type cache, so they respect any custom `=` definition for domain or composite element types. NULL elements require special handling: searching for NULL uses `IS NOT DISTINCT FROM` semantics, matching NULL elements in the array. Searching for a non-NULL value skips NULL elements without error.

## Modification: array_remove and array_replace

`array_remove(arr anyarray, elem anyelement)` returns a copy of the array with all occurrences of `elem` removed. NULL removal is supported: passing NULL as `elem` removes all NULL elements from the array.

`array_replace(arr anyarray, search anyelement, replace anyelement)` returns a copy where every occurrence of `search` is replaced by `replace`. Either argument may be NULL. Searching for NULL replaces all NULL elements. The replacement may itself be NULL, to introduce NULLs.

Both functions iterate once over the source array and produce a new array. They do not modify in place.

## Random Sampling: array_shuffle and array_sample

Added in PostgreSQL 16, these functions operate on a copy of the input array:

`array_shuffle(arr anyarray)` returns the array with elements in a uniformly random order, computed with the Fisher-Yates algorithm. The random source is the backend's `pg_prng` state, seeded per-session.

`array_sample(arr anyarray, n int)` returns an array of exactly `n` randomly chosen elements without replacement. Internally it calls `array_shuffle_n()`, which performs a partial Fisher-Yates shuffle of the first `n` elements. This avoids the cost of shuffling the entire array when only a small sample is needed.

Neither function is `VOLATILE` in the standard sense that would block CTE inlining, but the results differ between calls due to the random source. Both preserve the element type and, for `array_shuffle`, the original lower bound of the array's dimension.

## Related Topics

- [[subsystems/types/array-internals|Array Type Internals]]
- [[subsystems/executor/aggregate|Aggregate Execution]]
- [[sql-features/advanced-aggregation|Advanced Aggregation]]

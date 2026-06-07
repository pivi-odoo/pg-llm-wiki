---
title: "Array Subscripting Internals"
aliases:
  - array subscripting
  - array subscript
  - SubscriptRoutines
  - arraysubs
tags:
  - theme/extensibility
source_files:
  - src/backend/utils/adt/arraysubs.c
  - src/include/nodes/subscripting.h
  - src/include/utils/array.h
symbols:
  - array_subscript_handler
  - raw_array_subscript_handler
  - array_subscript_transform
  - array_exec_setup
  - array_subscript_check_subscripts
  - array_subscript_fetch
  - array_subscript_fetch_slice
  - array_subscript_assign
  - array_subscript_assign_slice
  - array_subscript_fetch_old
  - SubscriptRoutines
  - ArraySubWorkspace
  - array_get_element
  - array_set_element
  - array_get_slice
  - array_set_slice
---

PostgreSQL implements array subscripting — the `arr[i]` and `arr[2:4]` syntax — as a pluggable dispatch mechanism, not a hardwired compiler rule. Each type registers a handler function in `pg_type.typsubscript` that returns a `SubscriptRoutines` struct. The planner and executor call into that struct for parse analysis and runtime evaluation. Arrays use `array_subscript_handler` (defined in `arraysubs.c`). jsonb and any custom type that wants to support subscript syntax use the same interface.

## Element vs Slice Access

A plain subscript like `arr[2]` is an element fetch: it returns a scalar value whose type is the array's element type. A range subscript like `arr[2:4]` is a slice fetch: it returns a new array, even when the range covers exactly one element. `arr[2:2]` yields a one-element array, not a scalar — the result type is the array type itself, not the element type.

`array_subscript_transform` encodes the distinction at parse time. If the expression is a slice (any subscript uses the colon syntax), it sets `sbsref->refrestype` to `refcontainertype` — the array type. For a plain element access, it sets `refrestype` to `refelemtype`. That single field change propagates through the planner. The executor calls a different fetch function. The result lands in a different slot type.

When a slice expression mixes plain subscripts with range subscripts — for example `arr[2:4][1]` on a 2D array — the transform function normalises the plain subscript to a unit range by inserting a constant lower bound of 1. This is why `arr[2:4][1]` and `arr[2:4][1:1]` are identical after parsing.

## 1-Based Indexing and Variable Lower Bounds

PostgreSQL arrays default to a lower bound of 1, not 0. PostgreSQL stores the lower bound in the `lbs[]` array in the `ArrayType` header, one entry per dimension. For a freshly created `ARRAY[10, 20, 30]` the header records `dims = {3}` and `lbs = {1}`, so valid subscripts run from 1 to 3.

The lower bound is not always 1. Slicing a 1-based array at an offset — `arr[3:5]` — produces a result with `lbs = {3}`, not `lbs = {1}`. The slice preserves the subscript meaning of the result: element 3 of the original is still accessible as element 3 of the slice. `array_fill(0, ARRAY[3], ARRAY[5])` similarly produces a three-element array whose valid subscripts are 5, 6, and 7. Developers coming from languages with 0-based indexing may expect `arr[0]` to raise an error. Instead it returns NULL silently, because it is out of bounds for a 1-based array. `fetch_leakproof = true` in the `SubscriptRoutines` declaration tells the executor to suppress out-of-range errors on reads.

Assignment subscripting, on the other hand, is not leakproof (`store_leakproof = false`). An out-of-range write on a fixed-length array type — like writing to index 0 of a NAME column, a fixed-length raw array — raises an error rather than silently discarding the value.

## Multi-Dimensional Arrays

The `dims[]` and `lbs[]` header arrays each have one entry per dimension. `MAXDIM` is 6 — the hard limit on dimensionality. Subscripting a 2D array requires two subscript expressions: `arr[i][j]`. At parse time, the parser collects these into a single `SubscriptingRef` node with two entries in `refupperindexpr`. There is no nesting of `SubscriptingRef` nodes for multiple dimensions. All subscripts for one container access travel together.

For multi-dimensional slicing, `array_subscript_fetch_slice` calls `array_get_slice`. `array_get_slice` uses the multidimensional array iteration utilities (`mda_get_range`, `mda_get_prod`, `mda_next_tuple`) to walk every valid combination of indexes across all dimensions in row-major order. It copies matching elements into the result as it walks. The lower bounds of each dimension in the result reflect the requested slice start positions.

The `ArraySubWorkspace` struct holds two flat integer arrays of length `MAXDIM`, `upperindex` and `lowerindex`. `array_subscript_check_subscripts` populates these when subscripts are evaluated at runtime. Then the dispatched fetch or assign function consumes them. The MAXDIM-length allocation is intentional. `array_get_slice` may write past the caller-supplied length when it iterates. So the workspace must always be full-length, even when fewer dimensions are subscripted.

## Assignment Rebuilds the Entire Array

PostgreSQL arrays are immutable varlena values, so subscript assignment can never mutate storage in place — see [[subsystems/types/array-internals|array immutability and copy-on-write]] for why a single-element update copies the whole array on disk. `UPDATE t SET arr[1] = x` dispatches to `array_set_element`. Slice assignment via `arr[2:4] = ARRAY[7,8,9]` dispatches to `array_set_slice`. Both allocate a fresh `ArrayType` and copy into it rather than touching the original.

When the array being assigned to is NULL — a common case when building an array incrementally with repeated single-element assignments — the varlena path substitutes a zero-dimensional empty array (via `construct_empty_array`) as the starting point. It then inserts the new element. The result is a singleton array. Fixed-length array types (like `point`) handle the NULL case differently. If either the container or the replacement value is NULL, the assignment is a no-op. The original value comes back unchanged.

The "fetch old" path exists for nested container assignments like `arr[1][2] = x` when the inner subscript is on a separate nested array type rather than a second dimension of the same array. In that case, the executor needs to read the existing sub-value before evaluating the right-hand expression. `array_subscript_fetch_old` reads the old element into `sbsrefstate->prevvalue` without disturbing the current result register. This is a rare code path for varlena arrays because PostgreSQL treats adjacent subscripts on an array as dimensions of the same array, not as nested containers.

## The SubscriptRoutines Interface

The `SubscriptRoutines` struct (`subscripting.h`) is the full contract between a container type and the subscripting machinery. It has two method pointers and three bool flags:

- `transform` — called at parse analysis time to coerce subscript expressions to integers, build the upper/lower index lists, and set the result type on the `SubscriptingRef` node.
- `exec_setup` — called at executor startup to allocate workspace and fill a `SubscriptExecSteps` struct with the four runtime function pointers: `sbs_check_subscripts`, `sbs_fetch`, `sbs_assign`, and `sbs_fetch_old`.
- `fetch_strict` — true for arrays: a NULL container or a NULL subscript causes the fetch to return NULL rather than entering the fetch function.
- `fetch_leakproof` — true for arrays: out-of-bounds subscript reads return NULL rather than raising errors, which allows the planner to push them below security barrier views.
- `store_leakproof` — false for arrays: out-of-bounds subscript writes raise errors.

Two distinct handlers register effectively the same `SubscriptRoutines`: `array_subscript_handler` for standard varlena arrays, and `raw_array_subscript_handler` for fixed-length raw arrays (sequences of fixed-width elements with no ArrayType overhead, such as `point`). Having separate handler OIDs in `pg_type.typsubscript` lets the catalog signal which storage model a type uses, even though the current implementation shares the same code paths.

The jsonb type registers its own handler with a completely different `transform` implementation that accepts string subscripts alongside integer ones. Any extension type can participate in the `arr[...]` syntax by implementing `SubscriptRoutines` and registering the handler function in `pg_type.typsubscript`.

## Practical Gotchas

Lower bounds other than 1 surface in real queries more often than developers expect. `SELECT (ARRAY[10,20,30])[1:2])[1]` returns 10 because the slice preserves `lbs = {1}`. But `SELECT ('{10,20,30}'::int[])[1:2])[0]` returns NULL — the result of the slice has lower bound 1, so index 0 is out of range. When the source array itself has a non-1 lower bound, the arithmetic shifts accordingly. `array_lower` and `array_upper` expose these bounds explicitly.

Out-of-bounds reads return NULL rather than an error, which can mask bugs. `arr[cardinality(arr) + 1]` returns NULL, not an index-out-of-range exception. Code that needs strict bounds checking must test the subscript against `array_lower` and `array_upper` before fetching.

Because single-element assignment copies the whole array, a PL/pgSQL loop that updates `arr[i]` for each `i` copies the array on every iteration. The expanded array representation amortises this for PL/pgSQL code inside a single function call. It does not help an application-level loop issuing repeated `UPDATE` statements.

## Related Topics

- [[subsystems/types/array-internals|Array type internals]] — on-disk format, header fields, expanded representation, GIN indexing
- [[subsystems/storage/toast|TOAST]] — how large arrays are compressed and moved out-of-line
- [[subsystems/indexes/gin|GIN]] — inverted index used for array containment and overlap queries

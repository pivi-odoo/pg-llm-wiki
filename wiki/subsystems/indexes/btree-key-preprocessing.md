---
title: "B-tree Scan Key Preprocessing and Skip Scan"
aliases:
  - btree skip scan
  - nbtree skip scan
  - _bt_preprocess_keys
  - skip array
  - btree key preprocessing
source_files:
  - src/backend/access/nbtree/nbtpreprocesskeys.c
  - src/backend/utils/adt/skipsupport.c
  - src/include/access/nbtree.h
  - src/include/utils/skipsupport.h
symbols:
  - _bt_preprocess_keys
  - _bt_preprocess_array_keys
  - _bt_preprocess_array_keys_final
  - _bt_num_array_keys
  - _bt_skiparray_shrink
  - _bt_skiparray_strat_adjust
  - BTArrayKeyInfo
  - BTScanOpaqueData
  - SkipSupportData
  - PrepareSkipSupportFromOpclass
  - SK_BT_REQFWD
  - SK_BT_REQBKWD
  - SK_BT_SKIP
  - SK_BT_MINVAL
  - SK_BT_MAXVAL
  - BTSKIPSUPPORT_PROC
---

Before a B-tree index scan begins, the raw scan keys provided by the planner are transformed into an optimised internal representation. This representation drives both the initial tree descent and per-tuple filtering as the scan proceeds. This transformation — called preprocessing — eliminates redundant conditions, detects unsatisfiable quals, and annotates each surviving key with flags. These flags let the scan stop early without testing every remaining tuple. PostgreSQL 18 extended this machinery to synthesise "skip arrays" for leading index columns that have no equality predicate. This lets the index satisfy queries on trailing columns without degenerating into a full index scan.

## What preprocessing produces

The raw `ScanKey` array from the planner lives in `scan->keyData[]`. Preprocessing copies it into `so->keyData[]` (inside `BTScanOpaqueData`), which is the form the rest of the B-tree code uses throughout the scan's lifetime. The output is not a trivial copy. Conditions on the same column are reduced to at most one equality key or one pair of range bounds. DESC-column strategies are commuted so that the physical ordering seen by the scan matches the logical ordering implied by the query. NULL comparisons are handled specially, since all btree operators are treated as strict.

Two boolean outcomes terminate preprocessing early without building any output. If a comparison value is NULL (strict operators can never match), or if the quals are demonstrably contradictory — `x = 1 AND x > 2`, or an `IN (...)` list that intersects to empty — `so->qual_ok` is set to `false`. The scan is abandoned before it starts (`_bt_preprocess_keys()`, `nbtpreprocesskeys.c`).

## Required-key annotations

Each output scan key is tagged with `SK_BT_REQFWD` and/or `SK_BT_REQBKWD`. These flags control early scan termination: once a tuple fails a required-forward key, no later tuple in the forward direction can match. The scan can then stop. The rule for assigning them is simple: a key on attribute _k_ can be marked required only if every attribute from 1 through _k_−1 has an equality (`=`) constraint. An equality key is marked both `SK_BT_REQFWD` and `SK_BT_REQBKWD`; a `<` or `<=` key is marked only `SK_BT_REQFWD`; a `>` or `>=` key only `SK_BT_REQBKWD`. Without this annotation system, the scan would have to traverse every qualifying range to the end before giving up.

When multiple redundant conditions survive (because no cross-type comparison operator was available to resolve them), `_bt_unmark_keys()` is called to ensure that no attribute has more than one required `>=/>`key and no more than one required `<=/< `key. Surplus required markings would cause `_bt_first` and `_bt_checkkeys` to disagree about the scan boundary.

## Array scan keys

`SK_SEARCHARRAY` keys — produced by planner clauses like `col = ANY(array)` or `col IN (val, …)` — are processed separately before the main loop, in `_bt_preprocess_array_keys()`. Each equality-type array key is deconstructed from its `ArrayType`. Null elements are discarded and duplicates removed. The remaining elements are then sorted in index order. Inequality-type array keys (e.g. `col < ANY(array)`) degenerate to a scalar bound by extracting the most restrictive element.

When two equality array keys target the same attribute (an unusual but legal case), they are merged by intersecting their element lists. An empty intersection means the qual is unsatisfiable. If a cross-type merge is impossible due to missing opfamily support, both arrays are kept. Redundancy resolution is then deferred.

Each surviving equality array key is paired with a `BTArrayKeyInfo` entry in `so->arrayKeys[]` that tracks the sorted element list and the index of the element currently being matched. The scan advances through array elements in `_bt_advance_array_keys()` as it traverses the index. It jumps forward to the next element's position when the current element's range is exhausted.

Single-element arrays surviving all preprocessing are converted into plain equality keys, since a plain key is faster to test than an array at runtime.

## Skip arrays (PostgreSQL 18)

**PostgreSQL 18** introduced skip arrays to handle the case where a query predicate applies to a non-leading index column. Given an index on `(a, b)` and a query `WHERE b = 42`, prior releases performed a full index scan. Preprocessing now synthesises a synthetic equality-strategy array key for `a` — called a skip array — that covers every possible value of `a`. Combined with the real `b = 42` key, this allows the scan to mark both keys as required and to jump between distinct values of `a` rather than reading every index tuple.

A skip array differs structurally from an `SK_SEARCHARRAY` equality array. Its `BTArrayKeyInfo.num_elems` is set to `-1`, the sentinel that distinguishes it from SAOP arrays (which always have a non-negative count). It also carries the `SK_BT_SKIP` flag on its scan key. Rather than holding a pre-enumerated list of values, a skip array generates its elements on demand. When the scan needs to advance past the current `a` value, it reads the next distinct `a` value directly from the index tuples it encounters. It then uses that value as the new array element.

The decision to generate a skip array is made in `_bt_num_array_keys()`. This function walks the input scan keys and identifies "gaps" — attributes that lack an equality condition but are followed by attributes that do have one. Skip arrays are generated only for the minimum prefix of gap attributes necessary to make all subsequent scan keys required. For example, given `(a, b, c, d)` with only `WHERE c = 5`, the output keys are `skip a AND skip b AND c = 5`. No skip array is generated for `d`, since there is no scan key after `d` to benefit from one.

Skip arrays are not generated when the opfamily for that attribute lacks an equality operator (`BTEqualStrategyNumber`), when a row comparison precedes the gap (since row comparisons are inherently multi-column and cannot be merged into a skip array), or when the compile-time macro `DEBUG_DISABLE_SKIP_SCAN` is set.

### Range skip arrays

When the input includes inequality conditions on an attribute that otherwise lacks an equality predicate — for example `WHERE a >= 10 AND b = 42` — preprocessing still generates a skip array for `a`. It absorbs the inequality into the array as a lower or upper bound, rather than discarding it. These bounds are stored as `BTArrayKeyInfo.low_compare` and `BTArrayKeyInfo.high_compare`. During scanning, when the array is at its MINVAL sentinel (the start of the range), `low_compare` is applied directly instead of the usual equality comparison. Similarly, when the array is at its MAXVAL sentinel, `high_compare` applies instead.

After the final bounds are established, `_bt_skiparray_strat_adjust()` may transform them. A `>` lower bound can be converted to `>=` by incrementing the bound's datum via the opclass skip support function. A `<` upper bound can be converted to `<=` by decrementing it. This avoids an extra tree descent. With a `>=` bound, the scan can include the bound value in the first insertion scan key, whereas a strict `>` bound requires an extra descent to skip past the bound value.

## SkipSupport

The increment and decrement operations drive range-bound transformation and, in a broader sense, the ability to "step to the next value" during skip scan. They come from a per-opclass support function registered as `BTSKIPSUPPORT_PROC` (support function 6). This is optional. Operator classes that do not register it can still participate in skip scans, but the scan cannot convert strict bounds into non-strict ones. This costs one additional tree descent per boundary.

`PrepareSkipSupportFromOpclass()` (`skipsupport.c`) looks up the function by opfamily and opcintype and calls it once to fill a `SkipSupportData` struct. The struct provides:

- `low_elem` and `high_elem`: the lowest and highest representable non-NULL values of the type, used to seed the initial scan.
- `decrement` and `increment`: callbacks that return the predecessor or successor of a datum. They set an overflow/underflow flag when no such value exists.

For DESC-indexed columns, `PrepareSkipSupportFromOpclass()` swaps `low_elem`/`high_elem` and swaps `decrement`/`increment` so the rest of the machinery can ignore sort direction.

Built-in numeric and text types register skip support functions in `src/backend/utils/adt/`. An operator class for a user-defined type can register its own to enable equivalent behaviour.

## IS NULL and skip arrays

If the index stores NULL values in the range covered by a skip array, the array optionally appends a NULL element. `BTArrayKeyInfo.null_elem` controls whether the NULL "slot" is active. When a scalar `IS NOT NULL` key is merged into a skip array, `null_elem` is set to `false`. The `IS NOT NULL` key is consumed as redundant — the array simply will not produce a NULL element. An `IS NULL` key is contradictory with any skip array and causes preprocessing to emit `qual_ok = false`.

## Scan key output invariants

After all preprocessing steps complete, `so->keyData[]` satisfies several invariants that the rest of the nbtree code depends on:

- Keys are ordered by `sk_attno`. Within an attribute, required keys precede non-required ones (enforced by `_bt_unmark_keys()`).
- At most one equality key exists per attribute (after SAOP merging).
- Skip array keys are always required (`SK_BT_REQFWD`), and never appear on the last index attribute.
- `so->arrayKeys[]` is ordered to match `so->keyData[]` — `BTArrayKeyInfo.scan_key` is an offset into `so->keyData[]`, established by the final pass in `_bt_preprocess_array_keys_final()`.
- `so->orderProcs[]` provides 3-way ORDER comparison functions for every required equality key and every array key, subscripted by the same `so->keyData[]` offsets.

These invariants allow `_bt_first()` and `_bt_checkkeys()` to make the same boundary decisions independently, which is essential for the array-advancement logic in `_bt_advance_array_keys()` to be correct.

## Related Topics

- [[subsystems/indexes/btree|B-tree Index Internals]] — page layout, tree descent, deduplication, and VACUUM
- [[code-paths/index-scan|Index Scan Code Path]] — executor path that invokes `btgettuple()` and drives the scan

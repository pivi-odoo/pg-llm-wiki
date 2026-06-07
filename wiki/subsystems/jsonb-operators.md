---
title: JSONB Operators
aliases:
  - jsonb operators
  - jsonb containment
  - jsonb path extraction
  - jsonb @>
  - jsonb ?
source_files:
  - src/backend/utils/adt/jsonb_op.c
  - src/backend/utils/adt/jsonfuncs.c
  - src/backend/utils/adt/jsonb_util.c
  - src/include/utils/jsonb.h
symbols:
  - jsonb_contains
  - jsonb_contained
  - jsonb_exists
  - jsonb_exists_any
  - jsonb_exists_all
  - jsonb_concat
  - jsonb_delete
  - jsonb_delete_array
  - jsonb_delete_idx
  - jsonb_delete_path
  - jsonb_object_field
  - jsonb_object_field_text
  - jsonb_array_element
  - jsonb_array_element_text
  - jsonb_extract_path
  - jsonb_extract_path_text
  - JsonbDeepContains
  - IteratorConcat
  - setPath
  - findJsonbValueFromContainer
  - getKeyJsonValueFromContainer
---

# JSONB Operators

PostgreSQL's JSONB operator set divides into four functional groups: containment tests (`@>`, `<@`), key-existence checks (`?`, `?|`, `?&`), path extraction (`->`, `->>`, `#>`, `#>>`), and structural manipulation (`||`, `-`, `#-`). Each group has distinct semantics, distinct implementation paths, and different relationships to GIN indexes. Understanding which operator does what — and which can use an index — is essential for writing efficient queries against [[subsystems/jsonb|JSONB]] columns.

## Containment: @> and <@

The containment operators ask whether one JSONB value is a structural subset of another. `val @> tmpl` returns true when every key-value pair in `tmpl` appears somewhere in `val`, recursively. `<@` is the commuted form: `tmpl <@ val` is equivalent to `val @> tmpl`. The function `jsonb_contains()` backs `@>`, and `jsonb_contained()` backs `<@`. Both call `JsonbDeepContains()` from `jsonb_util.c` after initialising a pair of `JsonbIterator` cursors.

The semantics are not equality. They are best understood as injective structural embedding:

- **Objects**: every key in the right operand must exist in the left operand with an equal value. Extra keys on the left are ignored.
- **Arrays**: every element of the right array must appear somewhere in the left array. Duplicates and order do not matter.
- **Nested containers**: containment is applied recursively at each level. A nested object on the right must be contained by a nested object at the same structural position on the left — the mapping of container nodes is parent-child–preserving.

```sql
-- true: left has both keys with matching values
SELECT '{"a": 1, "b": 2}'::jsonb @> '{"a": 1}';

-- true: [1, 2, 3] contains every element of [1, 3]
SELECT '[1, 2, 3]'::jsonb @> '[1, 3]';

-- false: nested objects must also satisfy containment
SELECT '{"x": {"a": 1, "b": 2}}'::jsonb @> '{"x": {"a": 1, "c": 3}}';
```

`JsonbDeepContains()` iterates both documents in parallel using `JsonbIteratorNext()`. For objects it uses `getKeyJsonValueFromContainer()` — the binary-search key lookup into the sorted on-disk key array — so each right-hand key lookup is O(log n) in the number of left-hand keys. When a key is found, values are compared: scalars with `equalsJsonbScalarValue()`, nested containers by recursing into `JsonbDeepContains()`. An early-exit short-circuit is available for objects. Because JSONB objects deduplicate keys, if the right operand has more pairs than the left, it cannot possibly be contained. The function then returns false immediately.

For arrays the implementation is deliberately weaker. Scalars are looked up with `findJsonbValueFromContainer()`, which scans the array linearly because arrays have no sorted structure. Nested container elements within arrays require an O(N²) nested loop: each right-hand container element is tested against every left-hand container element. The code comments this explicitly (`/* XXX: Nested array containment is O(N^2) */`). This is an inherent consequence of arrays lacking the key-sort invariant that objects enjoy.

A boundary case: a raw scalar JSONB value (stored internally as a pseudo-array with `JB_FSCALAR`) can be contained by another raw scalar or by an array, but cannot contain an array. `JsonbDeepContains()` checks `rawScalar` flags before the main loop.

Both `@>` and `<@` are covered by GIN indexes under `jsonb_ops` (strategy number `JsonbContainsStrategyNumber = 7`) and also under `jsonb_path_ops`. This makes them the primary operators for high-throughput filtering on JSONB columns. See subsystems/jsonb-query-patterns for index selection guidance.

## Key Existence: ?, ?|, ?&

The three existence operators check whether string keys (or string array elements) are present at the top level of a JSONB value. They do not recurse. All three are implemented in `jsonb_op.c`. They use `findJsonbValueFromContainer()` with `JB_FOBJECT | JB_FARRAY` flags, which searches keys in objects and string elements in arrays alike.

`?` (`jsonb_exists()`) takes a single text argument. It constructs a `JsonbValue` of type `jbvString` and calls `findJsonbValueFromContainer()` once:

```sql
-- true: 'status' is a top-level key
SELECT '{"status": "active", "ts": 1}'::jsonb ? 'status';

-- true: '1' is a string element of the array
SELECT '["a", "b", "1"]'::jsonb ? '1';

-- false: only top-level; 'b' is nested
SELECT '{"a": {"b": 1}}'::jsonb ? 'b';
```

`?|` (`jsonb_exists_any()`) takes a `text[]` argument and returns true as soon as any key in the array is found. It deconstructs the array with `deconstruct_array_builtin()` and iterates, returning on the first hit.

`?&` (`jsonb_exists_all()`) takes the same `text[]` argument but returns false as soon as any key is missing. It is the dual: a single miss short-circuits to false. It returns true only after all keys have been found.

```sql
-- ?| returns true if either key exists
SELECT '{"a": 1}'::jsonb ?| ARRAY['a', 'b'];   -- true

-- ?& returns true only if both exist
SELECT '{"a": 1}'::jsonb ?& ARRAY['a', 'b'];   -- false
```

All three existence operators use GIN indexes under `jsonb_ops` (`JsonbExistsStrategyNumber = 9`, `JsonbExistsAnyStrategyNumber = 10`, `JsonbExistsAllStrategyNumber = 11`). They are not supported by `jsonb_path_ops`, which indexes only root-to-leaf paths and cannot answer existence queries without a key component.

A common confusion: `?` matches string keys in objects and string elements in arrays. It does not match non-string values. `'[1, 2, 3]'::jsonb ? '1'` is false — integer 1 is not a string. The GIN index treats string array elements as keys (`JGINFLAG_KEY`) for exactly this reason.

## Path Extraction: ->, ->>, #>, #>>

The four path-extraction operators navigate into a JSONB document and return a sub-value. They differ on two axes: single-step vs. multi-step, and whether the result is JSONB or text.

| Operator | Right operand | Result type | Function |
|----------|--------------|-------------|----------|
| `->` | `text` (key) or `int` (index) | `jsonb` | `jsonb_object_field()` / `jsonb_array_element()` |
| `->>` | `text` or `int` | `text` | `jsonb_object_field_text()` / `jsonb_array_element_text()` |
| `#>` | `text[]` (path) | `jsonb` | `jsonb_extract_path()` |
| `#>>` | `text[]` (path) | `text` | `jsonb_extract_path_text()` |

`->` with a text argument calls `jsonb_object_field()` (`jsonfuncs.c`), which invokes `getKeyJsonValueFromContainer()` — the binary search over the sorted on-disk key array. `jsonb_object_field()` passes the result to `JsonbValueToJsonb()` to build a new varlena datum. For non-objects the function returns NULL rather than erroring.

`->` with an integer argument calls `jsonb_array_element()`, which calls `getIthJsonbValueFromContainer()`. Negative indices count from the end: `-1` is the last element. Out-of-bounds indices return NULL.

```sql
-- object field access
SELECT '{"a": {"b": 42}}'::jsonb -> 'a';          -- {"b": 42}
SELECT '{"a": {"b": 42}}'::jsonb -> 'a' -> 'b';   -- 42
SELECT '{"a": {"b": 42}}'::jsonb ->> 'a';          -- {"b": 42}  (text)

-- array element access
SELECT '[10, 20, 30]'::jsonb -> 1;    -- 20
SELECT '[10, 20, 30]'::jsonb -> -1;   -- 30
```

`->>` differs from `->` in two ways beyond the return type. For `jsonb_object_field_text()`, a value of JSON null (`jbvNull`) returns SQL NULL rather than the string `"null"`. For `jsonb_array_element_text()` the same applies. This means `->>` lossy-converts the JSONB value: booleans become `"true"` or `"false"`, numbers become their decimal string representation. Once the value is extracted as text, the original JSONB type is unrecoverable without a parse step.

`#>` and `#>>` accept a `text[]` path and call `jsonb_extract_path()` / `jsonb_extract_path_text()`. These are thin wrappers around `get_jsonb_path_all()`. That function calls `jsonb_get_element()`, which walks the path one segment at a time. `jsonb_get_element()` tries each segment as a key lookup if the current container is an object, or converts it to an integer for array indexing. If any segment does not match or the container type is wrong, `jsonb_get_element()` returns NULL. Path segments that parse as integers are tried as array indices even in object context. They still fail the key lookup.

```sql
-- equivalent to -> 'a' -> 'b'
SELECT '{"a": {"b": 42}}'::jsonb #> '{a,b}';    -- 42
SELECT '{"a": {"b": 42}}'::jsonb #>> '{a,b}';   -- 42 (text)
```

None of the four path-extraction operators can use a GIN index directly. A GIN index does not store per-key values in a form that allows field-by-field extraction. Queries that filter on `data->>'key' = 'value'` require an expression index on the extracted value, not a GIN index. The difference between `->>` returning JSONB null as SQL NULL vs. the string `"null"` matters when building expression indexes: `CREATE INDEX ON t ((data->>'flag'))` and `CREATE INDEX ON t ((data->'flag'))` produce different index types and different NULL handling.

## Concatenation: ||

`||` (`jsonb_concat()`, `jsonfuncs.c`) merges two JSONB values. The result type depends on what is being merged. The behavior is implemented in `IteratorConcat()`:

- **Object || Object**: produces a single object containing all key-value pairs from both. When the same key appears in both, the right operand's value wins. This is shallow merge. Nested objects are not recursively merged. The right value replaces the left entirely for any conflicting key.
- **Array || Array**: produces a single array with all elements from the left followed by all elements from the right, in order.
- **Object || Array** or **Array || Object**: the non-array operand is wrapped inside the result array. `{"a":1} || [2, 3]` produces `[{"a":1}, 2, 3]`. `[1, 2] || {"a":1}` produces `[1, 2, {"a":1}]`.

```sql
-- shallow object merge; right wins on conflict
SELECT '{"a": 1, "b": 2}'::jsonb || '{"b": 99, "c": 3}';
-- {"a": 1, "b": 99, "c": 3}

-- array concatenation
SELECT '[1, 2]'::jsonb || '[3, 4]';
-- [1, 2, 3, 4]

-- mixed: object wrapped into array
SELECT '{"a": 1}'::jsonb || '[2, 3]';
-- [{"a": 1}, 2, 3]
```

`IteratorConcat()` does not check for or resolve duplicate keys in the right object. It simply appends all tokens from the right after all tokens from the left. Deduplication relies on `pushJsonbValue()` / `JsonbValueToJsonb()` during the final construction step, where last-value-wins applies. The short-circuit in `jsonb_concat()` returns the non-empty operand directly when the other is an empty container of the same type, avoiding unnecessary allocation.

`||` does not use GIN indexes — it is a write-path operation producing a new datum, not a filter predicate.

## Deletion: - and #-

The deletion operator `-` removes a key from an object or an element from an array and returns the modified copy. The original datum is never mutated. All deletion functions iterate the source document with `JsonbIteratorNext()` and reconstruct it with `pushJsonbValue()`, skipping the matched key or element.

`-` with a text right operand calls `jsonb_delete()`, which matches by string equality against object keys and string array elements. Only top-level matches are removed. Nested keys are unaffected. Applying `-` to a scalar raises an error (`"cannot delete from scalar"`).

`-` with an integer right operand calls `jsonb_delete_idx()`, which counts elements positionally and skips the one at the target index. Negative indices count from the end. Applying an integer index to an object raises an error (`"cannot delete from object using integer index"`).

```sql
-- remove a key from an object
SELECT '{"a": 1, "b": 2, "c": 3}'::jsonb - 'b';
-- {"a": 1, "c": 3}

-- remove element at index 1 from an array
SELECT '[10, 20, 30]'::jsonb - 1;
-- [10, 30]

-- negative index: remove last element
SELECT '[10, 20, 30]'::jsonb - -1;
-- [10, 20]
```

The `text[]` overload of `-` (backed by `jsonb_delete_array()`) removes all keys in the supplied array in a single pass. The inner loop is a linear scan over the key array for each candidate element, so performance is O(n * k) where n is the number of document elements and k is the number of keys to delete. For large delete lists this is not index-aided.

`#-` (`jsonb_delete_path()`) removes the value at a multi-level `text[]` path. It delegates to `setPath()` with `JB_PATH_DELETE` mode. `setPath()` walks the document one path segment at a time, rebuilding each level around the deletion point. If the path does not exist, `setPath()` returns the original document unchanged.

```sql
-- remove a nested key
SELECT '{"a": {"b": 1, "c": 2}}'::jsonb #- '{a,b}';
-- {"a": {"c": 2}}

-- remove an array element by path
SELECT '{"items": [10, 20, 30]}'::jsonb #- '{items,1}';
-- {"items": [10, 30]}
```

Neither `-` nor `#-` has any index support. They always produce a new datum through full document traversal and reconstruction. For high-frequency in-place updates, the cost is proportional to document size. [[subsystems/storage/toast|TOAST]] overhead also applies for large documents.

## GIN Index Coverage Summary

```
┌─────────────┬────────────────┬────────────────┐
│  Operator   │  jsonb_ops     │ jsonb_path_ops │
├─────────────┼────────────────┼────────────────┤
│  @>         │  YES           │  YES           │
│  <@         │  YES*          │  YES*          │
│  ?          │  YES           │  NO            │
│  ?|         │  YES           │  NO            │
│  ?&         │  YES           │  NO            │
│  ->         │  NO            │  NO            │
│  ->>        │  NO            │  NO            │
│  #>         │  NO            │  NO            │
│  #>>        │  NO            │  NO            │
│  ||         │  NO            │  NO            │
│  -          │  NO            │  NO            │
│  #-         │  NO            │  NO            │
└─────────────┴────────────────┴────────────────┘
* <@ rewrites to @> for index access
```

The GIN strategy numbers for containment and existence are defined in `jsonb.h`: `JsonbContainsStrategyNumber = 7`, `JsonbExistsStrategyNumber = 9`, `JsonbExistsAnyStrategyNumber = 10`, `JsonbExistsAllStrategyNumber = 11`.

## Practical Notes for Web Applications

A common mistake when moving from key-value stores to JSONB is writing filters like `WHERE data->>'status' = 'active'`. This is a sequential scan even when a GIN index exists, because `->>` extracts text and `@>` is not involved. Rewriting the filter as a containment test, or adding an expression index on the extracted value, fixes it. See [[subsystems/jsonb-query-patterns|JSONB Query Patterns for Performance]] for the full diagnosis with EXPLAIN output and the partial-index variant.

For objects that evolve at runtime, `||` is the standard update idiom:

```sql
UPDATE events
SET data = data || jsonb_build_object('status', 'closed', 'closed_at', now())
WHERE id = $1;
```

This performs a full document read and write. For documents that grow large, consider whether specific keys should be promoted to regular columns (with a generated column in PostgreSQL 12+) to gain column-level statistics and B-tree range support. The `?` operator is well-suited to "does this document have a feature flag set" queries, and benefits from a `jsonb_ops` GIN index without needing to know the flag's value.

Array containment is order-insensitive by design. This can cause confusion:

```sql
-- true: containment ignores order
SELECT '[3, 1, 2]'::jsonb @> '[1, 2]';

-- false: looking for nested [1,2] as an element, not members
SELECT '[[1,2],[3,4]]'::jsonb @> '[1,2]';
```

The second example is false because `[1,2]` is tested as a nested container element, not as a flat set of scalars. That nested containment check runs the O(N²) loop described above.

## Related Topics

- [[subsystems/jsonb]] — storage format, binary representation, in-memory structures
- [[subsystems/jsonb-query-patterns]] — index selection, expression indexes, EXPLAIN patterns
- [[subsystems/indexes/gin]] — GIN index internals and operator class strategy numbers

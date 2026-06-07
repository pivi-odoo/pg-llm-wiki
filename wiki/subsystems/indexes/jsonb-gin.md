---
title: GIN Index Support for JSONB
aliases:
  - jsonb gin opclasses
  - jsonb_ops
  - jsonb_path_ops
source_files:
  - src/backend/utils/adt/jsonb_gin.c
  - src/include/utils/jsonb.h
symbols:
  - gin_extract_jsonb
  - gin_extract_jsonb_path
  - gin_extract_jsonb_query
  - gin_extract_jsonb_query_path
  - gin_consistent_jsonb
  - gin_consistent_jsonb_path
  - gin_triconsistent_jsonb
  - gin_triconsistent_jsonb_path
  - make_text_key
  - make_scalar_key
  - extract_jsp_query
  - execute_jsp_gin_node
---

`jsonb_gin.c` implements the bridge between [[subsystems/jsonb|JSONB's]] binary representation and [[subsystems/indexes/gin|GIN's]] inverted index machinery. It provides two distinct opclasses — `jsonb_ops` and `jsonb_path_ops`. Each decomposes a JSONB document into a flat set of GIN index entries, then reconciles GIN's per-entry match results back into a pass/fail decision for each query operator. The two opclasses make fundamentally different tradeoffs between which operators they can support and how large the resulting index is.

## Two Opclasses, Two Philosophies

The core design tension in JSONB indexing is that a document has structure — keys at known paths leading to typed values — but a GIN index stores only a flat bag of scalar entries. Any opclass must decide how much of the document's structure to encode in each entry.

`jsonb_ops` indexes every key and every value as a separate entry. A document like `{"a": {"b": 42}}` produces entries for the key `"a"`, the key `"b"`, and the numeric value `42`. This makes the index useful for a wide range of operators: `@>` (containment), `?` (key exists), `?|` (any key), and `?&` (all keys). The cost is that the index grows proportionally to the total number of keys and values in the corpus.

`jsonb_path_ops` takes the opposite tradeoff. It discards all structural information and stores only one hash per leaf value. It computes this hash by hashing the entire key path down to that value, together with the value itself. `{"a": {"b": 42}}` produces a single `uint32` hash of `(hash("a") combined with hash("b") combined with hash(42))`. Because the path is folded into the hash, the index can distinguish `{"foo": 42}` from `{"bar": 42}` even though both contain the same value. `jsonb_ops` cannot make this distinction for containment without a heap recheck. The tradeoff is that key-existence operators (`?`, `?|`, `?&`) become impossible: there is no entry for a key alone, only for a key-path-plus-value. `jsonb_path_ops` supports only `@>` containment and certain jsonpath operators.

## Entry Extraction at Index Build Time

When a row is indexed, GIN calls the opclass's `extractValue` function to decompose the stored JSONB into entries.

For `jsonb_ops`, `gin_extract_jsonb()` iterates over the JSONB binary representation using `JsonbIteratorNext()`, visiting every token in document order. Each `WJB_KEY` token produces a text entry tagged with `JGINFLAG_KEY`. Each `WJB_VALUE` token produces a text entry tagged with the appropriate scalar flag (`JGINFLAG_NULL`, `JGINFLAG_BOOL`, `JGINFLAG_NUM`, or `JGINFLAG_STR`). Array elements (`WJB_ELEM`) that are strings receive the key tag (`JGINFLAG_KEY`) rather than the string-value tag. This is an intentional imprecision that makes the `?` operator work uniformly whether a string appears as an object key or as an array element (`jsonb.h`).

The first byte of each text entry is the flag byte, followed by a canonical text representation of the value. Numeric values use `numeric_normalize()` to produce a trailing-zero-free string so that numerically equal values always produce identical entries. Entries longer than `JGIN_MAXLENGTH` (125 bytes) are replaced by an 8-hex-digit representation of their `hash_any()` value, with the `JGINFLAG_HASHED` bit set in the flag (`make_text_key()`, `jsonb.h`). This keeps every entry short enough to use a compact varlena header in the index and prevents overrunning GIN's maximum entry length. When a hashed entry appears during a query, a heap recheck is required. This costs nothing, because a recheck is already forced for other reasons.

For `jsonb_path_ops`, `gin_extract_jsonb_path()` maintains a stack of partial hashes corresponding to the nesting depth as it iterates the document. On `WJB_BEGIN_OBJECT` or `WJB_BEGIN_ARRAY`, a new stack frame inherits the parent's hash. On `WJB_KEY`, the key's hash is mixed into the current frame via `JsonbHashScalarValue()`. On `WJB_VALUE` or `WJB_ELEM`, the leaf value's hash is mixed in, and the combined hash is emitted as a `uint32` GIN entry. The frame hash is then reset to the parent's value, ready for the next sibling key. This stack discipline means that an entry for `{"a": {"b": 42}}` incorporates hashes of both `"a"` and `"b"` before the value `42`. As a result, the resulting hash identifies the complete path, not just the value.

## Query Entry Extraction

When a query is executed, GIN calls the opclass's `extractQuery` function to produce a set of entries that must be present in any matching document. The strategy number identifies which operator is being used.

For `jsonb_ops` with `@>` (strategy `JsonbContainsStrategyNumber`), `gin_extract_jsonb_query()` simply calls `gin_extract_jsonb()` on the query JSONB, reusing the same decomposition. An empty query (`{}` or `[]`) produces zero entries, which is handled by setting `GIN_SEARCH_MODE_ALL` to force a full index scan.

For `?` (strategy `JsonbExistsStrategyNumber`), the query is a single text string treated as a key entry. For `?|` and `?&`, the query is a text array. Each element becomes a key entry.

For the jsonpath operators `@@` and `@?` (strategies `JsonbJsonpathPredicateStrategyNumber` and `JsonbJsonpathExistsStrategyNumber`), `extract_jsp_query()` parses the jsonpath expression and walks its AST to extract a logical tree of GIN entries. The result is not a flat list but a tree of AND/OR nodes (`JsonPathGinNode`) where the leaves are index entries. This tree is stored in GIN's `extra_data` array alongside the flat entry list so that the consistent function can later evaluate it.

The jsonpath extraction logic recognizes two statement forms: `path == scalar` (a specific value at a specific path) and `EXISTS(path)` (existence of a path). For `jsonb_ops`, a `path == scalar` statement decomposes into AND-ed key entries for each `.key` step in the path, combined with the scalar value entry. For `jsonb_path_ops`, the same statement produces a single hash entry computed the same way as at index build time (`jsonb_path_ops__extract_nodes()`, `jsonb_path_ops__add_path_item()`). Neither opclass extracts EXISTS statements, because key-path entries alone are too non-selective to be useful. Extracting them would mislead the planner about index selectivity.

The `@@` and `@?` operators are equivalent in expressiveness: `jb @? 'path'` is identical to `jb @@ 'EXISTS(path)'`, and vice versa. The extraction code therefore handles them symmetrically. When no entries can be extracted from a jsonpath expression (for example, it uses operators or item methods that the opclass does not understand), the extraction function sets `searchMode` to `GIN_SEARCH_MODE_ALL`. GIN then falls back to a sequential scan of all indexed tuples.

## Consistency and Recheck

After GIN looks up each extracted entry in the index, it calls the opclass's consistent function with a boolean array indicating which entries matched. The consistent function must return true if the document might satisfy the query.

For operators where all extracted entries must be present (`@>` and `?&`), both `gin_consistent_jsonb()` and `gin_triconsistent_jsonb()` return false as soon as any entry is absent, and otherwise return `GIN_MAYBE` (the triconsistent form never returns `GIN_TRUE`). For operators where at least one entry suffices (`?|`), the consistent function returns `GIN_MAYBE` if any entry matched.

For jsonpath operators, `execute_jsp_gin_node()` recursively evaluates the AND/OR tree stored in `extra_data[0]`, substituting each leaf's match status from the `check` array. AND nodes require all children to be non-false. OR nodes require at least one child to be true or maybe. The triconsistent variant operates on three-valued logic (`GIN_TRUE`/`GIN_FALSE`/`GIN_MAYBE`) directly. Even when `execute_jsp_gin_node()` returns `GIN_TRUE`, the consistent function downgrades the result to `GIN_MAYBE`, because a heap recheck is always required. GIN index entries do not encode positional or structural information. As a result, the index alone cannot verify that matched keys and values appear in the required relationship to each other.

Both opclasses set the `*recheck = true` flag unconditionally for all strategies. For `jsonb_ops`, this is necessary because the index cannot verify structural nesting. A document containing key `"a"` and value `42` separately would produce the same entries as `{"a": 42}`. The executor must therefore recheck the heap tuple to confirm the containment relationship. For `jsonb_path_ops`, hash collisions add a further reason: two different key-path-value combinations can produce the same `uint32` hash.

## The Path Hash in Detail

The `jsonb_path_ops` hash is not a simple hash of the value. It is a running combination that accumulates as the iterator descends into the document. The `PathHashStack` maintains one `uint32` hash per nesting level. When a key is encountered, `JsonbHashScalarValue()` mixes the key into the current level's hash. When a value is encountered, `JsonbHashScalarValue()` mixes in the value. The combined result is then emitted. The stack frame's hash is then reset to the parent's value so that sibling keys start from the same base.

This means that for `{"a": 1, "a": 2}` (after deduplication), the entries for values `1` and `2` both incorporate the hash of key `"a"`. The value hash differs, though, so the entries remain distinct. For `{"a": {"b": 1}}`, the entry incorporates hashes of both `"a"` and `"b"`, making it structurally more specific than a flat `{"b": 1}` would be. Arrays are transparent to the hash accumulation: `WJB_BEGIN_ARRAY` pushes a frame that inherits the parent hash unchanged. As a result, array elements carry the same path hash as they would if stored directly at the object level. This is noted in the source as a minor imprecision with no practical consequence.

On the query side, `jsonb_path_ops__add_path_item()` performs the same incremental hashing over the jsonpath's key steps. As a result, the hash produced for a query value matches the hash stored for the corresponding document value, provided the path is fully specified. `jsonb_path_ops` does not support wildcard steps (`..*`, `.**`), because there is no single hash to produce for an unknown key sequence.

## Operator Support Summary

| Operator | jsonb_ops | jsonb_path_ops |
|----------|-----------|----------------|
| `@>` | yes (recheck) | yes (recheck) |
| `?` | yes (recheck) | no |
| `?|` | yes (recheck) | no |
| `?&` | yes (recheck) | no |
| `@@` | partial (falls back on unsupported paths) | partial |
| `@?` | partial (falls back on unsupported paths) | partial |

The jsonpath operators receive GIN support only for path patterns the opclass understands. Expressions involving arithmetic, item methods, or unsupported accessor types cause `extract_jsp_query()` to return NULL entries, triggering a full index scan with heap-level evaluation.

## See Also

- [[subsystems/jsonb|JSONB storage format]]
- [[subsystems/indexes/gin|GIN index internals]]

## Related Topics

- [[subsystems/types/json-type|JSONB type internals]] — binary representation and operators that the opclasses index
- [[subsystems/indexes/gin|GIN index internals]] — the inverted-index AM that both opclasses plug into
- [[subsystems/indexes/index-am|Index Access Method interface]] — the API contract every opclass must satisfy
- [[subsystems/planner/selectivity-estimation|Selectivity estimation]] — how the planner estimates the cost of a GIN index scan
- [[subsystems/planner/statistics|Planner statistics]] — column statistics used when choosing between jsonb_ops and jsonb_path_ops
- [[subsystems/indexes/partial-indexes|Partial indexes]] — partial predicates can further narrow a jsonb GIN index

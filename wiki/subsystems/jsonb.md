---
title: JSONB Storage and Indexing
aliases:
  - jsonb
  - JSONB
  - jsonb binary format
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/jsonb.c
  - src/backend/utils/adt/jsonb_util.c
  - src/include/utils/jsonb.h
symbols:
  - Jsonb
  - JsonbContainer
  - JsonbValue
  - JEntry
  - JsonbIterator
  - JsonbPair
  - JsonbParseState
  - JsonbDeepContains
  - findJsonbValueFromContainer
  - getKeyJsonValueFromContainer
  - JsonbIteratorNext
  - JsonbValueToJsonb
  - uniqueifyJsonbObject
  - JB_FOBJECT
  - JB_FARRAY
  - JENTRY_HAS_OFF
  - JB_OFFSET_STRIDE
---

# JSONB Storage and Indexing

PostgreSQL ships two JSON column types that look identical at the SQL surface but diverge completely in what they store on disk. `json` preserves the input byte-for-byte: whitespace, key ordering, and duplicate keys all survive a round-trip. `jsonb` parses the input at write time, discards whitespace, deduplicates keys (last-value-wins), sorts object keys, and writes a compact binary tree. That up-front cost buys everything that matters for querying: O(log n) key lookup, containment tests without string scanning, and indexable operators.

## The Binary Format

A `jsonb` datum is a standard varlena. The first four bytes hold the total size (accessed via `VARSIZE()`/`SET_VARSIZE()`). The rest is a `JsonbContainer` tree. Because it is varlena, large values compress and migrate to TOAST exactly like any other variable-length column (see [[subsystems/storage/toast]]).

The root `JsonbContainer` carries a 32-bit header field followed by a flat array of `JEntry` values, then the variable-length data for all children laid out contiguously after the array:

```c
typedef struct JsonbContainer {
    uint32   header;                      /* count + type flags */
    JEntry   children[FLEXIBLE_ARRAY_MEMBER];
    /* variable-length data follows */
} JsonbContainer;
```

The header encodes both the element/pair count (lower 28 bits, `JB_CMASK`) and the container type (`JB_FOBJECT`, `JB_FARRAY`, `JB_FSCALAR`). A scalar value — a lone string, number, or boolean — is stored as a single-element array with `JB_FSCALAR | JB_FARRAY` set, because a root node must always be a container.

### JEntry: Type and Location in 32 Bits

Each `JEntry` is a `uint32` that packs the child's type and its location in the data region:

| Bits | Field | Meaning |
|------|-------|---------|
| 31 | `JENTRY_HAS_OFF` | 1 = field holds end-offset; 0 = holds length |
| 30–28 | `JENTRY_TYPEMASK` | `ISSTRING`, `ISNUMERIC`, `ISBOOL_TRUE`, `ISBOOL_FALSE`, `ISNULL`, `ISCONTAINER` |
| 27–0 | `JENTRY_OFFLENMASK` | 28-bit length or end+1 offset |

Storing an offset rather than a length enables O(1) random access: to find child *i*, read `JEntry[i]` and either use its stored offset directly or walk back to the nearest stride boundary. Storing a length instead enables better TOAST compression, because a length array (many small similar values) compresses far better than an offset array (monotonically increasing values). The format resolves this tension with a stride: every `JB_OFFSET_STRIDE` (32) entries store an offset; the rest store lengths. Locating any child requires examining at most 32 entries — O(1) regardless of container size (`getJsonbOffset()`, `jsonb_util.c`).

### Object Layout: Keys First, Values After

For an object with *n* key-value pairs, the `children` array holds `2n` `JEntry` values: all *n* key entries first, then all *n* value entries. The variable-length data area mirrors that layout. This keeps keys contiguous in memory, making the binary search over keys cache-friendly.

Keys appear in sorted order (by length first, then lexicographically via `lengthCompareJsonbPair()`). This is what makes `getKeyJsonValueFromContainer()` a binary search rather than a linear scan. The function divides on `stopMiddle`, fetches the candidate key's address from `baseAddr + getJsonbOffset(container, stopMiddle)`, and compares with `lengthCompareJsonbString()`. When a key matches, it adds *n* to the index to land on the corresponding value entry.

`uniqueifyJsonbObject()` eliminates duplicate keys during construction. `JsonbPair.order` records the original insertion order, so that "last value wins" is deterministic.

### Nested Containers

When a value is itself an object or array, its `JEntry` type bits are set to `JENTRY_ISCONTAINER`. The value data is another inline `JsonbContainer` subtree. The iterator (`JsonbIteratorNext()`) recurses into nested containers transparently, maintaining a linked stack of `JsonbIterator` nodes so callers can traverse arbitrarily deep trees without managing recursion themselves.

## In-Memory Representation

The on-disk `Jsonb` and the in-memory `JsonbValue` are distinct types. `JsonbValue` is a tagged union used during construction and manipulation:

| `jbvType` | Meaning |
|-----------|---------|
| `jbvNull` | SQL NULL value |
| `jbvString` | `{len, val}` — not null-terminated |
| `jbvNumeric` | PostgreSQL `Numeric` |
| `jbvBool` | C `bool` |
| `jbvArray` | In-memory array of `JsonbValue` elems |
| `jbvObject` | In-memory array of `JsonbPair` |
| `jbvBinary` | Pointer into an existing on-disk `JsonbContainer` (zero-copy slice) |
| `jbvDatetime` | Used for jsonpath datetime comparisons only |

The `jbvBinary` variant is the performance key: operators that navigate into a document but don't need to deserialize nested structures return a `jbvBinary` pointing directly into the heap tuple, avoiding any copying. `JsonbToJsonbValue()` wraps the entire on-disk buffer as a single `jbvBinary` for exactly this reason.

## Operators

| Operator | Description | Result type |
|----------|-------------|-------------|
| `->` | Get object field or array element | `jsonb` |
| `->>` | Get object field or array element | `text` |
| `#>` | Get value at path | `jsonb` |
| `#>>` | Get value at path | `text` |
| `@>` | Left contains right | `boolean` |
| `<@` | Left is contained by right | `boolean` |
| `?` | Key exists in object / value exists in array | `boolean` |
| `?\|` | Any of the given keys exist | `boolean` |
| `?&` | All of the given keys exist | `boolean` |
| `\|\|` | Concatenate / merge | `jsonb` |
| `-` | Delete key or element | `jsonb` |
| `#-` | Delete at path | `jsonb` |

Path operators (`#>`, `#>>`) accept a `text[]` array of keys and array indices. They call `jsonb_get_element()`, which iterates the path segments and recurses through nested containers one level at a time.

## Containment

Containment (`@>`) is the conceptual center of JSONB querying. The semantics are:

- **Objects**: every key-value pair of the right-hand side must appear in the left-hand side. Extra pairs on the left are fine.
- **Arrays**: every value in the right-hand array must appear somewhere in the left-hand array. Order does not matter.
- **Nesting**: containment is recursive — a nested object on the right must be contained by some nested object at the same structural position on the left.

Formally this is top-down unordered subtree isomorphism. `JsonbDeepContains()` implements it by iterating both documents in parallel. For objects it uses `getKeyJsonValueFromContainer()` — the binary-search key lookup — to find each right-hand key without scanning the left-hand side linearly. For arrays it falls back to a linear scan because arrays have no sorted key structure.

The pair-count short-circuit is only safe for objects: because key deduplication guarantees that each key appears exactly once, an object with fewer pairs than the right-hand side cannot possibly contain it. Arrays have no such guarantee and cannot use the same shortcut.

## GIN Indexing

GIN (Generalized Inverted Index, [[subsystems/indexes/gin]]) inverts a document into a set of index entries. For JSONB there are two operator classes:

### jsonb_ops (default)

This class indexes every key and every scalar value in the document. GIN's entry format uses a one-byte prefix to distinguish the entry type:

| Flag | Meaning |
|------|---------|
| `JGINFLAG_KEY` (0x01) | Object key or string array element |
| `JGINFLAG_NULL` (0x02) | Null value |
| `JGINFLAG_BOOL` (0x03) | Boolean value |
| `JGINFLAG_NUM` (0x04) | Numeric value |
| `JGINFLAG_STR` (0x05) | String value (non-array-element) |
| `JGINFLAG_HASHED` (0x10) | OR'd in when the value was hashed |

`jsonb_ops` hashes strings longer than `JGIN_MAXLENGTH` (125 bytes) to an 8-hex-digit representation, to cap the length of a GIN entry and ensure the datum fits a short varlena header. Hashed entries require a heap recheck. That recheck is essentially free, because JSONB GIN always rechecks anyway.

This class supports `@>`, `?`, `?|`, and `?&`. The "exists" operators work because `jsonb_ops` indexes string array elements with `JGINFLAG_KEY`, treating them equivalently to object keys.

### jsonb_path_ops

This class indexes only paths from root to leaf, hashing the entire path into a single `uint32`. This produces a much smaller index than `jsonb_ops` because intermediate keys are not indexed independently. The trade-off is that this class supports only `@>` — key-existence queries require `jsonb_ops`.

### Choosing Between Them

```
-- jsonb_ops: general-purpose, supports ?, ?|, ?&
CREATE INDEX ON events USING GIN (payload);

-- jsonb_path_ops: smaller, faster for pure containment
CREATE INDEX ON events USING GIN (payload jsonb_path_ops);
```

For workloads dominated by containment queries on deeply nested documents, `jsonb_path_ops` wins on both size and scan speed. For key-existence checks or mixed workloads, `jsonb_ops` is required.

## jsonpath (PostgreSQL 12+)

`jsonpath` is a dedicated path language, analogous to XPath for XML. A jsonpath expression can filter, project, and test JSON documents:

```sql
-- All prices > 100
SELECT jsonb_path_query(doc, '$.items[*] ? (@.price > 100)');

-- Predicate: does any item cost more than 100?
SELECT doc @@ '$.items[*].price > 100';
```

The `@@` operator uses strategy numbers `JsonbJsonpathExistsStrategyNumber` (15) and `JsonbJsonpathPredicateStrategyNumber` (16). GIN also supports these strategy numbers. As a result, a GIN index can accelerate jsonpath predicates that reduce to containment-equivalent checks.

`jsonb_path_query()`, `jsonb_path_exists()`, and `jsonb_path_match()` are the function equivalents. The path language supports arithmetic, string methods, datetime functions, and recursive descent (`.**`).

**PostgreSQL 17** completed the SQL/JSON function set with full SQL-standard coverage. `JSON_TABLE()` is usable in the `FROM` clause to shred a JSON document into a relational table. Its `NESTED PATH` clause handles nested arrays by producing cross-joined rows. `JSON_EXISTS(doc, path)` tests whether a jsonpath expression matches any value. `JSON_VALUE(doc, path)` extracts a scalar with `DEFAULT ... ON ERROR` / `ON EMPTY` clauses for safe error handling. `JSON_QUERY(doc, path)` extracts a JSON object or array. It accepts `WITH WRAPPER` to force array wrapping of scalar results. The constructor functions `JSON()`, `JSON_SCALAR()`, and `JSON_SERIALIZE()` round out the set. **PostgreSQL 17** also added type-conversion methods on the `jsonpath` type itself — `.bigint()`, `.boolean()`, `.date()`, `.decimal()`, `.integer()`, `.number()`, `.string()`, `.time()`, `.time_tz()`, `.timestamp()`, `.timestamp_tz()`. These let a jsonpath expression cast a matched value to a SQL type inline within the path expression, rather than requiring a separate cast after extraction.

## JSONB vs. EAV and Typed Columns

JSONB enables semi-structured data — columns whose schema varies per row — without `ALTER TABLE`. The cost is real. JSONB has no column-level statistics. The planner cannot use histogram bounds on values inside a document. Type safety lives only in application code or in `CHECK` constraints with jsonpath predicates. Queries against typed columns with [[subsystems/indexes/btree]] indexes will generally outperform equivalent JSONB queries, especially for range scans.

EAV (entity-attribute-value) tables spread semi-structured data across rows. This means joins and pivots become expensive as attribute counts grow. JSONB keeps one row per entity, keeps the document readable, and lets GIN cover many access patterns. For truly schemaless or frequently evolving data, JSONB is usually the better choice; for high-cardinality columns queried with range predicates, a typed column wins.

## Version History

**PostgreSQL 18** extended `jsonb_strip_nulls()` and `json_strip_nulls()` with an optional boolean parameter. When the parameter is true, it also removes null elements from arrays. Previously both functions only stripped object keys whose value was JSON null, leaving null array elements intact. **PostgreSQL 18** also changed the behavior of casting a `jsonb` null literal to a scalar SQL type: the cast now returns SQL `NULL` instead of raising an error, aligning jsonb null with SQL's convention for null.

## Key Data Structures

| Structure | Role |
|-----------|------|
| `Jsonb` | Top-level varlena datum; contains `vl_len_` + root `JsonbContainer` |
| `JsonbContainer` | On-disk node: header (count + type flags) + `JEntry[]` + data |
| `JEntry` | 32-bit per-child descriptor: type bits + offset-or-length |
| `JsonbValue` | In-memory tagged union for construction and manipulation |
| `JsonbPair` | In-memory key+value pair with original `order` for dedup |
| `JsonbParseState` | Stack frame during incremental construction via `pushJsonbValue()` |
| `JsonbIterator` | Cursor for sequential traversal; forms a parent-linked stack for nesting |

## Related Topics

- [[subsystems/jsonb-operators|JSONB Operators]] — detailed reference for the operator set (`->`, `@>`, `?`, `||`, etc.) that is introduced in this article.
- [[subsystems/jsonb-query-patterns|JSONB Query Patterns]] — practical patterns for querying JSONB documents, building on the containment and path semantics described here.
- [[subsystems/jsonb-subscripting|JSONB Subscripting]] — covers the `jsonb[key]` subscript syntax added in PostgreSQL 14, an alternative access path to the `->` operator.
- [[subsystems/indexes/gin|GIN Index]] — the Generalized Inverted Index internals; JSONB's `jsonb_ops` and `jsonb_path_ops` operator classes are built on top of GIN.
- [[subsystems/indexes/jsonb-gin|JSONB GIN Index]] — specifics of the JSONB GIN operator classes, entry format, hashing strategy, and scan behaviour.
- [[subsystems/storage/toast|TOAST]] — the variable-length storage mechanism that compresses and off-pages large JSONB values, referenced in the Binary Format section.
- [[subsystems/types/json-type|JSON Type]] — the companion `json` type that preserves input text verbatim, contrasted with JSONB throughout this article.

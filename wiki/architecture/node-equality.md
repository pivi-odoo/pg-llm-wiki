---
title: "Node Equality, Value Nodes, and Extensible Nodes"
aliases:
  - equal()
  - equalfuncs
  - ExtensibleNode
  - RegisterExtensibleNodeMethods
  - MultiBitmapSet
  - Value nodes
  - makeInteger
  - makeString
tags:
  - theme/extensibility
source_files:
  - src/backend/nodes/equalfuncs.c
  - src/backend/nodes/extensible.c
  - src/backend/nodes/multibitmapset.c
  - src/backend/nodes/value.c
symbols:
  - equal
  - _equalExtensibleNode
  - RegisterExtensibleNodeMethods
  - GetExtensibleNodeMethods
  - RegisterCustomScanMethods
  - GetCustomScanMethods
  - ExtensibleNode
  - ExtensibleNodeMethods
  - makeInteger
  - makeFloat
  - makeBoolean
  - makeString
  - makeBitString
  - mbms_add_member
  - mbms_add_members
  - mbms_int_members
  - mbms_is_member
  - mbms_overlap_sets
---

Several utility facilities in `src/backend/nodes/` complete the [[architecture/node-infrastructure|node infrastructure]]: structural equality comparison for query and plan trees, scalar value nodes for the raw parse tree, a registry mechanism that lets extensions embed custom node types in plan trees, and a two-dimensional bitmapset for tracking (relation, column) pairs in the planner. Each facility has a distinct role. But all four are tightly coupled to the same node-tag and tree-serialization infrastructure that the rest of the backend depends on.

## Structural Equality and Plan Cache Hit Detection

`equal()` (`equalfuncs.c`) performs a deep, field-by-field comparison of two node trees. It dispatches on `NodeTag`. It then calls a per-node comparison function generated from the node definitions. The comparison macros — `COMPARE_SCALAR_FIELD`, `COMPARE_NODE_FIELD`, `COMPARE_STRING_FIELD`, `COMPARE_BITMAPSET_FIELD` — make each generated function a straightforward enumeration of the node's fields.

The most significant design choice is that comparison deliberately excludes parse-location fields. `COMPARE_LOCATION_FIELD` expands to a no-op. This means two nodes that are structurally identical but came from different positions in a query string — or from independently parsed copies of the same query — compare as equal. Without this, the plan cache would fail to recognize a resubmitted query as matching a cached generic plan, because the byte offsets in the text would differ.

The plan cache uses `equal()` to check whether the post-analysis `Query` tree of a new query matches a cached generic plan's `Query` tree (after stripping constants). A match means the cached plan can be reused without re-planning. The equivalence-class machinery in the planner also uses `equal()` to detect duplicate join clauses: if two `RestrictInfo` nodes compare equal, they encode the same constraint and can be merged.

`Const` nodes require a custom comparison function `_equalConst`, because their value field is a `Datum`. Comparing a `Datum` requires `datumIsEqual()`, not simple pointer equality. NULL constants of the same type are always considered equal, because `datumIsEqual` cannot operate on null values.

## Value Nodes in the Raw Parse Tree

`Integer`, `Float`, `Boolean`, `String`, and `BitString` (defined in `src/include/nodes/value.h`, constructed in `value.c`) are thin wrapper nodes used in raw parse trees to carry literal scalar values before semantic analysis assigns PostgreSQL types to them. The parser produces these when it scans integer literals, floating-point literals, boolean keywords, quoted strings, and bit-string literals. Constructor functions — `makeInteger()`, `makeFloat()`, `makeBoolean()`, `makeString()`, `makeBitString()` — simply allocate the appropriate node and fill in its single value field.

These types exist only in the raw tree. After the analyzer runs, literals become typed `Const` nodes with a specific type OID, a `Datum` value, and all the type metadata the executor needs. The `Value` types carry just enough information to survive the textual parse phase: `Float` stores a string representation rather than a C `double`, preserving exact source text for the analyzer to interpret with full type context.

## ExtensibleNode: Custom Node Types Without Core Modifications

`T_ExtensibleNode` is a single `NodeTag` value shared by all extension-defined node types. An extension that needs to embed its own struct in a plan tree — most commonly a custom scan provider — declares a struct whose first member is `ExtensibleNode`. It sets the `extnodename` field to a unique string. It registers an `ExtensibleNodeMethods` table at startup via `RegisterExtensibleNodeMethods()` (`extensible.c`).

The methods table provides four callbacks:

| Callback | Purpose |
|---|---|
| `nodeCopy` | Deep-copy the extension fields |
| `nodeEqual` | Compare the extension fields for equality |
| `nodeOut` | Serialize the extension fields to text |
| `nodeRead` | Deserialize the extension fields from text |

When the core node infrastructure encounters a `T_ExtensibleNode`, it looks up the registered methods by `extnodename` using `GetExtensibleNodeMethods()`. It then delegates to the appropriate callback. For equality, `_equalExtensibleNode` first confirms the `extnodename` fields match, ensuring both nodes are the same extension type. It then calls `methods->nodeEqual`. A parallel registry (`extensible_node_methods` and `custom_scan_methods`) handles `CustomScan` nodes separately via `RegisterCustomScanMethods()` and `GetCustomScanMethods()`.

The consequence of this design is that extension-defined nodes survive the full plan lifecycle — `EXPLAIN`, parallel query plan shipping, and any other path that serializes and deserializes a plan tree — without requiring any modification to the core `NodeTag` enum or the generated copy/equal/out/read dispatch tables. Extensions must register before PostgreSQL serializes or deserializes any plan containing their node type. In practice, this means registering in the shared library's `_PG_init()` function.

## MultiBitmapSet: Two-Dimensional Membership Sets

A `MultiBitmapSet` (`multibitmapset.c`) is a `List` of `Bitmapset *` pointers, where the list index provides one coordinate and the bit position within a `Bitmapset` provides the other. The primary use in the planner is tracking which (varno, varattno) pairs — that is, which (relation, column) combinations — appear in a set of expressions.

The implementation represents the empty set as `NIL`, consistent with the convention for `List *`. Accessing an absent list element returns an empty set rather than an error. Adding a member to an out-of-range list index extends the list with `NULL` entries as padding. The API mirrors the single-dimensional `Bitmapset` API:

| Function | Purpose |
|---|---|
| `mbms_add_member(a, listidx, bitidx)` | Add a single (listidx, bitidx) pair |
| `mbms_add_members(a, b)` | Union: add all members of `b` to `a` in-place |
| `mbms_int_members(a, b)` | Intersection: reduce `a` to members also in `b` in-place |
| `mbms_is_member(listidx, bitidx, a)` | Membership test |
| `mbms_overlap_sets(a, b)` | Returns a `Bitmapset` of list indexes where the two sets overlap |

Like single-dimensional bitmapsets, mutation functions return a (possibly reallocated) `List *` that the caller must use. The input pointer may be stale after the call. `mbms_overlap_sets` is the most distinctive operation: it returns not a `MultiBitmapSet` but a plain `Bitmapset` of list indexes, making it efficient to ask "which relations have overlapping column sets between these two expression sets?" without iterating the full 2-D space.

## See also

- [[architecture/node-infrastructure|Node Infrastructure]]
- [[architecture/node-list|Node Lists and Linked Lists]]
- [[subsystems/planner/overview|Planner Overview]]
- [[subsystems/parser/overview|Parser Overview]]
- [[subsystems/extensions/custom-scan|Custom Scan Providers]]

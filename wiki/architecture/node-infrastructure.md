---
title: "Node Infrastructure"
aliases:
  - NodeTag
  - makeNode
  - nodeTag
  - IsA
  - castNode
  - expression_tree_walker
  - expression_tree_mutator
  - Bitmapset
  - nodeToString
  - outfuncs
  - makefuncs
source_files:
  - src/include/nodes/nodes.h
  - src/backend/nodes/makefuncs.c
  - src/backend/nodes/bitmapset.c
  - src/backend/nodes/nodeFuncs.c
  - src/backend/nodes/outfuncs.c
  - src/backend/nodes/readfuncs.c
  - src/backend/nodes/copyfuncs.c
symbols:
  - Node
  - NodeTag
  - makeNode
  - nodeTag
  - IsA
  - castNode
  - Bitmapset
  - bms_add_member
  - bms_is_member
  - bms_overlap
  - expression_tree_walker
  - expression_tree_mutator
  - query_tree_walker
  - exprType
  - exprLocation
  - nodeToString
  - stringToNode
  - copyObject
  - makeVar
  - makeConst
  - makeFuncExpr
---

Every object in PostgreSQL's query-processing pipeline — parse tree nodes, planner paths, plan nodes, executor state structs — is a tagged C struct that begins with a `NodeTag` field. This common prefix lets the backend treat any tree node polymorphically: a single `Node *` pointer can hold a `Var`, a `FuncExpr`, a `SortPath`, or a `Plan`. The tag identifies which type it actually holds. The infrastructure in `src/backend/nodes/` provides the allocation macros, convenience constructors, type inspection utilities, tree-traversal callbacks, and textual serialization that the rest of the backend relies on constantly.

## The NodeTag System

Every node type has a corresponding `T_TypeName` constant in the `NodeTag` enum (nodes.h). The `Node` base struct holds nothing but the tag:

```c
typedef struct Node {
    NodeTag type;
} Node;
```

Because every concrete node type places its `NodeTag` first, any node pointer can be safely cast to `Node *` to read the tag, regardless of the actual type. The standard idiom for type-safe access is `nodeTag(ptr)` (a macro reading the first field), `IsA(ptr, TypeName)` (a boolean tag check), and `castNode(TypeName, ptr)` (asserts `IsA` in debug builds before casting).

New nodes are allocated with the `makeNode(_type_)` macro (nodes.h):

```c
#define makeNode(_type_)  ((_type_ *) newNode(sizeof(_type_), T_##_type_))
```

`newNode` calls `palloc0` to zero-fill the struct, then writes the tag. Unset optional fields therefore default to zero/NULL without explicit initialization.

## Convenience Constructors (makefuncs.c)

`makefuncs.c` provides factory functions for the most frequently constructed node types. These are thin wrappers that allocate a node and fill in its required fields:

- `makeVar` — constructs a `Var` node (a reference to a relation column) given varno, attno, type OID, typmod, collation, and varlevelsup.
- `makeConst` — constructs a `Const` node from a `Datum` and type metadata.
- `makeTargetEntry` — wraps an expression in a `TargetEntry` with a given resno and name.
- `makeFuncExpr` — builds a `FuncExpr` from a function OID, arguments list, and return type.
- `makeSimpleA_Expr` — builds a pre-analysis `A_Expr` (raw parse tree operator expression) from an unqualified operator name.
- `makeTypeNameFromNameList` / `makeTypeNameFromOid` — construct `TypeName` nodes used during parse analysis.

The rationale for centralising these is not code reuse for its own sake. Centralising them documents the required fields and their initialization invariants once, in one place, so callers do not need to know which fields are mandatory. For example, `makeVar` sets `varnosyn` and `varattnosyn` to mirror `varno` and `varattno`, which is the correct default for newly minted Vars (the synonymous fields are only distinct after certain rewriting steps).

## Bitmapsets (bitmapset.c)

A `Bitmapset` is a variable-length set of non-negative integers stored as an array of machine words, with each bit representing membership of the corresponding integer. The empty set is always represented as `NULL` — there is no non-null zero-length bitmapset. Bitmapsets appear throughout the planner and executor wherever a compact set of column numbers, attribute numbers, or relation indexes is needed:

- `RelOptInfo.attrs_used` — the set of column attribute numbers needed from a relation.
- `Relids` — typedef for `Bitmapset *`, used pervasively in the planner to represent sets of relation indexes (RT indexes) that appear in a join.
- `PlannerInfo.all_baserels` — all base-relation RT indexes in the query.
- `IndexOptInfo.indrelid` — attribute numbers covered by an index.

The primary operations are:

| Function | Purpose |
|---|---|
| `bms_make_singleton(x)` | Create a set containing exactly `x` |
| `bms_add_member(a, x)` | Return `a` with `x` added (may palloc) |
| `bms_del_member(a, x)` | Return `a` with `x` removed |
| `bms_is_member(x, a)` | Test membership |
| `bms_overlap(a, b)` | True if the sets share any element |
| `bms_is_subset(a, b)` | True if `a ⊆ b` |
| `bms_union` / `bms_intersect` / `bms_difference` | Set operations (allocate new result) |
| `bms_next_member(a, prev)` | Iterate over members in ascending order |

The word size is `BITS_PER_BITMAPWORD` (32 or 64 depending on platform). Operations use hardware popcount and bit-scan intrinsics when available (`port/pg_bitutils.h`). Bitmapsets are palloc'd values without reference counting, so mutation functions return a (possibly new) pointer. The caller must use this returned pointer, similar to `List *`.

## Tree Walkers and Mutators (nodeFuncs.c)

`nodeFuncs.c` provides two complementary generic tree-traversal mechanisms that eliminate the need for every pass to hand-code its own node-type dispatch.

`expression_tree_walker(node, walker, context)` performs a depth-first walk of an expression tree, calling the user-supplied `walker` callback on each node. The callback returns `true` to stop the walk early (useful for existence tests) or `false` to continue. If the callback does not recognize a node type, it should call `expression_tree_walker` recursively on it. This lets the generic walker handle the children it knows about.

`expression_tree_mutator(node, mutator, context)` works similarly but constructs and returns a new (modified) copy of the tree. The mutator callback can return a replacement node or call `expression_tree_mutator` to recursively copy-and-transform the subtree. Substitution passes that rewrite Vars, replace Params, or adjust type information during planning and rewriting use this pattern.

`query_tree_walker` and `query_tree_mutator` extend these to also visit range table entries, CTE lists, and other top-level query structures that `expression_tree_walker` does not descend into.

`nodeFuncs.c` also provides a family of type-inspection functions that extract metadata from expression nodes without the caller needing to know the concrete type:

- `exprType(expr)` — returns the `Oid` of the result type.
- `exprTypmod(expr)` — returns the typmod of the result type.
- `exprCollation(expr)` — returns the collation OID.
- `exprLocation(expr)` — returns the source-text byte offset for error reporting.
- `expression_returns_set(expr)` — true if the expression returns a set.

These are used throughout the analyzer and planner wherever code must ask "what type does this expression produce?" without switching on every possible `NodeTag`.

## Textual Serialization (outfuncs.c / readfuncs.c)

`outfuncs.c` implements `nodeToString`, which converts any node tree to a parenthesized text representation. The format is similar to S-expressions: each node starts with its type label followed by field names and values. `readfuncs.c` implements the inverse `stringToNode`. `copyfuncs.c` implements deep copy via `copyObject`.

This serialization machinery is used in several practical contexts:

- **`EXPLAIN` output** uses `nodeToString` when formatting the internal representation of expressions for `EXPLAIN VERBOSE`.
- **Parallel query** ships serialized plan trees and expression trees to worker processes through shared memory, where they are deserialized with `stringToNode` (`src/backend/executor/execParallel.c`).
- **Cached plans** — generic plan trees are deep-copied with `copyObject` before each execution to allow per-execution parameter substitution without modifying the cached plan.
- **Debug output** — `elog(DEBUG5, "%s", nodeToString(node))` is a standard technique for dumping a tree during development.

`outfuncs.c` uses a set of macro helpers (`WRITE_INT_FIELD`, `WRITE_OID_FIELD`, `WRITE_NODE_FIELD`, etc.). Each helper appends one field to a `StringInfo` buffer, which keeps the per-node output functions uniform and readable.

## Related Topics

- [[architecture/node-list|Node Lists and Linked Lists]]
- [[architecture/internal-data-structures|Internal Data Structure Library]]
- [[subsystems/planner/overview|Planner Overview]]
- [[subsystems/executor/expression-eval|Expression Evaluation]]
- [[subsystems/parser/overview|Parser Overview]]

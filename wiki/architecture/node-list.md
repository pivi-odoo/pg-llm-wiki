---
title: "Node Lists and Linked Lists"
aliases:
  - List
  - NIL
  - ListCell
  - pg_list
source_files:
  - src/backend/nodes/list.c
  - src/include/nodes/pg_list.h
symbols:
  - List
  - ListCell
  - lappend
  - lcons
  - foreach
  - NIL
  - list_concat
  - list_copy
  - list_sort
---

PostgreSQL's `List` type is the universal container used throughout the query processing pipeline. The parser uses it for target lists, from clauses, and group-by expressions. The planner builds its path and plan trees from lists. The executor carries lists of result columns and scan targets. Despite being called a "list", the implementation is a dynamic array, not a linked list. The name and much of the API vocabulary survive from an era when the codebase descended from Lisp-based code with genuine cons cells.

## The Array-Backed Design

The original cons-cell implementation allocated each list element in a separate `palloc` chunk. This made cell pointers stable across mutations: inserting or deleting elsewhere never moved other cells. However, it imposed one palloc call per element and poor cache locality for traversal. The current implementation, introduced in PostgreSQL 13, stores elements in a contiguous `ListCell` array owned by the `List` header.

The `List` struct (`pg_list.h`) has four fields that matter:

| Field | Type | Purpose |
|---|---|---|
| `type` | `NodeTag` | Distinguishes `T_List`, `T_IntList`, `T_OidList`, `T_XidList` |
| `length` | `int` | Number of valid elements |
| `max_length` | `int` | Capacity of the elements array |
| `elements` | `ListCell *` | Pointer to the cell array |
| `initial_elements` | `ListCell[]` | Inline storage embedded in the header |

The `initial_elements` flexible array member is the key optimization: for short lists, `elements` simply points to `initial_elements`, so the header and data occupy a single palloc chunk. Allocation sizes are rounded up to powers of two, starting at 8 `ListCell` units. As a result, lists of up to four or five pointers fit in one allocation — the same size the old implementation spent on a single cons cell. When the list grows past the inline capacity, `enlarge_list()` allocates a separate array in the same [[subsystems/memory/contexts|memory context]] as the header. It then copies the data over (list.c).

Each `ListCell` is a union of `void *`, `int`, `Oid`, and `TransactionId`. The `type` field in the header enforces which union member is active — assert macros (`IsPointerList`, `IsIntegerList`, etc.) verify this in debug builds.

## The NIL Invariant

An empty list is always represented as `NIL`, which is simply a null `List *`. There is no such thing as a non-NIL list with `length == 0`. Any operation that reduces a list to zero elements frees the header and returns `NIL`. This invariant simplifies nil-check code throughout the tree: a guard `if (list == NIL)` is a reliable empty-list test. Code that retains a `List *` pointer to an empty list would be holding a dangling pointer.

As a consequence, mutation functions return a (potentially new) `List *`. Callers must use this returned pointer. All such functions are declared `pg_nodiscard`. Every caller must write `list = lappend(list, item)` rather than discarding the return value. This pattern is unconditional: even `lappend` on a non-NIL list may return the same pointer. The API makes no guarantee either way.

## Pointer Stability Is Gone

The old cons-cell API guaranteed that a `ListCell *` remained valid across insertions and deletions elsewhere in the list. The array representation invalidates that guarantee entirely. Inserting at the head (`lcons()`) shifts all existing cells forward with `memmove`. Any prior `ListCell *` into the array then points at different data. Deletions collapse the array similarly.

PostgreSQL ships a `DEBUG_LIST_MEMORY_USAGE` mode, enabled automatically under Valgrind builds. It deliberately forces every mutation to allocate new storage and move all data, then poisons the old memory. This surfaces stale-pointer bugs that would otherwise be silently correct in production-sized lists.

The only safe exception is `list_truncate()`. It only decrements `length` and does not move any cells. Cells before the truncation point retain their addresses.

## Four List Flavors

The four `NodeTag` values each impose type discipline enforced by the `lfirst_*` / `llast_*` accessor macros:

- **`T_List`** — pointer list, `lfirst(cell)` returns `void *`. Used everywhere node trees appear, such as `SelectStmt.targetList` and `Query.rtable`.
- **`T_IntList`** — integer list, `lfirst_int(cell)`. Used for column numbers, attribute indices, and similar small integers.
- **`T_OidList`** — OID list, `lfirst_oid(cell)`. Heavily used to accumulate sets of relation OIDs, operator OIDs, and similar catalog references.
- **`T_XidList`** — transaction ID list, `lfirst_xid(cell)`. Infrastructure is thinner than the other types; the header note warns that `int`, `Oid`, and `TransactionId` happen to be the same size today but need not always be.

Mixing types is prevented at the assertion level: `lappend_int()` asserts `IsIntegerList(list)` before proceeding (list.c).

## Traversal and Iteration

The `foreach` macro is a `for` loop that maintains a `ForEachState` struct on the stack. This struct tracks the list pointer and the current index. The `foreach` macro exposes the current cell as the named `ListCell *` variable. After normal loop exit the variable is NULL. An early `break` leaves it pointing at the last visited cell.

Because `foreach` tracks position by index rather than by pointer, it tolerates appending to the list mid-loop. New elements will be visited. It also tolerates deleting the current element via `foreach_delete_current()`, which decrements the saved index to compensate. Any other structural modification during traversal (inserting earlier, deleting a non-current element) produces undefined behavior.

The header also defines `forboth`, `forthree`, `forfour`, and `forfive` for simultaneously walking two to five lists in lockstep, stopping when the shortest list runs out. The planner uses these extensively to correlate parallel lists of, for example, group-by expressions and their sort operators.

## Mutation Costs and Trade-offs

Because the backing store is a contiguous array, `lappend()` is O(1) amortized (occasional doubling realloc). `lcons()` and `list_delete_first()` are O(n) because they shift all elements. This makes `List` a good stack (push/pop from the tail with `lappend`/`list_delete_last`) but a poor FIFO queue. The source comment in list.c is direct: "you can make an efficient stack from a List, but not an efficient FIFO queue."

The set operations `list_union`, `list_intersection`, and `list_difference` all run in O(n²) time because they use linear membership tests. The header's own comments recommend avoiding them on long lists. For high-cardinality sets, a hash-based structure is more appropriate.

`list_sort()` delegates to `qsort` over the `elements` array, giving O(n log n) in-place sorting. PostgreSQL provides standard comparators `list_int_cmp` and `list_oid_cmp`. `list_deduplicate_oid()` provides an efficient O(n) dedup pass for an already-sorted OID list (list.c).

## Copying and Ownership

`list_copy()` produces a shallow copy: the new list's cells hold the same pointer values as the original, so both lists reference the same node objects. This is the normal case throughout plan copying and query rewriting, where the tree nodes themselves are managed separately.

`list_copy_deep()` additionally copies each pointed-to node via `copyObjectImpl()`, producing fully independent trees. `list_free_deep()` does the inverse: it pfrees each pointed-to object before freeing the cells and header. This only makes sense for pointer lists, where every cell points to a separately palloc'd block.

Because lists live in [[subsystems/memory/contexts|memory contexts]], the most common way to free a list is to simply reset or delete the context. Direct `list_free()` calls are necessary only when the list's lifetime is shorter than its context, such as when accumulating a work list inside a long-lived backend session.

## Related Topics

- [[subsystems/parser/parse-tree-nodes|Parse Tree Nodes]] — `List *` fields appear on nearly every raw and analyzed parse node
- [[subsystems/memory/contexts|Memory Contexts]] — lists are palloc'd into the current memory context; enlargement preserves context affinity

---
title: SP-GiST Index
aliases:
  - spgist
  - SP-GiST
  - space-partitioning index
  - spgist internals
tags:
  - theme/extensibility
  - theme/storage-format
source_files:
  - src/backend/access/spgist/spgdoinsert.c
  - src/backend/access/spgist/spgscan.c
  - src/backend/access/spgist/spgutils.c
  - src/backend/access/spgist/spginsert.c
  - src/backend/access/spgist/spgtextproc.c
  - src/backend/access/spgist/spgquadtreeproc.c
  - src/backend/access/spgist/spgkdtreeproc.c
  - src/include/access/spgist.h
  - src/include/access/spgist_private.h
symbols:
  - SpGistInnerTupleData
  - SpGistLeafTupleData
  - SpGistNodeTupleData
  - SpGistDeadTupleData
  - SpGistState
  - SpGistScanOpaqueData
  - SpGistSearchItem
  - SpGistPageOpaqueData
  - SpGistMetaPageData
  - spgConfigOut
  - spgChooseIn
  - spgChooseOut
  - spgPickSplitIn
  - spgPickSplitOut
  - spgInnerConsistentIn
  - spgInnerConsistentOut
  - spgLeafConsistentIn
  - spgLeafConsistentOut
  - spgdoinsert
  - spgWalk
  - spgInnerTest
  - spgLeafTest
---

# SP-GiST Index

SP-GiST (Space-Partitioned Generalized Search Tree) is PostgreSQL's framework for building space-partitioning index structures. Where [[subsystems/indexes/btree]] imposes a total order and [[subsystems/indexes/gin]] inverts a set of keys, SP-GiST captures a fundamentally different idea. The value space can be recursively partitioned into non-overlapping regions. Each value belongs to exactly one region at each level of the tree. That non-overlap guarantee is the source of SP-GiST's distinctive efficiency. When searching, a query can eliminate entire subtrees with certainty, with no need to visit a branch twice.

The framework is general enough to host tries, radix trees, quadtrees, and k-d trees without changing the core engine. The engine handles storage, concurrency, WAL, and traversal. The operator class supplies only the partitioning logic.

## How SP-GiST Differs from GiST

[[subsystems/indexes/gist]] organises data around bounding predicates that may overlap. A point can fall inside the bounding boxes of multiple inner nodes, so a query must descend multiple branches to be sure it has found all matches. SP-GiST forbids this. At every inner node the value space is sliced into disjoint partitions, so a search value can belong to at most one child. This makes SP-GiST naturally more efficient for data types whose structure is already hierarchically partitioned — geographic points in quadrants, strings sharing prefixes, integers decomposed by bit position.

The trade-off is inflexibility: because partitions cannot overlap, an operator class must be able to assign any indexed value unambiguously to one branch. Data types that lack a clean non-overlapping decomposition belong in GiST instead.

## The Operator Class Contract

An SP-GiST operator class must supply five mandatory support functions. Together they encode everything the engine needs to build and search a particular tree shape.

**`config`** (support function 1) tells the engine about the types involved. It fills `spgConfigOut`:

| Field | Purpose |
|---|---|
| `prefixType` | OID of the data type stored as an inner-node prefix |
| `labelType` | OID of the data type used to label outgoing edges (nodes) |
| `leafType` | OID of the data type stored in leaf tuples |
| `canReturnData` | true if the opclass can reconstruct the original value from the index alone (enables index-only scans) |
| `longValuesOK` | true if the opclass handles values larger than a single page |

**`choose`** (support function 2) drives descent during an insert. Given the current inner tuple and the value being inserted, it returns one of three actions encoded in `spgChooseResultType`:

| Action | Meaning |
|---|---|
| `spgMatchNode` | descend into an existing child node |
| `spgAddNode` | add a new node to the current inner tuple, then descend |
| `spgSplitTuple` | split the current inner tuple by introducing a new inner tuple above it with a shorter prefix |

**`picksplit`** (support function 3) is called when a leaf page overflows and a new inner tuple must be created to redistribute the leaves. Given an array of leaf datums, it chooses a prefix and a set of node labels, and maps each datum to one of the nodes.

**`inner_consistent`** (support function 4) is the search-time pruning oracle for inner nodes. Given an inner tuple and the scan's query conditions, it returns the subset of child nodes that could possibly contain matching leaves. Returning fewer nodes means more pruning and a faster search.

**`leaf_consistent`** (support function 5) performs the final check at a leaf. It tests whether the leaf datum actually satisfies the query conditions and, optionally, reconstructs the original indexed value for an index-only scan.

An optional `compress` function (support function 6) can transform the indexed value before storing it in a leaf tuple, similar in spirit to GiST's compress method.

## Tree Structure on Disk

Every SP-GiST index file begins with three fixed pages: block 0 is the metapage, block 1 is the root for non-null entries, and block 2 is the root for null entries. Pages carry a `SpGistPageOpaqueData` trailer that identifies them as inner or leaf pages using flags:

| Flag | Meaning |
|---|---|
| `SPGIST_META` | This is the metapage |
| `SPGIST_LEAF` | All tuples on this page are leaf tuples |
| `SPGIST_NULLS` | This page stores null-value entries |

Inner pages and leaf pages are kept separate. Inner pages use a three-way parity scheme (`GBUF_INNER_PARITY`) to distribute inner tuples across page groups. This reduces contention during concurrent inserts (spgutils.c).

### Inner Tuples

An inner tuple (`SpGistInnerTupleData`) represents one node in the partitioning hierarchy. Its layout is:

```
[ header | optional prefix datum | array of SpGistNodeTuple ]
```

The header packs four fields into 32 bits plus a 16-bit size word:

| Field | Width | Meaning |
|---|---|---|
| `tupstate` | 2 bits | LIVE / REDIRECT / DEAD / PLACEHOLDER |
| `allTheSame` | 1 bit | all child nodes are equivalent (degenerate partition) |
| `nNodes` | 13 bits | number of outgoing edges, max 8191 |
| `prefixSize` | 16 bits | byte length of the prefix datum, 0 if absent |

The prefix encodes what all values in this subtree share — the common string prefix, the centroid point, or whatever discriminator the opclass uses. Each `SpGistNodeTuple` is a standard `IndexTupleData` whose datum holds the edge label and whose `t_tid` field points to the child page and offset.

### Leaf Tuples

A leaf tuple (`SpGistLeafTupleData`) carries the actual indexed datum along with the heap TID:

| Field | Meaning |
|---|---|
| `tupstate` | LIVE / REDIRECT / DEAD / PLACEHOLDER |
| `size` | total byte size of the tuple |
| `t_info` | 14-bit `nextOffset` linking same-node leaves into a chain, plus flags |
| `heapPtr` | `ItemPointerData` pointing to the heap row |

Leaves belonging to the same inner-node child are chained together on the leaf page via `nextOffset`. The leaf datum itself follows on a `MAXALIGN` boundary after the header (and an optional null bitmap when INCLUDE columns are present).

### Dead Tuple States

SP-GiST uses in-place dead tuple markers rather than physically removing tuples during concurrent operations. The four states apply to both inner and leaf tuples:

| State | Value | Meaning |
|---|---|---|
| `SPGIST_LIVE` | 0 | Normal live tuple |
| `SPGIST_REDIRECT` | 1 | Temporary forwarding pointer left after a split or move |
| `SPGIST_DEAD` | 2 | Dead, but cannot be removed because other tuples link to it |
| `SPGIST_PLACEHOLDER` | 3 | Empty slot preserving an offset number for concurrent readers |

VACUUM cleans up redirect tuples. It replaces them with placeholders, or removes them, once no active transaction can hold a reference to the old location (spgvacuum.c).

## Insert Path

Inserting a value walks the tree from the root using `spgdoinsert()` (spgdoinsert.c). At each inner tuple, the engine calls the opclass `choose` function. This function examines the tuple's prefix and node labels, and returns one of the three actions described above.

When `choose` returns `spgMatchNode`, the engine follows the chosen node's downlink and repeats at the next level. This continues until a leaf page is reached, where the new leaf tuple is appended to the chain for that node.

When `choose` returns `spgAddNode`, the engine must rewrite the inner tuple itself on its page with one additional node. The engine allocates the expanded inner tuple — either fitting it back on the same page or moving it to a new page, leaving a redirect tuple in its old slot.

When `choose` returns `spgSplitTuple`, the current prefix is too long for the value being inserted. The engine must insert a new inner tuple above the current one. The new upper inner tuple takes the shorter prefix, has one child node pointing to the (now lower) existing inner tuple, and another child node for the new value's branch. This mirrors what a radix-tree insert does when two strings diverge mid-prefix.

If a leaf page fills up before `choose` finishes, the engine invokes `picksplit`. It groups the page's existing leaf tuples plus the new one under a fresh inner tuple, distributing them across nodes. The inner tuple is placed on an inner page. The leaves are spread across one or more leaf pages.

## Search Path

Search is driven by `spgWalk()` (spgscan.c), which maintains a priority queue (`SpGistScanOpaque.scanQueue`) of work items. Each item is a `SpGistSearchItem` pointing to either an inner tuple or a heap TID:

```c
typedef struct SpGistSearchItem {
    pairingheap_node phNode;    /* priority queue linkage */
    Datum        value;          /* reconstructed value at this point */
    void        *traversalValue;/* opclass-specific state threaded through descent */
    int          level;          /* depth from root */
    ItemPointerData heapPtr;     /* target block/offset */
    bool         isLeaf;         /* true once we reach a heap TID */
    double       distances[FLEXIBLE_ARRAY_MEMBER]; /* for KNN ordering */
} SpGistSearchItem;
```

The scan begins by pushing the root block onto the queue. On each iteration `spgWalk` pops the front item. If it is an inner tuple, it calls `spgInnerTest()`, which invokes the opclass `inner_consistent` function to determine which child nodes to enqueue. If it is a leaf page entry, `spgLeafTest()` calls `leaf_consistent` to decide whether the leaf datum satisfies the query predicates.

The non-overlapping partition guarantee makes this pruning exact: if `inner_consistent` says a subtree cannot contain a match, no match exists there. There is no need for a recheck step at the inner level the way GiST sometimes requires.

For ordered (KNN) scans the pairing heap orders items by estimated distance, so the scan can return nearest neighbours without scanning the whole tree. The opclass `inner_consistent` function returns estimated distances for child nodes. `leaf_consistent` returns the exact distance for leaf datums. Items flagged `recheckDistances = true` are re-evaluated against the heap row before final delivery.

For bitmap scans (`spggetbitmap`), the walk continues until the queue is empty, adding every passing leaf TID to the `TIDBitmap`. For tuple-at-a-time scans (`spggettuple`), the walk stops as soon as one leaf page's worth of results has been collected, resuming on the next call.

## Reconstructed Values and Index-Only Scans

Because SP-GiST stores only partial information at each level — a leaf in a text radix tree stores only the suffix below the last inner-node prefix — SP-GiST reconstructs the full original value by threading state down through the tree. The `reconstructedValue` field in both `spgInnerConsistentIn` and `spgLeafConsistentIn` carries the value assembled so far. The opclass `inner_consistent` function extends it for each child. The `leaf_consistent` function completes it for each leaf.

When `spgConfigOut.canReturnData` is true, `leaf_consistent` returns this reconstructed value in `spgLeafConsistentOut.leafValue`. The executor uses it directly, avoiding a heap fetch. This is how SP-GiST supports index-only scans (spgcanreturn(), spgscan.c).

## Built-in Operator Classes

PostgreSQL ships four SP-GiST operator classes that illustrate the range of tree shapes the framework supports.

**`point_ops`** (spgquadtreeproc.c) implements a quadtree over `point` values. Each inner tuple stores the centroid point as its prefix. The four quadrants are the nodes. Since labels are not needed — the quadrant is determined by the centroid alone — `labelType` is `VOIDOID`. The `inner_consistent` function checks which quadrants the query region intersects, returning only those. For a point containment or KNN query over non-overlapping spatial data this is substantially faster than a GiST R-tree, which may need to descend multiple overlapping bounding boxes.

**`text_ops`** (spgtextproc.c) implements a compressed trie (radix tree) over `text`. Inner tuples carry a common prefix string. Node labels are the next byte value (0–255 plus two sentinel values for end-of-string and split artefacts). Descent matches one byte per level after consuming the prefix. To reconstruct the full string at a leaf, the engine concatenates the prefix and label along the path taken from root to leaf, then appends the leaf's suffix datum.

**`kd_point_ops`** (spgkdtreeproc.c) implements a k-d tree that alternates split dimension by level. Even-numbered levels split on the x coordinate, odd levels on y. Unlike the quadtree, each inner tuple splits the space into exactly two half-planes, making the tree more balanced for uniformly distributed data.

**`inet_ops`** in `network.c` implements a trie over `inet`/`cidr` values, splitting on network prefix bits. Subnet containment queries (`<<`, `>>`) map naturally to trie prefix matching.

## Page Allocation Strategy

SP-GiST separates inner and leaf tuples onto different pages to simplify the space accounting. When an insert needs a page, `SpGistGetBuffer()` (spgutils.c) consults the `SpGistLUPCache` (last-used page cache). This cache records up to eight recently used pages grouped by their parity class. Each backend keeps a private copy of this cache in `index->rd_amcache`, periodically flushing it back to the shared metapage. This avoids repeated metapage reads under heavy insert load while still allowing backends to share newly freed pages across cache flushes.

The parity grouping (`GBUF_INNER_PARITY`) exists to enforce a structural rule: an inner tuple and its immediate children should not reside on pages whose block numbers are in the same parity group. This prevents certain deadlock scenarios when two inserts each hold a lock on one page and need a second.

## Concurrency and WAL

SP-GiST uses standard buffer manager locking: shared locks for reads, exclusive locks for writes. Because splits can require updating multiple pages — the leaf page, the new inner page, and the parent inner tuple — the WAL records for SP-GiST operations can span multiple buffers (spgxlog.c).

Redirect tuples decouple readers from in-progress structural changes. A concurrent reader that arrives at a tuple mid-move follows the redirect chain rather than seeing a torn state. Redirect entries carry the XID of the transaction that created them. Once that XID is no longer running, VACUUM can clean them up and reclaim the space as placeholder tuples.

## Related Topics

- [[subsystems/indexes/spgist-wal|SP-GiST WAL]] — WAL record formats and replay logic specific to SP-GiST structural changes
- [[subsystems/indexes/gist|GiST]] — the overlapping bounding-predicate cousin of SP-GiST, used when value space partitions cannot be made disjoint
- [[subsystems/indexes/index-am|Index Access Method API]] — the generic AM interface that SP-GiST implements, covering scan, insert, and vacuum callbacks
- [[subsystems/indexes/index-only-scans|Index-Only Scans]] — how `canReturnData` and reconstructed values allow SP-GiST to avoid heap fetches
- [[subsystems/extensions/custom-index-am|Custom Index AM]] — how to register a new index access method using the same infrastructure SP-GiST relies on
- [[subsystems/storage/buffer-manager|Buffer Manager]] — buffer pool locking and page access primitives used by every SP-GiST operation
- [[subsystems/types/network-types|Network Types]] — `inet`/`cidr` types whose `inet_ops` SP-GiST operator class is a canonical example of trie-based indexing
- [[subsystems/indexes/btree|B-Tree Index]] — the ordered index for equality and range queries, the general-purpose alternative when values do not need a space-partitioned structure
- [[subsystems/indexes/gin|GIN]] — the inverted-index access method for multi-valued and full-text indexing, a different specialization from SP-GiST's space partitioning
- [[code-paths/insert|Insert]] — how heap inserts trigger index maintenance, including the `spgdoinsert()` descent described above
- [[code-paths/vacuum|Vacuum]] — how VACUUM cleans redirect and dead tuples from SP-GiST pages

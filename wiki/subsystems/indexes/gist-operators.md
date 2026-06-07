---
title: "GiST Built-in Operator Classes"
aliases:
  - gist geometric ops
  - gistproc
tags:
  - theme/storage-format
source_files:
  - src/backend/access/gist/gistproc.c
symbols:
  - gist_box_consistent
  - gist_box_union
  - gist_box_penalty
  - gist_box_picksplit
  - gist_box_same
  - gist_poly_compress
  - gist_poly_consistent
  - gist_circle_compress
  - gist_circle_consistent
  - gist_point_compress
  - gist_point_fetch
  - gist_point_consistent
  - gist_point_distance
  - gist_point_sortsupport
  - ConsiderSplitContext
  - CommonEntry
  - SplitInterval
---

PostgreSQL ships several built-in [[subsystems/indexes/gist|GiST]] operator classes for the native 2-D geometric types: `box`, `polygon`, `circle`, and `point`. All four share a common strategy: indexing values as minimum bounding rectangles (MBRs). They also reuse the same core support routines implemented in `gistproc.c`. This shared architecture means that split, penalty, union, and equality are implemented once for `BOX` and then adapted for the other shapes through compression.

## The bounding-box abstraction

Every operator class in `gistproc.c` stores index keys as `BOX` values regardless of the actual data type. `box` columns store their values directly. `polygon` columns compress to `polygon->boundbox` on insert (gist_poly_compress(), gistproc.c). `circle` columns compress to a rectangle whose corners are `center ± radius` (gist_circle_compress(), gistproc.c). `point` columns compress to a degenerate box where `high == low` equals the point (gist_point_compress(), gistproc.c).

This uniformity keeps the union, penalty, and picksplit implementations entirely independent of the original type. Those three functions deal only with `BOX` arithmetic. The trade-off is that `polygon` and `circle` consistency checks are always lossy: the stored bounding box may overlap the query's bounding box even when the actual shape does not match. Accordingly, `gist_poly_consistent()` and `gist_circle_consistent()` always set `*recheck = true`, telling the executor to re-evaluate the predicate against the heap tuple. `gist_box_consistent()` sets `*recheck = false` because the box representation is exact.

`point` is a special case: because its compressed form is an exact degenerate box, point-to-point queries (`=`, `<<`, `>>`, `^`, `|/`) do not need recheck. Queries that test whether a point is inside a polygon or circle go through the bounding-box test first and then call `poly_contain_pt()` or `circle_contain_pt()` directly at the leaf level before clearing recheck (gist_point_consistent(), gistproc.c).

## Consistent: leaf vs. inner behaviour

The `consistent()` contract has different semantics at leaf pages versus inner pages. At a leaf, the key is a single indexed value (or its exact bounding box), so the predicate is applied directly. At an inner page, the key is a union bounding box covering an entire subtree; the predicate must be relaxed to "could any value in this subtree possibly match?"

For `box`, this duality is handled by two separate helpers:

- `gist_box_leaf_consistent()` applies the requested strategy operator literally against the stored box.
- `rtree_internal_consistent()` applies a *relaxed* version of each strategy. For example, the "strictly left" strategy (`<<`) is tested at leaves as `box_left(key, query)`, but at inner pages as `NOT box_overright(key, query)`. If the bounding box is not strictly to the right of the query, some descendant might lie strictly to the left.

The table below shows the twelve supported strategies and how the relaxation works:

| Strategy | Operator | Leaf test | Inner-page test |
|----------|----------|-----------|-----------------|
| `RTLeftStrategyNumber` | `<<` | `box_left` | `NOT box_overright` |
| `RTOverLeftStrategyNumber` | `&<` | `box_overleft` | `NOT box_right` |
| `RTOverlapStrategyNumber` | `&&` | `box_overlap` | `box_overlap` |
| `RTOverRightStrategyNumber` | `&>` | `box_overright` | `NOT box_left` |
| `RTRightStrategyNumber` | `>>` | `box_right` | `NOT box_overleft` |
| `RTSameStrategyNumber` | `~=` | `box_same` | `box_contain` |
| `RTContainsStrategyNumber` | `@>` | `box_contain` | `box_contain` |
| `RTContainedByStrategyNumber` | `<@` | `box_contained` | `box_overlap` |
| `RTBelowStrategyNumber` | `<<\|` | `box_below` | `NOT box_overabove` |
| `RTOverBelowStrategyNumber` | `&<\|` | `box_overbelow` | `NOT box_above` |
| `RTAboveStrategyNumber` | `\|>>` | `box_above` | `NOT box_overbelow` |
| `RTOverAboveStrategyNumber` | `\|&>` | `box_overabove` | `NOT box_below` |

For `polygon` and `circle`, the same `rtree_internal_consistent()` function is reused for both leaf and inner pages because the stored key is already a bounding box approximation in both cases.

## Split algorithm: double sorting

`gist_box_picksplit()` implements the "double sorting" algorithm described by Korotkov (2011), which is an improvement over the quadratic algorithm in Guttman's original R-tree paper.

The algorithm projects each entry as a 1-D interval on the X axis and then the Y axis. For each axis, it considers all possible split points. It sorts intervals by lower bound and by upper bound separately, then scans through both sorted arrays simultaneously. This finds the combination that minimises overlap while maintaining a minimum ratio (`LIMIT_RATIO = 0.3`) between the two resulting groups.

The `ConsiderSplitContext` struct accumulates the best split found so far:

| Field | Meaning |
|-------|---------|
| `boundingBox` | MBR of all entries being split |
| `leftUpper` / `rightLower` | The boundary that defines the split on the chosen axis |
| `ratio` | Fraction of entries in the smaller group |
| `overlap` | Normalised overlap between left and right bounding boxes |
| `dim` | Axis chosen (0 = X, 1 = Y) |
| `range` | Width of the overall MBR on the chosen axis |

When comparing candidate splits across axes, the algorithm uses *non-negative* overlap rather than raw overlap as the primary criterion. This prevents degenerate cases where all good splits cluster along one axis. Such clustering would produce elongated MBRs and poor search performance. If two splits have equal non-negative overlap, the one with the larger range wins. This tends to produce more square bounding boxes.

After choosing the split axis and boundary, entries fall into three categories:

1. Entries whose interval lies entirely to the left of the split boundary.
2. Entries whose interval lies entirely to the right.
3. **Common entries** — entries whose interval spans the boundary and could go to either side without changing the selected-axis overlap.

Common entries are collected into a `CommonEntry` array. Their delta (the absolute difference between the penalty of inserting them into the left group versus the right group) is computed. They are then sorted by delta, processing the most ambiguous ones first. Each common entry then goes to whichever side has the lower insertion penalty (box_penalty(), gistproc.c), subject to enforcing the minimum-ratio constraint.

If no acceptable split is found (all splits violate `LIMIT_RATIO`), `fallbackSplit()` assigns the first half of entries to the left page and the second half to the right, ensuring termination.

## Penalty: area enlargement

`gist_box_penalty()` measures the cost of routing a new index entry through an existing subtree by computing how much the subtree's bounding box would need to grow. Formally, penalty is `area(union(original, new)) - area(original)`. A penalty of zero means the new entry fits entirely within the existing bounding box. The subtree key does not change. This is the standard R-tree area-enlargement heuristic.

The `size_box()` helper treats zero-width boxes as having area zero and zero-by-infinity boxes also as zero (to avoid NaN from `0 * infinity`). Boxes with a NaN high coordinate are treated as infinite. This means they impose maximum penalty.

The same penalty function is reused for `point` columns because points are stored as degenerate boxes.

## Points: distance and sort support

`point` is the only built-in geometric type that supports `ORDER BY` (nearest-neighbour) queries via the `<->` operator. The `gist_point_distance()` function computes a lower-bound distance from the query point to each index entry. For leaf entries (degenerate boxes with `high == low`) this is the ordinary Euclidean distance. For inner-page entries it is the minimum distance from the query point to the MBR — zero if the point is inside the box, otherwise the perpendicular distance to the nearest edge or corner (`computeDistance()`, gistproc.c).

For fast index builds, `gist_point_sortsupport()` provides a sort order based on Z-order (Morton code). Each point is mapped to a 64-bit integer by interleaving the bits of its float32 X and Y coordinates (`point_zorder_internal()`, gistproc.c). IEEE float32 values are first converted to a comparable uint32 representation. This representation maps negative floats to `[0, 0x7FFFFFFF]` and non-negative floats to `[0x80000000, 0xFFFFFFFF]`, preserving order across the sign boundary (`ieee_float32_to_uint32()`, gistproc.c). Sorting by Z-order before building the index clusters spatially close points near each other in the leaf pages, improving both build speed and search locality.

## Index-only scans and the fetch callback

Because `polygon` and `circle` compress their heap values to bounding boxes, they cannot support index-only scans: the original value cannot be reconstructed from the stored box. `point` can, because its degenerate box encodes the point exactly. The `gist_point_fetch()` function reconstructs a `Point` from the stored `BOX` by reading `box->high` (which equals `box->low` for a point entry). This fetch callback is what allows `SELECT point_col FROM t WHERE ...` to avoid heap access when the GiST index covers the query.

## Related Topics

- [[subsystems/indexes/gist|GiST Index]] — the framework these operator classes plug into
- [[subsystems/indexes/btree|B-tree]] — the comparison-based alternative for ordered scalar types
- [[subsystems/indexes/gin|GIN]] — preferable for multi-element types like arrays and tsvectors
- [[subsystems/indexes/brin|BRIN]] — coarse range-based indexing for physically sorted data

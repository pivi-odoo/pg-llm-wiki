---
title: "Geometric Types"
aliases:
  - geometric types
  - geo_ops
  - point
  - line
  - lseg
  - box
  - circle
  - polygon
  - path
source_files:
  - src/backend/utils/adt/geo_ops.c
  - src/backend/utils/adt/geo_spgist.c
  - src/include/utils/geo_decls.h
symbols:
  - Point
  - LINE
  - LSEG
  - BOX
  - PATH
  - POLYGON
  - CIRCLE
  - point_inside
  - lseg_crossing
  - getQuadrant
  - RectBox
  - spg_box_quad_config
  - spg_box_quad_inner_consistent
  - spg_box_quad_leaf_consistent
  - spg_box_quad_picksplit
  - spg_poly_quad_compress
---

PostgreSQL's geometric types represent two-dimensional objects — points, infinite lines, line segments, axis-aligned boxes, open and closed paths, polygons, and circles. They share a common floating-point coordinate representation. They also expose a rich operator vocabulary covering containment, intersection, distance, and affine transformations. Their implementation lives almost entirely in `geo_ops.c` and `geo_decls.h`, with indexing support for boxes and polygons layered on top in `geo_spgist.c`.

## Type Representations

All coordinates are `float8` (IEEE 754 double precision). Comparisons throughout the codebase go through the `FPeq`/`FPlt`/`FPge` family of macros (geo_decls.h). These macros apply an epsilon of `1.0E-06` to compensate for floating-point rounding. This fuzzy equality is intentional: exact IEEE equality would make operations like "is this point on this line?" unreliable in practice.

The six concrete types differ in their storage class:

| Type | C struct | Storage | Notes |
|---|---|---|---|
| `point` | `Point` | fixed (16 bytes) | two `float8` coordinates |
| `lseg` | `LSEG` | fixed (32 bytes) | two `Point` endpoints |
| `box` | `BOX` | fixed (32 bytes) | two `Point` corners, sorted high/low |
| `line` | `LINE` | fixed (24 bytes) | general equation Ax+By+C=0 |
| `circle` | `CIRCLE` | fixed (24 bytes) | center `Point` plus `float8` radius |
| `path` | `PATH` | varlena (toastable) | `npts` + `closed` flag + `Point[]` |
| `polygon` | `POLYGON` | varlena (toastable) | `npts` + precomputed `BOX` + `Point[]` |

`PATH` and `POLYGON` are varlena types. They can be [[subsystems/storage/toast|TOAST]]ed. All others pass by reference as fixed-size heap allocations.

### Box Invariant

A `BOX` always stores its corners in canonical order: `high` holds the greater x and y coordinates and `low` holds the lesser. `box_in()` and `box_recv()` (geo_ops.c) sort the corners at parse time. So every downstream operation can rely on this invariant without re-checking.

### Line Representation

A `LINE` stores the coefficients of the general equation Ax+By+C=0 rather than two endpoint coordinates. `line_construct()` (geo_ops.c) normalises the equation: a vertical line uses A=-1, B=0, C=x-intercept; a horizontal line uses A=0, B=-1, C=y-intercept; all others use B=-1 and derive A and C from the slope and a given point. The general-equation form lets parallelism, perpendicularity, and intersection tests reduce to simple arithmetic on three scalars without ever needing to reconstruct slope explicitly.

### Polygon Bounding Box

Every `POLYGON` stores a precomputed `BOX` field (`boundbox`). `make_bound_box()` (geo_ops.c) computes this at parse and deserialisation time. Complex operations like polygon-to-polygon intersection use the bounding box for a cheap preliminary filter before doing the exact O(n²) edge check.

## Operator Conventions

The source file documents a small set of idioms that all operators follow (geo_ops.c, opening comment block):

- **Intersection**: `type1_interpt_type2(Point *result, T1 *a, T2 *b)` returns a bool indicating whether the objects intersect. If `result` is non-NULL, the function sets it to the intersection point.
- **Containment**: `type1_contain_type2(T1 *container, T2 *contained)` returns a bool.
- **Closest point / distance**: `type1_closept_type2(Point *result, T1 *a, T2 *b)` returns the minimum distance between the two objects as a `float8`. If `result` is non-NULL, the function sets it to the nearest point on `a` to `b`.

These internal functions back multiple SQL-level operators. For example, `line_parallel` checks whether `line_interpt_line` returns false (geo_ops.c:1146).

## Point Arithmetic as Complex Numbers

The `*` and `/` operators on `point` treat points as complex numbers (real part = x, imaginary part = y). `point_mul_point()` computes (x1·x2 − y1·y2, x1·y2 + y1·x2). `point_div_point()` divides by the modulus squared (geo_ops.c). The practical effect is that multiplying all vertices of a path or polygon by a point rotates and scales the shape uniformly. The same operation applies to `box`, `circle`, and `path` via their respective `*` operators (geo_ops.c:4431, 5007).

## Point-in-Polygon

Containment for closed paths and polygons uses a ray-casting algorithm implemented in `point_inside()` and `lseg_crossing()` (geo_ops.c). The algorithm translates the query point to the origin. A horizontal ray then extends rightward. `lseg_crossing()` tests each polygon edge for crossing. It returns ±2 if the edge crosses the positive X axis, ±1 if one endpoint lies on it, and `POINT_ON_POLYGON` if the segment passes through the origin. Summing the crossing values gives zero for exterior points and nonzero for interior points. The algorithm is O(n) in the number of polygon vertices. Many operator shortcircuits also accept the polygon as the convex hull: GiST and SP-GiST both use the precomputed `boundbox` as the first filter.

## Indexing: SP-GiST 4D Quadtree for Boxes

A simple point-based structure cannot index boxes, because a box has four degrees of freedom (x\_low, y\_low, x\_high, y\_high). The `geo_spgist.c` module solves this by embedding each 2D box as a point in 4-dimensional space and building an [[subsystems/indexes/spgist|SP-GiST]] quadtree over that space. At each inner node, the four coordinates of the centroid box partition the space into 16 quadrants (2⁴). Each box maps unambiguously to exactly one quadrant.

### 4D Quadrant Assignment

`getQuadrant()` (geo_spgist.c) assigns a box to one of 16 quadrants by testing each of the four corners' coordinates against the centroid independently:

```
bit 3: inBox->low.x  > centroid->low.x
bit 2: inBox->high.x > centroid->high.x
bit 1: inBox->low.y  > centroid->low.y
bit 0: inBox->high.y > centroid->high.y
```

The resulting 4-bit value selects the quadrant. Because the test is strict (greater than, not greater-than-or-equal), boxes that share an edge with the centroid fall into the lower-numbered quadrant. This is consistent with SP-GiST's non-overlapping partition requirement.

### Traversal Value and RectBox

SP-GiST's `inner_consistent` function must be able to prune subtrees during a search. To do this, `spg_box_quad_inner_consistent()` (geo_spgist.c) carries a `RectBox` structure as its traversal value. A `RectBox` records the allowed range of each of the four box coordinates for all boxes that can live within the current subtree:

```
RectBox
├── range_box_x: RangeBox   ← bounds on low.x and high.x
│   ├── left:  Range {low, high}
│   └── right: Range {low, high}
└── range_box_y: RangeBox   ← bounds on low.y and high.y
    ├── left:  Range {low, high}
    └── right: Range {low, high}
```

At the root, `initRectBox()` initialises all bounds to ±infinity. Each descent into a quadrant tightens one bound per dimension based on whether the box's corner was above or below the centroid in that dimension (`nextRectBox()`). By the time the traversal reaches a leaf, the `RectBox` is tight enough to decide with certainty whether any box in the subtree can satisfy the query.

### Supported Strategies

The `inner_consistent` function maps each SP-GiST strategy number to a 4D predicate over the traversal `RectBox`. For example, overlap is `overlap4D()`, containment is `contain4D()`, and directional predicates like "strictly left of" map to `left4D()`. All strategies reduce to two 2D range checks, one per spatial dimension (geo_spgist.c:643–698). The index handles queries involving `polygon` via their precomputed bounding boxes, falling back to `recheck = true` at the leaf for strategies that are not exact at the bounding-box level.

### Median Centroid at Split

`spg_box_quad_picksplit()` (geo_spgist.c) chooses the centroid for a new inner node by taking the independent medians of all four coordinate arrays. `spg_box_quad_picksplit()` sorts each array with `qsort` and selects the middle element. This is coordinate-wise, not point-wise. So the resulting centroid box does not have to correspond to any actual input box. The approach balances the quadrants well under uniform data but can degrade under highly skewed distributions.

### Polygon Compression

The `poly_ops` operator class indexes polygons using the same 4D quadtree. `spg_poly_quad_compress()` (geo_spgist.c) extracts the polygon's precomputed `boundbox`. It stores that in the leaf, making the leaf datum a `BOX`. Since the original polygon cannot be recovered from its bounding box, `spg_bbox_quad_config()` sets `canReturnData = false`, preventing index-only scans. Queries that are not exact at the bounding-box level set `recheck = true` in `spg_box_quad_leaf_consistent()`, causing the executor to recheck the heap row.

## Related Topics

- [[subsystems/indexes/spgist|SP-GiST]] — the index framework used by box and polygon operator classes
- [[subsystems/indexes/gist|GiST]] — R-tree-style index for overlapping spatial data; geometric types also have GiST operator classes
- [[subsystems/storage/toast|TOAST]] — relevant for large paths and polygons

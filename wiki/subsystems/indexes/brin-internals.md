---
title: "BRIN Tuple Layout and Opclass Internals"
aliases:
  - BRIN tuple format
  - BRIN inclusion opclass
  - BRIN WAL
  - brin_tuple
tags:
  - theme/storage-format
source_files:
  - src/backend/access/brin/brin_tuple.c
  - src/backend/access/brin/brin_inclusion.c
  - src/backend/access/brin/brin_validate.c
  - src/backend/access/brin/brin_xlog.c
  - src/include/access/brin_tuple.h
  - src/include/access/brin_internal.h
symbols:
  - BrinTuple
  - BrinMemTuple
  - BrinValues
  - BrinOpcInfo
  - BrinDesc
  - brin_form_tuple
  - brin_deform_tuple
  - brin_form_placeholder_tuple
  - brin_inclusion_add_value
  - brin_inclusion_consistent
  - brin_inclusion_union
  - brinvalidate
  - brin_redo
---

# BRIN Tuple Layout and Opclass Internals

[[subsystems/indexes/brin|BRIN index internals]] covers the overall page structure, build process, and scan mechanics of BRIN. This page goes deeper into three complementary areas: how summary data is packed into on-disk tuples and expanded back into memory, how the inclusion opclass stores and queries a union of values rather than a min/max range, and how BRIN expresses its structural changes as WAL records.

## BrinTuple: on-disk format

Every entry in a BRIN index is a `BrinTuple` (`brin_tuple.h`). The on-disk header is intentionally minimal — just five bytes:

```c
typedef struct BrinTuple
{
    BlockNumber bt_blkno;   /* first heap block of the range */
    uint8       bt_info;    /* flags + data offset */
} BrinTuple;
```

The `bt_blkno` field identifies which heap block range the tuple summarises. `bt_info` packs four pieces of information into a single byte using bit masks:

| Bits | Mask | Meaning |
|---|---|---|
| 7 (high) | `BRIN_NULLS_MASK` | A null bitmap follows the header |
| 6 | `BRIN_PLACEHOLDER_MASK` | Placeholder — range is being summarised concurrently |
| 5 | `BRIN_EMPTY_RANGE_MASK` | No live tuples exist in this range |
| 4–0 | `BRIN_OFFSET_MASK` | Byte offset to the opclass data within the tuple |

The low five bits encoding the data offset allow the data area to start at any aligned offset up to 31 bytes into the tuple. This accommodates tuples with no null bitmap (plain BRIN header only) as well as tuples that need the bitmap. When `BRIN_NULLS_MASK` is set, a null bitmap immediately follows the fixed header before the opclass data. The bitmap uses two bits per indexed column. The first half of the bitmap stores `allnulls` flags (column is entirely null in this range, so no data value is stored). The second half stores `hasnulls` flags (at least one null exists, but non-null values are also stored). Bit sense is reversed from the standard heap convention: a `1` bit means null (`brin_form_tuple()`, `brin_tuple.c`).

After the optional null bitmap, the opclass data area holds the actual summarisation values. The layout is determined entirely by the opclass through `BrinOpcInfo.oi_nstored`: each indexed column contributes exactly `oi_nstored` `Datum`-sized slots. For the `minmax` opclass `oi_nstored = 2` (minimum and maximum); for `inclusion` it is `3`; for `bloom` it is `1` (a varlena Bloom filter). Columns where `allnulls` is true have no representation in the data area at all. This is what makes BRIN tuples variable-length even though the column count is fixed.

## BrinMemTuple: in-memory form

Before an opclass callback can inspect or modify a summary, the on-disk tuple must be deformed into a `BrinMemTuple` (`brin_deform_tuple()`, `brin_tuple.c`). The in-memory form stores the same information but in a shape convenient for the opclass to manipulate:

```c
typedef struct BrinMemTuple
{
    bool        bt_placeholder;
    bool        bt_empty_range;
    BlockNumber bt_blkno;
    MemoryContext bt_context;
    Datum      *bt_values;       /* flat array of all stored Datums */
    bool       *bt_allnulls;     /* per-column allnulls flags */
    bool       *bt_hasnulls;     /* per-column hasnulls flags */
    BrinValues  bt_columns[FLEXIBLE_ARRAY_MEMBER];
} BrinMemTuple;
```

Each element of `bt_columns` is a `BrinValues` struct holding the per-column state: the attribute number, the null flags, and a pointer into the flat `bt_values` array at the slice belonging to that column. The memory layout is constructed once by `brin_new_memtuple()`. It is then reused across ranges by `brin_memtuple_initialize()`, which avoids repeated allocation during index builds.

When modifications are complete, `brin_form_tuple()` packs the `BrinMemTuple` back into the compact on-disk form, detoasting and optionally compressing any varlena values in the process. The round-trip is: `BrinTuple` (disk) → `brin_deform_tuple()` → `BrinMemTuple` (memory, opclass modifies) → `brin_form_tuple()` → `BrinTuple` (disk, written back).

A special case is the placeholder tuple, written by `brin_form_placeholder_tuple()`. It sets `BRIN_NULLS_MASK | BRIN_PLACEHOLDER_MASK | BRIN_EMPTY_RANGE_MASK` and marks all columns `allnulls`, producing the smallest possible valid tuple. Placeholders are written during concurrent range summarisation. This gives concurrent inserts a valid revmap entry to update while the summarisation scan is in progress.

## BrinDesc: the per-index descriptor

`BrinDesc` (`brin_internal.h`) is built once per index open by `brin_build_desc()` and is the glue passed to every opclass callback. It caches the heap tuple descriptor, the disk tuple descriptor used for packing, the per-column `BrinOpcInfo` array, and `bd_totalstored` — the total count of `Datum` slots across all columns of all opclasses. Every operation that touches a `BrinTuple` or `BrinMemTuple` requires a `BrinDesc`.

## Inclusion opclass

The `minmax` opclass covered in [[subsystems/indexes/brin|BRIN index internals]] is optimal for columns with a total order, but many useful types have no natural ordering. The `inclusion` opclass (`brin_inclusion.c`) serves geometric types, network address types, and [[subsystems/types/range-types|range types]] by storing the *union* of all values seen in a block range rather than a min and max.

The opclass stores three `Datum` values per column (`oi_nstored = 3`):

| Index | Constant | Type | Meaning |
|---|---|---|---|
| 0 | `INCLUSION_UNION` | indexed type | Union of all values in the range |
| 1 | `INCLUSION_UNMERGEABLE` | `bool` | Range contains values that cannot be merged |
| 2 | `INCLUSION_CONTAINS_EMPTY` | `bool` | Range contains at least one empty value |

The `INCLUSION_UNMERGEABLE` flag exists because not all values of a type can always be merged. The `inet` type is the canonical example: an IPv4 address and an IPv6 address cannot be represented by a single union value. When the opclass detects an unmergeable pair, it sets the flag and stops trying to maintain a valid union. At query time, an unmergeable range always returns `true` from `consistent` — the range cannot be excluded because the actual values are unknown.

### Adding a value

`brin_inclusion_add_value()` handles the case where the range's stored union must be widened. The logic follows a priority order:

1. If the column was entirely null (`bv_allnulls`), copy the new value as the initial union.
2. If `INCLUSION_UNMERGEABLE` is already set, return immediately — no further narrowing is possible.
3. If the opclass defines an empty-check function (`PROCNUM_EMPTY`), test whether the new value is empty. Empty values are tracked via `INCLUSION_CONTAINS_EMPTY` but are not merged into the union, since they are conceptually contained by everything.
4. If the opclass defines a containment function (`PROCNUM_CONTAINS`), test whether the union already contains the new value. If so, no update is needed.
5. If the opclass defines a mergeability function (`PROCNUM_MERGEABLE`), test whether the new value can be merged with the current union at all. If not, set `INCLUSION_UNMERGEABLE` and return.
6. Otherwise, call the mandatory merge function (`PROCNUM_MERGE`) to produce the new union value.

The `PROCNUM_CONTAINS` shortcut is important for performance: when data in a block range is mostly redundant (many rows with the same bounding box, or the same network prefix), the shortcut avoids re-running the merge on every insert. This mirrors the way `minmax` short-circuits when a value falls within the existing bounds.

### Consistency checking

`brin_inclusion_consistent()` maps each R-tree strategy number to the appropriate test against the stored union. The strategies fall into three groups:

- **Placement strategies** (`RTLeftStrategyNumber`, `RTBelowStrategyNumber`, etc.): these test whether the union is entirely to one side of the query value. The opclass implements them by negating the converse operator — "union is left of query" becomes "union does NOT overlap-right query". A false result means the query could still intersect the range.
- **Overlap and containment strategies** (`RTOverlapStrategyNumber`, `RTContainsStrategyNumber`, etc.): the opclass calls the operator directly on the union and the query value. If the union overlaps or contains the query, the range is consistent.
- **Contained-by strategies** (`RTContainedByStrategyNumber`, etc.): these cannot use the stored union directly. Even if the full union is not contained by the query, individual elements in the range might still be. So the opclass falls back to an overlap check. If the range also `INCLUSION_CONTAINS_EMPTY`, that is checked separately. Empty values are contained by everything by convention.

When `INCLUSION_UNMERGEABLE` is set, the consistent function conservatively returns `true` for all queries — the range cannot be excluded.

### Union of two summaries

`brin_inclusion_union()` merges two `BrinValues` for the same column in-place (updating the first from the second). It propagates both boolean flags first — if the second summary contains empty values or unmergeable values, the first is updated to match. Only then does it attempt to merge the actual union values via `PROCNUM_MERGE`.

## WAL records

BRIN's WAL footprint is small by design. Because one index entry covers many heap pages, structural changes to the index are infrequent relative to heap changes. The WAL record types, declared in the `brin_redo()` dispatch (`brin_xlog.c`), are:

| Record type | Trigger | Pages touched |
|---|---|---|
| `XLOG_BRIN_CREATE_INDEX` | `CREATE INDEX` | Metapage only |
| `XLOG_BRIN_INSERT` | New summary tuple written | Regular page + revmap page |
| `XLOG_BRIN_SAMEPAGE_UPDATE` | Updated summary fits in existing slot | Regular page only |
| `XLOG_BRIN_UPDATE` | Updated summary moved to a new page | Old regular page + new regular page + revmap page |
| `XLOG_BRIN_REVMAP_EXTEND` | Revmap needs a new page | Metapage + new revmap page |
| `XLOG_BRIN_DESUMMARIZE` | `brin_desummarize_range()` call | Revmap page + regular page |

The contrast with heap WAL is significant. A heap `INSERT` or `UPDATE` record captures the changed tuple — one record per row modified. A `XLOG_BRIN_SAMEPAGE_UPDATE` record captures the replacement of a summary tuple that might cover millions of heap rows. Even so, it touches only a single 8 kB page. Even `XLOG_BRIN_UPDATE`, the most expensive case (a cross-page move), touches only three pages in total.

Replay is straightforward. `brin_xlog_insert_update()` is shared between insert and update replay. It re-initializes the target page if the `XLOG_BRIN_INIT_PAGE` flag is set (first tuple on a fresh page). Then it adds the `BrinTuple` at the recorded offset. Finally it updates the revmap entry to point to the new location. `brin_xlog_samepage_update()` overwrites the existing item in place using `PageIndexTupleOverwrite()`. Revmap extension replay (`brin_xlog_revmap_extend()`) updates `BrinMetaPageData.lastRevmapPage` and re-initializes the new page as a revmap page. There is no full-page image for this record, because the content is entirely reconstructable from the record data.

## Opclass validation

`brinvalidate()` (`brin_validate.c`) is called when `ALTER EXTENSION`, `CREATE OPERATOR CLASS`, or `ALTER OPERATOR FAMILY` modifies a BRIN opfamily. It performs three categories of checks:

**Support function signatures.** Each of the four mandatory procedures (`BRIN_PROCNUM_OPCINFO` through `BRIN_PROCNUM_UNION`) must have the exact signature the access method expects. `ADDVALUE` must accept four `INTERNAL` arguments and return `bool`; `CONSISTENT` must accept three or four `INTERNAL` arguments and return `bool`; `UNION` must accept three `INTERNAL` arguments and return `bool`. The optional `BRIN_PROCNUM_OPTIONS` procedure must match the `amoptsproc` signature. Support function numbers outside `1–5` and `11–15` are rejected.

**Operator constraints.** All operators in the opfamily must be search operators (`AMOP_SEARCH`); BRIN does not support `ORDER BY` operators. Operator strategy numbers must fall in the range `1–63`. All operators must return `bool`. Their argument types must match the opfamily's declared type pair.

**Completeness.** After grouping all operators and functions by their left/right type pair, every group that has any functions must have the same set of operators as the non-cross-type groups, and vice versa. The named opclass itself must have all four mandatory support functions. Cross-type groups with no functions at all are permitted. They represent opfamily entries that provide operators without corresponding support functions. This is legal when those operators are handled via the main type's support functions.

## Related Topics

- [[subsystems/indexes/brin|BRIN index internals]]
- [[subsystems/indexes/brin-minmax-multi|BRIN minmax-multi]]
- [[subsystems/wal/overview|WAL overview]]

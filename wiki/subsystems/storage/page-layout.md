---
title: "PostgreSQL Page Layout"
aliases:
  - "Page Format"
  - "PageHeaderData"
  - "8KB Page"
tags:
  - theme/storage-format
source_files:
  - src/include/storage/bufpage.h
  - src/backend/storage/page/bufpage.c
  - src/include/storage/itemid.h
symbols:
  - PageHeaderData
  - ItemIdData
  - PageAddItemExtended
  - PageRepairFragmentation
---

# PostgreSQL Page Layout

PostgreSQL divides every relation file into fixed-size **pages** (default 8192 bytes). Heap tables, indexes, and free-space map files all use the same page format — each with its own use of the special space at the end. Understanding the page layout is prerequisite knowledge for heap tuple access, index internals, and VACUUM.

## Page regions

```
┌──────────────────────────────────┐ ← byte 0
│         PageHeaderData           │  24 bytes
├──────────────────────────────────┤
│      ItemId array (line ptrs)    │  grows downward from pd_lower
│          ...                     │
├──────────────────────────────────┤ ← pd_lower
│           free space             │
├──────────────────────────────────┤ ← pd_upper
│      tuple / index data          │  grows upward toward header
│          ...                     │
├──────────────────────────────────┤ ← pd_special
│         special space            │  AM-specific (0 bytes for heap)
└──────────────────────────────────┘ ← byte 8192
```

Free space is the gap between `pd_lower` and `pd_upper`. PostgreSQL appends new line pointers at `pd_lower` (which grows down), and places new tuple data just below `pd_upper` (which grows up).

## PageHeaderData

Defined in `src/include/storage/bufpage.h`:

| Field | Type | Purpose |
|---|---|---|
| `pd_lsn` | `PageXLogRecPtr` | LSN of the last WAL record that touched this page |
| `pd_checksum` | `uint16` | Page checksum (when data checksums are enabled) |
| `pd_flags` | `uint16` | Flag bits (see below) |
| `pd_lower` | `LocationIndex` | Byte offset to end of line pointer array |
| `pd_upper` | `LocationIndex` | Byte offset to start of tuple data |
| `pd_special` | `LocationIndex` | Byte offset to start of special space |
| `pd_pagesize_version` | `uint16` | Page size (high 8 bits) + format version (low 8 bits) |
| `pd_prune_xid` | `TransactionId` | Oldest deletable XID on page (hint for pruning) |

### pd_flags bits

| Bit | Name | Meaning |
|---|---|---|
| `0x0001` | `PD_HAS_FREE_LINES` | At least one line pointer with `LP_UNUSED` state |
| `0x0002` | `PD_PAGE_FULL` | Not enough free space for a new tuple (soft hint) |
| `0x0004` | `PD_ALL_VISIBLE` | All tuples on this page are visible to all transactions |

`PD_ALL_VISIBLE` mirrors the [[subsystems/storage/visibility-map|visibility map]] bit; it allows index-only scans to skip heap fetches. The system clears it whenever any tuple on the page is modified, and VACUUM sets it again.

## Line pointer array (ItemIdData)

Between the page header and `pd_lower` lies the **line pointer array** — one `ItemIdData` entry per tuple slot. An `ItemIdData` is a 32-bit value packed as:

| Bits | Name | Meaning |
|---|---|---|
| 0–14 | `lp_off` | Byte offset from page start to the tuple |
| 15–16 | `lp_flags` | State of this line pointer (see below) |
| 17–31 | `lp_len` | Length of the tuple in bytes |

### lp_flags values

| Value | Name | Meaning |
|---|---|---|
| `0` | `LP_UNUSED` | Pointer is unused; slot available for reuse |
| `1` | `LP_NORMAL` | Pointer is live; `lp_off` and `lp_len` are valid |
| `2` | `LP_REDIRECT` | HOT redirect; `lp_off` holds the index of the target line pointer (not a byte offset) |
| `3` | `LP_DEAD` | Tuple is dead; VACUUM can reclaim this slot |

HOT pruning sets `LP_REDIRECT` when it replaces an old tuple's line pointer with a redirect to the new version on the same page. The index entry still points to the original line pointer number; the redirect transparently forwards access to the current version.

Heap pruning or VACUUM sets `LP_DEAD` when it determines a tuple is dead. `PageAddItemExtended` can reuse the slot once the page is compacted.

## Tuple placement

PostgreSQL places tuples from the **end of the page toward the header**: it decrements `pd_upper` by the (MAXALIGN-rounded) tuple size, then writes the tuple data at the new `pd_upper`. It then appends a new line pointer at `pd_lower`, and increments `pd_lower` by `sizeof(ItemIdData)`.

A page has enough room for a new tuple of size `sz` when:

```
pd_upper - pd_lower >= sizeof(ItemIdData) + MAXALIGN(sz)
```

`PageGetFreeSpace` returns `pd_upper - pd_lower - sizeof(ItemIdData)` (accounting for the line pointer overhead).

## Special space

`pd_special` points to the last region of the page, reserved for index access methods:

| AM | Special struct | Content |
|---|---|---|
| Heap | (none, size 0) | — |
| B-tree | `BTPageOpaqueData` | Level, left/right sibling block numbers, page flags |
| GIN | `GinPageOpaqueData` | Flags, right-link |
| GiST | `GISTPageOpaqueData` | Flags, right-link, NSN |
| Hash | `HashPageOpaqueData` | Bucket number, flags |

## MAXALIGN

All tuple start offsets (`lp_off`) and `pd_upper` must be `MAXALIGN`-aligned — 8 bytes on 64-bit platforms. This means PostgreSQL rounds up tuple sizes before placement. There may be padding bytes between tuples as a result.

## PageAddItemExtended

`PageAddItemExtended` (`bufpage.c`) is the function heap and index code use to insert an item onto a page. It:

1. Finds a line pointer slot (reuses an `LP_UNUSED` slot if `PAI_REUSE_HOTP` flag is set, otherwise appends).
2. Checks there is enough free space.
3. Decrements `pd_upper` by the rounded item size.
4. Copies the item data to `pd_upper`.
5. Sets the line pointer `lp_off`, `lp_len`, and `lp_flags = LP_NORMAL`.
6. Updates `pd_lower`.

## PageRepairFragmentation

After many insertions and deletions, the tuple area becomes fragmented: live tuples are interspersed with holes left by dead tuples. `PageRepairFragmentation` compacts the page in-place by:

1. Building an array of live `(offset, length)` pairs sorted by offset.
2. Packing the live tuples contiguously at `pd_special` (moving upward).
3. Updating each line pointer's `lp_off` to reflect the new position.
4. Setting `pd_upper` to the start of the compacted tuple area.

`heap_page_prune` calls this after pruning dead HOT chains. `PageAddItemExtended` also calls it when the page is full but has enough fragmented free space.

## Page checksums

When `data_checksums` is enabled (set at `initdb` time), PostgreSQL computes a checksum for each page before writing it to disk and verifies it on read.

`pg_checksum_page` (`storage/checksum.c`) computes the checksum and stores it in `pd_checksum`. It XOR-folds the page into 32 256-byte blocks, mixes each block with its block number, and combines the results. The checksum covers the entire page including the header (with `pd_checksum` zeroed during computation).

PostgreSQL reports checksum failures as `FATAL` errors; they indicate physical corruption. `pg_checksums` can enable/disable checksums offline.

## See also

- [[subsystems/storage/heap]] — heap tuple header format and how tuples are placed on pages
- [[subsystems/storage/hot]] — HOT chains and LP_REDIRECT line pointers
- [[subsystems/storage/fsm]] — how free space per page is tracked across the relation
- [[subsystems/indexes/btree]] — B-tree special space (BTPageOpaqueData)
- [[subsystems/storage/buffer-manager]] — how pages are pinned and locked before access

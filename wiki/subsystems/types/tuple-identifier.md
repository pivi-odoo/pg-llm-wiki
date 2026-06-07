---
title: "Tuple Identifier (TID / ctid)"
aliases:
  - ctid
  - TID
  - ItemPointer
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/tid.c
  - src/include/storage/itemptr.h
symbols:
  - ItemPointerData
  - tidin
  - tidout
  - currtid_byrelname
  - ItemPointerSet
  - ItemPointerCompare
---

A tuple identifier (TID) is the physical address of a single tuple on disk: it encodes which page of a relation file the tuple lives on, and which slot within that page. Every heap tuple carries its own TID in the `ctid` system column. Every index entry points back to the heap via a TID. Understanding TIDs is essential for reading EXPLAIN output, interpreting `RETURNING ctid`, and knowing when physical addresses can and cannot be trusted across transactions.

## Physical layout

The underlying C type is `ItemPointerData` (`itemptr.h`), a packed 6-byte struct with two fields:

| Field | Type | Size | Meaning |
|---|---|---|---|
| `ip_blkid` | `BlockIdData` | 4 bytes | Page (block) number within the relation file |
| `ip_posid` | `OffsetNumber` | 2 bytes | 1-based index into the page's line pointer array |

PostgreSQL explicitly annotates the struct `pg_attribute_packed()` and `pg_attribute_aligned(2)` to suppress compiler padding — because a TID lives inside every heap and index tuple header on disk, an extra two bytes of padding per tuple would waste gigabytes at scale.

The SQL-level type `tid` is a direct projection of `ItemPointerData`. Text representation uses the `(block,offset)` notation produced by `tidout()` and parsed by `tidin()` (`tid.c`). The binary wire format (`tidrecv`/`tidsend`) serialises the block number as a 4-byte big-endian integer followed by the offset as a 2-byte integer, matching the on-disk layout.

## What ctid means

Every visible heap tuple has a `ctid` value. Under normal circumstances `ctid` points to the tuple itself: `(0,1)` means the first tuple in the first page, `(3,5)` means the fifth slot in page 3. You can query it directly:

```sql
SELECT ctid, * FROM orders LIMIT 5;
```

The `ip_posid` field is an index into the page's item identifier (line pointer) array, not a byte offset. The line pointer in turn records the actual byte offset and length of the tuple within the page. This indirection is what makes in-place updates and vacuum compaction possible without rewriting every index entry.

Two special sentinel values are reserved in `ip_posid`:
- `SpecTokenOffsetNumber` (0xfffe) — used during speculative insertion before the tuple is fully committed; the `ip_blkid` field then holds the speculative token rather than a block number.
- `MovedPartitionsOffsetNumber` (0xfffd) — set on an old tuple version when an `UPDATE` moves the row to a different partition of a partitioned table.

`ctid` is a physical address, not a logical identity. Several routine operations change it:

**Full UPDATE.** A non-HOT update writes the new tuple version to a different location (possibly a different page). PostgreSQL then rewrites the old tuple's `ctid` to point forward to the new version, forming an update chain. The new tuple gets a fresh `ctid` matching its actual location. After [[subsystems/background/autovacuum|autovacuum]] reclaims the old version, even that forward pointer disappears.

**HOT (Heap-Only Tuple) UPDATE.** When an updated column is not covered by any index and the new version fits on the same page, PostgreSQL performs a HOT update. PostgreSQL writes the new tuple into a new slot on the same page, but it does not update index entries — they still point to the old line pointer, which now redirects to the new slot. From the perspective of an index scan the `ctid` appears unchanged, but the actual tuple data now lives at a different `ip_posid`. The old slot becomes a redirect line pointer, not a real tuple.

**VACUUM.** After all versions of a dead tuple have become invisible, VACUUM reclaims the line pointer slots. Subsequent inserts reuse those slots. A `ctid` that was valid before a VACUUM may then point to a completely different row — or to nothing at all.

**CLUSTER / pg_repack.** These operations rewrite the entire table, assigning all tuples new block and offset numbers.

The practical consequence: if you capture a `ctid` from a `SELECT` or `RETURNING` clause and use it later in the same session, it is only reliable within the same transaction (snapshot). Across transactions, any intervening write activity may have invalidated it.

## Following update chains with currtid

The `currtid_byrelname()` function (`tid.c`) traverses the update chain starting from a given TID and returns the latest visible version's TID. Internally it opens a table scan in TID mode (`table_beginscan_tid`) and calls `table_tuple_get_latest_tid`. It returns whatever TID the storage layer resolves to under a freshly registered snapshot.

This is primarily useful for cursor-based patterns in client libraries that issue a positioned `UPDATE` or `DELETE`. After fetching a row, the library can re-resolve the TID to confirm the row has not moved before writing. For views, `currtid_for_view()` walks the view's rewrite rules to find which base-relation column the view's `ctid` column maps to, then delegates to the base relation.

## Comparison and ordering

`ItemPointerCompare()` defines a total order: block number is the major key, offset number is the minor key. This ordering is significant for bitmap index scans. They collect TIDs into a `TIDBitmap`, sort them, and then perform heap fetches in physical order to minimise random I/O. The comparison functions `tideq`, `tidlt`, `tidle`, `tidgt`, `tidge`, and `bttidcmp` expose this ordering to SQL. A B-tree operator class for `tid` also exists, so you can build indexes on `ctid` columns in other tables (useful for denormalised audit logs).

Hashing (`hashtid`, `hashtidextended`) hashes the raw bytes of both fields together, explicitly avoiding `sizeof(ItemPointerData)` to guard against any future compiler that might add padding.

## Legitimate uses

Despite its instability across transactions, `ctid` has well-understood legitimate uses:

**Bulk delete without a unique key.** When a table has no primary key and contains duplicate rows, `ctid` can serve as a tiebreaker for a single-transaction delete:

```sql
DELETE FROM duplicates
WHERE ctid NOT IN (
    SELECT min(ctid) FROM duplicates GROUP BY col1, col2
);
```

This is safe because the query reads and consumes the `ctid` values within the same snapshot.

**Cursor positioning in client drivers.** PostgreSQL's JDBC and libpq drivers historically used `ctid` to implement positioned updates (`UPDATE ... WHERE CURRENT OF cursor`). The driver captures the `ctid` of the last fetched row and re-fetches it using a TID scan before issuing the write. This is why `currtid_byrelname` exists.

**Storage-level diagnostics.** `ctid` appears in `EXPLAIN (ANALYZE)` output for TID scans. It is invaluable when using `pageinspect` to correlate query-visible tuples with their raw on-disk representation.

## See also

- [[subsystems/storage/fsm|Free Space Map (FSM)]] — tracks per-page free space that determines where new tuple versions land
- [[subsystems/storage/visibility-map|Visibility Map]] — per-page flags used to skip pages during vacuum and index-only scans
- [[subsystems/transactions/hint-bits|Hint bits]] — status bits within tuple headers that work alongside ctid to determine tuple visibility
- [[subsystems/background/autovacuum|Autovacuum]] — reclaims dead tuple slots, invalidating stale ctid values

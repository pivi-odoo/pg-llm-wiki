---
title: "SP-GiST WAL Recovery and Vacuum"
aliases:
  - SP-GiST WAL
  - SP-GiST vacuum
  - spgxlog
  - spgvacuum
tags:
  - theme/durability
  - theme/vacuum-and-maintenance
source_files:
  - src/backend/access/spgist/spgxlog.c
  - src/backend/access/spgist/spgvacuum.c
  - src/include/access/spgist_private.h
  - src/include/access/spgxlog.h
symbols:
  - spg_redo
  - spgRedoAddLeaf
  - spgRedoMoveLeafs
  - spgRedoAddNode
  - spgRedoSplitTuple
  - spgRedoPickSplit
  - spgRedoVacuumLeaf
  - spgRedoVacuumRoot
  - spgRedoVacuumRedirect
  - spgvacuumpage
  - vacuumLeafPage
  - vacuumLeafRoot
  - vacuumRedirectAndPlaceholder
  - spgvacuumscan
  - SpGistDeadTupleData
  - SpGistPageOpaqueData
---

SP-GiST WAL recovery and vacuum together manage the structural mutations that arise from the index's space-partitioning design. Leaf tuples can be moved between pages during splits. Inner nodes can be promoted to new pages. The redirect mechanism that makes those moves safe for concurrent readers must eventually be cleaned up. The combination of eight WAL record types and a two-pass vacuum is more elaborate than what B-tree requires, because a single SP-GiST split can touch up to four distinct pages simultaneously — source leaf page, destination leaf page, inner node page, and parent page.

## Tuple States and the Redirect Mechanism

Every SP-GiST tuple — whether inner or leaf — carries a two-bit `tupstate` field as its first element. Four states are defined in `spgist_private.h`:

| State | Value | Meaning |
|---|---|---|
| `SPGIST_LIVE` | 0 | Normal live tuple |
| `SPGIST_REDIRECT` | 1 | Temporary forwarding pointer to a new location |
| `SPGIST_DEAD` | 2 | Dead; cannot be removed because a chain head slot must remain |
| `SPGIST_PLACEHOLDER` | 3 | Empty slot; preserves offset numbering for other tuples |

The redirect tuple is the key to concurrent safety. When a leaf tuple is moved to a new page — either during a move-leaves operation or a picksplit — the old slot is not simply freed. Instead it is replaced with a `SpGistDeadTuple` whose `tupstate` is `SPGIST_REDIRECT` and whose `pointer` field stores the `(BlockNumber, OffsetNumber)` of the new location. A concurrent index scan that had already noted the old location will follow the redirect rather than lose its position, without needing any lock on the source page at the time of the move.

The `SpGistDeadTuple` struct (defined in `spgist_private.h`) also carries an `xid` field — the transaction ID that created the redirect. This XID is the key that vacuum uses to decide when the redirect can be reclaimed.

The page opaque area (`SpGistPageOpaqueData`) maintains counters `nRedirection` and `nPlaceholder` so that vacuum can quickly determine whether a page contains any work to do without scanning every slot.

## WAL Record Types

The eight SP-GiST WAL record types, dispatched by `spg_redo()` in `spgxlog.c`, cover every structural mutation the index can perform:

**`XLOG_SPGIST_ADD_LEAF`** — Insert a single new leaf tuple onto a leaf page, optionally updating the parent inner tuple's downlink. Replay in `spgRedoAddLeaf()` handles two sub-cases: inserting into an empty slot (replacing a placeholder) and replacing a DEAD tuple. If a parent block reference is present, the replay also updates the node link in the inner tuple on the parent page.

**`XLOG_SPGIST_MOVE_LEAFS`** — Move a group of leaf tuples from a source page to a destination page, then update the parent downlink. The `spgxlogMoveLeafs` record carries the count of moved tuples, two offset arrays (source positions and destination positions), and the leaf tuple data. Replay in `spgRedoMoveLeafs()` inserts tuples on the destination first, so the redirect target is valid. It then calls `spgPageIndexMultiDelete()` on the source page, passing `SPGIST_REDIRECT` as the replacement state during normal operation, or `SPGIST_PLACEHOLDER` during an index build where no concurrent readers can exist.

**`XLOG_SPGIST_ADD_NODE`** — Expand an inner tuple by adding a new node. If the enlarged tuple no longer fits on the original page, it is moved to a new inner page. The original slot is then replaced with a redirect. The `spgxlogAddNode` record encodes a `parentBlk` field (0, 1, 2, or −1) to describe which of the up-to-three referenced pages holds the parent downlink, avoiding a redundant fourth page reference in the common cases where the parent is co-located with either the source or destination.

**`XLOG_SPGIST_SPLIT_TUPLE`** — Split a single inner tuple into a prefix part and a postfix part when a new node must be inserted between them. This always operates on inner pages. The `postfixBlkSame` flag in `spgxlogSplitTuple` indicates whether both halves fit on the original page. If not, replay in `spgRedoSplitTuple()` writes the postfix tuple to a second page first, to avoid a dangling forward pointer.

**`XLOG_SPGIST_PICKSPLIT`** — The most complex record type, used when the opclass's `picksplit` function partitions a leaf page's contents between a source page, an optional destination leaf page, and a new inner node page, with a possible separate parent update. The `spgxlogPickSplit` record references up to four backup blocks (src, dest, inner, parent). Replay in `spgRedoPickSplit()` inserts tuples onto whichever leaf pages need them, then installs the new inner tuple. It deliberately holds the source-page lock until the inner tuple is committed, so that any redirect tuple on the source is not a dangling pointer.

**`XLOG_SPGIST_VACUUM_LEAF`** — Records the result of vacuuming dead tuples from a regular (non-root) leaf page. The `spgxlogVacuumLeaf` struct encodes four categories of change: offsets to become DEAD, offsets to become PLACEHOLDER, a pair of offset arrays describing intra-page tuple moves that bring live chain heads to their canonical positions, and a pair of offset arrays for chain-link (`nextOffset`) corrections. This fine-grained encoding allows replay to reconstruct exactly the same page state without re-running the vacancy decision logic.

**`XLOG_SPGIST_VACUUM_ROOT`** — Simpler record for the root-as-leaf special case. The root page never uses placeholder or redirect tuples (all tuples on the root are LIVE), so cleanup is a straight `PageIndexMultiDelete()` of the dead offsets.

**`XLOG_SPGIST_VACUUM_REDIRECT`** — Records the conversion of aged-out redirect tuples to placeholders, and the removal of trailing placeholder slots. Replay in `spgRedoVacuumRedirect()` also invokes `ResolveRecoveryConflictWithSnapshot()` when running in Hot Standby. It passes the `snapshotConflictHorizon` (the newest XID among removed redirects), so that any standby query that might have been following those redirects is cleanly cancelled.

This set of eight record types is more elaborate than B-tree WAL because SP-GiST has no locality guarantee. A B-tree page split is self-contained, affecting only the split page, the new right sibling, and a single parent slot. SP-GiST's picksplit can migrate leaf tuples from one leaf page to two new leaf pages, while simultaneously requiring a new inner page. This means a single logical write can touch four structurally unrelated pages. Each page must be updated atomically with respect to crash recovery, so each WAL record captures all referenced pages as backup block references with full per-page redo instructions. The `spgxlogPickSplit` struct handles this by carrying per-tuple page-selector bytes (`leafPageSelect[]`) that tell replay whether each inserted leaf tuple belongs on the source or destination page. A further ordering constraint applies during replay. The destination page must be populated before the source page's redirect is written. The inner page must be committed before source-page locks are released. `spgRedoPickSplit()` enforces this by holding `srcBuffer` and `destBuffer` open until after the inner tuple has been written to `innerBuffer`.

## The `fillFakeState` Convention

Several redo functions need a minimal `SpGistState` solely to call `spgFormDeadTuple()`. Rather than reading the index's catalog entries at recovery time, `fillFakeState()` (spgxlog.c) constructs a zeroed `SpGistState` from only two fields embedded in the WAL record — `myXid` (the transaction that performed the operation) and `isBuild` (whether this was an index build). When `isBuild` is true, replay replaces redirects with placeholders instead, because index builds hold `AccessExclusiveLock` and no concurrent readers can exist. The `STORE_STATE` macro in `spgist_private.h` captures these two fields into an `spgxlogState` struct at WAL-write time.

## Vacuum: Two-Pass Design

SP-GiST vacuum runs in two logically distinct passes driven by `spgvacuumscan()` in `spgvacuum.c`.

### Bulk-Delete Pass

`spgbulkdelete()` calls `spgvacuumscan()` with a callback from the VACUUM machinery that identifies which heap TIDs are dead. For each non-root leaf page, `spgvacuumpage()` calls `vacuumLeafPage()` followed by `vacuumRedirectAndPlaceholder()`. For inner pages and the root-as-leaf case, only `vacuumRedirectAndPlaceholder()` runs.

`vacuumLeafPage()` performs a two-step analysis. First it scans all tuples to build a `deletable[]` bitmap and a `predecessor[]` chain map. Then it walks each leaf chain (identified by chain heads — tuples with no predecessor) and classifies every deletable tuple as one of:

- **toDead** — the entire chain is removable; the head slot must remain as a DEAD placeholder because inner nodes may point to it.
- **toPlaceholder** — a middle or tail element of a live chain; the slot can be recycled freely.
- **moveSrc / moveDest** — the chain's first live element is not at the head position. It must be swapped into the head slot so that the parent downlink remains valid.

`vacuumLeafPage()` implements the move by swapping `ItemId` line pointers rather than copying tuple data, making it safe from page-overflow even for large tuples.

If vacuum encounters a REDIRECT tuple whose `xid` is greater than or equal to its own snapshot's `xmin` (`bds->myXmin`), the redirect was created by a transaction concurrent with this vacuum run. Vacuum adds the redirect's target TID to a `pendingList` and revisits the target page after finishing the current page. `spgprocesspending()` handles this. This avoids locking more than one buffer at a time and prevents infinite loops through the duplicate-filtering in `spgAddPendingTID()`.

### Redirect Cleanup Pass

`vacuumRedirectAndPlaceholder()` runs on every page, including inner pages. It converts REDIRECT tuples to PLACEHOLDER when the redirect's `xid` is older than the global visibility horizon — tested via `GlobalVisTestIsRemovableXid()`. Vacuum can remove a redirect whose `xid` is `InvalidTransactionId` (set by `REINDEX CONCURRENTLY`, which holds `AccessExclusiveLock`) immediately.

After converting redirects, the function removes placeholder tuples that appear at the tail of the page's item array. Vacuum cannot remove placeholders that precede any live tuple, because doing so would renumber subsequent items and invalidate all existing downlinks pointing into the page.

The resulting WAL record (`XLOG_SPGIST_VACUUM_REDIRECT`) carries the `snapshotConflictHorizon` — the newest XID among all redirects converted in this pass — so that Hot Standby can detect and cancel conflicting queries without having to inspect individual redirect entries.

### Root-Page Special Case

The root page (block 1 for normal entries, block 2 for null entries) uses a flat layout: all tuples are LIVE, and no chaining occurs. As a result, `vacuumLeafRoot()` simply calls `PageIndexMultiDelete()` on the dead offsets and writes an `XLOG_SPGIST_VACUUM_ROOT` record. Vacuum skips the `vacuumRedirectAndPlaceholder()` call for root pages, because redirect and placeholder tuples cannot appear there.

## Related Topics

- [[subsystems/indexes/spgist]]
- [[subsystems/wal/overview]]
- [[subsystems/transactions/hint-bits]]
- [[subsystems/memory/resource-owner]]

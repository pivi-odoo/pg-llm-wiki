---
title: "Heap WAL Replay"
aliases:
  - "heap_redo"
  - "heap2_redo"
  - "Heap WAL Redo"
  - "heapam_xlog"
  - "Heap XLOG"
tags:
  - theme/durability
source_files:
  - src/backend/access/heap/heapam_xlog.c
  - src/include/access/heapam_xlog.h
symbols:
  - heap_redo
  - heap2_redo
  - heap_xlog_insert
  - heap_xlog_delete
  - heap_xlog_update
  - heap_xlog_multi_insert
  - heap_xlog_prune_freeze
  - heap_xlog_visible
  - heap_xlog_lock
  - heap_xlog_inplace
  - heap_xlog_confirm
  - heap_mask
  - xl_heap_insert
  - xl_heap_delete
  - xl_heap_update
  - xl_heap_prune
  - xl_heap_visible
  - xl_heap_lock
  - xl_heap_inplace
  - xlhp_freeze_plan
---

The heap WAL replay module (`heapam_xlog.c`) contains the redo functions that reconstruct heap page state during crash recovery and standby apply. Every heap-modifying operation — INSERT, UPDATE, DELETE, HOT update, tuple locking, pruning, freezing, and visibility map updates — emits a structured WAL record; the redo functions read those records and re-apply the same page mutations without access to the original transaction context. PostgreSQL 18 moved all of this logic from `heapam.c` into a dedicated `heapam_xlog.c`. This groups WAL record definitions, serialisation helpers, and redo dispatch together, and reduces the size of the already large `heapam.c`.

## Two resource managers, one module

The heap access method registers two WAL resource managers: `RM_HEAP_ID` and `RM_HEAP2_ID`. This split is a historical accident: the opcode field in a WAL record header is only 3 bits wide (the fourth is the `XLOG_HEAP_INIT_PAGE` flag), which gives at most 8 distinct operations per resource manager. When heap operations grew past eight, PostgreSQL introduced a second resource manager rather than redesigning the record format. Both are dispatched from `heapam_xlog.c`:

- `heap_redo()` handles `RM_HEAP_ID` records: INSERT, DELETE, UPDATE, HOT_UPDATE, CONFIRM, LOCK, INPLACE, and TRUNCATE.
- `heap2_redo()` handles `RM_HEAP2_ID` records: MULTI_INSERT, VISIBLE, PRUNE (three variants), LOCK_UPDATED, NEW_CID, and REWRITE.

There is no conceptual difference between the two groups; the boundary is purely an artifact of the 3-bit opcode limit.

## The INIT_PAGE flag

Many insertion operations can initialize an entirely fresh page instead of applying a tuple-level delta to an existing one. When the `XLOG_HEAP_INIT_PAGE` flag bit is set in the WAL record info field, the redo routine calls `XLogInitBufferForRedo()` and then `PageInit()` before adding the new tuple. This discards whatever was on the page before. This makes replay of a first-insert-on-a-new-page idempotent even if the page had not yet been flushed to disk: the redo function reconstructs it from scratch using the data embedded in the WAL record.

## Insert replay

`heap_xlog_insert()` reconstructs a single-tuple insert. The WAL record (`xl_heap_insert`) carries only the target offset number and a flag byte; the actual tuple data is stored as block data attached to buffer reference 0. During redo, the function reassembles the `HeapTupleHeaderData` from the stored `xl_heap_header` (which records `t_infomask2`, `t_infomask`, and `t_hoff`) plus the raw tuple body. It stamps `t_xmin` from the record's top-level XID and sets `t_cid` to `FirstCommandId`. It then calls `PageAddItem()` at the recorded offset. Redo always resets the command ID to `FirstCommandId` because a standby does not need the within-transaction ordering that `t_cid` encodes for the original executor.

If the `XLH_INSERT_ALL_VISIBLE_CLEARED` flag is set, redo clears the [[subsystems/storage/visibility-map|visibility map]] bit for the page unconditionally, before consulting the buffer's LSN. This ensures that a page that was already all-visible before the insert correctly loses that designation, even if the heap page itself does not need redo.

`heap_xlog_multi_insert()` applies the same logic iteratively for `XLOG_HEAP2_MULTI_INSERT` records, which batch multiple tuples into a single WAL record for bulk-load efficiency. When `XLOG_HEAP_INIT_PAGE` is set, redo places tuples at consecutive offsets starting from `FirstOffsetNumber`; otherwise, it reads each tuple's target offset from the `offsets[]` array in the record.

## Delete replay

`heap_xlog_delete()` modifies the existing tuple in place. A delete does not remove the tuple from the page; it sets `t_xmax` to the deleting XID and adjusts the infomask bits. The WAL record (`xl_heap_delete`) stores `xmax`, the target `offnum`, and a packed `infobits_set` byte that encodes the `t_infomask` and `t_infomask2` changes in compressed form. During redo, `fix_infomask_from_infobits()` expands the packed bits back into the tuple header fields. `PageSetPrunable()` records the XID as a candidate for future pruning.

The `XLH_DELETE_IS_SUPER` flag handles the case where the deleting transaction is the same one that inserted the tuple (a self-visible delete). In that case, redo sets `t_xmin` to `InvalidTransactionId` rather than setting `t_xmax`. This signals to visibility logic that the tuple was born dead and can be immediately discarded.

`XLH_DELETE_IS_PARTITION_MOVE` signals that the tuple was moved across partitions during an UPDATE on a partitioned table. The redo function calls `HeapTupleHeaderSetMovedPartitions()` rather than writing a normal `t_ctid`. This encodes a sentinel value that tells concurrent readers the chain continues in a different relation.

## Update and HOT update replay

`heap_xlog_update()` handles both `XLOG_HEAP_UPDATE` and `XLOG_HEAP_HOT_UPDATE`. It takes a `hot_update` boolean to choose the correct `HEAP_HOT_UPDATED` flag on the old tuple. An update requires modifying two tuple versions: the old tuple (setting `t_xmax` and `t_ctid` to point forward) and the new tuple (inserting it onto the target page).

The WAL record (`xl_heap_update`) encodes the old offset number, the old `xmax`, the packed `old_infobits_set`, the new offset number and page (if different), and the new tuple header. Because HOT updates always stay on the same page, a cross-page update implies `hot_update` is false. The new tuple's body in the WAL record may be compressed using a prefix and/or suffix borrowed from the old tuple: if `XLH_UPDATE_PREFIX_FROM_OLD` is set, a `uint16` prefix length precedes the stored middle section. The redo routine splices the prefix from the old tuple still in the buffer. The same applies to `XLH_UPDATE_SUFFIX_FROM_OLD`. Redo applies this delta compression only when the old and new tuples are on the same page, since the redo routine needs the old tuple data in the buffer when it reconstructs the new one.

After redo updates both pages, it emits FSM hints only for non-HOT updates. After a HOT update, either the old or the new tuple will be dead after pruning. The effective free space after pruning will therefore approximately match the pre-update state, so updating the [[subsystems/storage/fsm|free space map]] would be premature.

## Prune and freeze replay

`XLOG_HEAP2_PRUNE_ON_ACCESS`, `XLOG_HEAP2_PRUNE_VACUUM_SCAN`, and `XLOG_HEAP2_PRUNE_VACUUM_CLEANUP` all dispatch to `heap_xlog_prune_freeze()`. The three opcodes carry identical record formats; the distinction exists only for debugging and analysis — the opcode reveals whether the prune was triggered by on-access pruning, the first VACUUM pass, or the second VACUUM pass.

The record (`xl_heap_prune`) carries a compact serialisation of all page-level changes: redirect items, dead items, now-unused items, and freeze plans. `heap_xlog_deserialize_prune_and_freeze()` (defined in `heapdesc.c` so it can be shared with frontend WAL analysis tools) parses the variable-length block data into typed arrays. Redo then calls `heap_page_prune_execute()` to apply redirections and dead/unused markings. It also iterates over `xlhp_freeze_plan` structs to call `heap_execute_freeze_tuple()` on each listed offset.

The `XLHP_CLEANUP_LOCK` flag in `xl_heap_prune.flags` determines the lock mode used to acquire the buffer: a cleanup lock (which blocks all concurrent access, including hint-bit setters) when redirect operations or dead-item markings are present, or a plain exclusive lock when redo is only applying freezing or LP_DEAD-to-LP_UNUSED conversions. This distinction mirrors the original operation: redirecting line pointers requires a cleanup lock because it moves tuple data, while freezing in place does not.

Hot Standby mode adds one more step. If the record carries a `snapshot_conflict_horizon` XID and `XLHP_HAS_CONFLICT_HORIZON` is set, redo calls `ResolveRecoveryConflictWithSnapshot()` before acquiring the buffer lock. This aborts any standby query whose snapshot predates the horizon. This prevents it from observing tuples that the primary has already pruned or frozen.

PostgreSQL 17 merged the former `XLOG_HEAP2_FREEZE_PAGE` record type into the prune record. A `XLOG_HEAP2_PRUNE_VACUUM_SCAN` record can now carry both pruning and freezing actions together. Older WAL streams (PostgreSQL 16 and earlier) use a separate `XLOG_HEAP2_FREEZE_PAGE` opcode that is no longer emitted in PostgreSQL 17+.

## Visibility map replay

`heap_xlog_visible()` sets the all-visible and/or all-frozen bits in the [[subsystems/storage/visibility-map|visibility map]] and, when `wal_hint_bits` or checksums are enabled, also sets `PD_ALL_VISIBLE` in the heap page header. The record (`xl_heap_visible`) carries two buffer references: buffer 0 is the visibility map page, buffer 1 is the heap page.

A subtle ordering requirement governs heap page updates. Redo only updates the heap page's LSN when `XLogHintBitIsNeeded()` is true (checksums or `wal_log_hints` enabled). When neither is set, redo writes the heap page's PD_ALL_VISIBLE bit without bumping the LSN. This accepts a torn-page risk that is harmless in practice: the redo function does not read the existing page content when setting the bit, so replaying it multiple times has no ill effect.

Redo always updates the visibility map page, even if the heap page skips redo. This is because the invariant — the VM bit may be set only when the heap page's PD_ALL_VISIBLE bit is also set — is enforced in the other direction: any WAL record that clears the VM bit does so before checking the page LSN. Necessary clearing therefore still happens.

After releasing the heap buffer, the redo function also updates the [[subsystems/storage/fsm|free space map]] for the page. This compensates for the FSM becoming stale on a standby. If the standby is later promoted and VACUUM runs, VACUUM would otherwise skip FSM updates for already-all-visible pages. This can leave the FSM with optimistic estimates that cause insert failures and stalls.

## Lock replay

`heap_xlog_lock()` and `heap_xlog_lock_updated()` apply `SELECT FOR UPDATE`/`SHARE` locking records. These do not change tuple data — they update `t_xmax` and the lock infomask bits to record which transaction holds the lock. On replay, redo decodes the infobits from the packed byte and applies them to the live tuple. If `XLH_LOCK_ALL_FROZEN_CLEARED` is set, redo clears the visibility map's all-frozen bit for the page, because acquiring a row lock on a frozen page invalidates its frozen status.

## Inplace update replay

`heap_xlog_inplace()` replays catalog-level in-place updates, which overwrite the data portion of an existing tuple without creating a new version. PostgreSQL uses this path for system catalog rows that must be updated without MVCC versioning overhead — for example, updating `pg_class.relpages`. The WAL record stores the new tuple body, which must be exactly the same length as the old body. The redo function copies it directly over `t_hoff` bytes into the existing tuple using `memcpy`. After applying the page change, `ProcessCommittedInvalidationMessages()` replays the shared-invalidation messages embedded in the record, so that cached catalog entries on the standby are invalidated at the same logical point as they were on the primary.

## Speculative insert confirmation

`heap_xlog_confirm()` finalises a speculative insert. During `INSERT ... ON CONFLICT`, PostgreSQL initially inserts a tuple with a speculative token in `t_ctid` rather than the tuple's real self-pointer. Once the conflict check passes and the insert is confirmed, PostgreSQL writes `XLOG_HEAP_CONFIRM`. The redo function overwrites `t_ctid` with the tuple's real TID (block number plus offset). This converts it from speculative to regular. Before confirmation, concurrent transactions that encounter the speculative tuple know to wait.

## Page masking for consistency checks

`heap_mask()` is the function that the WAL replay infrastructure registers to blank out fields that may legitimately differ between a primary page and a replayed standby page, before the two are compared during `--debug` or consistency-check tooling. It masks:

- The LSN and checksum fields, which differ by design.
- [[subsystems/transactions/hint-bits|Hint bits]] for tuples whose `t_xmin` is not yet frozen — hint bits are set lazily without WAL, so a standby page may lack hints set by backends on the primary.
- The `t_cid` field in every tuple, because replay always writes `FirstCommandId`.
- The `t_ctid` of speculative tuples, because speculative tokens are backend-local and are never WAL-logged.
- Alignment padding between the end of a tuple and the next MAXALIGN boundary, which may contain uninitialized bytes.

## Related Topics

- [[subsystems/storage/heap|Heap Storage and Tuple Format]] — the on-disk page and tuple structure that replay reconstructs
- [[subsystems/storage/hot|HOT Updates]] — design of heap-only tuple chains replayed by heap_xlog_update
- [[subsystems/storage/visibility-map|Visibility Map]] — the VM structure updated by heap_xlog_visible
- [[subsystems/storage/fsm|Free Space Map]] — the FSM hints maintained during replay
- [[subsystems/wal/checkpoint|Checkpoints]] — how checkpoints interact with WAL replay and buffer flush
- [[code-paths/update|UPDATE Execution]] — the primary-side code path that writes the WAL records replayed here
- [[subsystems/replication/pitr|Point-in-Time Recovery]] — the recovery framework that drives redo dispatch

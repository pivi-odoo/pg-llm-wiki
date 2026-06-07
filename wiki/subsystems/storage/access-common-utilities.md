---
title: "Access Method Common Utilities"
aliases:
  - access common utilities
  - relation open close
  - table open close
  - ScanKey initialization
  - table AM dispatch
  - toast helper
  - bufmask
tags:
  - theme/extensibility
source_files:
  - src/backend/access/common/bufmask.c
  - src/backend/access/common/printsimple.c
  - src/backend/access/common/relation.c
  - src/backend/access/common/scankey.c
  - src/backend/access/common/session.c
  - src/backend/access/index/amvalidate.c
  - src/backend/access/nbtree/nbtvalidate.c
  - src/backend/access/spgist/spgproc.c
  - src/backend/access/spgist/spgvalidate.c
  - src/backend/access/table/table.c
  - src/backend/access/table/tableamapi.c
  - src/backend/access/table/toast_helper.c
symbols:
  - relation_open
  - relation_close
  - table_open
  - table_close
  - GetTableAmRoutine
  - TableAmRoutine
  - ScanKeyInit
  - ScanKeyEntryInitialize
  - ScanKeyCopy
  - InitSession
  - MySession
  - mask_page_by_amop
  - RestoreBlockImage
  - PrintCommandTag
  - check_amop_signature
  - check_amproc_signature
  - toast_insert_or_update
  - toast_delete
---

A collection of shared utilities holds the PostgreSQL access method layer together, providing the glue between the planner/executor and the physical storage implementations. These utilities handle relation opening and locking, scan specification, AM dispatch, TOAST management, operator class validation, and WAL consistency masking. All of these are concerns that cut across multiple AM implementations and must behave consistently, regardless of the underlying storage engine.

## Relation and Table Opening

`relation_open()` (`relation.c`) is the universal entry point for acquiring a reference to any relation by OID. It takes an OID and a lock mode, acquires the requested lock, builds or fetches the relcache entry, and returns a `Relation`. The complementary `relation_close()` releases the lock and decrements the reference count. These wrappers work for any `relkind` — tables, indexes, sequences, views, or composite types.

`table_open()` (`table.c`) is the narrowed form that asserts the target relation is a table (or a partition or matview). The backend-wide convention is to use `table_open()` rather than the older `heap_open()` alias: callers that use `table_open()` signal that they are AM-agnostic and will dispatch through the [[subsystems/storage/table-am|table AM]] interface rather than calling heap-specific routines directly. `table_close()` mirrors `relation_close()` and is equally thin.

`table.c` also defines scan lifecycle wrappers — `table_beginscan()`, `table_endscan()`, and friends. They delegate immediately to the AM's `scan_begin` and `scan_end` callbacks held in the `TableAmRoutine` struct, so the caller never needs to know which AM it is talking to.

## Table AM Dispatch

`GetTableAmRoutine()` (`tableamapi.c`) resolves an AM handler function OID to a populated `TableAmRoutine` struct. Every table AM registers a handler function. The relcache stores its OID in `rd_amhandler`. The first time a relation is accessed, `GetTableAmRoutine()` calls the handler function, which fills in function pointers for every AM operation (tuple insert, update, delete, scan, freeze, etc.). PostgreSQL caches the result in `rd_tableam` on the relcache entry, so the dispatch overhead is one pointer dereference on subsequent calls rather than a full OID lookup.

This indirection is what makes custom table AMs — columnar storage, time-series engines, or foreign-data wrappers masquerading as heap tables — a first-class feature. All code that goes through `table_open()` and the `TableAmRoutine` callbacks works transparently with any registered AM.

## Scan Key Initialization

A `ScanKey` is the per-attribute comparison specification used by both index scans and sequential [[subsystems/storage/heap|heap]] scans. It bundles the operator function OID, the strategy number (e.g., `BTLessStrategyNumber`), the comparison datum, and flags that indicate whether the datum is null or whether the operator is a row compare. `ScanKeyInit()` and the lower-level `ScanKeyEntryInitialize()` (`scankey.c`) populate these fields. `ScanKeyCopy()` duplicates an array of scan keys into a palloc'd block.

The design separates expression compilation from scan execution. The planner prepares scan keys once, resolving operator OIDs and evaluating constant arguments. The AM's scan loop then applies them to each candidate tuple, without re-evaluating the full expression tree. This matters for index AM authors: a scan key array passed to `index_beginscan()` is a stable specification that the AM can inspect to choose an optimal scan strategy (e.g., index skip scans or covering-index short-circuits).

## Shared Session State for Parallel Workers

`session.c` maintains a `Session` struct allocated in dynamic shared memory (DSM), shared between a parallel leader and all its workers. Each backend that participates in a parallel query sets the global `MySession` pointer. Currently the session carries the typemod registry — the table of `BlessTupleDesc()` results that assigns type OIDs to anonymous record types. Because a worker process may construct composite rows and return them to the leader, both sides must agree on the typemod assignment. Without the shared session, a record type built in one process would have an unrecognised typemod in another.

The `Session` struct is intentionally minimal; it is not a general scratchpad. Developers should add new cross-process state here only when it genuinely cannot be reconstructed independently in each worker.

## WAL Consistency Masking

`RestoreBlockImage()` verifies [[subsystems/wal/wal-records|WAL records]] that contain full page images at recovery time. It compares the recovered page byte-for-byte against what was written. But several page fields are legitimately different between the original write and a recovered copy. [[subsystems/transactions/hint-bits|Hint bits]] are set opportunistically during reads and are never WAL-logged individually. `pd_lsn` may be updated without a full-page write. Checksum fields need special treatment. `mask_page_by_amop()` (`bufmask.c`) zeros out all these volatile fields in both the expected and actual images before the comparison, preventing false-positive corruption reports.

Each AM provides its own masking function (registered as `REGBUF_STANDARD_INFO` or via the AM's `rm_mask` callback in the [[subsystems/wal/overview|resource manager]] table). `bufmask.c` provides the shared masking primitives — zeroing the page header's volatile fields, zeroing free space, and zeroing individual item pointers — that AM-specific masking functions compose.

## Operator Class Validation

When `CREATE OPERATOR CLASS` or `REINDEX` is run, PostgreSQL verifies that the operator class satisfies the AM's requirements. `amvalidate.c` provides two shared helpers. `check_amop_signature()` verifies that an operator entry has the expected input and output types for its strategy number. `check_amproc_signature()` does the same for support procedures. Every AM's validation function calls these helpers rather than re-implementing type-signature checking.

`nbtvalidate.c` implements B-tree operator class validation using those helpers: it checks that the opclass provides all required comparison operators (less-than through greater-than-or-equal) and the mandatory support procedures (`btorder`, `btequalimage`, etc.). `spgvalidate.c` performs the equivalent checks for SP-GiST operator classes, verifying that inner-consistency, leaf-consistency, and picksplit support functions are present and have correct signatures.

## SP-GiST Shared Support Procedures

`spgproc.c` holds support procedures that are shared across multiple built-in SP-GiST operator classes. The distance procedure for KNN queries computes the ordering metric generically from the stored key without needing per-opclass specialisation. Range-containment helpers, used by range-type and `inet` opclasses, live here to avoid code duplication between the several operator classes that model prefix or containment relationships.

## TOAST Management

`toast_helper.c` drives [[subsystems/storage/toast|TOAST]] insertion and deletion. It is the AM-facing side of out-of-line storage. A table AM (primarily heapam) calls `toast_insert_or_update()` when it is about to store a tuple that contains oversized varlena attributes. The helper opens the relation's TOAST table, slices each oversized attribute into fixed-size chunks, inserts the chunks with their chunk-sequence OIDs, and rewrites the parent attribute as an external TOAST pointer. `toast_delete()` follows TOAST pointers in a tuple being deleted, opens the TOAST relation, and removes all chunk rows for each out-of-line attribute.

By centralising this logic in `toast_helper.c`, a custom table AM gains TOAST support for free — it only needs to call these helpers at the right points in its insert, update, and delete paths, rather than duplicating the chunking and chunk-deletion logic.

## See also

- [[subsystems/storage/table-am|Table Access Method Interface]]
- [[subsystems/storage/heap|Heap Storage]]
- [[subsystems/storage/toast|TOAST Out-of-Line Storage]]
- [[subsystems/wal/wal-records|WAL Records]]
- [[subsystems/wal/overview|WAL Resource Managers]]
- [[subsystems/locking/lwlocks|Lightweight Locks]]
- [[subsystems/memory/contexts|Memory Contexts]]
- [[subsystems/transactions/hint-bits|Hint Bits]]

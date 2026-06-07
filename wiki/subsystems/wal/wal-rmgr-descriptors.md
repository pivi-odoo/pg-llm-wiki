---
title: "WAL Resource Manager Descriptor Functions"
aliases:
  - "rmgrdesc"
  - "WAL desc callbacks"
  - "pg_waldump descriptors"
tags:
  - theme/observability
source_files:
  - src/backend/access/rmgrdesc/brindesc.c
  - src/backend/access/rmgrdesc/clogdesc.c
  - src/backend/access/rmgrdesc/committsdesc.c
  - src/backend/access/rmgrdesc/dbasedesc.c
  - src/backend/access/rmgrdesc/genericdesc.c
  - src/backend/access/rmgrdesc/gindesc.c
  - src/backend/access/rmgrdesc/gistdesc.c
  - src/backend/access/rmgrdesc/hashdesc.c
  - src/backend/access/rmgrdesc/heapdesc.c
  - src/backend/access/rmgrdesc/logicalmsgdesc.c
  - src/backend/access/rmgrdesc/mxactdesc.c
  - src/backend/access/rmgrdesc/nbtdesc.c
  - src/backend/access/rmgrdesc/relmapdesc.c
  - src/backend/access/rmgrdesc/replorigindesc.c
  - src/backend/access/rmgrdesc/rmgrdesc_utils.c
  - src/backend/access/rmgrdesc/seqdesc.c
  - src/backend/access/rmgrdesc/smgrdesc.c
  - src/backend/access/rmgrdesc/spgdesc.c
  - src/backend/access/rmgrdesc/standbydesc.c
  - src/backend/access/rmgrdesc/tblspcdesc.c
  - src/backend/access/rmgrdesc/xlogdesc.c
symbols:
  - heap_desc
  - heap2_desc
  - heap_identify
  - heap2_identify
  - btree_desc
  - btree_identify
  - gin_desc
  - gin_identify
  - gist_desc
  - hash_desc
  - brin_desc
  - spg_desc
  - clog_desc
  - commit_ts_desc
  - multixact_desc
  - seq_desc
  - dbase_desc
  - tblspc_desc
  - relmap_desc
  - smgr_desc
  - generic_desc
  - standby_desc
  - logicalmsg_desc
  - replorigin_desc
  - xlog_desc
  - array_desc
  - offset_elem_desc
  - redirect_elem_desc
  - oid_elem_desc
  - RmgrData
---

Every [[subsystems/wal/wal-records|WAL resource manager]] must provide two callbacks for human-readable WAL inspection. `rm_desc` appends a field-by-field description of a decoded record to a `StringInfo` buffer. `rm_identify` returns the name of the record type given an `xl_info` byte. Together these callbacks power `pg_waldump` output and any other tool that needs to interpret WAL records without replaying them. The implementations for all built-in resource managers live in `src/backend/access/rmgrdesc/`, one file per rmgr. This grouping keeps them away from the redo logic, so they can compile into `pg_waldump` without pulling in the full backend.

## The desc/identify contract

The `RmgrData` struct (defined in `src/include/access/xlog_internal.h`) holds both callbacks alongside `rm_redo` and others:

```c
void        (*rm_desc)     (StringInfo buf, XLogReaderState *record);
const char *(*rm_identify) (uint8 info);
```

The caller invokes `rm_identify` first: given the raw `xl_info` byte, it returns a short uppercase token such as `"INSERT"`, `"VACUUM"`, or `"CHECKPOINT_ONLINE"`. The caller then invokes `rm_desc` to append additional detail — field values decoded from the record's data payload — to the same buffer. The README in the `rmgrdesc` directory describes the expected output format: a JSON-like `key: value` style at the top level, with arrays rendered as `[elem, elem]` and nested objects as `{ key: val }`. The format is advisory rather than a stable API. Individual rmgrs invent local conventions when they improve readability.

Recovery needs neither callback. Recovery invokes only `rm_redo`, during crash recovery or streaming replay. The `rm_desc`/`rm_identify` pair is exclusively for inspection tools. That is why the entire `rmgrdesc/` subtree can compile standalone.

## Heap descriptor (heapdesc.c)

PostgreSQL splits the [[subsystems/storage/heap|heap]] rmgr across two resource manager IDs (`RM_HEAP_ID` and `RM_HEAP2_ID`). Accordingly, `heapdesc.c` exports two desc functions, `heap_desc()` and `heap2_desc()`, each dispatching on `xl_info & XLOG_HEAP_OPMASK`.

`heap_desc()` handles the primary DML operations. For `XLOG_HEAP_INSERT` it emits the tuple offset number and flags byte. For `XLOG_HEAP_DELETE` and `XLOG_HEAP_LOCK` it decodes `xl_heap_delete.infobits_set` through the local `infobits_desc()` helper, which renders the `XLHL_XMAX_*` flags as a bracket-enclosed list. `XLOG_HEAP_UPDATE` and `XLOG_HEAP_HOT_UPDATE` use the same path since their WAL structs are identical (`xl_heap_update`), printing old and new xmax/offset pairs plus infobits.

`heap2_desc()` covers vacuum-oriented operations. The three `XLOG_HEAP2_PRUNE_*` variants share a single decode path that calls `heap_xlog_deserialize_prune_and_freeze()`. The redo path also uses this function. It lives in `heapdesc.c`, so `pg_waldump` gets it without linking against `heapam.c`. The function walks the block data to extract freeze plans, redirect pairs, dead offsets, and unused offsets. `heap2_desc()` then renders each array via `array_desc()` from `rmgrdesc_utils.c`.

## B-tree descriptor (nbtdesc.c)

`btree_desc()` (nbtdesc.c) covers the full set of nbtree WAL record types. Most cases are short. `XLOG_BTREE_INSERT_*` records print only the target offset number. `XLOG_BTREE_NEWROOT` prints the tree level. `XLOG_BTREE_DEDUP` prints the deduplication interval count.

The most complex output comes from `XLOG_BTREE_VACUUM` and `XLOG_BTREE_DELETE`, which may carry variable-length block data listing deleted and updated posting-list entries. The local `delvacuum_desc()` function reads that block data and renders deleted offsets via `array_desc()` and updated entries as nested objects of the form `{ off: N, nptids: M, ptids: [p0, p1] }`. This format mirrors the physical layout of `xl_btree_update` structs packed after the offset arrays in the block data.

## Index descriptors

Each index access method has its own desc file. `gindesc.c` (`gin_desc()`) is the most elaborate: GIN insert records carry either an `ginxlogInsertEntry` (internal node entry) or a `ginxlogRecompressDataLeaf` structure for compressing leaf posting lists. The local `desc_recompress_leaf()` walks the packed action sequence — `GIN_SEGMENT_INSERT`, `GIN_SEGMENT_REPLACE`, `GIN_SEGMENT_ADDITEMS`, `GIN_SEGMENT_DELETE` — and emits a per-segment summary.

`gistdesc.c` (`gist_desc()`), `hashdesc.c` (`hash_desc()`), `brindesc.c` (`brin_desc()`), and `spgdesc.c` (`spg_desc()`) follow the same pattern: a switch on the info byte, casting `XLogRecGetData()` to the appropriate WAL struct, and printing selected fields. BRIN records show heap block number, pages-per-range, and summary offset numbers. SP-GiST records cover leaf additions, node additions, tuple splits, and vacuum operations with their affected offset numbers.

## Transaction infrastructure descriptors

`clogdesc.c` (`clog_desc()`) handles the two [[subsystems/storage/clog|CLOG]] operations. `CLOG_ZEROPAGE` emits the 64-bit page number. `CLOG_TRUNCATE` emits the page number plus the oldest transaction ID being discarded.

`committsdesc.c` (`commit_ts_desc()`) mirrors `clog_desc()` almost exactly, covering `COMMIT_TS_ZEROPAGE` and `COMMIT_TS_TRUNCATE` for the subsystem that tracks commit timestamps (enabled by `track_commit_timestamp`).

`mxactdesc.c` (`multixact_desc()`) handles multixact. Zero-page records just print the page number. `XLOG_MULTIXACT_CREATE_ID` records print the multixact ID, offset, member count, and the list of member XIDs with their lock status strings (`(keysh)`, `(sh)`, `(forupd)`, etc.), each formatted by the local `out_member()` helper.

`standbydesc.c` (`standby_desc()`) covers three record types. `XLOG_RUNNING_XACTS` prints the next XID, latest completed XID, oldest running XID, and lists of active top-level and sub-transaction XIDs — the snapshot used by hot standby conflict detection. `XLOG_STANDBY_LOCK` prints the XID and relation OIDs of locks being held. `XLOG_INVALIDATIONS` delegates to `standby_desc_invalidations()`. `xactdesc.c` also calls this function for commit records that carry cache invalidation messages. The function prints each `SharedInvalidationMessage` by type (catcache, catalog, relcache, snapshot, etc.). Sharing this helper avoids duplicating the invalidation decoding logic between the standby and transaction rmgr descriptors.

## DDL and storage descriptors

`dbasedesc.c` (`dbase_desc()`) covers `XLOG_DBASE_CREATE_FILE_COPY`, `XLOG_DBASE_CREATE_WAL_LOG`, and `XLOG_DBASE_DROP`, printing the source and target tablespace/database OID pairs or the list of tablespace IDs being removed.

`tblspcdesc.c` (`tblspc_desc()`) prints the OID and filesystem path for `XLOG_TBLSPC_CREATE`, and just the OID for `XLOG_TBLSPC_DROP`.

`relmapdesc.c` (`relmap_desc()`) handles `XLOG_RELMAP_UPDATE`, printing the database OID, tablespace OID, and byte size of the updated relation-map file.

`smgrdesc.c` (`smgr_desc()`) covers the storage manager records. `XLOG_SMGR_CREATE` resolves the `RelFileLocator` to a path string via `relpathperm()`. `XLOG_SMGR_TRUNCATE` prints the path, the new block count, and the truncation flags.

`seqdesc.c` (`seq_desc()`) handles only `XLOG_SEQ_LOG`, printing the three-part relation locator (tablespace/database/relation OIDs).

`genericdesc.c` (`generic_desc()`) describes generic WAL records used by extension code that calls the generic WAL API. The record body is a sequence of `(offset, length, data)` triples describing page regions to overwrite. The desc function walks the payload and prints each `offset, length` pair. `generic_identify()` unconditionally returns `"Generic"` since the format distinguishes no subtypes.

## Logical replication descriptors

`logicalmsgdesc.c` (`logicalmsg_desc()`) handles `XLOG_LOGICAL_MESSAGE`: it prints whether the message is transactional or non-transactional, the prefix string, the payload size in bytes, and the raw payload bytes as space-separated hex values.

`replorigindesc.c` (`replorigin_desc()`) covers replication origin records. `XLOG_REPLORIGIN_SET` prints the origin node ID, the remote LSN being tracked, and the force flag. `XLOG_REPLORIGIN_DROP` prints the node ID being dropped.

## XLOG/checkpoint descriptor (xlogdesc.c)

`xlogdesc.c` (`xlog_desc()`) handles the XLOG resource manager's own records. Checkpoint records (`XLOG_CHECKPOINT_SHUTDOWN`, `XLOG_CHECKPOINT_ONLINE`) produce the most detailed output: redo LSN, timeline IDs, full-page-writes flag, WAL level, next XID/OID/multixact/offset, oldest XID and its database, oldest multixact and its database, oldest and newest commit-timestamp XIDs, and oldest running XID. `XLOG_PARAMETER_CHANGE` prints the GUC values in effect when PostgreSQL wrote the record (connection limits, WAL level, `wal_log_hints`, `track_commit_timestamp`). FPI records (`XLOG_FPI`, `XLOG_FPI_FOR_HINT`) produce no additional output beyond the block reference printed by the record reader.

`xlogdesc.c` also contains `XLogRecGetBlockRefInfo()`, a utility used by `pg_waldump` to print the block reference list (relation OID triplet, fork, block number, and any FPI metadata including compression method and hole size) for every block referenced in a WAL record. This function is separate from the rmgr callbacks and applies universally across all record types.

## Shared formatting utilities (rmgrdesc_utils.c)

`rmgrdesc_utils.c` provides helpers shared across multiple desc files:

- `array_desc()` — iterates an array of fixed-size elements, calling a per-element callback and inserting `, ` separators, or emitting `[]` for an empty array.
- `offset_elem_desc()` — formats a single `OffsetNumber` as a decimal integer.
- `redirect_elem_desc()` — formats a pair of offset numbers as `from->to`, used for heap prune redirect entries.
- `oid_elem_desc()` — formats an `Oid` as a decimal integer.

These helpers exist because several rmgrs emit arrays of offset numbers (heap prune, B-tree vacuum, BRIN). Without them, duplicate formatting would otherwise accumulate across the desc files.

## See also

- [[subsystems/wal/wal-records]] — WAL record format, the RmgrData struct, and the full resource manager table
- [[subsystems/wal/overview]] — WAL architecture and the role of resource managers
- [[subsystems/wal/xlog-reader]] — XLogReaderState and the decoding infrastructure that desc callbacks receive
- [[subsystems/storage/heap]] — heap WAL record types described by heapdesc.c
- [[subsystems/wal/btree-wal]] — B-tree WAL operations described by nbtdesc.c
- [[subsystems/wal/generic-xlog]] — generic WAL API described by genericdesc.c

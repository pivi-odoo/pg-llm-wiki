---
title: "WAL Record Format and Resource Managers"
aliases:
  - "WAL Records"
  - "XLogRecord"
  - "Resource Managers"
  - "Full-Page Images"
  - "FPI"
tags:
  - theme/durability
source_files:
  - src/include/access/xlogrecord.h
  - src/backend/access/transam/xloginsert.c
  - src/backend/access/transam/rmgr.c
  - src/include/access/rmgrlist.h
  - src/backend/access/transam/xlog.c
symbols:
  - XLogRecord
  - XLogRegisterData
  - XLogRegisterBuffer
  - XLogInsert
  - RmgrData
  - XLogRecPtr
---

# WAL Record Format and Resource Managers

PostgreSQL records every database change that must survive a crash as a WAL record. It writes that record before applying the change to shared buffers. Understanding the record format is essential for reading WAL (via `pg_waldump`), writing custom WAL (custom resource managers, PG 15+), and understanding recovery.

## LSN (Log Sequence Number)

An LSN (`XLogRecPtr`, a `uint64`) is a byte offset within the WAL stream. LSNs increase monotonically. PostgreSQL uses them as the ordering primitive for:

- `pd_lsn` in page headers (last WAL record touching the page)
- Replication progress (`sent_lsn`, `flush_lsn`, `replay_lsn`)
- Checkpoint redo pointer
- Slot `confirmed_flush_lsn`

`pg_current_wal_lsn()` and `pg_waldump` both work with LSNs.

## XLogRecord header

Every WAL record begins with a fixed header (`src/include/access/xlogrecord.h`):

| Field | Type | Purpose |
|---|---|---|
| `xl_tot_len` | `uint32` | Total length of the record (header + data) |
| `xl_xid` | `TransactionId` | XID of the generating transaction (0 for non-transactional) |
| `xl_prev` | `XLogRecPtr` | LSN of the previous WAL record (forms a backward chain) |
| `xl_info` | `uint8` | Upper 4 bits: resource manager flags; lower 4 bits: RM-specific op code |
| `xl_rmid` | `RmgrId` | Resource manager ID |
| `xl_crc` | `pg_crc32c` | CRC of the record (header + all data blocks) |

After the header come zero or more **registered block descriptors** (one per modified buffer) and then the non-buffer data payload.

## Building a WAL record (modern API)

The modern insertion API (`xloginsert.c`) uses a registration model:

```c
XLogBeginInsert();

// Register non-buffer data (e.g. XLOG_HEAP_INSERT payload)
XLogRegisterData(data_ptr, data_len);

// Register a modified buffer; may emit a Full-Page Image
XLogRegisterBuffer(block_id, buffer, flags);

// Register buffer-specific data (e.g. the tuple offset)
XLogRegisterBufData(block_id, data_ptr, data_len);

lsn = XLogInsert(RM_HEAP_ID, XLOG_HEAP_INSERT);
```

`XLogInsert` assembles the record and appends it to the WAL buffer under a WAL insertion lock.

## Full-Page Images (FPI)

After a checkpoint, the WAL record for the **first modification** of any page carries a full copy of the page, ahead of the delta. This is called a Full-Page Image (FPI) or backup block.

**Why:** If the OS writes only part of an 8KB page before a crash (torn write), recovery cannot safely apply a delta record to an inconsistent page. A FPI makes recovery idempotent: recovery restores the page from the image before it applies the delta.

`XLogRegisterBuffer` checks whether it needs an FPI (comparing `pd_lsn` against the checkpoint redo LSN) and sets the `BKPBLOCK_WILL_INIT` or standard FPI flag accordingly.

FPI-related `xl_info` flags per block descriptor:

| Flag | Meaning |
|---|---|
| `BKPBLOCK_HAS_IMAGE` | A full-page image follows the block descriptor |
| `BKPBLOCK_HAS_DATA` | Per-block data follows the (optional) image |
| `BKPBLOCK_WILL_INIT` | Recovery should zero-initialize the page before applying the image |
| `BKPBLOCK_SAME_REL` | This block is in the same relation as the previous block descriptor |

`wal_compression` (GUC) enables LZ4/zstd/pglz compression of FPIs, reducing WAL volume at the cost of CPU.

`wal_log_hints` forces FPIs for hint-bit updates (normally not WAL-logged) so that checksums and replicas stay consistent.

## Resource managers

Each WAL-generating subsystem registers a **resource manager** (`RmgrData`) with a numeric ID. PostgreSQL stores the RM ID in `xl_rmid`. The op code occupies the lower bits of `xl_info`.

### Core resource managers

| RM ID | Name | Records written for |
|---|---|---|
| 0 | `XLOG` | Checkpoint, FPI, NOOP, switch, backup_block |
| 1 | `Transaction` | COMMIT, ABORT, PREPARE, COMMIT_PREPARED, ABORT_PREPARED |
| 2 | `Storage` | SMGR_CREATE, SMGR_TRUNCATE |
| 3 | `CLOG` | [[subsystems/storage/clog|CLOG]] page zero/truncate |
| 4 | `Database` | CREATE DATABASE, DROP DATABASE |
| 5 | `Tablespace` | CREATE TABLESPACE, DROP TABLESPACE |
| 6 | `MultiXact` | MULTIXACT_ZERO_OFF_PAGE, MULTIXACT_CREATE_ID |
| 7 | `RelMap` | Relation mapping file updates |
| 8 | `Standby` | Running transactions, lock records for hot standby |
| 9 | `Heap2` | VACUUM, FREEZE_PAGE, VISIBLE, MULTI_INSERT, LOCK_UPDATED |
| 10 | `Heap` | INSERT, DELETE, UPDATE, HOT_UPDATE, LOCK, INPLACE |
| 11 | `Btree` | INSERT_LEAF, INSERT_UPPER, SPLIT_L, SPLIT_R, VACUUM, DELETE, MARK_PAGE_HALFDEAD, UNLINK_PAGE, NEWROOT, REUSE_PAGE, META_CLEANUP |
| 12 | `Hash` | INIT_META_PAGE, INSERT, MOVE_PAGE, REMOVE_PAGE, SQUEEZE_PAGE, DELETE, SPLIT_ALLOCATE_PAGE, SPLIT_PAGE, SPLIT_COMPLETE, VACUUM_ONE_PAGE |
| 13 | `Gin` | GIN_CREATE_INDEX, GIN_CREATE_PTREE, GIN_INSERT, GIN_DELETE, GIN_UPDATE_META_PAGE, GIN_INSERT_LISTPAGE, GIN_DELETE_LISTPAGE |
| 14 | `Gist` | GIST_PAGE_UPDATE, GIST_DELETE, GIST_PAGE_REUSE, GIST_ASSIGN_LSN |
| 15 | `Sequence` | LOG |
| 16 | `SPGist` | CREATE_INDEX, ADD_LEAF, MOVE_LEAFS, ADD_NODE, SPLIT_TUPLE, VACUUM_LEAF, VACUUM_ROOT, VACUUM_REDIRECT |
| 17 | `BRIN` | CREATEIDX, INSERT, UPDATE, SAMEPAGE_UPDATE, REVMAP_EXTEND, DESUMMARIZE |
| 18 | `CommitTs` | ZEROPAGE, TRUNCATE |
| 19 | `ReplicationOrigin` | TIMESTAMP |
| 20 | `Generic` | Generic WAL records (for extensions using the generic WAL API) |
| 21 | `LogicalMessage` | Logical replication messages |

### RmgrData callbacks

Each resource manager provides:

| Callback | Purpose |
|---|---|
| `rm_redo` | Replay the record during recovery |
| `rm_desc` | Produce a human-readable description (used by `pg_waldump`) |
| `rm_identify` | Return a string name for the op code |
| `rm_startup` | Called at recovery start |
| `rm_cleanup` | Called at recovery end |
| `rm_mask` | Zero out fields that are not relevant for consistency checks |

## Custom resource managers (PG 15+)

Extensions can register their own resource manager via `RegisterCustomRmgr`. This allows extensions with custom on-disk formats (e.g. custom index AMs) to write and replay their own WAL records rather than using the generic WAL API.

## WAL page header

PostgreSQL writes WAL to 1MB segment files in `$PGDATA/pg_wal/`. Each segment begins with an `XLogLongPageHeaderData` (first segment page) or `XLogPageHeaderData` (subsequent pages):

| Field | Purpose |
|---|---|
| `xlp_magic` | Magic number for format identification |
| `xlp_info` | Page flags (`XLP_LONG_HEADER`, `XLP_FIRST_IS_CONTRECORD`) |
| `xlp_tli` | Timeline ID |
| `xlp_pageaddr` | LSN of the start of this page |
| `xlp_rem_len` | Bytes remaining from a record that started on a previous page |

Records that span page boundaries have a continuation header (`XLogRecordDataHeaderLong`) on the next page.

## See also

- [[subsystems/wal/overview]] — WAL architecture, buffers, and insertion locking
- [[subsystems/wal/checkpoint]] — how checkpoints advance the redo LSN and recycle segments
- [[subsystems/wal/recovery]] — how rm_redo callbacks are called during crash recovery
- [[subsystems/storage/buffer-manager]] — how pd_lsn is checked before registering a buffer for WAL

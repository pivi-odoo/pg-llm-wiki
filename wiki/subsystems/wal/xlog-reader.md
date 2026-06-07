---
title: "WAL Record Reader"
aliases:
  - XLogReader
  - xlogreader
  - WAL reader
tags:
  - theme/durability
source_files:
  - src/backend/access/transam/xlogreader.c
  - src/include/access/xlogreader.h
symbols:
  - XLogReaderState
  - XLogReaderRoutine
  - DecodedXLogRecord
  - DecodedBkpBlock
  - XLogReadRecord
  - XLogNextRecord
  - XLogReadAhead
  - XLogBeginRead
  - XLogFindNextRecord
  - DecodeXLogRecord
  - RestoreBlockImage
  - WALRead
---

The WAL record reader (`xlogreader.c`) is generic infrastructure for traversing WAL. Every PostgreSQL component that needs to traverse WAL uses it: crash recovery, streaming replication, `pg_waldump`, logical decoding, and timeline following. Its design separates the mechanics of locating and assembling raw record bytes from the decisions about where to fetch those bytes, letting callers plug in their own I/O strategy via callbacks.

## The callback-driven I/O model

`XLogReaderState` holds no knowledge of the source of WAL pages. Instead, callers supply an `XLogReaderRoutine` struct containing three callbacks: `page_read`, `segment_open`, and `segment_close`. The reader invokes the `page_read` callback whenever it needs a WAL page. The callback fills `state->readBuf` and returns the number of bytes read. This design makes the same reader usable whether the callback fetches pages from a local `pg_wal/` directory, from an archive, from a primary over the network, or even from a preallocated in-memory buffer — the core logic never changes.

The reader caches the last page it read. When the requested data is already present in `readBuf` (same segment number and same page offset with sufficient length), `ReadPageInternal()` returns immediately without invoking the callback. Entering a new segment always triggers a read of the segment's first page to validate the `XLogLongPageHeaderData`, even if the target record is not on that first page. This catches mismatched `system_identifier`, `ws_segsize`, or `XLOG_BLCKSZ` fields before the reader trusts any record data (xlogreader.c, `ReadPageInternal()`).

## Two-stage lifecycle: decode buffer and decode queue

The reader maintains two related but distinct memory regions. The raw page cache (`readBuf`, one `XLOG_BLCKSZ`-sized block) holds the most recently fetched WAL page. The decode buffer is a larger circular arena, defaulting to 64 KB (`DEFAULT_DECODE_BUFFER_SIZE`), where fully assembled and decoded records live as `DecodedXLogRecord` structs.

Decoded records form a linked list called the decode queue. The head of the queue is the oldest unconsumed record; the tail is the most recently decoded one. When a caller calls `XLogNextRecord()`, it receives the head record. The reader then advances. `XLogReleasePreviousRecord()` then recycles that record's space in the circular buffer by advancing `decode_buffer_head` to the next in-buffer record. This means the reader consumes records in strict FIFO order. That matches the invariant that recovery always replays WAL forward.

The reader allocates records that are too large to fit in the circular buffer individually with `palloc()` and flags them `oversized`. They participate in the decode queue like normal records, but the reader frees them with `pfree()` on release instead of advancing the circular buffer pointer (xlogreader.c, `XLogReadRecordAlloc()`). The circular buffer logic handles two layout configurations — tail to the right of head, or wrapped — via explicit size comparisons rather than modular arithmetic, which avoids off-by-one hazards at the segment boundaries.

## Nonblocking readahead

The public function `XLogReadAhead()` attempts to decode the next record without blocking. It passes `nonblocking = true` down to `XLogDecodeNextRecord()`, which propagates it to the page_read callback via `state->nonblocking`. If the callback cannot supply the required data immediately, it returns `XLREAD_WOULDBLOCK`. The reader then propagates that status up without modifying any queue state. The caller can then consume previously decoded records from the queue while WAL becomes available in the background.

If the decode buffer is full and the caller is only reading ahead, `XLogReadRecordAlloc()` returns NULL, and the reader again returns `XLREAD_WOULDBLOCK`. This signals that the consumer must drain the queue to make room. Only then can the reader decode more records. This coupling between the producer (`XLogReadAhead`) and consumer (`XLogNextRecord`) is the primary back-pressure mechanism for the WAL prefetcher.

## Multi-page record assembly

WAL records can cross page boundaries. The reader detects this when `total_len` from the record header exceeds the bytes remaining on the current page. It then enters a reassembly loop: it copies the first fragment into `readRecordBuf`, reads successive pages, and appends continuation data. Before appending each page, it checks that the page header carries `XLP_FIRST_IS_CONTRECORD` and that `xlp_rem_len` is consistent with the expected remaining length. A mismatch in either check fails the read immediately (xlogreader.c, `XLogDecodeNextRecord()`).

The reader intentionally defers CRC validation until the record is fully assembled. Before the record is fully read, the reader trusts only `xl_tot_len` from the header. It validates the rest of the header only once all bytes are in hand, to prevent acting on garbage data from a recycled WAL page that happens to have a plausible-looking `xl_tot_len`.

The `XLP_FIRST_IS_OVERWRITE_CONTRECORD` flag handles partial records at the end of a WAL stream. When a primary crashes mid-record and a new primary later writes over the continuation pages, the reader detects the flag, records `abortedRecPtr` and `missingContrecPtr` on the state, and restarts reading from the overwrite point. Recovery uses these fields to skip the broken record and signal downstream WAL consumers appropriately.

## Validation layers

The reader applies validation at three levels before trusting record content.

First, `XLogReaderValidatePageHeader()` checks each page header: the magic number, legal flag bits, the `xlp_pageaddr` field agreeing with the expected LSN, and the timeline ID never going backward across successive pages. The reader performs the timeline monotonicity check only for pages at an LSN greater than `state->latestPagePtr`. This avoids false failures when re-reading a page already validated in a previous scan.

Second, `ValidXLogRecordHeader()` validates the record header: `xl_tot_len` is at least `SizeOfXLogRecord`, `xl_rmid` is a known resource manager ID, and the `xl_prev` back-link matches the previous record's position. In sequential reads, the reader checks the back-link exactly. In random-access reads (e.g., `XLogFindNextRecord()`), it enforces only the weaker constraint that `xl_prev < RecPtr`, because it does not know the exact previous LSN.

Third, CRC verification via `ValidXLogRecord()` covers both the record body and the header fields up to `xl_crc`. The reader does not use any byte of the payload before it confirms the CRC.

## Decoding into DecodedXLogRecord

Once a raw record passes validation, `DecodeXLogRecord()` parses the packed binary format into a `DecodedXLogRecord`. `DecodeXLogRecord()` lays out the decoded struct contiguously in memory: the fixed header members come first, then a variable-length `blocks[]` array (one `DecodedBkpBlock` per block reference, up to `XLR_MAX_BLOCK_ID + 1`), followed immediately by the raw block images, per-block data, and main data, all copied out of the raw record buffer. Pointer members such as `blk->data` and `decoded->main_data` point into this same contiguous allocation, making the entire decoded record self-contained and safe to hand off to consumers after the reader reuses the raw page buffer.

`DecodeXLogRecord()` decodes block references in order of their `block_id` value. `BKPBLOCK_SAME_REL` compresses records that modify several blocks of the same relation by omitting the `RelFileLocator` from the second and subsequent block headers; the decoder propagates the most recently seen locator to fill these in. The decoder leaves compressed full-page images (`bimg_info & BKPIMAGE_COMPRESS_*`) in compressed form in the decoded record; `RestoreBlockImage()` decompresses them on demand into a caller-supplied buffer, supporting pglz, LZ4, and zstd.

## Key data structures

| Type | Purpose |
|---|---|
| `XLogReaderState` | All reader state: buffers, queue pointers, cursor LSNs, error message |
| `XLogReaderRoutine` | Caller-supplied callbacks: `page_read`, `segment_open`, `segment_close` |
| `DecodedXLogRecord` | Fully decoded record: header copy, block array, pointers to data sections |
| `DecodedBkpBlock` | Per-block reference: relation/fork/block identity, image metadata, data pointer |
| `WALOpenSegment` | Currently open segment file descriptor, segment number, timeline |
| `WALSegmentContext` | Segment size and `pg_wal` directory path |
| `WALReadError` | Structured error from `WALRead()`: errno, offset, bytes requested/read |

## Dual-use compilation

`xlogreader.c` is compiled for both the backend and frontend (tools like `pg_waldump`). The file avoids `ereport`, server-defined static variables, and other backend-only constructs. `#ifndef FRONTEND` guards backend-only code such as `XLogRecGetFullXid()`. This means `pg_waldump` and recovery exercise the same validation and decoding logic. Bugs in decoding affect both contexts equally, which narrows the testing surface.

## Related Topics

- [[subsystems/wal/overview|WAL Overview]] — WAL segments, LSNs, checkpoints, and the write path
- [[subsystems/wal/wal-records|WAL Record Format and Resource Managers]] — on-disk record layout, block headers, and FPI flags that the reader parses

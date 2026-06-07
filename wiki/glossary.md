---
title: "Glossary"
aliases:
  - "Glossary"
---

# Glossary

Terms and short explanations used across the wiki. Each entry is intentionally brief; follow the links for depth.

---

**AM (Access Method)** — A pluggable storage or index implementation behind a standard interface. Table AMs handle heap reads/writes; index AMs (btree, hash, GiST, GIN, BRIN, SP-GiST) handle index traversal.

**AllocSet** — the default memory context allocator; uses size-class freelists (8–8192 bytes) and block-based allocation.

**[[subsystems/background/autovacuum|autovacuum]]** — Background worker that automatically runs VACUUM and ANALYZE on tables that have accumulated enough dead tuples or stale statistics.

**[[subsystems/background/bgwriter|bgwriter]]** — Auxiliary process that proactively flushes dirty shared buffers to disk, reducing I/O stalls for foreground backends. See [[subsystems/storage/buffer-manager]].

**BLCKSZ** — The compile-time page size, default 8192 bytes. All heap pages, index pages, and WAL pages are exactly BLCKSZ bytes.

**BRIN (Block Range INdex)** — a tiny index that stores one summary (e.g. min/max) per range of heap pages rather than indexing individual tuples. Efficient for naturally-ordered data. See [[subsystems/indexes/brin]].

**buffer** — A shared-memory frame holding one page of data. Backends pin buffers before reading or modifying pages. See [[subsystems/storage/buffer-manager]].

**checkpointer** — Auxiliary process that periodically writes a checkpoint WAL record. It also triggers a flush of dirty buffers. See [[subsystems/wal/overview]].

**checkpoint** — A WAL record marking a point from which crash recovery can begin. All dirty pages at checkpoint time are guaranteed to be on disk before the next checkpoint completes. See [[subsystems/wal/overview]].

**[[subsystems/storage/clog|clog]] (commit log)** — A bitmap stored in `pg_xact/` recording whether each XID committed, aborted, or is still in progress. Used by MVCC visibility checks. See [[subsystems/transactions/mvcc]].

**CTE (Common Table Expression)** — A `WITH` clause. The planner may inline a CTE into the main query or materialise it as a separate subplan, depending on version and usage.

**ctid** — The physical location of a tuple on disk, expressed as `(block_number, offset_number)`. Index entries store ctids to point back to heap tuples.

**deduplication** — a B-tree optimisation that merges multiple index entries with the same key value into a single *posting list*, reducing index size. See [[subsystems/indexes/btree]].

**DSA (Dynamic Shared Area)** — a shared memory region managed with a heap allocator. Parallel workers use it to share variable-sized data structures with each other, without needing fixed-size pre-allocation. See [[subsystems/memory/dsa]].

**DSM (Dynamic Shared Memory)** — a shared memory segment created at runtime for inter-process communication. Parallel query workers use it for this. See [[subsystems/executor/parallel]].

**EState** — The per-query executor state struct shared by all plan nodes in a query execution. See [[subsystems/executor/overview]].

**ExprState** — The compiled form of an expression tree, as a flat array of `ExprEvalStep` instructions evaluated by the expression interpreter or JIT. See [[subsystems/executor/expression-eval]].

**fast-path locking** — a per-`PGPROC` cache of up to 16 weak relation locks, avoiding the main lock table for common cases. See [[subsystems/locking/overview]].

**FDW (Foreign Data Wrapper)** — An extension implementing the foreign table AM interface, allowing PostgreSQL to scan external data sources (other databases, files, APIs) as if they were tables.

**[[subsystems/storage/fillfactor|fillfactor]]** — A storage parameter (10–100, default 100) controlling how full INSERT fills each heap or index page, leaving room for in-place HOT updates. See [[subsystems/storage/heap]].

**fork** — A named file associated with a relation. The main fork holds table data; the FSM fork tracks free space; the [[subsystems/storage/visibility-map|visibility map]] fork tracks all-visible and all-frozen pages.

**FSM ([[subsystems/storage/fsm|Free Space Map]])** — A per-relation file tracking approximately how much free space each heap page has. Used by `RelationGetBufferForTuple()` to find pages with room for new tuples.

**frozen tuple** — A tuple whose `t_xmin` has been replaced with `FrozenTransactionId`, making it visible to all snapshots and safe from [[subsystems/transactions/xid-wraparound|XID wraparound]]. VACUUM FREEZE performs this replacement.

**full-page write (FPW)** — writing an entire 8 KB page into WAL on the first modification after a checkpoint, protecting against torn pages during crash recovery. See [[subsystems/wal/checkpoint]].

**GEQO (Genetic Query Optimizer)** — a genetic algorithm that replaces exhaustive dynamic-programming join enumeration when the query has more than `geqo_threshold` (default 12) relations. See [[subsystems/planner/overview]].

**GIN (Generalized Inverted Index)** — an index type mapping each extracted *key* (lexeme, array element, jsonb key) to the set of heap TIDs containing it. See [[subsystems/indexes/gin]].

**GiST (Generalized Search Tree)** — an index framework that delegates key comparison, union, and penalty to operator-class callbacks; used by PostGIS, range types, full-text search, and others. See [[subsystems/indexes/gist]].

**high-key** — the first item on a B-tree non-rightmost page, storing the upper bound for keys on that page; essential for the Lehman & Yao concurrent split protocol. See [[subsystems/indexes/btree]].

**[[subsystems/transactions/hint-bits|hint bit]]** — A flag in a tuple's `t_infomask` caching whether the inserting or deleting transaction is known committed or aborted, to avoid repeated CLOG lookups. See [[subsystems/transactions/mvcc]].

**HOT (Heap Only Tuple)** — An update optimization where a new tuple version has no index entry. Instead, the new version is only reachable through the old version's `t_ctid` chain. This works only when both versions are on the same page and no indexed column changed. See [[subsystems/storage/heap]].

**index AM** — See *AM*.

**JIT** — Just-in-time compilation of expression evaluation and tuple deformation using LLVM, enabled above a cost threshold. See [[subsystems/executor/expression-eval]].

**latch** — a lightweight, process-local event flag. A backend can sleep on its own latch. Another process can set that latch to wake it. Latches are the primary IPC mechanism for signalling between PostgreSQL processes, without polling. See [[subsystems/storage/latch-and-ipc]].

**LOCKTAG** — a 16-byte struct identifying the object being locked (relation, tuple, transaction ID, advisory key, etc.). The key in the lock manager's hash table. See [[subsystems/locking/overview]].

**LSN (Log Sequence Number)** — A byte offset into the WAL stream (`XLogRecPtr`). Used to track how far along the WAL a page, replica, or recovery point is.

**[[subsystems/locking/lwlocks|LWLock]] (Lightweight Lock)** — A shared-memory lock supporting shared and exclusive modes, used to protect in-memory data structures. Cheaper than a heavyweight lock; no deadlock detection.

**memory context** — a named subtree of allocations that can be freed in bulk with a single `MemoryContextReset()` or `MemoryContextDelete()` call. See [[subsystems/memory/contexts]].

**MultiXactId** — an XID-space identifier representing a set of transactions that all hold locks on the same tuple; stored in `t_xmax` when more than one transaction locks a row. See [[subsystems/locking/overview]].

**MVCC (Multiversion Concurrency Control)** — The mechanism by which readers and writers do not block each other. Each transaction sees a snapshot of committed data; multiple versions of the same row coexist on disk. See [[subsystems/transactions/mvcc]].

**palloc** — PostgreSQL's `malloc` wrapper; allocates from the current `MemoryContext`. See [[subsystems/memory/contexts]].

**parallel safety** — a function property (`SAFE`, `RESTRICTED`, `UNSAFE`) controlling whether the planner may execute the function inside a parallel worker. See [[subsystems/executor/parallel]].

**partial path** — a query execution path designed to be run inside a parallel worker, producing a subset of the total rows; wrapped by a Gather or GatherMerge node. See [[subsystems/executor/parallel]].

**path** — An optimizer data structure representing one candidate strategy for accessing a relation, with a cost estimate. The planner picks the cheapest path and converts it to a plan node. See [[subsystems/planner/overview]].

**pending list** — a GIN structure that batches new index entries on overflow pages before merging them into the main entry tree during vacuum or cleanup. See [[subsystems/indexes/gin]].

**PGPROC** — The per-backend shared-memory record tracking the backend's XID, snapshot xmin, lock state, and semaphore. See [[architecture/overview]].

**pin** — Incrementing a buffer's reference count to prevent it from being evicted. A backend must pin a buffer before it reads or writes the buffer's contents. See [[subsystems/storage/buffer-manager]].

**PlannerInfo** — The per-query-level optimizer state, holding the parsed query, relation arrays, join lists, and upper relation paths. See [[subsystems/planner/overview]].

**PlanState** — The runtime state node for one plan node, mirroring the plan tree. Holds open scan descriptors, tuple slots, and expression state. See [[subsystems/executor/overview]].

**posting list / posting tree** — GIN structures storing the set of matching heap TIDs for one key; a posting list is an inline sorted array; a posting tree is a separate B-tree used when the TID set is large. See [[subsystems/indexes/gin]].

**portal** — The execution wrapper for one statement instance. Holds a `PlannedStmt`, a memory context, and a cursor position. Even non-cursor queries go through a portal. See [[code-paths/simple-select]].

**postmaster** — The supervisor process that listens for connections. It forks a backend process for each connection. See [[architecture/overview]].

**prepared statement** — A parsed and analysed (but not yet planned) query stored server-side. The extended query protocol creates these via the Parse message. See [[code-paths/extended-query]].

**publication / subscription** — The two sides of logical replication. A publication defines which table changes to stream. A subscription connects to a publication on another server. It then applies the changes locally. See [[subsystems/replication/logical]] and [[subsystems/replication/subscriptions]].

**RelOptInfo** — The optimizer's representation of a relation (base table, join, or subquery) during path generation, including candidate paths and row-count estimates. See [[subsystems/planner/overview]].

**replication slot** — A server-side bookmark that tracks how far a replica or logical consumer has consumed the WAL, preventing the server from removing WAL or dead rows still needed by the consumer. See [[subsystems/replication/slots]].

**[[subsystems/memory/resource-owner|ResourceOwner]]** — A scoped container that tracks resources (buffer pins, lock acquisitions, file handles). It releases them automatically on error or scope exit. See [[subsystems/memory/contexts]].

**RLS (Row-Level Security)** — A policy mechanism that appends `WHERE`-like predicates to queries transparently, restricting which rows each role can read or write. See [[subsystems/row-level-security]].

**SLRU (Simple LRU)** — A lightweight page cache used for commit log, subtransaction log, and multixact data. Pages are evicted in LRU order when the fixed-size cache is full.

**snapshot** — A consistent view of which transactions had committed at a point in time. Taken at statement or transaction start depending on isolation level. See [[subsystems/transactions/mvcc]].

**SP-GiST (Space-Partitioned GiST)** — an index framework for non-balanced, space-partitioning data structures (radix trees, quadtrees, k-d trees); useful for point data and IP ranges. See [[subsystems/indexes/spgist]].

**spinlock** — A CPU-level busy-wait lock used for very short critical sections (updating a few fields). No sleep, no queue, no deadlock detection.

**SSI (Serializable Snapshot Isolation)** — The implementation of the `SERIALIZABLE` isolation level. It uses predicate locks to detect serialization anomalies (read-write conflicts). It then aborts one of the conflicting transactions.

**subtransaction** — A named savepoint within a transaction, implemented as a child transaction with its own XID. Aborting a subtransaction rolls back only its changes; the parent transaction continues. See [[subsystems/transactions/subtransactions]].

**tablespace** — A named directory path where PostgreSQL stores relation files, allowing data to be spread across multiple filesystems or storage tiers. See [[subsystems/storage/tablespaces]].

**TID (Tuple Identifier)** — A `(block_number, offset_number)` pair identifying a tuple's physical location on disk. Stored as `ItemPointerData`; synonymous with *ctid* in user-facing contexts.

**TOAST** — The Oversized-Attribute Storage Technique; compresses or stores large column values out-of-line in a `pg_toast_NNNN` table. See [[subsystems/storage/toast]].

**TupleTableSlot** — An abstraction over the tuple data exchanged between executor nodes. Avoids constructing heap tuples at every node boundary. See [[subsystems/executor/tuple-table-slot]].

**two-phase commit (2PC)** — A protocol for atomic commits across multiple databases or sessions. A `PREPARE TRANSACTION` durably records intent. A later `COMMIT PREPARED` or `ROLLBACK PREPARED` finalises it. See [[subsystems/transactions/two-phase-commit]].

**upper rel** — an optimizer concept representing the result relation after aggregation, sorting, or limit is applied; distinct from the scan/join `RelOptInfo`. See [[subsystems/planner/overview]].

**VACUUM** — The process that reclaims storage from dead tuples (those no longer visible to any snapshot). It updates the FSM and visibility map. It can also freeze old XIDs.

**varlena** — the variable-length datum type used by `text`, `bytea`, `jsonb`, and others; the leading bytes encode length and storage type (compressed, external TOAST pointer, etc.). See [[subsystems/storage/toast]].

**visibility map** — A per-relation bitmap (one bit per heap page) recording which pages are *all-visible* (every tuple visible to all active transactions) and *all-frozen*. Used by index-only scans and VACUUM.

**Volcano model** — The pull-based execution model used by the executor. Each plan node exposes a "give me the next tuple" interface. It pulls from its children on demand. See [[subsystems/executor/overview]].

**WAL (Write-Ahead Log)** — The durability log. Changes are recorded in WAL before being applied to data pages, ensuring crash recovery can replay them. See [[subsystems/wal/overview]].

**WAL summarizer** — A background process, introduced in PostgreSQL 17, that generates WAL summary files. Each summary file records which blocks were modified in each LSN range. This enables incremental base backups. See [[subsystems/wal/wal-summarizer]].

**[[subsystems/executor/work-mem-and-spill|work_mem]]** — The memory budget for one sort or hash operation. When an operation's working set exceeds this limit, it spills to temporary files on disk.

**XID (Transaction ID)** — A 32-bit unsigned integer assigned to each transaction that performs writes. Used in tuple headers (`t_xmin`, `t_xmax`) and the commit log for MVCC visibility.

**xmax** — In a snapshot: the XID that will be assigned next (upper bound). In a tuple header: the XID of the transaction that deleted or locked this tuple.

**xmin** — In a snapshot: the oldest XID still active (lower bound of the uncertain range). In a tuple header: the XID of the transaction that inserted this tuple.

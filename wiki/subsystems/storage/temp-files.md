---
title: Temporary Files and work_mem
aliases:
  - temp files
  - work_mem spill
  - pgsql_tmp
source_files:
  - src/backend/storage/file/buffile.c
  - src/backend/storage/file/fd.c
  - src/include/storage/buffile.h
  - src/include/storage/fd.h
symbols:
  - BufFile
  - BufFileCreateTemp
  - BufFileRead
  - BufFileWrite
  - BufFileSeek
  - BufFileAppend
  - OpenTemporaryFile
  - OpenTemporaryFileInTablespace
  - ReportTemporaryFileUsage
  - RegisterTemporaryFile
  - Vfd
  - VfdCache
  - FD_DELETE_AT_CLOSE
  - FD_CLOSE_AT_EOXACT
  - FD_TEMP_FILE_LIMIT
  - temporary_files_size
  - max_files_per_process
---

# Temporary Files and [[subsystems/executor/work-mem-and-spill|work_mem]]

PostgreSQL executes sorts, hash joins, and hash aggregations in memory whenever the working set fits within a configurable budget. When it does not fit, the operation spills to disk. That transition — from memory to temporary file — is one of the sharpest performance cliffs in the system: a query that barely exceeds its budget can become 10–100× slower than one that stays in memory.

## The work_mem budget

`work_mem` is the maximum amount of memory a single sort or hash operation may consume before spilling. The critical subtlety is that it applies *per operation*, not per query. A query with three sort nodes and two hash aggregates can use up to five times `work_mem` simultaneously if all five operations run in parallel. The [[subsystems/executor/overview]] does not reserve the full budget upfront. Each node claims memory as it accumulates tuples. It spills when it reaches its private limit.

The sort and hash code enforce the budget, not a central allocator. When `tuplesort` or `exechashjoin` determines that its in-memory state has grown beyond `work_mem`, it initiates a spill. It writes sorted runs or hash batches to temporary files, then merges or re-probes from those files.

## Where temporary files live

Temporary files go into a directory named `pgsql_tmp/` (`PG_TEMP_FILES_DIR` in `fd.h`). By default this sits under `base/pgsql_tmp/` inside the data directory, which is the database's default tablespace. The `temp_tablespaces` GUC overrides this: PostgreSQL cycles through the listed tablespaces in round-robin order, placing successive segment files in different locations. This lets a DBA route spill I/O to faster storage or spread it across multiple disks.

File names follow the pattern `pgsql_tmp<pid>.<counter>` (`PG_TEMP_FILE_PREFIX` + process PID + a per-process counter). The PID component means files from different backends never collide. The prefix makes orphan cleanup straightforward at startup.

## The BufFile abstraction

Direct use of OS files for spilling would expose two problems: OS file size limits (on many platforms, a 32-bit `off_t` caps files at 2 GB) and overhead from repeatedly opening and closing file descriptors. `BufFile` (`buffile.c`) solves both.

A `BufFile` appears to its callers as a single seekable stream, but internally it spans multiple physical segment files, each capped at 1 GB (`MAX_PHYSICAL_FILESIZE = 0x40000000`). When a write would push the current segment past that limit, `extendBufFile()` opens a new physical file. The logical position then seamlessly crosses the boundary. `BufFileSeek` and `BufFileTell` expose a two-part position — a segment index and an offset within that segment. This lets callers record and revisit positions across segment boundaries.

```c
struct BufFile {
    int       numFiles;      /* number of physical segments */
    File     *files;         /* VFD handles, one per segment */
    bool      isInterXact;   /* survive transaction end? */
    bool      dirty;         /* buffer needs flushing? */
    bool      readOnly;
    FileSet  *fileset;       /* non-NULL for shared/FileSet-based files */
    int       curFile;       /* current segment index */
    off_t     curOffset;     /* byte offset within current segment */
    int       pos;           /* position within in-memory buffer */
    int       nbytes;        /* valid bytes in buffer */
    PGAlignedBlock buffer;   /* 8 kB I/O buffer */
};
```

The 8 kB buffer amortises the cost of virtual-file operations. Reducing how often the code touches a virtual file matters, because each access may need to reopen a real OS file descriptor — see the VFD discussion below.

`BufFile` is used by:

- `tuplesort` — writes sorted runs during the pass-0 phase; re-reads them during merge passes.
- Hash join — writes inner-relation batches that do not fit in the hash table.
- The logical replication reorder buffer — writes uncommitted changes that exceed `logical_decoding_work_mem`.

For parallel query, `BufFileCreateFileSet` creates a file anchored in a `SharedFileSet` so multiple workers can access the same spill data. For ordinary use, `BufFileCreateTemp(false)` creates an anonymous file owned by the current [[subsystems/memory/resource-owner|resource owner]].

## Virtual file descriptors

Operating systems impose per-process limits on open file descriptors, typically a few thousand. PostgreSQL often needs many more logical files open simultaneously — relation forks, index files, temporary spill files, and files opened by library code. `fd.c` manages this through a virtual file descriptor (VFD) layer.

Each VFD is an entry in `VfdCache`, an array that grows as needed. The `File` type is simply an index into this array, not a real OS descriptor. The VFD records the file name, open flags, and current size, and carries a real OS `fd` only when the file is actually open. When the pool of real descriptors runs low, `ReleaseLruFiles()` closes the least-recently-used real files. This keeps the VFD alive, so the file can be transparently reopened on next access.

```c
typedef struct vfd {
    int            fd;              /* real OS descriptor, or VFD_CLOSED */
    unsigned short fdstate;         /* FD_DELETE_AT_CLOSE | FD_CLOSE_AT_EOXACT | FD_TEMP_FILE_LIMIT */
    ResourceOwner  resowner;
    File           nextFree;
    File           lruMoreRecently;
    File           lruLessRecently;
    off_t          fileSize;        /* non-zero for temporary files */
    char          *fileName;
    int            fileFlags;
    mode_t         fileMode;
} Vfd;
```

The relevant `fdstate` bits for temporary files:

| Flag | Meaning |
|------|---------|
| `FD_DELETE_AT_CLOSE` | Unlink the file when the VFD is closed |
| `FD_CLOSE_AT_EOXACT` | Close (and therefore delete) at end of transaction |
| `FD_TEMP_FILE_LIMIT` | Count this file's bytes toward `temp_file_limit` |

`max_files_per_process` (default 1000) limits how many VFDs can hold real OS descriptors. The postmaster probes the OS `ulimit` at startup and sets `max_safe_fds` accordingly. PostgreSQL keeps a reserve of `NUM_RESERVED_FDS` (10) for code that opens files without going through `fd.c`. `FD_MINFREE` (48) is the absolute minimum below which PostgreSQL refuses to start.

## Temporary file lifecycle

When `OpenTemporaryFile(interXact=false)` creates a file, it sets both `FD_DELETE_AT_CLOSE` and `FD_CLOSE_AT_EOXACT` and registers the VFD with `CurrentResourceOwner`. This means the file is deleted:

- explicitly, when the operation calls `BufFileClose()`;
- automatically, when PostgreSQL releases the resource owner at transaction end;
- automatically, when the transaction aborts due to an error.

Error paths need no explicit cleanup. The resource owner mechanism guarantees that a cancelled query or rolled-back transaction leaves no orphan files behind. Files opened with `interXact=true` (such as those used by the reorder buffer across subtransaction boundaries) survive until explicitly closed. They are not registered with a resource owner.

At process exit, `CleanupTempFiles()` sweeps the VFD cache for any files still marked `FD_DELETE_AT_CLOSE`. On server startup, `RemovePgTempFiles()` removes leftover `pgsql_tmp*` files from previous runs, since a crash could have prevented normal cleanup.

## Enforcing temp_file_limit

`temp_file_limit` caps the total temporary file space a single session may consume, in kilobytes. The check happens inside `FileWrite()` in `fd.c`, just before every write. The global counter `temporary_files_size` tracks the aggregate size of all open temp files marked `FD_TEMP_FILE_LIMIT`. When a write would push the session past the cap:

```c
if (newTotal > (uint64) temp_file_limit * (uint64) 1024)
    ereport(ERROR,
            (errcode(ERRCODE_CONFIGURATION_LIMIT_EXCEEDED),
             errmsg("temporary file size exceeds temp_file_limit (%dkB)",
                    temp_file_limit)));
```

PostgreSQL cancels the query immediately. This is a hard limit: there is no grace period. It applies to the session as a whole, so a single complex query with many spilling operations accumulates against one counter.

## The performance cliff

CPU and memory bandwidth bound in-memory sort and hash operations. Once an operation spills, the executor writes every tuple to disk during the build phase and reads it back during the probe or merge phase. For a hash join that spills to many batches, the executor may read the inner relation multiple times. For a sort with many merge passes, the data traverses disk repeatedly.

The spill boundary is not gradual. An operation that fits in `work_mem` by a narrow margin completes entirely in memory. One that exceeds `work_mem` by a single tuple triggers a full disk-based algorithm. This means that tuning `work_mem` effectively requires understanding not just average query memory usage but worst-case peaks. It also means that a small increase in `work_mem` can produce a disproportionate speedup for queries sitting just above the threshold.

Hash join has an additional sensitivity. If the hash table fits in memory but only barely, it may suffer cache thrashing. This degrades performance even before a full spill occurs.

## Monitoring

Several tools expose temporary file activity:

**`log_temp_files`** (GUC, in kilobytes): when a temporary file closes, `ReportTemporaryFileUsage()` logs its path and size. It does this only if the size meets or exceeds this threshold. Setting it to `0` logs every temporary file. This is the easiest way to discover which queries are spilling.

**[[subsystems/observability/pg-stat-statements|pg_stat_statements]]**: the `temp_blks_written` and `temp_blks_read` columns accumulate blocks written to and read from temporary files across all executions of a normalized query. High values here indicate chronic spilling.

**`pg_stat_activity`**: does not directly expose temp file usage, but combined with `log_temp_files` output it identifies the connection responsible for large spills.

**`EXPLAIN (ANALYZE, BUFFERS)`**: reports `Temp Read Blocks` and `Temp Written Blocks` for individual plan nodes, pinpointing exactly which sort or hash operation caused the spill.

## Related Topics

- [[subsystems/executor/work-mem-and-spill|work_mem and Spill]] — the memory budget that governs when a sort or hash operation spills to a temporary file
- [[subsystems/executor/hash-join-spill|Hash Join Spill]] — how hash joins partition inner batches into temporary files when the hash table exceeds work_mem
- [[subsystems/executor/sort|Sort]] — the tuplesort implementation that writes sorted runs to BufFile segments during external merge sort
- [[subsystems/storage/fileset|FileSet]] — the shared file set mechanism that lets parallel workers access the same spill files across processes
- [[subsystems/storage/shared-fileset|Shared FileSet]] — how shared BufFiles are coordinated between a leader and parallel workers via a named FileSet
- [[subsystems/memory/resource-owner|Resource Owner]] — the mechanism that tracks VFD ownership and guarantees temporary file cleanup on transaction end or error
- [[troubleshooting/slow-queries|Slow Queries]] — diagnosing queries that hit the spill cliff and strategies for reclaiming in-memory execution
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the shared buffer pool for relation data; temp files bypass it entirely
- [[subsystems/executor/overview|Executor Overview]] — how executor nodes request memory and decide when to spill
- [[subsystems/storage/heap|Heap Storage]] — relation storage that temp-file data is ultimately derived from

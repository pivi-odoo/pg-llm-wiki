---
title: "FileSet: Named Namespaces for Shared Temporary Files"
aliases:
  - fileset
  - FileSet
  - shared temp files
  - cross-backend temp files
tags:
  - theme/parallelism
source_files:
  - src/backend/storage/file/fileset.c
  - src/include/storage/fileset.h
symbols:
  - FileSet
  - FileSetInit
  - FileSetCreate
  - FileSetOpen
  - FileSetDelete
  - FileSetDeleteAll
  - FileSetPath
  - FilePath
  - ChooseTablespace
---

A `FileSet` is a named namespace for [[subsystems/storage/temp-files|temporary files]] that multiple backends can independently access by name. Ordinary process-local temporary files vanish when their owning process closes them. A `FileSet` persists across backend boundaries and survives for as long as its owner keeps it alive. This makes it the mechanism that allows parallel workers to hand intermediate results back to the leader process.

## Named files vs. process-local files

Ordinary temporary files in PostgreSQL are anonymous: `fd.c` assigns them internal file descriptors. They disappear when the process exits or explicitly closes them. Nothing outside the creating process can find them by name.

A `FileSet` adds a stable address space on top of the filesystem. Each fileset is identified by the PID of the creating process and a monotonically incrementing sequence number. Together these form a unique directory name under the temp tablespace (`pgsql_tmp<pid>.<number>.fileset`). Any backend that holds a pointer to the `FileSet` struct can open files within it by supplying the same logical name used at creation. The struct is small enough to embed in shared memory or pass through a parallel-query control structure.

This design avoids any central registry. The directory name encodes enough identity to be deterministic — given the same `FileSet` value, every backend computes the same path. There is no shared hash table or lock required just to locate a file.

## Lifecycle and ownership

`FileSetInit` claims a fileset by recording the calling process's PID, assigning the next sequence number, and resolving the `temp_tablespaces` GUC into an array of up to eight tablespace OIDs stored inline in the struct (`fileset.c`). If a tablespace OID resolves to `InvalidOid`, it falls back to `MyDatabaseTableSpace`.

`FileSetCreate` materialises a file inside the namespace. The first call for a given fileset creates the directory lazily. The code first tries `PathNameCreateTemporaryFile`. If that fails, it creates the directory via `PathNameCreateTemporaryDir` and retries. This avoids the overhead of creating the directory when the fileset is allocated but never written to.

`FileSetOpen` reopens an existing file by name. Worker processes use this path to read files written by other workers or by the leader.

`FileSetDelete` removes a single named file, returning a bool that indicates whether the file existed. `FileSetDeleteAll` removes every file in the fileset by iterating over all configured tablespaces and deleting the fileset directory from each.

The creating backend is responsible for calling `FileSetDeleteAll` before discarding the `FileSet` struct. Worker processes never call `FileSetDeleteAll`; they may call `FileSetDelete` to remove individual files they own, but cleanup of the namespace is the leader's job.

## Tablespace distribution

When multiple [[subsystems/storage/tablespaces|tablespaces]] are configured via `temp_tablespaces`, a `FileSet` spreads its files across them. The static helper `ChooseTablespace` hashes the logical file name with `hash_any` and takes the result modulo `ntablespaces`. This distributes I/O load across storage devices without requiring any coordination between backends — each backend independently hashes the name and arrives at the same tablespace.

The inline tablespace array is capped at eight entries, matching the assumption in `fileset.h` that a large number of temp tablespaces is unusual. Exceeding eight silently truncates the list.

## Use in parallel query

The primary consumers of the `FileSet` API are parallel sort and parallel hash join. When a query sorts more data than fits in [[subsystems/executor/work-mem-and-spill|work_mem]], the executor spills intermediate runs to disk. In a parallel sort, each worker writes its own sorted runs into a shared `FileSet`; the leader then opens each run by its logical name, performs a merge read, and produces the final sorted output. Parallel hash join uses the same mechanism when the hash table overflows: workers partition the input into named files. The leader reads them back for the probe phase.

Because the parallel query passes the `FileSet` struct through its shared-memory control block, workers never need to know the absolute filesystem path — they receive the struct and derive paths deterministically. This means a query that spills to disk during a parallel operation creates a coordinated set of temporary files rather than an unrelated scatter of anonymous files. This makes cleanup reliable even when workers exit abnormally.

## See also

- [[subsystems/storage/temp-files|Temporary files]]
- [[subsystems/storage/tablespaces|Tablespaces]]
- [[subsystems/executor/work-mem-and-spill|work_mem and spill to disk]]
- [[subsystems/executor/parallel|Parallel query execution]]

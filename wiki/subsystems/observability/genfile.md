---
title: "Server-Side File Access Functions"
aliases:
  - genfile
  - pg_read_file
  - pg_ls_dir
  - pg_stat_file
source_files:
  - src/backend/utils/adt/genfile.c
symbols:
  - convert_and_check_filename
  - read_binary_file
  - read_text_file
  - pg_read_file_common
  - pg_read_binary_file_common
  - pg_stat_file
  - pg_ls_dir
  - pg_ls_dir_files
  - pg_ls_logdir
  - pg_ls_waldir
  - pg_ls_tmpdir
  - pg_ls_replslotdir
---

The server-side file access functions — `pg_read_file`, `pg_read_binary_file`, `pg_stat_file`, and `pg_ls_dir` — give privileged database users direct access to files on the server filesystem from within SQL. They are the primary mechanism for inspecting PostgreSQL log files, WAL segments, configuration files, and other on-disk state without dropping out of the database client.

## Access Control Model

The `pg_read_server_files` predefined role gates access, not traditional superuser status. The central check lives in `convert_and_check_filename`, which every public entry point calls before touching the filesystem.

Members of `pg_read_server_files` may open any file the PostgreSQL backend process can read — there is no path restriction beyond what the OS enforces. Users who lack that role privilege face a tighter constraint. Relative paths must stay within or below the data directory. PostgreSQL permits absolute paths only when they fall inside `DataDir` or `Log_directory`. This two-tier design lets administrators delegate log inspection (`pg_read_server_files`) without granting full filesystem access. It still allows less-privileged monitoring roles to read the files they are expected to reach.

See [[subsystems/roles-privileges|roles and privileges]] for how predefined roles are structured in general.

## Path Canonicalisation and Safety

Before the privilege check runs, the filename undergoes `canonicalize_path`, which resolves `.`, `..`, and double slashes. This happens in-place and can shorten the string. As a result, the rest of the call stack uses the resulting pointer. The canonicalisation step means that PostgreSQL normalises path-traversal strings, like `$PGDATA/global/../../etc/passwd`, to their real form before the prefix comparisons take place. The security property holds because `path_is_prefix_of_path` operates on the canonical form.

## Reading Files

`read_binary_file` is the workhorse for both the text and binary variants. It uses `AllocateFile` (the VFD-aware wrapper around `fopen`). This way, PostgreSQL's virtual file descriptor management tracks the open handle, and closes it if the backend runs out of file descriptors.

When callers provide a byte count, the function allocates a `bytea` of exactly that size and reads into it with a single `fread`. When reading to end-of-file (signalled by a negative byte count internally), it uses a `StringInfo` as a resizable buffer, growing in `MIN_READ_SIZE` (4096-byte) increments. The `StringInfo` doubling strategy means fewer syscalls on large files. The code still correctly detects when the file would exceed `MaxAllocSize`. Because the raw bytes land in the `StringInfo`'s allocation, the function returns the result by simply reinterpreting that buffer as a `bytea`. This avoids an extra copy.

`read_text_file` wraps `read_binary_file` and adds a call to `pg_verifymbstr` before returning. This rejects byte sequences that are not valid in the database encoding. That check matters when the file contains binary content, or was written in a different encoding.

Both functions support a `seek_offset` parameter. PostgreSQL treats a non-negative offset as `SEEK_SET`, and a negative offset as `SEEK_END`. This lets callers tail the last N bytes of a file without knowing its length.

## File Metadata

`pg_stat_file` calls the OS `stat(2)` syscall and maps the result into a six-column composite type: size, access time, modification time, status-change time, creation time, and a boolean for whether the path is a directory. On Unix, the creation-time column is always NULL because Unix does not track it; on Windows, the status-change column is NULL instead. The function builds the `TupleDesc` on each call rather than caching it — a minor inefficiency acceptable given that stat calls are not on critical paths.

## Directory Listing

`pg_ls_dir` is a set-returning function that walks a directory using `AllocateDir`/`ReadDir`/`FreeDir` — the VFD-equivalent wrappers for `opendir`/`readdir`/`closedir`. It materialises the full result set immediately using `InitMaterializedSRF`. This means `pg_ls_dir` opens, fully scans, and closes the directory within a single call to the C function, not across multiple SRF calls. This design avoids the complexity of holding a directory handle open across executor calls while tolerating concurrent modifications to the directory.

An optional `include_dot_dirs` boolean controls whether `.` and `..` appear in the output. A separate `missing_ok` flag causes a missing directory to return an empty set rather than an error.

## Specialised Directory Listing Functions

`pg_ls_dir_files` is an internal variant used by a family of fixed-path functions. Unlike the public `pg_ls_dir`, it returns three columns per entry (name, size, modification time) and silently skips hidden files and non-regular files. This makes it suitable for the specific-purpose views:

- `pg_ls_logdir` — lists the server log directory (`Log_directory`)
- `pg_ls_waldir` — lists the [[subsystems/wal/overview|WAL]] segment directory (`pg_wal`)
- `pg_ls_archive_statusdir` — lists `pg_wal/archive_status`
- `pg_ls_tmpdir` — lists the `pgsql_tmp` directory within a tablespace; validates that the tablespace OID exists before constructing the path
- `pg_ls_replslotdir` — lists files within a named replication slot's directory after confirming the slot exists
- `pg_ls_logicalsnapdir` / `pg_ls_logicalmapdir` — list logical replication snapshot and mapping directories

These functions bypass the `convert_and_check_filename` path restriction entirely — their paths are hardcoded or validated through other means (tablespace OID lookup, replication slot lookup). PostgreSQL controls access to them purely via SQL privileges on the functions themselves, which it grants to the `pg_monitor` role in the default catalog.

## Read Size Limits

Both `read_binary_file` and `read_text_file` refuse requests larger than `MaxAllocSize - VARHDRSZ`, which is the maximum size of a palloc'd varlena object. PostgreSQL validates requests for a specific byte count upfront; open-ended reads detect overflow at the `MaxAllocSize - 1` boundary during the streaming read loop. This ensures that even a pathologically large file cannot exhaust the process's memory allocation limit silently.

## Related Topics

- [[subsystems/roles-privileges|Roles and privileges]]
- [[subsystems/wal/overview|WAL]]
- [[subsystems/storage/temp-files|Temporary files]]
- [[subsystems/storage/tablespaces|Tablespaces]]
- [[subsystems/observability/overview|Observability overview]]

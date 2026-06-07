---
title: "System Configuration Utilities"
aliases:
  - system configuration
  - pg_controldata SQL functions
  - pg_config SQL function
  - superuser check
  - timezone abbreviation parser
source_files:
  - src/backend/utils/misc/conffiles.c
  - src/backend/utils/misc/help_config.c
  - src/backend/utils/misc/pg_config.c
  - src/backend/utils/misc/pg_controldata.c
  - src/backend/utils/misc/superuser.c
  - src/backend/utils/misc/tzparser.c
symbols:
  - superuser
  - superuser_arg
  - has_rolreplication
  - has_bypassrls
  - pg_control_checkpoint
  - pg_control_system
  - pg_control_init
  - pg_control_recovery
  - pg_config
  - GucInfoMain
  - AbsoluteConfigLocation
  - GetConfFilesInDir
  - load_tzoffsets
---

PostgreSQL bundles a set of focused utilities that expose system configuration state — install layout, cluster identity, GUC metadata, and privilege predicates — through SQL functions and command-line entry points. These utilities sit in `src/backend/utils/misc/`. They share a common design principle: they surface information that would otherwise require filesystem access or catalog expertise, making it consumable by monitoring scripts, extensions, and operators alike.

## Superuser and Privilege Predicates

The `superuser()` function (`superuser.c`) answers a single question — does the current user hold the superuser attribute — without requiring callers to touch `pg_authid` directly. It delegates to `superuser_arg(roleid)`, which looks up `rolsuper` in the `AUTHOID` syscache and caches the result in a module-local one-entry cache keyed on the last queried OID. A `CacheRegisterSyscacheCallback` on `AUTHOID` flushes that cache whenever the role catalog changes. Privilege escalations and revocations are therefore reflected promptly.

The important escape hatch is the single-user mode check: when `IsUnderPostmaster` is false and the queried OID is `BOOTSTRAP_SUPERUSERID`, `superuser_arg()` returns `true` unconditionally, bypassing catalog access entirely. This allows a DBA to recover a cluster even after all superuser grants have been accidentally dropped. Outside single-user mode, the catalog is authoritative.

`has_rolreplication()` and `has_bypassrls()` (`superuser.c`) follow the same pattern — syscache lookup of `rolreplication` and `rolbypassrls` respectively — giving callers consistent, cache-friendly predicate functions rather than ad-hoc catalog queries.

## Control File Introspection

The `$PGDATA/global/pg_control` file records fundamental cluster state: the system identifier assigned at `initdb`, the last completed checkpoint location, recovery minimum LSN, and compile-time parameters baked into the cluster. The command-line tool `pg_controldata` is the traditional way to inspect this file. However, it requires filesystem access to `$PGDATA`. The SQL functions in `pg_controldata.c` expose the same data through four set-returning functions — `pg_control_system()`, `pg_control_checkpoint()`, `pg_control_recovery()`, and `pg_control_init()` — making the file readable by monitoring queries that run inside the database.

Each function acquires `ControlFileLock` in shared mode and reads the file via `get_controlfile()`. It then verifies the CRC and populates a composite return type. The [[subsystems/locking/lwlocks|LWLock]] acquisition is lightweight. The lock is normally uncontended because `pg_control` is written only during checkpoints and WAL switches. The CRC check ensures that a partially written control file — possible during a crash — is rejected rather than silently returned.

`pg_control_checkpoint()` exposes the fields most useful for monitoring: checkpoint LSN, REDO start point, timeline ID, `fullPageWrites` state, oldest active XID, and the timestamp of the last checkpoint. `pg_control_init()` exposes compile-time constants such as `blcksz`, `xlog_seg_size`, and `data_checksum_version`, which extensions and replication tools use to verify compatibility. `pg_control_recovery()` exposes the minimum recovery point and backup range, relevant when inspecting a standby or a just-restored base backup.

## Build Configuration via SQL

`pg_config()` (`pg_config.c`) implements a `SETOF record` SQL function that returns the same key-value pairs as the `pg_config` command-line tool: installation prefix, `bindir`, `includedir`, `pkgincludedir`, `libdir`, compiler flags, and version string. The implementation is a thin wrapper around `get_configdata(my_exec_path, &configdata_len)` from `src/common/config_info.c`, which builds the list by deriving paths relative to the server executable.

This function is primarily useful for extensions that need to locate PostgreSQL header files or libraries at runtime without shelling out to `pg_config`. It requires no special privileges and returns the same data regardless of who calls it.

## GUC Metadata Emission

`GucInfoMain()` (`help_config.c`) is the entry point for the `postgres --help-config` (or `--describe-config`) mode. It calls `build_guc_variables()` to populate the GUC table, then iterates over every `config_generic` entry to emit its name, type, default value, and valid range to standard output. `GucInfoMain()` skips variables with flags `GUC_NO_SHOW_ALL` or `GUC_NOT_IN_SAMPLE` in the general listing. Because `GucInfoMain()` derives the output directly from the `GucVariable` array compiled into the server binary, the output is always consistent with the running version. No separate documentation file is involved.

## Configuration File Path Resolution

`AbsoluteConfigLocation()` (`conffiles.c`) resolves a potentially relative configuration path to an absolute one. When a calling file path is provided (the case for `include` and `include_dir` directives inside `postgresql.conf`), `AbsoluteConfigLocation()` resolves relative paths against the directory containing that file. When no calling file is present, resolution falls back to `DataDir`. This anchoring rule means that an `include` directive in a file located at `/etc/postgresql/postgresql.conf` resolves relative paths relative to `/etc/postgresql/`, not relative to `$PGDATA`. This behavior allows configuration fragments to be kept alongside the main file.

`GetConfFilesInDir()` (`conffiles.c`) supports the `include_dir` directive by enumerating all files ending in `.conf` in a directory, sorting them alphabetically, and returning the list to the GUC loader. `GetConfFilesInDir()` excludes files starting with `.` to prevent accidental inclusion of hidden files, backup copies, and editor swap files. The alphabetical ordering guarantees deterministic precedence when multiple fragment files set the same GUC.

## Timezone Abbreviation Parsing

PostgreSQL allows operators to configure which set of timezone abbreviations is active via the `timezone_abbreviations` GUC. PostgreSQL stores the supported sets (`Default`, `Australia`, `India`, etc.) as text files in `share/timezonesets/`. `tzparser.c` parses these files into an array of `tzEntry` structures, each mapping an abbreviation string to a UTC offset in seconds and a DST flag.

The GUC check-hook for `timezone_abbreviations` invokes the parser (`load_tzoffsets()`, `tzparser.c`). The parser reports errors — malformed lines, abbreviations exceeding `TOKMAXLEN`, offsets outside ±14 hours — via `GUC_check_errmsg()` rather than `elog(ERROR)`, which means a bad timezone file causes a GUC validation failure rather than a hard server error. This lets PostgreSQL reject a misconfigured `timezone_abbreviations = 'BadFile'` gracefully and roll back to the previous value, instead of crashing the session.

The files support an `@INCLUDE` directive, allowing one abbreviation set to extend another. A depth counter limits include nesting to prevent infinite recursion.

## See also

- [[subsystems/locking/lwlocks|LWLocks]]
- [[subsystems/memory/contexts|Memory contexts]]
- [[subsystems/wal/overview|WAL and resource managers]]

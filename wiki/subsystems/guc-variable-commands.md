---
title: "GUC Variable Commands and Database Commands"
aliases:
  - GUC variable hooks
  - SET variable hooks
  - CREATE DATABASE internals
  - dbcommands
source_files:
  - src/backend/commands/variable.c
  - src/backend/commands/dbcommands.c
symbols:
  - check_datestyle
  - assign_datestyle
  - check_timezone
  - assign_timezone
  - check_transaction_read_only
  - check_transaction_isolation
  - check_session_authorization
  - check_role
  - role_auth_extra
  - createdb
  - dropdb
  - CreateDatabaseUsingWalLog
  - CreateDatabaseUsingFileCopy
  - CreateDBStrategy
  - CreateDBRelInfo
  - RenameDatabase
  - movedb
---

`variable.c` and `dbcommands.c` together implement the parts of PostgreSQL's [[subsystems/guc|GUC: Grand Unified Configuration]] that require deep integration with session state, transaction semantics, and physical storage. The former provides the check/assign/show hook implementations for parameters whose validation or side-effects are non-trivial — timezone, datestyle, transaction isolation, role, encoding, and seed. The latter implements the DDL commands that create, drop, rename, and move databases, including the WAL infrastructure that makes those operations crash-safe.

## How complex GUC parameters are validated

The [[subsystems/guc|GUC]] framework's three-hook model (check, assign, show) is deliberately minimalist: the check hook validates a proposed value and packs any expensive computation into an `extra` pointer; the assign hook reads that pointer and applies side-effects; the show hook can override the displayed representation. `variable.c` demonstrates the full range of ways this can be used.

### Datestyle: multi-token parsing with canonical output

`check_datestyle()` (variable.c) accepts a comma-separated list of keywords such as `"ISO, DMY"` or `"German"`. It walks the token list tracking which style and order tokens have been seen, rejecting conflicting combinations. `check_datestyle()` handles the `DEFAULT` keyword by recursively calling itself on the GUC's current reset string — the cleanest way to handle "inherit the default for this cluster." On success, the hook rewrites `*newval` with a canonical string (e.g., `"ISO, MDY"`) and stuffs a two-element `int` array into `*extra`. `assign_datestyle()` then reads the two integers directly into the global `DateStyle` and `DateOrder` variables. The canonicalization in the check hook means `pg_settings.setting` always shows a predictable form regardless of how the user spelled the input.

### Timezone: three input formats, one `pg_tz *`

`check_timezone()` (variable.c) accepts three syntactic forms: an `INTERVAL 'hh:mm'` string (SQL standard compliance only), a bare numeric offset in hours, and a timezone name. `pg_tzset_offset()` or `pg_tzset()` resolves all three to a `pg_tz *` pointer. The pointer is stored in `*extra` as a `guc_malloc`'d `pg_tz **`. `assign_timezone()` copies that pointer into the process-global `session_timezone`. `show_timezone()` uses `pg_get_timezone_name()` to return the canonical Olson name rather than the potentially abbreviated name the user typed. `log_timezone` mirrors this design but does not accept the interval form. There is no practical reason to use interval syntax in a configuration file.

PostgreSQL explicitly rejects leap-second timezones (`pg_tz_acceptable()`) because its timestamp arithmetic does not account for them. Silently accepting one would produce wrong results.

### Transaction-scoped parameters

`check_transaction_read_only()`, `check_transaction_isolation()`, and `check_transaction_deferrable()` (variable.c) enforce the SQL standard rule that `SET TRANSACTION` parameters can only be changed before any snapshot is taken. The checks consult `FirstSnapshotSet` and `IsSubTransaction()` to enforce this. Attempting to set `SERIALIZABLE` on a hot standby is also blocked, since serializable snapshots require a primary.

These hooks use no `extra` storage — all their work is validation and the assign hooks update straightforward global variables (`XactReadOnly`, `XactIsoLevel`, `XactDeferrable`). The check hooks also tolerate idempotent changes at any time, which prevents churn when GUC restore logic re-applies a setting that matches the current value.

### Session authorization and role

`check_session_authorization()` and `check_role()` (variable.c) look up the target role in `pg_authid` using the system cache, verify that the current session has the right to assume that identity, and pack the resolved OID and superuser flag into a `role_auth_extra` struct in `*extra`. The assign hooks then call `SetSessionAuthorization()` and `SetCurrentRoleId()` respectively.

Two design details are worth noting. First, during parallel worker initialization (`InitializingParallelWorker`), the catalog lookup is skipped: the worker copies the leader's already-resolved state. This is necessary because the catalog may have changed since the leader's session started. The worker's security context must match the leader's, not the current catalog state. Second, when `GucSource` is `PGC_S_TEST` — used during `ALTER DATABASE SET` or `ALTER ROLE SET` validation — the hooks emit `NOTICE` instead of `ERROR` for nonexistent roles, allowing the DDL to succeed and surface the problem only when the setting is actually applied at connection time.

The `show_role()` hook handles a subtle invariant: `SET SESSION AUTHORIZATION` should logically reset the role to `none`, but the GUC machinery cannot call `set_config_option()` from inside an assign hook (since that would recurse into GUC internals). Instead, `show_role()` checks `GetCurrentRoleId()` directly and returns `"none"` when the OID is invalid, regardless of what the GUC string variable holds.

### Random seed: source-gated assignment

`check_random_seed()` (variable.c) stores a flag in `*extra` indicating whether the assignment source is interactive (`source >= PGC_S_INTERACTIVE`). `assign_random_seed()` only calls `setseed()` if that flag is set, then clears it. This prevents config file reloads or transaction rollbacks from re-seeding the RNG, which would be surprising and non-deterministic. The `show_random_seed()` hook always returns `"unavailable"`, documenting that once set, the seed cannot be read back.

### Client encoding: preparation and deferred application

`check_client_encoding()` (variable.c) calls `PrepareClientEncoding()` to verify that a conversion procedure exists for the combination of client encoding and database encoding. This may fail if called outside a transaction (no catalog access). The canonical encoding name replaces the user-supplied string so that aliases like `UTF-8`, `utf8`, and `UNICODE` all normalize to `UTF8`. `assign_client_encoding()` then calls `SetClientEncoding()` to make the conversion take effect. Parallel workers cannot change the client encoding mid-session and will `ereport(ERROR)` if any code (e.g., a function's `SET` clause) tries to do so outside of initialization.

## Database DDL: CREATE DATABASE

`createdb()` (dbcommands.c) is the implementation of `CREATE DATABASE`. Before any filesystem work begins, it performs a layered set of validation:

1. Acquires a `ShareLock` on the template database via `LockSharedObject()`, blocking concurrent drops and preventing new connections to the template while copying proceeds.
2. Verifies encoding-locale compatibility: the chosen encoding must match the locale's expected encoding unless the locale is `C`/`POSIX` or the encoding is `SQL_ASCII` (superuser-only exception). ICU locales are canonicalized to BCP 47 language tags before being stored.
3. When using a template other than `template0`, enforces that encoding, collation, ctype, locale provider, and ICU locale all match the template. `template0` is the only escape hatch for creating a database with different locale settings.
4. Checks for collation version mismatch between the stored `datcollversion` and the OS's current collation library version. A mismatch blocks creation, because the copy would inherit potentially wrong index orderings.

Only after all checks pass does `createdb()` insert the `pg_database` row. Name ownership is established at that point — any concurrent `CREATE DATABASE` using the same name blocks on the unique index and fails after commit.

### Two copy strategies

`CREATE DATABASE` supports two strategies, selected with `STRATEGY`:

| Strategy | Mechanism | WAL volume | Notes |
|---|---|---|---|
| `wal_log` (default) | Block-by-block copy via `CreateAndCopyRelationData()`, each block WAL-logged | High | Recoverable without checkpoints; no PITR gap |
| `file_copy` | Filesystem directory copy via `copydir()`, one WAL record per tablespace | Low for large DBs | Requires two forced checkpoints (before and after) |

`CreateDatabaseUsingWalLog()` (dbcommands.c) enumerates the source database's relations by reading its `pg_class` directly — without using the relcache, which only knows about the current database. It reads raw heap pages using `ReadBufferWithoutRelcache()`, applies a `GetLatestSnapshot()` for visibility, and constructs a `CreateDBRelInfo` list. Shared relations, relations without storage, and temporary relations are skipped. Each relation is then copied and WAL-logged block by block.

`CreateDatabaseUsingFileCopy()` (dbcommands.c) forces a pre-copy checkpoint to flush dirty buffers, iterates over tablespaces via a catalog scan, copies each tablespace directory, logs `XLOG_DBASE_CREATE_FILE_COPY` records, and then forces a post-copy checkpoint. The post-copy checkpoint ensures the `DBASE_CREATE` WAL record will never need to be replayed in a crash scenario, avoiding the two historical bugs (lost non-WAL-logged indexes, and copying template changes committed after the CREATE DATABASE statement).

`createdb_failure_callback()`, registered with `PG_ENSURE_ERROR_CLEANUP`, handles error cleanup. If the copy fails at any point, the callback drops shared buffers for the partially created database, cancels pending fsync requests, releases locks, and removes the partial directory tree.

## Database DDL: DROP DATABASE

`dropdb()` (dbcommands.c) acquires an `AccessExclusiveLock` on the database, then works through a sequence of safety checks: template protection, self-drop prohibition, active logical replication slots, active subscriptions, and other backends. With `FORCE`, it calls `TerminateOtherDBBackends()` before the backend count check.

The drop sequence separates catalog and filesystem operations deliberately. Before touching files, it marks the database as invalid with an in-place update (`datconnlimit = DATCONNLIMIT_INVALID_DB`) and flushes WAL. This ensures that any crash between the catalog update and the filesystem removal leaves the database in a recognizably broken state — the next `dropdb()` invocation can proceed to clean up rather than discovering an inconsistency. The catalog row is also deleted transactionally, while the filesystem removal (`remove_dbtablespaces()`) is post-commit and non-transactional. `ForceSyncCommit()` minimizes the window between filesystem removal and transaction commit.

## Database DDL: ALTER DATABASE SET TABLESPACE

`movedb()` (dbcommands.c) uses a session-level lock (`LockSharedObjectForSession()`) that persists across the internal commit/restart cycle the operation requires. After flushing buffers and closing all storage manager file descriptors via `EmitProcSignalBarrier(PROCSIGNAL_BARRIER_SMGRRELEASE)`, it copies the database directory to the new tablespace location using `copydir()` and updates the `pg_database` row. A `PG_ENSURE_ERROR_CLEANUP` block removes partial copies if the operation fails midway.

## Related Topics

- [[subsystems/guc|GUC: Grand Unified Configuration]] — the full GUC infrastructure including contexts, sources, hooks, and transaction stack
- [[subsystems/transactions/transaction-lifecycle|transaction lifecycle]] — how `SET LOCAL` and `SET` interact with transaction commit and abort
- [[subsystems/wal/recovery|WAL recovery]] — how `XLOG_DBASE_CREATE_WAL_LOG` and `XLOG_DBASE_CREATE_FILE_COPY` records are replayed

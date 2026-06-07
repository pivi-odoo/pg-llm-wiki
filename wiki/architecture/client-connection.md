---
title: "Client Connection Architecture"
aliases:
  - "Connection Model"
  - "Postmaster Fork Model"
  - "Backend Lifecycle"
tags:
  - theme/wire-protocol
  - symptom/auth-failure
source_files:
  - src/backend/postmaster/postmaster.c
  - src/backend/utils/init/postinit.c
  - src/backend/tcop/postgres.c
symbols:
  - ServerLoop
  - BackendStartup
  - BackendInitialize
  - PostgresMain
  - InitPostgres
  - PerformAuthentication
  - exec_simple_query
  - exec_parse_message
  - exec_bind_message
  - exec_execute_message
---

# Client Connection Architecture

PostgreSQL uses a **process-per-connection** model. The postmaster is a long-lived daemon that accepts incoming connections. For each new client, it forks a dedicated backend process that handles exactly that session for its entire lifetime. This model trades connection establishment overhead for isolation, simplicity, and fault containment.

## The postmaster as connection broker

The postmaster (`src/backend/postmaster/postmaster.c`) never executes SQL itself. It deliberately avoids shared memory operations, so that a crashing backend cannot destabilize it. Its responsibilities are:

- Listening on one or more TCP ports and Unix-domain sockets.
- Accepting new connections and forking backend processes.
- Launching and restarting auxiliary processes (checkpointer, background writer, WAL writer, [[subsystems/background/autovacuum|autovacuum]] launcher, archiver, WAL senders).
- Forwarding cancel requests to the correct backend via `SIGINT`.
- Performing an orderly or immediate shutdown by signalling children.

The core accept loop runs in `ServerLoop`. When an incoming connection arrives on a listening socket, the postmaster calls `ConnCreate` to allocate a `Port` struct. It then immediately calls `BackendStartup`. Before calling `fork_process()`, it generates a random cancel key for the new backend (`MyCancelKey`). It records this key in the `Backend` tracking struct. This happens before the fork. That way, the parent can answer cancel requests even before the child has initialized.

After `fork()`, the parent closes its copy of the client socket. It adds the new backend to `BackendList`. It then returns to the accept loop. The child owns the socket from that point. [[subsystems/client-connection|Client Connection Establishment]] has a full sequence diagram of this handshake down to the wire-protocol messages exchanged at each step. The table below summarizes the same phases at the process-lifecycle level.

### Key phases

| Phase | Function | What happens |
|---|---|---|
| TCP accept | `ConnCreate()` | `accept()` syscall; `Port` struct allocated with malloc |
| Fork | `BackendStartup()` | `fork_process()` (or `backend_forkexec` on Windows) |
| Pre-auth setup | `BackendInitialize()` | `pq_init()`, SSL/GSS negotiation, startup packet read |
| Process slot | `InitProcess()` | Claim `PGPROC` in shared memory; enables [[subsystems/locking/lwlocks|LWLocks]] |
| Session init | `InitPostgres()` | Auth, catalog cache setup, GUC application |
| Query loop | `PostgresMain()` | `ReadCommand` → dispatch → execute → response |
| Cleanup | `proc_exit()` | Resource release, shared memory detach |

## Pre-auth initialization

`BackendInitialize` runs before it touches any shared memory. It calls `pq_init()` to set up the libpq communication layer. It also sets `whereToSendOutput = DestRemote`, so that subsequent `ereport` calls can reach the client. SSL and GSSAPI negotiation happen here via `ProcessStartupPacket`. This function re-reads the connection until it has a real startup packet rather than a negotiation request.

The startup packet carries the protocol version number, target database name, user name, and an optional set of GUC name-value pairs that the client wants to apply. `ProcessStartupPacket` validates the protocol version (currently 3.0), extracts these fields, and checks `canAcceptConnections`. This check rejects connections early if the server is starting up, shutting down, or over its connection limit.

Because `BackendInitialize` has not yet touched shared memory, the child can exit with `_exit(1)` on timeout or SIGTERM at this stage without any cleanup — there is nothing to clean up. `BackendInitialize` starts an `AuthenticationTimeout` here, and `InitPostgres` starts it again later. Both timeouts limit how long a slow or hostile client can hold the child during authentication.

## Authentication flow

`PerformAuthentication` handles authentication. `InitPostgres` calls it after minimally initializing the catalog cache and relation cache (enough to read `pg_database` and `pg_authid`). The sequence is:

1. PostgreSQL searches the in-memory HBA table (loaded by the postmaster at startup, inherited across `fork`) for the first rule matching the client's address, database, and user.
2. The matching rule's `auth-method` field determines the challenge. Common methods:
   - **trust** — no challenge. The server accepts the connection immediately.
   - **peer** — the server compares the OS-level peer credentials of the Unix socket connection to the role name. The exchange involves no password.
   - **md5** — the server sends a random salt. The client responds with MD5(MD5(password + username) + salt). Deprecated in favor of SCRAM.
   - **scram-sha-256** — a full SCRAM exchange: server sends a nonce and iteration count, client proves knowledge of the password without transmitting it, server proves knowledge in return (mutual authentication).
   - **gss** / **sspi** — Kerberos or SSPI. The OS authenticates via ticket exchange.
   - **ldap**, **radius**, **cert** — an external service or certificate authority verifies the credentials.
3. `ClientAuthentication` (`src/backend/libpq/auth.c`) orchestrates the exchange. On success, control returns to `PerformAuthentication`, which disables the `STATEMENT_TIMEOUT` armed earlier to enforce `AuthenticationTimeout`.
4. Back in `InitPostgres`, `InitializeSessionUserId` maps the authenticated identity to a role OID. `InitializeSystemUser` records the authentication method and identity for `system_user`.

A failed authentication always terminates the process. There is no retry within a single connection.

## Process initialization after fork

`InitPostgres` (`src/backend/utils/init/postinit.c`) is the central initialization function. Its ordering reflects hard dependencies:

1. **`InitProcessPhase2`** — registers the `PGPROC` entry in the ProcArray, making the backend visible to lock managers and the snapshot machinery.
2. **`SharedInvalBackendInit`** — assigns `MyBackendId`, the unique per-backend index used for shared-invalidation messages.
3. **Timeout registration** — registers `DEADLOCK_TIMEOUT`, `STATEMENT_TIMEOUT`, `LOCK_TIMEOUT`, `IDLE_IN_TRANSACTION_SESSION_TIMEOUT`, `IDLE_SESSION_TIMEOUT`, and others via the timeout subsystem.
4. **`RelationCacheInitialize` / `InitCatalogCache` / `InitPlanCache`** — allocates hash tables but does not populate them yet. No catalog access yet.
5. **`RelationCacheInitializePhase2`** — loads relcache entries for the shared system catalogs (at minimum `pg_database` and the catalogs needed for authentication).
6. **`PerformAuthentication`** — as described above.
7. **Database lock and OID resolution** — takes a `RowExclusiveLock` on the database's `pg_database` entry to prevent a concurrent `DROP DATABASE` from proceeding while this backend initializes.
8. **`RelationCacheInitializePhase3`** — loads per-database system catalog entries. This is where the catalog cache becomes fully operational.
9. **`SetDatabasePath`** — sets the path to the database's data directory within the tablespace hierarchy.
10. **GUC application** — applies startup packet options, `pg_db_role_setting` rows for the database and role, and `session_preload_libraries`.
11. **`CommitTransactionCommand`** — releases the startup transaction.

After `InitPostgres` returns, `PostgresMain` frees the `PostmasterContext` (which held the HBA data and startup packet). It then calls `BeginReportingGUCOptions` to send `ParameterStatus` messages for all `GUC_REPORT` variables, followed by `BackendKeyData` (containing the PID and cancel key), and finally `ReadyForQuery`.

## The command loop

`PostgresMain` contains the main loop that drives the entire session. On each iteration it:

1. Resets `MessageContext` (freeing allocations from the previous command).
2. Conditionally releases the catalog snapshot to avoid blocking global xmin advance.
3. Sends `ReadyForQuery` if appropriate and arms idle-state timers.
4. Calls `ReadCommand` (blocking until a message arrives), which dispatches to `SocketBackend` for network connections or `InteractiveBackend` for the single-user mode.
5. Disables any idle-state timeout that was active.
6. Dispatches the message by its first-byte type code.

Error recovery uses `sigsetjmp`. On any `ERROR`, execution jumps back to the top of the loop. It aborts the current transaction and re-arms for the next command. `FATAL` and `PANIC` exit the process entirely.

## Simple vs extended query protocol

The message byte the client sends determines the protocol type. The backend tracks this with `doing_extended_query_message`.

**Simple query protocol** (`'Q'` message): a single string containing one or more SQL statements. The backend calls `exec_simple_query`. This function parses the entire string and loops over the resulting parse trees. For each tree, it performs analysis, rewrite, planning, portal creation, execution, and portal destruction inline. Multiple statements in one `'Q'` message run inside an implicit transaction block. The client receives a `CommandComplete` after each statement and a `ReadyForQuery` at the end.

**Extended query protocol** uses separate messages for each stage:

| Message | Byte | Handled by |
|---|---|---|
| Parse | `P` | `exec_parse_message` — parses the query, creates a `CachedPlanSource`, returns `ParseComplete` |
| Bind | `B` | `exec_bind_message` — binds parameters to a named or unnamed portal, returns `BindComplete` |
| Describe | `D` | `exec_describe_statement_message` / `exec_describe_portal_message` — returns `ParameterDescription` or `RowDescription` |
| Execute | `E` | `exec_execute_message` — runs the portal up to `max_rows` rows, returns data rows and `CommandComplete` |
| Sync | `S` | commits the transaction command and sends `ReadyForQuery` |
| Close | `C` | drops a named statement or portal |

The separation of Parse from Execute enables server-side prepared statements: a named statement created with `PREPARE` (or the extended protocol `P` message) survives across multiple `Bind`/`Execute` cycles. The `CachedPlanSource` retains the parsed and analyzed query tree. The planner may re-run at `Execute` time if the plan cache detects that statistics have changed significantly.

On an error during an extended-query sequence, the backend sets `ignore_till_sync = true`. It then discards all messages until a `Sync` arrives. This synchronization point is essential for clients that pipeline multiple messages — without it, error recovery would be ambiguous.

## Portal and snapshot lifecycle

A portal is the execution context for a single query. In the simple protocol, the backend creates and destroys an unnamed portal for each statement. In the extended protocol, portals can be named and held open across multiple `Execute` messages, which is how the protocol implements cursors.

`PortalStart` acquires a snapshot via `GetTransactionSnapshot`. It then pushes the snapshot onto the active snapshot stack. For read-committed isolation, `PortalStart` acquires a new snapshot at the start of each command. For repeatable read and serializable, it acquires the snapshot once per transaction and reuses it.

The snapshot acquired for parse analysis and planning is a separate push from the one used for execution. This is intentional: reusing the planning snapshot for execution would create anomalies, because PostgreSQL takes the snapshot before the planner acquires table locks. `src/backend/tcop/postgres.c` contains a comment referencing a mailing list discussion of this design choice.

When `PortalDrop` runs, it releases the portal's snapshot reference. If the portal holds a cursor that spans multiple `Execute` calls, PostgreSQL holds the snapshot until the cursor closes or the transaction ends.

## Idle-in-transaction timeout and statement timeout

PostgreSQL manages both timeouts through the central timeout subsystem (`src/backend/utils/timeout.c`) using `SIGALRM`. They fire at different points in the session lifecycle.

**`statement_timeout`** arms at the start of each transaction command (`start_xact_command`), specifically when `enable_statement_timeout` is called. It fires if the command runs longer than the configured limit. This delivers `SIGALRM` to the backend, which converts the signal into an `ERROR`. Because this is an `ERROR` and not `FATAL`, the session survives. It returns to `ReadyForQuery`.

**`idle_in_transaction_session_timeout`** arms at the top of the main loop, just before PostgreSQL sends `ReadyForQuery`. It arms only when the session is inside an open transaction block. It detects the case where a client opened a transaction and then went silent — a common cause of lock accumulation. When it fires, the backend calls `IdleInTransactionSessionTimeoutHandler`. This handler raises `FATAL`, terminating the session. The distinction from `statement_timeout` is that there is no running statement to interrupt. The session itself must be torn down.

`idle_session_timeout` operates similarly, but it applies when the session is fully idle (not inside any transaction). It terminates sessions that have stayed connected but inactive for too long.

PostgreSQL disarms both idle timeouts the moment `ReadCommand` returns with a new message. The code in `PostgresMain` explicitly disables them before checking for interrupts. This avoids a race where the timeout could fire between message receipt and timer disablement.

## Connection overhead

Every new connection is expensive in ways that accumulate:

**Fork cost**: `fork()` copies the postmaster's address space via copy-on-write. On Linux the cost is proportional to the postmaster's page table size, which grows with shared memory mappings. A postmaster with a large `shared_buffers` has a correspondingly larger page table to copy.

**Shared memory attachment**: the backend must attach to the shared memory segment (buffer pool, lock tables, ProcArray, etc.). It must also initialize its internal pointers. This is not just memory mapping — it involves acquiring LWLocks to register in the ProcArray.

**Catalog cache cold start**: the relation cache and system catalog caches begin empty. The first queries in a session trigger reads from system tables to populate entries for types, operators, functions, and relations referenced by those queries. For a complex query touching many tables, this can mean dozens of catalog lookups. Subsequent queries in the same session avoid those lookups because the cache is warm.

**GUC application**: `InitPostgres` applies startup packet options, role-level settings, and database-level settings during initialization. Each requires at least a catalog lookup.

**`pg_hba.conf` evaluation**: while the in-memory HBA structure is inherited from the postmaster, authentication itself may require network round-trips (LDAP, RADIUS) or crypto operations (SCRAM).

The cumulative effect is that a new connection takes on the order of 1–10 ms even on fast hardware, with the catalog cold-start dominating for complex workloads.

## Connection poolers

Connection poolers sit between the application and PostgreSQL, maintaining a pool of long-lived backend connections and multiplexing application requests onto them. The pooler amortizes the cost paid once at startup across thousands of application "connections." Two fundamentally different pooling strategies exist.

**Session pooling** assigns a backend to an application session for its entire lifetime. The application sees a stable connection with all session-level state (prepared statements, `SET` variables, `pg_temp` schemas, advisory locks, LISTEN registrations) preserved. The pooler gains nothing in terms of reducing backend count. It is mainly useful as a connection broker that enforces limits, provides a stable endpoint, and absorbs connection spikes without overwhelming the postmaster's fork rate.

**Transaction pooling** assigns a backend only for the duration of a transaction. Between transactions, the pooler can return the backend to the pool and reuse it for a different client. This allows, for example, 1000 application threads to share 50 PostgreSQL backends, provided no more than 50 are in an active transaction simultaneously. The savings are dramatic for workloads with short transactions and many idle connections.

Transaction pooling has constraints that session pooling does not:

- **Prepared statements** created with the extended query protocol `P` message live on the backend, not the pooler. After transaction pooling reassigns the backend, the prepared statement is gone. Poolers work around this in one of two ways. They can use protocol-level prepared statements named with a hash of the query text, tracking them per backend. Or they can disable server-side prepared statements entirely.
- **`SET` variables** applied within a session (outside a transaction) persist on the backend, not the application. A pooler must reset them before returning a backend to the pool, typically by issuing `RESET ALL; DEALLOCATE ALL;` or configuring `server_reset_query_mode`.
- **`LISTEN`/`NOTIFY`** registration is per-backend. An application relying on notifications cannot use transaction pooling without special support from the pooler.
- **Advisory locks** — the pooler releases advisory locks held outside a transaction when it reassigns the backend.
- **Temporary tables** — PostgreSQL creates temporary tables in `pg_temp_N`. They survive until the session ends. Transaction pooling typically disallows temporary tables or requires explicit cleanup.

PgBouncer is the most widely deployed pooler. In transaction mode it is highly effective at reducing backend count; in session mode it mainly limits connection spikes. Pgpool-II offers session pooling plus query routing and high-availability features.

## Fork-based isolation

Using one OS process per connection gives PostgreSQL several properties that a threading model would complicate:

- **Crash isolation** — the OS kills a backend that segfaults. The postmaster detects the exit and can restart it without affecting other sessions.
- **Memory isolation** — the OS automatically frees a backend's per-session allocation ([[subsystems/memory/contexts|memory contexts]], plan cache, session variables) on exit. No teardown code is required.
- **Shared memory simplicity** — all cross-backend state lives in the explicitly-managed shared memory segment. There is no risk of accidental sharing of per-session state.
- **Signal-based cancellation** — query cancellation uses `SIGINT` sent by the postmaster to the backend PID, which is simpler than thread-safe cancellation points.
- **Library safety** — many C libraries are not thread-safe. The postmaster comment notes that blocking behavior in SSL or PAM cannot cause denial of service to other clients because each connection runs in its own process.

The main cost is that `fork()` and process teardown are heavier than thread creation. This is why connection poolers are standard practice for high-connection workloads.

## Cancel requests

Cancelling a running query does not reuse the session's connection. The client opens a **new TCP connection** to the postmaster, carrying the `(PID, cancel_key)` pair it received in `BackendKeyData`. The postmaster forwards a `SIGINT` to the matching backend if the key checks out. The backend converts the signal into a `QueryCancelPending` flag, checked at the next interrupt point. This raises an `ERROR` rather than `FATAL`, so the session survives and returns to `ReadyForQuery`. The `CancelRequestPacket` layout and the postmaster-side verification steps are documented in the wire-protocol handshake reference linked below.

## Connection limits

PostgreSQL enforces `max_connections` twice: a soft check in the postmaster (`canAcceptConnections`) against the postmaster's child-process array before it forks the backend, and a hard check inside `InitPostgres` after the backend has already claimed a `PGPROC` slot and authenticated. The same reference covers the exact thresholds, the reserved-connections logic, and the `MaxBackends` formula in detail. The two-level design means a misbehaving client that passes the soft check but fails authentication still holds a process for the duration of the authentication exchange. `AuthenticationTimeout` limits this exposure.

## See also

- [[architecture/overview]]
- [[architecture/process-architecture]]
- [[architecture/connection-pooling-impact]]
- [[subsystems/client-connection]] — protocol-level handshake detail (SSL negotiation, startup packet, SCRAM exchange), connection-limit enforcement mechanics, and cancel-request wire format
- [[subsystems/wire-protocol]] — frontend/backend message format
- [[subsystems/roles-privileges]] — authentication and role system
- [[subsystems/guc]] — GUC application at session start
- [[code-paths/transaction]] — transaction lifecycle within a session

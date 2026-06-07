---
title: "Backend Startup and Protocol Negotiation"
aliases:
  - BackendMain
  - BackendInitialize
  - startup packet
  - SSL negotiation
  - GSS negotiation
  - protocol negotiation
  - ProcessStartupPacket
tags:
  - theme/wire-protocol
source_files:
  - src/backend/tcop/backend_startup.c
  - src/include/tcop/backend_startup.h
symbols:
  - BackendMain
  - BackendInitialize
  - ProcessSSLStartup
  - ProcessStartupPacket
  - ProcessCancelRequestPacket
  - SendNegotiateProtocolVersion
  - StartupPacketTimeoutHandler
  - process_startup_packet_die
  - log_connections
  - Trace_connection_negotiation
---

`backend_startup.c` contains the entry point and connection negotiation logic for each new client backend. When the postmaster forks a backend process, control flows through `BackendMain()`. This function orchestrates SSL/GSS layer setup, startup packet parsing, and the final handoff to the main query loop. This code runs before it touches shared memory. That gives it a distinct safety property: any failure during this phase can exit with `_exit(1)` rather than running full cleanup.

## Entry Point

`BackendMain()` receives a `BackendStartupData` struct from the postmaster that includes the `canAcceptConnections` (`CAC_state`) — a pre-evaluated verdict about whether the server is currently accepting new connections. `BackendMain()` defers the actual state check until after the backend reads the startup packet. This lets the server respond with a proper error message rather than silently dropping the connection.

In `EXEC_BACKEND` builds, the backend is a new process rather than a fork, so it may need to reinitialize SSL libraries. SSL libraries contain function pointers that cannot be transferred via the parameter file.

`BackendInitialize()` handles the pre-authentication phase. It:

1. Reserves a file descriptor for the client socket.
2. Optionally sleeps for `PreAuthDelay` seconds (a debugging aid for attaching a debugger to the nascent backend).
3. Sets `ClientAuthInProgress = true` to suppress some logging until authentication completes.
4. Initializes libpq and sets up the Port structure in `TopMemoryContext` so it persists through the entire backend lifetime.
5. Looks up the client's remote host and port for logging.
6. Installs a `SIGTERM` handler that calls `_exit(1)` (safe because shared memory is untouched) and registers an authentication timeout.

## Protocol Negotiation Loop

The wire protocol supports multiple optional encryption layers before the actual startup packet. `ProcessStartupPacket()` implements a small retry loop (`goto retry`) that handles:

- **Direct SSL** — detected by `ProcessSSLStartup()` before reading any startup data. The server peeks at the first byte of the stream. If it is `0x16` (the TLS `ClientHello` record type), it performs a full TLS handshake directly. Direct SSL connections require ALPN negotiation. If the server rejects SSL, it returns an error without reading the byte. This lets the client retry with a different approach.

- **SSL request** (protocol code `NEGOTIATE_SSL_CODE`) — the client sends a 4-byte special packet to ask whether SSL is supported. The server responds with `'S'` or `'N'`. If the client accepts, the TLS handshake follows. The server then sets `ssl_done`. The loop then retries to read the actual startup packet.

- **GSS encryption request** (protocol code `NEGOTIATE_GSS_CODE`) — analogous to SSL negotiation; the server responds with `'G'` or `'N'`. On success, a GSSAPI handshake follows. The server then sets `gss_done`.

- **Cancel request** (protocol code `CANCEL_REQUEST_CODE`) — the packet contains a backend PID and cancel key. `processCancelRequest()` signals the target backend. The function then returns an error to end the connection.

- **Regular startup packet** — a length-prefixed buffer of null-terminated `name=value` pairs. The parser extracts `database`, `user`, `options`, `replication`, and GUC options. Unknown options beginning with `_pq_.` are protocol-level extensions. The parser treats the rest as GUC parameters. It stores them for `InitPostgres()` to process later.

## Protocol Version Negotiation

The server calls `SendNegotiateProtocolVersion()` when the client requests a protocol minor version newer than what the server supports, or when the client included protocol options the server does not recognize. The message (`PqMsg_NegotiateProtocolVersion`) tells the client the highest minor version the server speaks. It also lists any unrecognized option names. This lets clients use optional options without requiring a full reconnect on servers that do not support them.

## Connection Acceptance Check

After successfully parsing the startup packet, `BackendInitialize()` checks the `CAC_state` and emits a `FATAL` error with the appropriate message:

| State | Message |
|---|---|
| `CAC_STARTUP` | database system is starting up |
| `CAC_NOTCONSISTENT` | recovery state not yet consistent (hot standby) |
| `CAC_SHUTDOWN` | database system is shutting down |
| `CAC_RECOVERY` | database system is in recovery mode |
| `CAC_TOOMANY` | too many clients already |

This placement — after the startup packet is read but before authentication — allows tools like `pg_isready` to receive a meaningful error code even when the server is not fully operational.

## Connection Logging

The `log_connections` GUC controls whether the server logs connection events. In PostgreSQL 17 and earlier it was a simple boolean — enabled or disabled for all connection events together. **PostgreSQL 18** changed it to a string-list GUC that names individual events:

| Option | Logged when |
|---|---|
| `receipt` | The connection is received (remote host/port known) |
| `authentication` | Authentication completes |
| `authorization` | The database and role are authorized |
| `setup_durations` | Connection setup finishes, including timing of each phase |
| `all` | All of the above |
| `on` | Backwards-compatible alias for `receipt,authentication,authorization` |

`BackendInitialize()` logs the `receipt` event immediately after it resolves the remote address, before authentication. This way, the event appears even for connections that are later rejected. `auth.c` logs the `authentication` event. `postinit.c` logs the `authorization` event. `postgres.c` logs the `setup_durations` event when the backend enters its main loop. This split lets operators log just the intake (`receipt`) without the noise of every authentication detail, or the reverse.

## Safety Invariant

A key design constraint is that `BackendInitialize()` must not touch shared memory before it receives and validates the startup packet. The SIGTERM handler (`process_startup_packet_die()`) and the startup packet timeout handler (`StartupPacketTimeoutHandler()`) both call `_exit(1)` directly rather than `proc_exit()`. This is safe precisely because no shared-memory cleanup is needed. If the code had touched shared memory, the handlers would need to run atexit callbacks. Running atexit callbacks is unsafe from signal context.

After `BackendInitialize()` returns, `InitProcess()` and `InitPostgres()` access shared memory.

## Related Topics

- [[architecture/backend-initialization]] — the full backend initialization sequence after startup packet handling
- [[architecture/client-connection]] — the overall client connection lifecycle
- [[architecture/connection-launch]] — how the postmaster forks and hands off to BackendMain
- [[architecture/startup-sequence]] — the broader server startup sequence

---
title: "LISTEN / NOTIFY"
aliases:
  - "LISTEN"
  - "NOTIFY"
  - "pg_notify"
  - "Async Notification"
  - "asyncQueueControl"
tags:
  - theme/wire-protocol
source_files:
  - src/backend/commands/async.c
  - src/include/commands/async.h
symbols:
  - Async_Notify
  - Async_Listen
  - Async_Unlisten
  - asyncQueueControl
  - AsyncQueueEntry
  - NotifyMyFrontEnd
  - ProcessIncomingNotify
  - AtCommit_Notify
  - AtAbort_Notify
  - SignalBackends
---

# LISTEN / NOTIFY

PostgreSQL's `LISTEN` / `NOTIFY` mechanism provides lightweight publish-subscribe messaging between database sessions. A backend issues `LISTEN channel` to subscribe. Any session issues `NOTIFY channel [, payload]` to publish. All listening backends receive an asynchronous notification when they next reach a safe checkpoint in their processing loop.

All implementation lives in `src/backend/commands/async.c` (~2 500 lines).

## Core objects

### asyncQueueControl (shared memory)

```c
typedef struct AsyncQueueControl
{
    QueuePosition head;           /* head of queue (next slot to write) */
    QueuePosition tail;           /* global tail: min of all reader positions */
    int           stopPage;       /* last page on disk (for wraparound) */
    BackendId     firstListener;  /* head of listener linked list */
    pg_time_t     lastQueueFillWarn; /* for throttle warnings */
    QueueBackendStatus backend[FLEXIBLE_ARRAY_MEMBER];
        /* one entry per MaxBackends slot, plus one sentinel */
} AsyncQueueControl;
```

`asyncQueueControl` lives in shared memory. It is the global coordination object: `head` and `tail` track the byte range of unread notifications in the queue pages stored in `pg_notify/`.

### Queue pages (pg_notify/)

Notifications are stored in 8 KB SLRU-style pages under `PGDATA/pg_notify/`. Each `AsyncQueueEntry` is variable-length:

```c
typedef struct AsyncQueueEntry
{
    int32   length;     /* total length of entry, including this field */
    Oid     dboid;      /* database OID — other-db notifications are ignored */
    TransactionId xid;  /* XID of notifying transaction (informational) */
    int32   srcPid;     /* PID of notifier */
    char    data[FLEXIBLE_ARRAY_MEMBER]; /* channel\0payload\0 */
} AsyncQueueEntry;
```

PostgreSQL recycles a page once every listening backend has read past its boundary.

### Per-listener position

Each backend has a `QueueBackendStatus` in `asyncQueueControl->backend[]`:

```c
typedef struct QueueBackendStatus
{
    QueuePosition pos;     /* how far this backend has read */
    BackendId     nextListener; /* linked list of listeners */
} QueueBackendStatus;
```

The global `tail` is `min(pos)` across all listeners.

## LISTEN

```sql
LISTEN channel_name;
```

`Async_Listen()` (`async.c`):
1. Checks for a duplicate entry in `listenChannels` (a backend-local list).
2. Adds the channel name to `listenChannels`.
3. Commit registration (`Exec_ListenCommit()`): at transaction commit, the backend registers itself as a listener by recording `MyBackendId` in `asyncQueueControl->backend[]` and updating the linked list.

Multiple `LISTEN` calls for the same channel are idempotent within a session.

`UNLISTEN channel` removes the entry; `UNLISTEN *` removes all. On session exit, the backend automatically calls `Async_UnlistenAll()` during cleanup.

## NOTIFY

```sql
NOTIFY channel_name;
NOTIFY channel_name, 'optional payload';
-- or equivalently:
SELECT pg_notify('channel_name', 'payload');
```

`NOTIFY` does **not** send the notification immediately. It accumulates pending notifications in a backend-local list (`pendingNotifies`).

### AtCommit_Notify

When the transaction commits, `CommitTransaction()` calls `AtCommit_Notify()`. It:

1. Iterates `pendingNotifies`.
2. Queue write (`queue_listen_signal()`): writes each pending notification as an `AsyncQueueEntry` into the `pg_notify/` pages, under an exclusive `AsyncQueueLock`.
3. Head advancement: moves `asyncQueueControl->head` forward to reflect the newly written entries.
4. Listener signalling (`SignalBackends()`): every backend whose listener position is behind the new head receives `PROCSIG_NOTIFY_INTERRUPT`.

Notification delivery is tied to commit. A `NOTIFY` inside a transaction that later rolls back sends no notification. `AtAbort_Notify()` simply clears `pendingNotifies`.

### Deduplication within a transaction

If the same `(channel, payload)` pair appears multiple times in `pendingNotifies` within a single transaction, PostgreSQL queues only one notification at commit time. This is a deliberate optimization: `NOTIFY` is idempotent within a transaction.

## Receiving notifications

### PROCSIG_NOTIFY_INTERRUPT

`SignalBackends()` sets the `PROCSIG_NOTIFY_INTERRUPT` flag in the target backend's `PGPROC` and sends `SIGUSR1`. The signal handler sets `notifyInterruptPending = true` and a `pg_atomic` flag, then returns.

The backend checks `notifyInterruptPending` at safe points:
- Between commands in `PostgresMain()`
- At `WaitLatch()` returns in idle loops
- At `CHECK_FOR_INTERRUPTS()`

### Draining the Notification Queue

`ProcessIncomingNotify()` runs when the backend observes `notifyInterruptPending`:

1. Takes `AsyncQueueLock` shared.
2. Scans from its current `pos` to `asyncQueueControl->head`.
3. Database and channel filtering: for each `AsyncQueueEntry` whose `dboid` matches the current database, it checks the entry's channel name against `listenChannels`.
4. Client delivery (`NotifyMyFrontEnd()`): on a match, it forwards the notification to the frontend with channel, payload, and source PID.
5. Position advancement: updates `backend[MyBackendId].pos` to reflect how far the backend has read.
6. Possibly advances the global `tail` if this backend was the straggler.

### Delivering the Notification to the Client

`NotifyMyFrontEnd()` sends a `NotificationResponse` ('A') message to the client immediately:

```
'A'  int32(msglen)  int32(pid)  cstring(channel)  cstring(payload)
```

libpq clients receive this message asynchronously between query responses and deliver it via the `PQnotifies()` interface.

## Queue capacity and throttling

The queue is finite. Suppose a notifier's commit would advance `head` so far that the oldest listener's `pos` falls more than `QUEUE_MAX_PAGE` pages behind. In that case, `queue_listen_signal()` blocks until the slow listener catches up. It emits a periodic warning:

```
WARNING:  pg_notify queue is XX% full
```

`QUEUE_MAX_PAGE` defaults to 16 384 pages × 8 KB = 128 MB of queue space. A backend that is connected but not calling into PostgreSQL regularly (e.g., stuck in application code) can hold back the tail and eventually block notifiers.

Suppose a listener falls so far behind that its position would drop off the recycled portion of the queue. PostgreSQL then forcibly unregisters it. On its next notify check, it receives an error: `ERROR: too many notifications in the queue`.

## pg_notification_queue_usage

```sql
SELECT pg_notification_queue_usage();
-- returns fraction of queue in use, e.g. 0.03 (3%)
```

Values near 1.0 indicate a slow or disconnected listener is blocking queue advancement.

## Interaction with connection poolers

Connection poolers that multiplex many clients over fewer server connections need to be aware of `LISTEN`/`NOTIFY`:

- PostgreSQL delivers notifications to the **server-side session**, not directly to clients. A transaction-mode pooler may route a client's `LISTEN` to one server connection but deliver the notification to a different client's command cycle.
- PgBouncer in session mode handles `LISTEN`/`NOTIFY` correctly because the server connection is dedicated for the session lifetime.
- Transaction-mode pooling is generally incompatible with `LISTEN`/`NOTIFY`.

## See also

- [[subsystems/transactions/begin-commit-rollback]] — AtCommit_Notify called from CommitTransaction
- [[subsystems/wire-protocol]] — 'A' (NotificationResponse) message format
- [[architecture/process-architecture]] — PROCSIG mechanism and signal delivery between processes

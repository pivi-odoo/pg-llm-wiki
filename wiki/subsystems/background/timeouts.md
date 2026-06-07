---
title: "Timeout Infrastructure"
aliases:
  - statement_timeout
  - lock_timeout
  - idle_in_transaction_session_timeout
  - SIGALRM
  - timeout multiplexing
source_files:
  - src/backend/utils/misc/timeout.c
  - src/include/utils/timeout.h
symbols:
  - TimeoutId
  - timeout_params
  - InitializeTimeouts
  - RegisterTimeout
  - enable_timeout_after
  - enable_timeout_at
  - enable_timeout_every
  - disable_timeout
  - disable_all_timeouts
  - handle_sig_alarm
  - schedule_alarm
---

PostgreSQL backends use a single OS-level mechanism — the `SIGALRM` signal — to implement all time-based interruptions. The timeout infrastructure in `timeout.c` multiplexes multiple logical timeouts onto this single signal. It maintains a priority queue of pending deadlines and fires the appropriate handler when each one expires.

## Design: One Signal, Many Timeouts

The OS `setitimer(ITIMER_REAL, ...)` call can only schedule one pending alarm at a time. When multiple timeouts are active simultaneously (e.g. a statement timeout and a lock timeout), the backend must arrange to receive `SIGALRM` at the nearest deadline, identify which timeouts are due, fire their handlers, and reschedule the alarm for the next outstanding deadline.

`timeout_params` (`timeout.c`) is the per-timeout record:

```c
typedef struct timeout_params
{
    TimeoutId   index;           /* priority order for tie-breaking */
    volatile bool active;        /* currently in the active queue */
    volatile bool indicator;     /* set to true when timeout fires */
    timeout_handler_proc timeout_handler;
    TimestampTz start_time;
    TimestampTz fin_time;        /* when it is due to fire */
    int         interval_in_ms;  /* > 0 for repeating timeouts */
} timeout_params;
```

The active queue is a small array sorted by `fin_time`. When a `SIGALRM` arrives, `handle_sig_alarm()` walks the queue, fires all handlers whose `fin_time` has passed, and calls `schedule_alarm()` for the next remaining entry.

## The alarm_enabled Gate

A subtle race condition exists between updating shared state and the signal handler running. If the handler fires while the main code is rearranging the queue, it could read a partially-updated structure. `timeout.c` avoids this with a `volatile sig_atomic_t alarm_enabled` flag. Any code that modifies the queue sets `alarm_enabled = false` first (`disable_alarm()`), makes its changes, then calls `schedule_alarm()`. `schedule_alarm()` re-enables the flag and calls `setitimer()` if needed.

This scheme avoids disabling the `SIGALRM` signal at the OS level on every queue update (which would require `sigprocmask()` syscalls). Instead, the handler checks `alarm_enabled` and does nothing if it is false. The small risk is a wasted interrupt, not a missed one.

## The signal_pending Optimization

On high-throughput workloads where `statement_timeout` is configured but rarely reached, the backend would otherwise call `setitimer()` on every query start and end — thousands of syscalls per second. `timeout.c` avoids this with a `signal_pending` flag: if a `SIGALRM` is already scheduled for a time at or before the needed deadline, `schedule_alarm()` returns without issuing a new `setitimer()` call. The existing signal will fire, and the handler will reschedule as needed. The cost of a single extra interrupt is much less than the cost of per-query syscalls.

## Timeout IDs and Priorities

`TimeoutId` (`timeout.h`) is an enum whose ordinal value doubles as priority — when two timeouts have identical `fin_time`, the one with the lower enum value fires first:

| `TimeoutId` | GUC / trigger | Behavior when fired |
|---|---|---|
| `STARTUP_PACKET_TIMEOUT` | `authentication_timeout` | Terminates a connection that hasn't sent a startup packet in time |
| `DEADLOCK_TIMEOUT` | `deadlock_timeout` | Triggers the deadlock detector after waiting that long for a lock |
| `LOCK_TIMEOUT` | `lock_timeout` | Raises an error if a lock wait exceeds the limit |
| `STATEMENT_TIMEOUT` | `statement_timeout` | Cancels the running query |
| `STANDBY_DEADLOCK_TIMEOUT` | — | Conflicts on a hot standby |
| `STANDBY_TIMEOUT` | `max_standby_streaming_delay` | Terminates a conflicting standby query |
| `STANDBY_LOCK_TIMEOUT` | — | Lock conflict on standby |
| `IDLE_IN_TRANSACTION_SESSION_TIMEOUT` | `idle_in_transaction_session_timeout` | Terminates a session that has been idle inside a transaction too long |
| `IDLE_SESSION_TIMEOUT` | `idle_session_timeout` | Terminates a session idle outside a transaction |
| `IDLE_STATS_UPDATE_TIMEOUT` | — | Flushes cumulative statistics for idle sessions |
| `CLIENT_CONNECTION_CHECK_TIMEOUT` | `client_connection_check_interval` | Checks whether the client socket is still connected |
| `STARTUP_PROGRESS_TIMEOUT` | `startup_progress_interval` | Logs a message if startup recovery is taking long |

Extensions can register their own timeouts using `RegisterTimeout(USER_TIMEOUT, handler)`, which allocates an ID from the `USER_TIMEOUT..MAX_TIMEOUTS` range.

## Repeating Timeouts

Periodic timeouts (used for the stats flush) set `interval_in_ms > 0`. When `handle_sig_alarm()` fires a periodic timeout, it re-enqueues it at `fin_time + interval_in_ms`. To avoid drift, `timeout.c` computes the new deadline from the *intended* fire time rather than the actual wall-clock time at which the handler ran. If an entire cycle was skipped (the handler ran very late), it computes the next deadline from the actual time instead, to avoid an immediate re-fire.

## What Happens When a Timeout Fires

The handler for `STATEMENT_TIMEOUT` and `LOCK_TIMEOUT` calls `StatementTimeoutHandler()` and `LockTimeoutHandler()`, which set `InterruptPending` and `QueryCancelPending` (for statement timeout) or `LockTimeoutPending` (for lock timeout) as volatile flags. The query execution loop checks `QueryCancelPending` at safe points and raises an error via `ProcessInterrupts()`. This is why statement timeouts don't interrupt a query mid-instruction — they interrupt it at the next interruption check point.

`IDLE_IN_TRANSACTION_SESSION_TIMEOUT` and `IDLE_SESSION_TIMEOUT` call handlers that set `IdleInTransactionSessionTimeoutPending` or `IdleSessionTimeoutPending`, checked by the main loop in `postgres.c` between commands.

## Setting and Clearing Timeouts

```c
/* Set a one-shot timeout N milliseconds from now */
enable_timeout_after(STATEMENT_TIMEOUT, statement_timeout);

/* Cancel it before it fires */
disable_timeout(STATEMENT_TIMEOUT, false);

/* Set multiple timeouts atomically (one setitimer call) */
EnableTimeoutParams timeouts[] = {
    {LOCK_TIMEOUT, TMPARAM_AFTER, lock_timeout},
    {STATEMENT_TIMEOUT, TMPARAM_AFTER, statement_timeout},
};
enable_timeouts(timeouts, 2);
```

`disable_all_timeouts()` clears every active timeout without issuing a `setitimer()` call. It leaves any pending OS alarm in place — the next `SIGALRM` will find nothing to do and reschedule to empty. This is cheaper than an extra syscall.

## Related Topics

- [[subsystems/locking/deadlock|Deadlock Detection]]
- [[subsystems/transactions/transaction-lifecycle|Transaction Lifecycle]]
- [[troubleshooting/lock-waits|Lock Waits and Deadlocks]]
- [[troubleshooting/slow-queries|Slow Query Investigation]]

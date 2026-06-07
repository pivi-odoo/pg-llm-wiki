---
title: "Async Executor Node Protocol"
aliases:
  - "async nodes"
  - "execAsync"
  - "asynchronous executor"
tags:
  - theme/extensibility
source_files:
  - src/backend/executor/execAsync.c
  - src/include/executor/execAsync.h
  - src/backend/executor/nodeAppend.c
  - src/backend/executor/nodeForeignscan.c
  - src/include/nodes/execnodes.h
symbols:
  - AsyncRequest
  - ExecAsyncRequest
  - ExecAsyncConfigureWait
  - ExecAsyncNotify
  - ExecAsyncResponse
  - ExecAsyncRequestDone
  - ExecAsyncRequestPending
  - ExecAsyncAppendResponse
  - ExecAsyncForeignScanRequest
  - ExecAsyncForeignScanConfigureWait
  - ExecAsyncForeignScanNotify
---

The async executor node protocol lets a single backend overlap I/O waits across multiple plan nodes without spawning additional processes. In the standard Volcano pull model, a node that is waiting on a remote result blocks the entire query. With async execution, an `Append` node can keep several `ForeignScan` children in flight simultaneously, collecting tuples from whichever child becomes ready first.

## The problem with pull-only execution

The [[subsystems/executor/overview|executor]] normally operates as a strict call stack: the root asks its child for a tuple, the child asks its grandchild, and so on, all the way to a leaf scan node. If a leaf must wait on a network round-trip — for example, fetching rows from a remote PostgreSQL server via `postgres_fdw` — the entire chain stalls until that response arrives. An `Append` node over ten foreign tables can fetch from only one remote server at a time even if all ten servers are equally idle and the network is the bottleneck.

Async execution breaks this stall. Instead of blocking on one child, `Append` fires requests at all async-capable children concurrently. It registers the underlying file-descriptor events it needs to wait on. It then returns whichever result becomes available first. The remaining children continue to make forward progress in the background while the executor delivers the ready tuple upstream.

## Roles: consumer and producer

The protocol involves exactly two node roles:

- **Consumer** — currently only `Append` (`nodeAppend.c`). Maintains a set of outstanding requests, collects results, and drives the wait-event loop.
- **Producer** — currently only `ForeignScan` (`nodeForeignscan.c`), acting as a delegate for the FDW's async callbacks. A `ForeignScan` is eligible only when its plan node's `async_capable` flag is set and EvalPlanQual is not active (re-evaluation of a single row during conflict checking uses the synchronous path).

`execAsync.c` is a thin dispatch layer with no logic of its own. Every function switches on `nodeTag` and calls into the node-specific implementation, returning an error if it encounters an unexpected node type. This keeps async capability an opt-in feature rather than a general contract of all plan nodes.

## The AsyncRequest struct

Every outstanding async operation is represented by an `AsyncRequest` (`src/include/nodes/execnodes.h`):

```c
typedef struct AsyncRequest
{
    struct PlanState *requestor;    /* Node that wants a tuple */
    struct PlanState *requestee;    /* Node from which a tuple is wanted */
    int         request_index;      /* Scratch space for requestor */
    bool        callback_pending;   /* Callback is needed */
    bool        request_complete;   /* Request complete, result valid */
    TupleTableSlot *result;         /* Result (NULL or empty slot if done) */
} AsyncRequest;
```

`Append` allocates one `AsyncRequest` per async-capable child during `ExecInitAppend()` and reuses them across the life of the query. `request_index` is the position of the child in the Append's subplan array, giving `ExecAsyncAppendResponse()` a direct index back into the Append's result and bookkeeping arrays. The `result` field is `NULL` when the request is pending. It points to a `TupleTableSlot` (possibly empty, signalling end-of-stream) when `request_complete` is true.

## The four protocol functions

`execAsync.h` exposes four functions that form the complete async interface:

### Requesting a tuple from a producer

Called by the consumer to ask a producer for a tuple (`ExecAsyncRequest()`, `execAsync.c`). The producer may respond immediately — by calling `ExecAsyncRequestDone(areq, slot)` before returning — or it may queue the request for later by calling `ExecAsyncRequestPending(areq)`. `ExecAsyncRequest` calls `ExecAsyncResponse` unconditionally on return, so `ExecAsyncResponse` delivers an immediate result to the requestor in the same call frame.

For `ForeignScan`, this delegates to `fdwroutine->ForeignAsyncRequest(areq)`, which for `postgres_fdw` sends the query to the remote server in non-blocking mode without waiting for any response.

### Registering wait events before blocking

Called by the consumer when it is about to block (`ExecAsyncConfigureWait()`, `execAsync.c`). Each producer with `callback_pending = true` gets a chance to add its file descriptor to the consumer's `WaitEventSet` via a single call of the form:

```c
AddWaitEventToSet(set, WL_SOCKET_READABLE, fd, NULL, areq);
```

The `user_data` argument is the `AsyncRequest *` pointer, which the wait-event loop uses to route the notification back to the correct request. For `ForeignScan` this delegates to `fdwroutine->ForeignAsyncConfigureWait(areq)`.

### Resuming a producer after its wait event fires

Called by the consumer when `WaitEventSetWait` fires on a socket that a producer registered (`ExecAsyncNotify()`, `execAsync.c`). The producer resumes where it left off and either calls `ExecAsyncRequestDone` (result ready) or `ExecAsyncRequestPending` again (still waiting). `ExecAsyncNotify` calls `ExecAsyncResponse` on return, just as `ExecAsyncRequest` does. For `ForeignScan` this delegates to `fdwroutine->ForeignAsyncNotify(areq)`.

### Delivering a completed result to the consumer

Called internally whenever a producer has set `request_complete` (`ExecAsyncResponse()`, `execAsync.c`). The consumer (`Append`) inspects `areq->result`. If the slot is non-empty, it stores the slot in `as_asyncresults[]` and marks the child as needing a new request. If the slot is empty (end-of-stream), it decrements `as_nasyncremain` and retires the child. The consumer never calls this function directly — `ExecAsyncRequest` and `ExecAsyncNotify` always invoke it at their tail.

## How Append drives the event loop

`ExecInitAppend` classifies each child plan as async-capable (those with `async_capable && !epq_active`) or synchronous, and allocates one `AsyncRequest` per async child. At the start of execution, `ExecAppendAsyncBegin` issues an `ExecAsyncRequest` to every valid async child simultaneously, priming the pipeline.

On each call to `ExecAppend`:

1. If any `as_asyncresults` are buffered, `ExecAppend` returns one immediately.
2. Otherwise, `ExecAppendAsyncRequest` issues new requests to any child that completed its previous request and needs a new one. It then returns a buffered result if one is now available.
3. If no result is ready and async children remain (`as_nasyncremain > 0`), `ExecAppend` calls `ExecAppendAsyncEventWait`. It builds a transient `WaitEventSet` and calls `ExecAsyncConfigureWait` for each child with `callback_pending`. It then calls `WaitEventSetWait`. If sync subplans are still active, the timeout is 0 (poll only). If all sync plans are exhausted, the timeout is −1 (block until an event fires). When `WL_SOCKET_READABLE` events arrive, it delivers `ExecAsyncNotify` to each affected request.
4. The Append's `as_syncdone` flag lets the node transition cleanly from a mixed sync+async phase to an async-only phase without restructuring the loop.

`ExecAppendAsyncEventWait` adds the process latch (`MyLatch`) to the `WaitEventSet` after all `ExecAsyncConfigureWait` calls, ensuring `CHECK_FOR_INTERRUPTS()` fires on signals even when blocking on remote I/O. An implementation note in the source explains why: `postgres_fdw` checks `GetNumRegisteredWaitEvents(set) == 1` to detect whether any other FD events have been registered. So the latch must be added last. This ordering is a fixed protocol invariant.

## How ForeignScan implements the producer side

`ExecInitForeignScan` sets `ps.async_capable` to true when the plan node's `async_capable` flag is set and EvalPlanQual is not active. EvalPlanQual is a row-level conflict check that re-evaluates individual rows synchronously. Running async I/O inside it would complicate locking, and it is not needed.

The three FDW callbacks (`ForeignAsyncRequest`, `ForeignAsyncConfigureWait`, `ForeignAsyncNotify`) are optional fields in `FdwRoutine`. If an FDW does not implement them, the planner cannot mark it `async_capable` in the first place. For FDWs that do implement them — `postgres_fdw` is the canonical example — the implementation sends a query to the remote server in non-blocking mode. It registers the underlying libpq socket for readability, and resumes fetching rows when notified.

## Declaring async capability

A plan node advertises async capability through `Plan.async_capable` (`src/include/nodes/plannodes.h`), a boolean flag set by the planner. The corresponding runtime flag is `PlanState.async_capable` (`src/include/nodes/execnodes.h`), set during init. `Append` looks at `initNode->async_capable` to decide which children to put in the async set. `ForeignScan` derives its flag from the plan node and the EvalPlanQual check. No other plan node type currently sets this flag on either the plan or the state.

## Relationship to parallel query

Async execution and [[subsystems/executor/parallel|parallel query]] address different bottlenecks and operate through entirely different mechanisms.

Parallel query spawns additional processes, each running an independent copy of a plan subtree. Tuples cross process boundaries through shared-memory queues. The benefit is parallelising CPU-bound work — scanning large tables or building hash tables simultaneously on multiple cores.

Async execution uses a single process and a single plan tree. It forks no processes. The benefit is overlapping I/O latency across multiple network connections: while one remote server is processing a query, another server's result can arrive and be consumed. The `Gather` node used for parallelism is a static fan-in of process streams. The `Append` node used for async is an event-driven fan-in of socket events.

The two mechanisms are not mutually exclusive at the plan level, but an async `Append` and a parallel `Append` are different code paths within `nodeAppend.c`.

## Practical impact

The primary use case is `postgres_fdw` queries that join or union foreign tables residing on different remote servers. Without async execution, fetching rows from each server is sequential: the query waits for server A to return all rows before sending any query to server B. With async execution, queries to all servers are in flight simultaneously. The local backend stitches results together as network responses arrive.

For foreign tables on the same server, the benefit disappears because the same connection serialises the queries anyway. For purely local tables, the planner never sets the `async_capable` flag, so the code path is never entered. The feature is opt-in and confined to `ForeignScan` producers. So it has no overhead for queries that do not use it.

## Related Topics

- [[subsystems/executor/overview|Executor overview]] — the Volcano pull model that async execution extends
- [[subsystems/executor/parallel|Parallel query]] — multi-process fan-out, contrasted with single-process async I/O
- [[subsystems/executor/fdw|Foreign data wrappers]] — the FDW callback API including `ForeignAsyncRequest`, `ForeignAsyncConfigureWait`, and `ForeignAsyncNotify`

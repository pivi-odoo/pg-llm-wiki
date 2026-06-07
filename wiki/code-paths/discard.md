---
title: "DISCARD"
aliases:
  - DISCARD ALL
  - DISCARD PLANS
  - DISCARD SEQUENCES
  - DISCARD TEMP
  - session reset
source_files:
  - src/backend/commands/discard.c
  - src/include/commands/discard.h
  - src/include/nodes/parsenodes.h
symbols:
  - DiscardCommand
  - DiscardAll
  - DiscardMode
  - DiscardStmt
  - ResetPlanCache
  - ResetTempTableNamespace
  - ResetSequenceCaches
---

`DISCARD` resets session-local state accumulated during a database session without closing the connection. It is PostgreSQL's answer to the question connection poolers face every time they return a backend to the pool: how do you guarantee that the next client starts with a clean slate? The command has no SQL standard equivalent. It exists precisely because the individual operations it bundles — closing cursors, invalidating plans, dropping temp tables, releasing advisory locks, resetting GUCs — would otherwise require multiple round trips.

## The four variants

`DISCARD` dispatches on a single `DiscardMode` enum stored in the parse node (`DiscardStmt.target`, `parsenodes.h`). The public entry point `DiscardCommand()` in `discard.c` routes each value to a separate implementation.

**DISCARD PLANS** calls `ResetPlanCache()` (`plancache.c`). `ResetPlanCache()` walks the list of all `CachedPlanSource` entries. It marks each one invalid by setting `plansource->is_valid = false`. The prepared statement entries themselves — the `PreparedStatement` structs in the `prepared_queries` hash — are untouched. On the next `EXECUTE`, the planner generates a fresh plan from scratch. This is a soft invalidation: the statement name remains valid and usable. `ResetPlanCache()` explicitly exempts transaction control statements like `ROLLBACK` from invalidation. These statements may need to execute in an aborted transaction. Replanning is impossible in that state.

**DISCARD SEQUENCES** calls `ResetSequenceCaches()` (`sequence.c`). This function destroys the backend-local `seqhashtab` hash table entirely via `hash_destroy()`. It also nulls out both the table pointer and `last_used_seq`. This variant permanently abandons any sequence values that earlier `nextval()` calls preallocated. The next `nextval()` fetches a fresh block from the server. This means this variant guarantees sequence gaps whenever it runs — the preallocated but unused values are gone.

**DISCARD TEMP** calls `ResetTempTableNamespace()` (`namespace.c`). This function drops all temp tables owned by this session. If `myTempNamespace` is a valid OID, it invokes `RemoveTempRelations()` to perform the actual drops.

**DISCARD ALL** is not a simple alias for the other three. It resets the full set of session-local state. It does so in a carefully chosen order. It also enforces one constraint the sub-variants do not: `DiscardAll()` explicitly calls `PreventInTransactionBlock()` to prohibit the statement inside a transaction block. The comment in `discard.c` acknowledges this is "arguably inconsistent" with the sub-variants, but the reasoning is practical. Running `DISCARD ALL` inside a transaction leaves the transaction uncommitted. This is almost never the programmer's intent.

## What DISCARD ALL resets and in what order

`DiscardAll()` (`discard.c`) performs these operations in sequence:

1. **Close all open portals** (`PortalHashTableDeleteAll()`). Portals are server-side cursors. This step comes first because closing a portal can invoke user-defined code — trigger functions, cleanup callbacks. That code should run before `DiscardAll()` wipes session-local state.
2. **Reset session authorization** (`SetPGVariable("session_authorization", NIL, false)`). Uses the GUC machinery to restore the default, equivalent to `SET SESSION AUTHORIZATION DEFAULT`.
3. **Reset all GUC parameters** (`ResetAllOptions()`). Equivalent to `RESET ALL` — restores every GUC parameter to its session default.
4. **Drop all prepared statements** (`DropAllPreparedStatements()`). Unlike `DISCARD PLANS`, this removes the statement entries entirely from `prepared_queries`. See [[code-paths/prepared-statements]] for the full lifecycle of prepared statements.
5. **Remove all LISTEN subscriptions** (`Async_UnlistenAll()`). Equivalent to `UNLISTEN *`.
6. **Release all advisory locks** (`LockReleaseAll(USER_LOCKMETHOD, true)`). Equivalent to `SELECT pg_advisory_unlock_all()`.
7. **Invalidate all cached plans** (`ResetPlanCache()`).
8. **Drop all temp tables** (`ResetTempTableNamespace()`).
9. **Reset sequence caches** (`ResetSequenceCaches()`).

The documentation describes `DISCARD ALL` as equivalent to issuing `CLOSE ALL`, `SET SESSION AUTHORIZATION DEFAULT`, `RESET ALL`, `DEALLOCATE ALL`, `UNLISTEN *`, `SELECT pg_advisory_unlock_all()`, `DISCARD PLANS`, `DISCARD TEMP`, and `DISCARD SEQUENCES` in sequence. The C implementation matches this semantics but reorders the steps to put portal teardown first.

## What DISCARD does not reset

`DISCARD ALL` cannot run inside a transaction, so it never needs to address open transactions. It does not roll back pending changes, release row-level locks acquired by DML, or close the physical connection. The normal transaction end releases explicit locks held through `LOCK TABLE` at statement level, not DISCARD.

`DISCARD` is not a substitute for `ROLLBACK`. If a client abandons a transaction and the pooler sends `DISCARD ALL`, the pooler is responsible for rolling back first. `DISCARD ALL` will refuse to run while a transaction is in progress.

## Relationship to RESET ALL

`RESET ALL` only resets GUC parameters. It does nothing to cached plans, prepared statements, temp tables, sequence caches, portals, advisory locks, or LISTEN subscriptions. `DISCARD ALL` is a strict superset of `RESET ALL` — step 3 in `DiscardAll()` is exactly a `RESET ALL`. Developers who use `SET` to override parameters for a single session can use `RESET ALL` for targeted cleanup. `DISCARD ALL` is for the full-session wipe that connection poolers need.

## SET LOCAL and transaction-scoped state

When the enclosing transaction ends, it automatically rolls back parameters set with `SET LOCAL` and other transaction-scoped state. They do not require explicit cleanup by the client or the pooler. `DISCARD` targets state that survives transaction boundaries: plans, temp tables, sequence caches, advisory locks, and session-level GUC changes made with plain `SET`. This distinction matters for poolers operating in transaction mode. Each client transaction may be routed to a different backend. In that mode, per-transaction state is already isolated. `DISCARD ALL` is not needed between transactions.

## Connection pooling context

In session-mode pooling (where a client maps to a single backend for the duration of its connection), the pooler must reset that backend before assigning it to a new client. PgBouncer uses `server_reset_query = DISCARD ALL` for this purpose — see [[architecture/connection-pooling-impact]] for the full discussion. `DISCARD ALL` is the only single statement that can guarantee a clean slate across all the session-local state categories.

The statement does carry overhead. `ResetPlanCache()` walks the entire saved plan list. `ResetTempTableNamespace()` issues DDL to drop tables. `PortalHashTableDeleteAll()` may invoke user-defined cleanup code. For workloads with very short transactions and high connection churn, this cost is measurable. That is why transaction-mode poolers avoid `DISCARD ALL` entirely and rely instead on per-transaction state isolation.

## Dispatch and access control

`T_DiscardStmt` reaches `DiscardCommand()` through `standard_ProcessUtility()` (`utility.c`). Before calling `DiscardCommand()`, the dispatcher calls `CheckRestrictedOperation("DISCARD")`. This call blocks the statement inside walsender processes and other restricted contexts. This gate applies to all four variants uniformly. The code's own comment raises the question of whether sub-variants like `DISCARD PLANS` warrant this restriction, but no exception exists. PostgreSQL logs all DISCARD variants at `LOGSTMT_ALL` level.

`utility.c` also assigns command tags: the four variants produce `CMDTAG_DISCARD_ALL`, `CMDTAG_DISCARD_PLANS`, `CMDTAG_DISCARD_TEMP`, and `CMDTAG_DISCARD_SEQUENCES` respectively.

## Related Topics

- [[code-paths/prepared-statements]] — lifecycle of prepared statements; `DropAllPreparedStatements()` and the `prepared_queries` hash table
- [[subsystems/wire-protocol]] — the extended query protocol's `Close` message, which closes individual portals by name rather than bulk-invalidating
- [[architecture/connection-pooling-impact]] — PgBouncer session mode and why `DISCARD ALL` is the standard reset query

---
title: "Command Tags"
aliases:
  - command tag
  - CommandComplete
  - CommandTag
  - PQcmdStatus
  - PQcmdTuples
  - rows affected
tags:
  - theme/wire-protocol
source_files:
  - src/backend/tcop/cmdtag.c
  - src/include/tcop/cmdtag.h
  - src/include/tcop/cmdtaglist.h
symbols:
  - CommandTag
  - QueryCompletion
  - BuildQueryCompletionString
  - GetCommandTagEnum
  - command_tag_display_rowcount
---

Every SQL command PostgreSQL completes sends a `CommandComplete` message back to the client over the wire protocol. That message carries a command tag — a short ASCII string like `SELECT 5`, `INSERT 0 3`, or `CREATE TABLE` — that identifies which command ran and, for data-modification commands, how many rows were processed. Client libraries surface this as "rows affected". ORMs use it to detect whether an UPDATE actually matched any rows.

## The CommandTag Enum and Behavior Table

Internally, a command tag is not a string but a `CommandTag` enum value — an integer index into a static table defined in `src/include/tcop/cmdtaglist.h`. PostgreSQL includes the table twice with different macro definitions: once to generate the enum constants (`CMDTAG_SELECT`, `CMDTAG_INSERT`, `CMDTAG_UPDATE`, and so on), and once to populate an array of `CommandTagBehavior` structs that record the tag's textual name and three boolean flags.

The three flags are:

- **`display_rowcount`** — whether `BuildQueryCompletionString` should append a row count to the tag name when building the wire-protocol string.
- **`event_trigger_ok`** — whether an event trigger can fire for this command.
- **`table_rewrite_ok`** — whether a `table_rewrite` event trigger can fire (only `ALTER TABLE` and `ALTER MATERIALIZED VIEW` and `ALTER TYPE` set this).

The `QueryCompletion` struct that travels through executor code carries just these two fields: the `CommandTag` enum value and a `uint64` named `nprocessed`. `BuildQueryCompletionString` formats the final wire-protocol string from these two pieces at send time.

`GetCommandTagEnum` performs the reverse lookup — string to enum — by binary-searching the behavior table. PostgreSQL keeps the table in alphabetical order precisely to make that binary search correct; any new tag added to `cmdtaglist.h` must respect that ordering.

## Which Commands Include a Row Count

The `display_rowcount` flag cleanly divides commands into two groups.

Commands that carry a count in their tag:

| Tag | Example |
|-----|---------|
| `SELECT` | `SELECT 42` |
| `INSERT` | `INSERT 0 3` |
| `UPDATE` | `UPDATE 7` |
| `DELETE` | `DELETE 0` |
| `MERGE` | `MERGE 4` |
| `COPY` | `COPY 1000` |
| `FETCH` | `FETCH 25` |
| `MOVE` | `MOVE 10` |

Commands that carry no count — DDL (`CREATE TABLE`, `DROP INDEX`, `ALTER TABLE`), transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`), session utilities (`SET`, `DISCARD`, `RESET`), and procedural commands (`DO`, `CALL`) — emit only their name string with no trailing number.

`TRUNCATE TABLE` is a notable case: it removes all rows but its tag carries no count. The row count is undefined for a truncate, because no per-row processing occurs. As a result, `display_rowcount` is false, and the tag is simply `TRUNCATE TABLE`.

## The INSERT Tag and the Legacy OID Field

The INSERT tag has an unusual two-number format: `INSERT 0 3` rather than `INSERT 3`. The first number was the OID of the newly inserted row. In early PostgreSQL, every table could have system-assigned OIDs. The protocol reflected that by returning the OID on single-row inserts, and `0` for multi-row inserts where no single OID applied.

PostgreSQL 12 removed user-visible OIDs from heap tables. Since then, `BuildQueryCompletionString` unconditionally writes `0` in the OID position for INSERT:

```c
if (tag == CMDTAG_INSERT)
{
    *bufp++ = ' ';
    *bufp++ = '0';
}
*bufp++ = ' ';
bufp += pg_ulltoa_n(qc->nprocessed, bufp);
```

The `0` is a wire-protocol compatibility artifact. Clients that parse `INSERT oid rows` must still handle the two-token format; the first token will always be `0` on any modern server. Developers sometimes see `INSERT 0 1` and wonder what `0` means — it is the vestigial OID slot, always zero since PostgreSQL 12.

## RETURNING Does Not Change the Tag

A DML statement with a `RETURNING` clause delivers result rows through the same `DataRow` / `CommandComplete` message stream as `SELECT`, but the command tag remains the DML tag. `INSERT … RETURNING` completes with `INSERT 0 N`, not `SELECT N`. `UPDATE … RETURNING` completes with `UPDATE N`. `DELETE … RETURNING` completes with `DELETE N`.

This matters because the row count in the tag reflects rows *modified*, not rows *returned*. For a query like `INSERT INTO t SELECT … RETURNING *`, the number in the tag counts inserted rows, which is the same as the number returned — but the tag name signals which operation occurred. An ORM that distinguishes "this was an insert" from "this was a select" by inspecting the tag will see the DML name regardless of whether `RETURNING` was used.

## DO Blocks

Anonymous PL/pgSQL blocks executed with `DO` complete with the tag `DO`. The `display_rowcount` flag is false for `DO`, so no count appears. A `DO` block may execute any number of DML statements internally, but the outer command tag does not surface those counts. The individual inner statements' `CommandComplete` messages appear only if the block issues them through `PERFORM` or explicit DML. Even then, they appear only if the block runs inside an extended-query pipeline that exposes sub-results.

## How Client Libraries Expose Tags

libpq provides two functions over the `PGresult` of a completed command:

- `PQcmdStatus(res)` returns the full tag string as received from the server, e.g. `"INSERT 0 3"` or `"UPDATE 7"`.
- `PQcmdTuples(res)` extracts and returns only the row-count portion as a C string, e.g. `"3"` or `"7"`. For commands with no row count it returns an empty string `""`.

ORMs built on libpq (and on drivers that wrap it) expose `PQcmdTuples` as "rows affected" or "rowcount". A few practical consequences:

- An `UPDATE` that matches no rows returns the tag `UPDATE 0`. `PQcmdTuples` returns `"0"`. ORMs that surface this as a boolean "did anything change?" return false. Debugging an update that "silently does nothing" usually starts here: check the command tag before assuming the query ran correctly.
- A `DELETE` that removes no rows returns `DELETE 0`. Unlike some other databases, PostgreSQL does not raise an error — it completes normally with a zero count.
- A `SELECT` returns a count in its tag (`SELECT N`), but ORMs often ignore `PQcmdTuples` for SELECT, in favour of counting the actual `DataRow` messages received. The two numbers should match, but the tag count is authoritative.
- `COPY` from the client side (`COPY FROM STDIN`) returns `COPY N` where N is the number of rows loaded. This is the standard way to confirm bulk-load row counts without running a separate `COUNT(*)`.

## Related Topics

- [[subsystems/triggers|triggers]] — event triggers fire based on `event_trigger_ok` from the same behavior table
- [[subsystems/wal/overview|WAL]] — DML that produces command tags also writes WAL records; the row count in the tag and the WAL record count are derived from the same executor counter
- [[subsystems/storage/toast|TOAST]] — TOAST storage is transparent to the executor row counter; a single logical row counts as one regardless of TOAST expansion

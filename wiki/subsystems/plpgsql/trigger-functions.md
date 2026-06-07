---
title: "PL/pgSQL Trigger Functions"
aliases:
  - "plpgsql triggers"
  - "RETURNS trigger"
  - "NEW OLD trigger variables"
source_files:
  - src/pl/plpgsql/src/pl_exec.c
  - src/pl/plpgsql/src/pl_comp.c
  - src/backend/commands/trigger.c
symbols:
  - plpgsql_exec_trigger
  - plpgsql_exec_event_trigger
  - PLPGSQL_PROMISE_TG_NAME
  - expanded_record_set_tuple
---

# PL/pgSQL Trigger Functions

A PL/pgSQL trigger function is a function declared `RETURNS trigger`. It cannot be called directly from SQL; the trigger machinery invokes it through the PL/pgSQL call handler `plpgsql_exec_trigger()` (`pl_exec.c`) when a triggering event fires. The same compiled function object can serve multiple trigger definitions on multiple tables.

## Execution entry point

When a trigger fires, `ExecBRInsertTriggers()` (or the equivalent for DELETE/UPDATE) in `trigger.c` calls the PL/pgSQL handler via `FunctionCall2()`. The handler calls `plpgsql_exec_trigger()`, which:

1. Initialises an execution state (`PLpgSQL_execstate`) and copies all datum slots from the compiled function template.
2. Populates the `NEW` and `OLD` record variables from the `TriggerData` event data.
3. Fills promise variables on first access.
4. Runs the function body.
5. Returns a `HeapTuple` to the trigger manager.

The executor always allocates both `NEW` and `OLD` as expanded record variables, even when one is not applicable to the trigger event. An unsupplied record reads all fields as NULL rather than erroring, which lets a single trigger function attach to INSERT, UPDATE, and DELETE without conditionalising on `TG_OP` before accessing the records.

## OLD and NEW

For row-level triggers, `NEW` and `OLD` are `PLpgSQL_rec` variables backed by expanded records built from the triggering table's `TupleDesc`.

| Event | NEW | OLD |
|-------|-----|-----|
| INSERT | new row being inserted | NULL (all fields null) |
| UPDATE | new row version | old row version |
| DELETE | NULL (all fields null) | row being deleted |

`NEW` is the tuple stored in `tg_newtuple` for UPDATE or `tg_trigtuple` for INSERT. `OLD` is always `tg_trigtuple`. For BEFORE UPDATE triggers, the executor explicitly nulls out stored generated columns in `NEW` before entering the function body, because the generated values have not yet been computed — `NEW.generated_col` will read as NULL in a BEFORE UPDATE trigger regardless of what the column ultimately stores.

## TG_* variables

PL/pgSQL implements the special trigger context variables as `PLPGSQL_DTYPE_PROMISE` datums. `exec_eval_simple_expr()` computes their values lazily on first access, dispatching to `plpgsql_exec_eval_datum()` → the promise handler in `pl_exec.c`. This avoids computing values that the function body never reads.

| Variable | Type | Value |
|----------|------|-------|
| `TG_NAME` | `name` | Name of the trigger definition |
| `TG_WHEN` | `text` | `'BEFORE'`, `'AFTER'`, or `'INSTEAD OF'` |
| `TG_LEVEL` | `text` | `'ROW'` or `'STATEMENT'` |
| `TG_OP` | `text` | `'INSERT'`, `'UPDATE'`, `'DELETE'`, or `'TRUNCATE'` |
| `TG_RELID` | `oid` | OID of the table that fired the trigger |
| `TG_TABLE_NAME` | `name` | Name of that table |
| `TG_TABLE_SCHEMA` | `name` | Schema of that table |
| `TG_NARGS` | `integer` | Number of arguments passed to the trigger |
| `TG_ARGV` | `text[]` | Array of trigger arguments (0-indexed) |

`TG_ARGV[0]` through `TG_ARGV[TG_NARGS-1]` contain the literal string arguments specified in `CREATE TRIGGER ... FOR EACH ROW EXECUTE FUNCTION f('arg1', 'arg2')`. These arguments are always text, regardless of the column types involved.

## Return value semantics

The trigger function must end with a `RETURN` statement. What it returns determines whether and how the row is modified:

**BEFORE row trigger:** Return `NEW` to allow the insert or update to proceed with that row. Returning a modified `NEW` substitutes the modified tuple for what gets written to the heap. Returning `NULL` suppresses the operation entirely — no row is inserted, updated, or deleted. This is the mechanism for conditional suppression or transparent row transformation.

**AFTER row trigger:** The trigger manager ignores the return value. Returning `NULL` or `NEW` both leave the already-written row unchanged. The common idiom is `RETURN NULL` or `RETURN NEW` for clarity.

**Per-statement trigger:** Must return `NULL`. Any other return value raises an error. Per-statement triggers have no row to modify and no access to `NEW` or `OLD`.

**INSTEAD OF trigger (on views):** Return `NEW` to signal success. Return `NULL` to silently skip the row. INSTEAD OF triggers are how views implement updatability.

When `plpgsql_exec_trigger()` returns the tuple, it verifies column compatibility between the returned tuple's `TupleDesc` and the triggering relation's descriptor. If they differ, `convert_tuples_by_position()` maps the columns. If the function returns `NEW` or `OLD` unchanged (the original trigger pointers), `plpgsql_exec_trigger()` needs no copy and returns the pointer as-is, saving allocation.

## Writing a trigger function

A minimal BEFORE INSERT trigger that validates a column:

```sql
CREATE FUNCTION check_positive() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.amount <= 0 THEN
        RAISE EXCEPTION 'amount must be positive, got %', NEW.amount;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER check_amount
    BEFORE INSERT OR UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION check_positive();
```

A BEFORE INSERT trigger that auto-stamps a column before it is written:

```sql
CREATE FUNCTION stamp_created_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_at := now();
    RETURN NEW;
END;
$$;
```

Modifying `NEW` directly (field assignment) and then returning it causes PL/pgSQL to write the modified tuple to the heap. The assignment compiles to an `AssignStmt` that updates the expanded record in the `NEW` datum slot; `plpgsql_exec_trigger()` extracts the `HeapTuple` from that expanded record as the return value.

## Event triggers

Event triggers fire on DDL statements rather than on table row changes. A PL/pgSQL event trigger function declares `RETURNS event_trigger`; PostgreSQL invokes it via `plpgsql_exec_event_trigger()` instead of `plpgsql_exec_trigger()`. It has no `NEW`/`OLD`/`TG_OP` variables; instead it accesses `TG_EVENT` (the DDL command tag, e.g. `'CREATE TABLE'`) and `TG_TAG`. Event trigger functions must return `NULL` — they cannot suppress or modify DDL.

## See also

- [[subsystems/plpgsql/overview]] — PL/pgSQL compilation and execution model
- [[subsystems/plpgsql/variable-scoping]] — how NEW, OLD, and TG_* are registered as datums
- [[code-paths/insert]] — the BEFORE/AFTER trigger firing sequence for INSERT
- [[code-paths/update]] — how BEFORE UPDATE triggers interact with generated columns
- [[subsystems/executor/overview]] — trigger execution context within the executor

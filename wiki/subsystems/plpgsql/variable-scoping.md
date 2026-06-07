---
title: "PL/pgSQL Variable Scoping"
aliases:
  - "plpgsql variables"
  - "plpgsql namespaces"
  - "plpgsql DECLARE"
source_files:
  - src/pl/plpgsql/src/pl_comp.c
  - src/pl/plpgsql/src/pl_funcs.c
  - src/pl/plpgsql/src/plpgsql.h
symbols:
  - plpgsql_ns_push
  - plpgsql_ns_pop
  - plpgsql_ns_lookup
  - plpgsql_build_variable
  - PLpgSQL_nsitem
  - PLpgSQL_var
  - PLpgSQL_datum
---

# PL/pgSQL Variable Scoping

PL/pgSQL resolves variable names at compile time using a namespace chain — a linked list of name-to-slot mappings. The compiler builds the chain as it processes each block. Name lookups at runtime use the chain snapshot recorded for each statement at compile time.

## The datum array

The compiler represents every variable, parameter, and special value in a PL/pgSQL function as a `PLpgSQL_datum`, stored in the function's flat `datums[]` array. Each datum has an integer index into this array. Statements reference variables by their datum index, not by name. The datum types relevant to variables are:

- `PLPGSQL_DTYPE_VAR` — a scalar variable (`PLpgSQL_var`), which holds a `Datum` and a null flag.
- `PLPGSQL_DTYPE_REC` — a record variable (`PLpgSQL_rec`), which holds a `HeapTuple` and a `TupleDesc`.
- `PLPGSQL_DTYPE_ROW` — a row variable (`PLpgSQL_row`), a named composite whose columns map to individual scalar datums.
- `PLPGSQL_DTYPE_PROMISE` — a special variable (like `TG_NAME` or `TG_OP`) whose value is filled in lazily at execution time from the trigger event data.

The compiler allocates datum slots by calling `plpgsql_build_variable()` (`pl_comp.c`), which appends to the growing `plpgsql_Datums` array and returns a pointer to the new datum. Once compilation finishes, `plpgsql_finish_datums()` copies the array into the function's permanent storage.

## The namespace chain

The namespace chain is a singly linked list of `PLpgSQL_nsitem` nodes, each recording a name, an item type, and a datum index. A global `ns_top` pointer manages the list; it points to the youngest (innermost) item. Nodes with `itemtype = PLPGSQL_NSTYPE_LABEL` mark block boundaries.

When the compiler enters a block, `plpgsql_ns_push()` (`pl_funcs.c`) prepends a label node to the chain. Each `DECLARE` variable declaration calls `plpgsql_ns_additem()`, which prepends a new node before `ns_top` with the variable's name and datum index. When the compiler exits the block, `plpgsql_ns_pop()` walks backward through the list until it reaches the block's label node. It then resets `ns_top` past that node, effectively discarding all the block's declarations.

Each compiled statement records the current `ns_top` pointer in its `expr->ns` field. This snapshot is a pointer into the same linked-list structure. It implicitly includes all variables that were in scope at the moment the compiler compiled the statement. Compilation never frees the permanent list nodes; only `ns_top` moves.

## Name lookup

`plpgsql_ns_lookup()` (`pl_funcs.c`) resolves a variable reference at compile time. It walks the namespace chain from `ns_cur` (the statement's recorded snapshot) outward toward older nodes. At each block level, it scans forward in the chain — from youngest to oldest within that level — looking for a name match. The first match found wins; inner declarations shadow outer ones.

PL/pgSQL also supports a two-component reference `label.variable`. When `name2` is non-NULL, `plpgsql_ns_lookup()` checks whether the label node at the current level matches `name1`, then searches that level for `name2`. This allows code to access an outer-scope variable that an inner declaration has shadowed:

```sql
<<outer>>
DECLARE
    x int := 1;
BEGIN
    DECLARE
        x int := 2;
    BEGIN
        RAISE NOTICE '%', outer.x; -- resolves to the outer x via qualified name
    END;
END;
```

If `localmode` is true, the lookup stops at the first block boundary and does not search outer levels. The compiler uses this when checking for duplicate declarations within a single `DECLARE` section.

## Parameter variables

The compiler registers function parameters as variables before it compiles the function body. `pl_compile()` (`pl_comp.c`) calls `plpgsql_build_variable()` for each argument, using names from `pg_proc.proargnames` when available, or positional names `$1`, `$2`, ... otherwise. The compiler also registers `OUT` parameters as writable variables; the executor returns their values at function exit to the caller.

For functions that return a composite type, the compiler registers a `$0` variable of the return type as a row variable. Assigning to its fields within the function body populates the return value.

## Special and implicit variables

Several variables are automatically available in every PL/pgSQL function without appearing in `DECLARE`:

- `FOUND` — a boolean that becomes `true` after `SELECT INTO` finds a row, after `UPDATE`/`DELETE`/`INSERT` affects at least one row, or after a `FOR` loop iterates at least once.
- `SQLSTATE` and `SQLERRM` — available inside `EXCEPTION` blocks, set from the caught error.
- `ROW_COUNT` — the number of rows affected by the last SQL command.

In trigger functions, the compiler pre-registers a set of `PLPGSQL_DTYPE_PROMISE` variables: `TG_NAME`, `TG_WHEN`, `TG_LEVEL`, `TG_OP`, `TG_RELID`, `TG_RELNAME`, `TG_TABLE_NAME`, `TG_TABLE_SCHEMA`, `TG_NARGS`, `TG_ARGV`, `NEW`, and `OLD`. The executor populates promise datums' values lazily, the first time it reads them during execution, pulling from the `TriggerData` structure in the execution state. The compiler registers `NEW` and `OLD` as record variables (`PLPGSQL_DTYPE_REC`), pre-filled with the trigger's new and old row, respectively.

## Sub-block declarations and shadowing

A `DECLARE` section inside a nested block creates a new block scope. Variables declared there shadow same-named variables from outer blocks for the duration of the inner block:

```sql
DO $$
DECLARE
    v text := 'outer';
BEGIN
    DECLARE
        v text := 'inner';
    BEGIN
        RAISE NOTICE '%', v;  -- prints 'inner'
    END;
    RAISE NOTICE '%', v;      -- prints 'outer'
END;
$$;
```

The compiler treats each `BEGIN ... END` pair as a new block level, pushes a label, compiles the inner `DECLARE` declarations into new datum slots, and pops the label on exit. The inner block does not modify the outer `v` datum; after the inner block ends, statements that recorded the outer namespace snapshot continue to see the outer variable.

## Execution-time variable storage

At execution time, the function's executor state (`PLpgSQL_execstate`) holds a copy of the `datums[]` array initialized from the compiled function's template. Scalar variables start as NULL (unless the declaration provides a `DEFAULT` expression). Assignments overwrite the `Datum` value and null flag at the variable's datum index. There is no runtime namespace lookup; the compiler resolved all names and encoded them as datum indices at compile time.

## See also

- [[subsystems/plpgsql/overview]] — PL/pgSQL compilation and execution model
- [[subsystems/plpgsql/exception-handling]] — SQLSTATE and SQLERRM in EXCEPTION blocks
- [[subsystems/plpgsql/cursors]] — cursor variables and their datum representation
- [[subsystems/memory/contexts]] — memory allocation for datum values during execution

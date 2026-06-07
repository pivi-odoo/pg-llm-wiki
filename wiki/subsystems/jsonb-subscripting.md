---
title: "JSONB Subscripting"
aliases:
  - jsonb subscript
  - jsonb bracket notation
  - jsonbsubs
tags:
  - theme/extensibility
source_files:
  - src/backend/utils/adt/jsonbsubs.c
  - src/backend/utils/adt/jsonfuncs.c
  - src/include/nodes/subscripting.h
  - src/include/catalog/pg_type.dat
  - src/include/catalog/pg_proc.dat
symbols:
  - jsonb_subscript_handler
  - jsonb_subscript_transform
  - jsonb_exec_setup
  - jsonb_subscript_check_subscripts
  - jsonb_subscript_fetch
  - jsonb_subscript_assign
  - jsonb_subscript_fetch_old
  - JsonbSubWorkspace
  - SubscriptRoutines
  - jsonb_get_element
  - jsonb_set_element
---

JSONB subscripting lets SQL use bracket notation — `col['key']`, `col[0]`, `col['a']['b']` — for both reading and writing jsonb values. Introduced in PostgreSQL 14 alongside a generic subscripting infrastructure, it is the only way to perform a partial in-place update of a jsonb column with a plain `UPDATE SET` statement. The `->` and `->>` operators can navigate into a jsonb value, but they cannot appear on the left-hand side of an assignment.

## The Generic Subscripting Infrastructure

PostgreSQL 14 added a type-level hook that lets any data type implement `col[...]` syntax. A type registers a handler by setting `typsubscript` in `pg_type` to the OID of a C function that returns a pointer to a static `SubscriptRoutines` struct. For jsonb this function is `jsonb_subscript_handler()` (`jsonbsubs.c`), registered with the catalog entry `typsubscript => 'jsonb_subscript_handler'`.

The `SubscriptRoutines` struct declares two function pointers — one for parse-time analysis and one for executor startup — plus three boolean flags:

```c
typedef struct SubscriptRoutines
{
    SubscriptTransform transform;   /* parse analysis */
    SubscriptExecSetup exec_setup;  /* expression compilation */
    bool        fetch_strict;
    bool        fetch_leakproof;
    bool        store_leakproof;
} SubscriptRoutines;
```

For jsonb, both `fetch_strict` and `fetch_leakproof` are `true`, while `store_leakproof` is `false`. `fetch_strict` means a NULL container or a NULL subscript during a read short-circuits immediately to a SQL NULL result. `fetch_leakproof` signals that reads never throw errors that depend on data values. An out-of-bounds array index or a missing object key silently yields NULL instead. This flag matters for row-security policies, where error leakage could reveal data. The asymmetry with `store_leakproof = false` is intentional. Reads are forgiving, but writes treat a NULL subscript in an assignment as an error.

## Parse-Time Type Coercion

The transform function (`jsonb_subscript_transform()`) runs during the query's parse analysis. Its first job is to reject slice syntax: `col[1:3]` is a parse error with code `ERRCODE_DATATYPE_MISMATCH` because jsonb has no ordered slice semantics. This is a hard distinction from array subscripting. Array subscripting supports `arr[1:3]`.

For each subscript expression the transform attempts implicit coercion — first to `INT4OID`, then to `TEXTOID`. A subscript type must coerce to exactly one of these:

- If it coerces to both, the transform errors immediately: "subscript type is not supported … jsonb subscript must be coercible to only one type, integer or text." This ambiguity rule prevents silent surprises when a custom domain or cast could go either way.
- If it coerces to neither, the transform also errors.
- Unknown-type literals (bare string constants like `'key'`) default to `TEXTOID`.

The transform stores the coerced subscript expressions in `sbsref->refupperindexpr`. It sets `reflowerindexpr` to `NIL` because slices are not supported. The result type is always `JSONBOID` regardless of how many subscript levels there are.

## Executor Workspace

At executor startup, `jsonb_exec_setup()` allocates a `JsonbSubWorkspace` that lives for the lifetime of the query's plan node:

```c
typedef struct JsonbSubWorkspace
{
    bool        expectArray;
    Oid        *indexOid;   /* one entry per subscript: INT4OID or TEXTOID */
    Datum      *index;      /* subscript values in Datum form */
} JsonbSubWorkspace;
```

The two pointer fields point into a contiguous slab allocated immediately after the struct with `MAXALIGN` padding, sized for `numupper` subscripts. Recording the OID of each subscript is what allows the runtime to distinguish integer from text subscripts without re-examining types on every row.

Unlike array subscripting, jsonb subscripting has no depth limit. The comment in `jsonb_exec_setup()` states this explicitly: "Opposite to the arrays subscription, there is no limit for number of subscripts as jsonb type itself doesn't have nesting limits." `col['a']['b']['c']['d']` is valid for any depth.

## The Read Path

Before every fetch or assign, `jsonb_subscript_check_subscripts()` runs. It iterates over the subscripts and:

1. Converts any `INT4OID` subscript to a text `Datum` using `int4out` then `CStringGetTextDatum`. The downstream traversal functions accept only text paths.
2. For the first subscript, if its type is `INT4OID` and the value is non-null, sets `workspace->expectArray = true`. This flag is only used by the assign path (see below).
3. On a NULL subscript value: during a fetch, returns `false` — the entire expression yields NULL. During an assign, raises an error.

The fetch proper (`jsonb_subscript_fetch()`) calls `jsonb_get_element()` in `jsonfuncs.c`, passing the text path array. That function walks the jsonb tree level by level:

- For object containers it calls `getKeyJsonValueFromContainer()` with the text key.
- For array containers it parses the text subscript as an integer, with negative values counting from the end. An out-of-range index or a non-integer text value sets `*isnull = true` and returns NULL — consistent with the `fetch_leakproof = true` promise.
- The traversal uses the `jbvBinary` variant of `JsonbValue` for intermediate levels. This variant holds a pointer into the on-disk binary without deserializing it — the same zero-copy optimization described in [[subsystems/jsonb]].

The result type is always `jsonb`, not `text`. This is the key semantic difference from `->>` and `#>>`: `col['key']` returns `jsonb`. Comparisons must use jsonb literals — `WHERE col['key'] = '"value"'` (double-quoted to make it a jsonb string) — not a bare SQL string.

## The Write Path

Subscript assignment — `UPDATE t SET col['key'] = $1` — triggers a read-modify-write cycle over the entire jsonb column value. Jsonb has no expanded in-place representation; every assignment produces a new `Datum`. This is different from arrays. Arrays have an "expanded" varlena form that accumulates changes. Jsonb always rewrites the full value.

When a NULL subscript appears in an assignment context, `jsonb_subscript_check_subscripts()` raises an error rather than returning NULL. This is the `store_leakproof = false` behavior: the write path does not promise to suppress errors.

The assign function (`jsonb_subscript_assign()`) executes three logical steps:

**Replacement value preparation.** `JsonbToJsonbValue()` converts the replacement value from its SQL `Datum` form to a `JsonbValue`. A SQL NULL replacement becomes a `jbvNull` node — JSON `null` — not a deleted key. To delete a key, use the `#-` operator.

**NULL-source initialization.** If the column value is SQL NULL, the function synthesizes an empty container before writing. Whether it creates an empty array or an empty object depends on `workspace->expectArray`: if the first subscript was an integer, the container starts as `jbvArray`; otherwise it starts as `jbvObject`. This is how `UPDATE t SET col[0] = '1'` on a NULL column produces `[1]`, while `UPDATE t SET col['k'] = '1'` produces `{"k": 1}`.

**Path-setting.** `jsonb_set_element()` (`jsonfuncs.c`) calls `setPath()` with three flags:

| Flag | Effect |
|---|---|
| `JB_PATH_CREATE` | Creates intermediate object keys or array elements that do not yet exist |
| `JB_PATH_FILL_GAPS` | Pads array gaps with JSON `null` when the target index exceeds the current length |
| `JB_PATH_CONSISTENT_POSITION` | Enforces type consistency: an integer subscript at a given level requires an array; a text subscript requires an object |

These flags together mean that `UPDATE t SET col[2] = '2'` on `col = '[]'` produces `[null, null, 2]`. They also mean that `UPDATE t SET col['a']['b'] = '1'` on an absent `col['a']` creates `{"a": {"b": 1}}`. The only error case is traversal through an existing scalar — the path-setting logic cannot treat a scalar as an intermediate container.

Because an assign always produces a new non-null jsonb datum, the executor never sets the result null flag (`*op->resnull`) to true after the assignment.

## Nested Assignment and the Fetch-Old Path

A subscripted assignment can be nested, either because there are multiple subscript levels or because the same column appears elsewhere in the expression. In that case the executor needs the current value of an intermediate subexpression before it can compute the new value. `jsonb_subscript_fetch_old()` handles this case. If the whole column is NULL it sets `prevnull = true`; otherwise it calls `jsonb_get_element()` and stores the result in `sbsrefstate->prevvalue`. The nested assignment then uses that intermediate value as the source for the inner subscript write.

## Comparison with Operator Syntax

The `->` and `->>` operators share the same underlying traversal machinery (`getKeyJsonValueFromContainer()`, `getIthJsonbValueFromContainer()`) as subscripting reads. `#>` / `#>>` explicitly delegate to `jsonb_get_element()`. The behavioural differences are:

| | `->` / `#>` | subscript read | subscript write |
|---|---|---|---|
| Return type | `jsonb` / `text` | `jsonb` | — |
| Missing key | NULL | NULL | creates key |
| NULL input | NULL | NULL | error on NULL subscript |
| Assignment | not possible | not possible | full column rewrite |
| Slice syntax | not applicable | parse error | parse error |

The operators are the natural choice for read-only queries. Subscripting is the only option for partial column updates.

## Related Topics

- [[subsystems/jsonb|JSONB Storage and Indexing]]
- [[subsystems/jsonb-operators|JSONB Operators]]
- [[subsystems/indexes/jsonb-gin|JSONB GIN Indexes]]

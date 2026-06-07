---
title: "SQL/JSON Functions"
aliases:
  - "JSON_TABLE"
  - "JSON_VALUE"
  - "JSON_EXISTS"
  - "JSON_QUERY"
  - "SQL/JSON"
  - "jsonpath functions"
source_files:
  - src/backend/executor/execExprInterp.c
  - src/backend/executor/nodeTableFuncscan.c
  - src/backend/parser/parse_jsontable.c
  - src/backend/utils/adt/jsonpath.c
  - src/backend/utils/adt/jsonfuncs.c
  - src/backend/utils/adt/jsonb_util.c
  - src/include/utils/jsonpath.h
  - src/include/nodes/primnodes.h
symbols:
  - JsonFuncExpr
  - JsonExpr
  - JsonPathQuery
  - JsonPathExists
  - JsonPathValue
---

PostgreSQL 17 added a comprehensive set of SQL-standard JSON functions that complement the earlier operator-based `jsonb` and `jsonpath` support. These functions follow the SQL:2016 specification's JSON path language. They also provide structured error handling through `ON ERROR` / `ON EMPTY` clauses, which makes it practical to write robust JSON queries without wrapping everything in exception handlers.

## JSON_TABLE

`JSON_TABLE` is a table-valued function usable in the `FROM` clause that turns a JSON document into a relation. Each JSONPath match against the document produces one row. Per-column path expressions extract column values from the matched item.

```sql
SELECT jt.*
FROM orders, JSON_TABLE(
    orders.payload, '$.items[*]'
    COLUMNS (
        item_id   int          PATH '$.id',
        name      text         PATH '$.name',
        qty       int          PATH '$.quantity'
            DEFAULT 1 ON EMPTY
    )
) AS jt;
```

The `NESTED` keyword drills into nested arrays. It generates a cross product between the outer path matches and the inner path matches — effectively a lateral join within a single `JSON_TABLE` call:

```sql
JSON_TABLE(doc, '$.orders[*]'
    COLUMNS (
        order_id int PATH '$.id',
        NESTED PATH '$.lines[*]' COLUMNS (
            sku text PATH '$.sku',
            qty int  PATH '$.qty'
        )
    )
)
```

`JSON_TABLE` is implemented as a `TableFuncScan` plan node (`src/backend/executor/nodeTableFuncscan.c`). The scan evaluates the outer path once per input row, then evaluates the column paths against each match.

## Predicate and Query Functions

Three functions cover the common patterns of testing, extracting scalars, and extracting JSON fragments.

**`JSON_EXISTS(doc, path)`** returns a boolean indicating whether the path matches anything. It never errors on a JSON type mismatch — it returns false. An optional `ON ERROR` clause controls what to return if the document itself is invalid JSON.

```sql
SELECT * FROM products
WHERE JSON_EXISTS(attributes, '$.color ? (@ == "red")');
```

**`JSON_VALUE(doc, path RETURNING type)`** extracts a scalar value and casts it to the specified SQL type. The `RETURNING` clause is optional (defaults to `text`). Error handling follows the `ON EMPTY` / `ON ERROR` clauses, which can be `NULL`, `DEFAULT expr`, or `ERROR`.

```sql
SELECT JSON_VALUE(payload, '$.price' RETURNING numeric DEFAULT 0 ON EMPTY)
FROM orders;
```

**`JSON_QUERY(doc, path)`** extracts a JSON object or array (not a scalar). The `WITH WRAPPER` clause wraps multiple matches in a JSON array. `WITH CONDITIONAL WRAPPER` wraps only when there are multiple matches.

```sql
SELECT JSON_QUERY(payload, '$.tags' WITH WRAPPER) AS tags_array
FROM articles;
```

All three functions use the `JsonExpr` / `JsonFuncExpr` node types in the query tree. The expression interpreter evaluates them with `ExecEvalJsonExpr()`.

## Constructor Functions

**`JSON(text)`** converts a text string to a `json` value and validates that it is well-formed JSON. It is the typed analog of a cast from text to json with validation.

**`JSON_SCALAR(val)`** converts a SQL scalar value (number, boolean, text, null) to its JSON representation.

**`JSON_SERIALIZE(value)`** converts a JSON value back to its text serialization.

## jsonpath Type Conversion Methods

JSONPath expressions can include type conversion methods that cast a match to a SQL type directly within the path. This avoids wrapping the whole query in a `CAST`:

| Method | Equivalent SQL type |
|--------|---------------------|
| `.bigint()` | `bigint` |
| `.boolean()` | `boolean` |
| `.date()` | `date` |
| `.decimal(p, s)` | `numeric(p, s)` |
| `.integer()` | `integer` |
| `.number()` | `numeric` |
| `.string()` | `text` |
| `.time()` | `time` |
| `.time_tz()` | `timetz` |
| `.timestamp()` | `timestamp` |
| `.timestamp_tz()` | `timestamptz` |

```sql
SELECT JSON_VALUE(doc, '$.created_at.timestamp()') AS created_at
FROM events;
```

## Error Handling Model

The SQL/JSON functions use a two-clause error model:

- **`ON EMPTY`** fires when the path matches nothing (no items, not a type error).
- **`ON ERROR`** fires when the path matches but the result cannot be converted to the requested type. It also fires when the input document is malformed.

Both clauses accept `NULL` (return null), `DEFAULT expr` (return a specific value), or `ERROR` (raise an exception). The default behavior for most functions is `NULL ON EMPTY NULL ON ERROR`, which silently returns null on any problem. This matches common application expectations for JSON queries against semi-structured data.

## Relationship to Existing JSON Support

PostgreSQL's existing `->`, `->>`, `#>`, `#>>` operators and `jsonb_path_query()` / `jsonb_path_exists()` / `jsonb_path_match()` functions remain fully supported. The SQL/JSON functions are additive, not replacements. Key differences:

| Feature | Existing operators | SQL/JSON functions |
|---------|-------------------|-------------------|
| Standard | PostgreSQL-specific | SQL:2016 compliant |
| Error control | Raise or return null | Fine-grained ON EMPTY / ON ERROR |
| Tabular output | Requires `jsonb_to_recordset()` | Native with `JSON_TABLE` |
| Type casting | Separate `CAST` or `::type` | Inline with `RETURNING` or path methods |

For simple path navigation in application code that does not need portability, the operator syntax is often more concise. `JSON_TABLE` and the `ON ERROR` / `ON EMPTY` clauses are the main reasons to prefer the new functions.

## Related Topics

- [[subsystems/jsonb|JSONB Internals]] — storage format, operators, and jsonpath evaluation
- [[subsystems/jsonb-query-patterns|JSONB Query Patterns]] — practical patterns for querying JSONB columns
- [[sql-features/lateral|LATERAL Joins]] — `JSON_TABLE` behaves similarly to a lateral subquery

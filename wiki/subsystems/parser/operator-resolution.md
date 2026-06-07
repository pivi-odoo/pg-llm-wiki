---
title: "Operator Resolution and Target Lists"
aliases:
  - operator resolution
  - operator overload resolution
  - target list
  - TargetEntry
  - parse_oper
  - parse_target
  - make_op
  - transformTargetList
tags:
  - theme/caching
source_files:
  - src/backend/parser/parse_oper.c
  - src/backend/parser/parse_target.c
  - src/include/parser/parse_oper.h
  - src/include/parser/parse_target.h
symbols:
  - OprCacheKey
  - OprCacheEntry
  - LookupOperName
  - oper
  - compatible_oper
  - make_op
  - make_scalar_array_op
  - get_sort_group_operators
  - transformTargetList
  - transformTargetEntry
  - transformAssignedExpr
  - ExpandColumnRefStar
  - ExpandAllTables
  - resolveTargetListUnknowns
  - markTargetListOrigins
---

When the PostgreSQL analyzer encounters an operator expression like `a + b` or `a = ANY(array)`, it must locate the specific `pg_operator` entry that matches the operand types. This process is called operator resolution. The result, packaged as an `OpExpr` node, feeds into the planner and executor with a concrete operator OID and underlying function OID already resolved. Separately, `parse_target.c` handles the construction of **target lists** — the ordered list of `TargetEntry` nodes describing what each output column of a query is, how it is named, and whether it is a "junk" column not returned to the client.

## Operator resolution (parse_oper.c)

### Exact and best-match lookup

Resolution starts in `oper()` (`parse_oper.c`). `transformExpr` calls it for binary operators, and `left_oper` calls it for prefix operators. The strategy is:

1. **Exact match**: call `binary_oper_exact()`, which asks `OpernameGetOprid` for an operator with the exact left and right operand types. If this succeeds, the analyzer needs no type coercion. It uses the result directly.

2. **Best match with coercion**: if exact lookup fails, the analyzer calls `func_get_detail()` with the operator name and operand types. This is the same overload resolution machinery used for function calls (`parse_func.c`). It enumerates all `pg_operator` rows with the given name and the right operator kind (`b` for binary, `l` for prefix). It then applies the SQL type coercion preference rules to pick the best match among candidates. `oper_select_candidate()` wraps these rules for the two-argument case.

3. **Error**: if no candidate can be selected, the analyzer calls `op_error()`. This function constructs the "operator does not exist" message using `op_signature_string()`, to show the type signatures of the candidates that were found (if any), alongside the one that was requested.

The `compatible_oper` variant accepts some additional type-coercion flexibility. It is used in contexts like `ORDER BY` clause matching, where the analyzer allows implicit-category coercions.

`make_op()` is the top-level function. It takes a raw operator name (a list of possibly-schema-qualified name strings) and left and right expression nodes. It produces an `OpExpr` node with the operator's OID, result type, and underlying function OID populated.

`make_scalar_array_op()` handles `expr op ANY(array)` / `expr op ALL(array)` expressions. It calls `oper()` to find the per-element operator. It then wraps the result in a `ScalarArrayOpExpr` node whose `useOr` flag distinguishes `ANY` from `ALL`.

The analyzer uses `get_sort_group_operators()` to find the comparison, equality, and hashing operators for a given type. It needs these when processing `ORDER BY`, `GROUP BY`, and `DISTINCT`.

### Operator lookup cache

Operator resolution involves syscache lookups against `pg_operator` that are repeated for every expression in every query. A process-local hash table (`OprCache`) caches resolved operator OIDs keyed on `(name, left_type, right_type, search_path)`. The `OprCacheKey` includes the entire active search path (up to `MAX_CACHED_PATH_LEN` = 16 namespaces) because the same operator name may resolve differently in different schemas.

A syscache callback registered against `OPEROID` invalidates cache entries. When `pg_operator` is modified (e.g. by `CREATE OPERATOR`), `InvalidateOprCacheCallBack` clears the entire process-local cache. This is conservative. However, the cache is small enough that a full flush is inexpensive.

## Target list construction (parse_target.c)

### What a target list is

A **target list** is the `List` of `TargetEntry` nodes attached to a `Query` node's `targetList` field. Each `TargetEntry` pairs an expression with metadata:

- `resno` — the output column number (1-based for visible columns, increasing from there for junk columns).
- `resname` — the column alias or inferred name shown to the client.
- `resjunk` — marks columns needed internally (sort keys, aggregation groups) but not returned in the result set.
- `ressortgroupref` — links this entry to a `SortGroupClause` for `ORDER BY` / `GROUP BY` / `DISTINCT`.

`transformTargetList()` processes the raw `SELECT` column list produced by the parser into a proper list of `TargetEntry` nodes. `transformTargetEntry()` processes each element. It calls `transformExpr()` on the expression. It then infers a column name via `FigureColname()`, if no `AS` alias was given.

### Column name inference

When a query provides no `AS` alias, PostgreSQL infers a column name from the expression structure. The rules, implemented in `FigureColnameInternal()`, are:

- A bare column reference (`a.b`) uses the final attribute name (`b`).
- A function call (`f(x)`) uses the function name.
- A type cast (`x::int`) uses the target type name.
- A subscript access (`a[1]`) uses the name of the base expression.
- Anything else falls through to `"?column?"`.

PostgreSQL stores this name in `resname`. Clients see it in `pg_statement` or via the wire protocol's `RowDescription` message.

### Star expansion

`ExpandColumnRefStar()` and `ExpandAllTables()` expand `SELECT *` and `SELECT t.*`, respectively. Star expansion is not a simple text substitution: it reads the current range table via the `ParseNamespaceItem` entries in the parse state to enumerate all visible columns. Star expansion silently skips columns marked as dropped (those with `pg_attribute.attisdropped = true`). Composite-type columns use `ExpandRowReference()` to recursively enumerate the composite's fields if needed.

### Assignment target list (INSERT / UPDATE)

For DML statements, `parse_target.c` constructs the assignment target list — the mapping from input expressions to table columns. `transformAssignedExpr()` handles the type coercion needed to make the source expression compatible with the column's declared type, including subscript and field-selection indirection for `UPDATE t SET col[1] = x` style statements. `transformAssignmentIndirection()` recursively processes nested subscript and field-selection paths, building `SubscriptingRef` and `FieldStore` nodes that the executor later uses to update sub-fields of composite or array columns.

`checkInsertTargets()` validates that the explicit column list given in `INSERT INTO t (a, b) VALUES ...` references valid, non-dropped columns. It returns the corresponding attribute numbers for use when matching values to columns.

### Resolving unknown type literals

After the analyzer assembles the full target list, `resolveTargetListUnknowns()` resolves any `Const` nodes whose type is `UNKNOWNOID` (untyped string literals) to `text`. This is the fallback when no other context forces a more specific type. `transformSelectStmt` calls it at the end, to ensure that `SELECT 'foo'` produces a `text` column rather than an `unknown`-typed one that would confuse clients.

## Related Topics

- [[subsystems/parser/type-resolution|Type Resolution]]
- [[subsystems/parser/semantic-analysis|Semantic Analysis]]
- [[subsystems/parser/overview|Parser Overview]]
- [[architecture/node-infrastructure|Node Infrastructure]]
- [[subsystems/planner/target-list|Planner: Target List Processing]]

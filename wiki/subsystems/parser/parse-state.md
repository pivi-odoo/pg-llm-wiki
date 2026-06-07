---
title: "ParseState: Semantic Analysis Context"
aliases:
  - "ParseState"
  - "parse_node"
  - "ParseExprKind"
  - "make_parsestate"
  - "ParseCallbackState"
  - "parser error position"
tags:
  - theme/extensibility
source_files:
  - src/backend/parser/parse_node.c
  - src/include/parser/parse_node.h
symbols:
  - ParseState
  - make_parsestate
  - free_parsestate
  - parser_errposition
  - ParseCallbackState
  - setup_parser_errposition_callback
  - cancel_parser_errposition_callback
  - ParseExprKind
  - transformContainerType
  - transformContainerSubscripts
  - make_const
---

`ParseState` is the mutable context object that carries accumulated knowledge through [[subsystems/parser/semantic-analysis|semantic analysis]]. It is not a parse tree node — it is a scratch area allocated at the start of analysis and discarded when analysis completes. Every function in the semantic analysis layer receives a `ParseState *`. It deposits its findings into it: range table entries, namespace items, boolean flags for aggregates and window functions, and the target-list counter. `src/include/parser/parse_node.h` defines the struct. `src/backend/parser/parse_node.c` manages its lifecycle.

## Lifecycle and nesting

`make_parsestate()` allocates and zero-initialises a `ParseState`, setting `p_next_resno` to 1 and accepting an optional `parentParseState` pointer. `free_parsestate()` releases it, closing `p_target_relation` if one was opened during INSERT/UPDATE/DELETE analysis.

The nesting model mirrors SQL scope rules. When the parser encounters a subquery, it calls `make_parsestate(outerPstate)`, linking the child to the parent via `parentParseState`. Column name resolution walks this chain upward when a name is not found at the current level, giving subqueries access to outer-query columns for correlated references. The chain is NULL-terminated at the top-level query.

Key fields accumulated during analysis:

| Field | Role |
|---|---|
| `p_rtable` | List of `RangeTblEntry` being built for this query level |
| `p_rteperminfos` | Permission-check structs parallel to `p_rtable` |
| `p_joinexprs` | `JoinExpr` nodes parallel to `p_rtable` (NULL for non-join RTEs) |
| `p_namespace` | `ParseNamespaceItem` list — what table and column names are currently visible |
| `p_next_resno` | Counter for target-list `resno` values; starts at 1 |
| `p_resolve_unknowns` | Whether to attempt resolution of `UNKNOWN`-typed literals at the end of analysis |
| `p_hasAggs`, `p_hasWindowFuncs`, `p_hasTargetSRFs`, `p_hasModifyingCTE` | Boolean flags set as expressions are processed |
| `p_sourcetext` | The original SQL string, used only for error-position conversion |
| `p_queryEnv` | `QueryEnvironment` for ephemeral named relations (e.g. transition tables in `AFTER` triggers) |

## Error position infrastructure

Raw parse tree nodes carry byte offsets (`location` fields) into the original SQL string. These are efficient to store, but they are not directly usable in error messages. Error messages require 1-based character positions for multibyte encodings. `parser_errposition(pstate, location)` performs the conversion by scanning `p_sourcetext` up to the given byte offset, counting characters. It returns a value suitable for inclusion in an `ereport()` call.

The conversion only works while a `ParseState` is on the call stack. Functions outside the parser that may need to report a parse location use `ParseCallbackState` instead. `setup_parser_errposition_callback()` installs a callback on the `error_context_stack` that captures the `ParseState` and the byte offset. If an error is thrown, the callback converts the offset. It also annotates the error message. `cancel_parser_errposition_callback()` removes it once the call is done. This lets catalog-lookup and type-coercion code — which has no direct `ParseState` parameter — still emit precise error positions.

## Context-aware expression analysis with ParseExprKind

`transformExpr()` is a single recursive function that handles every expression in every SQL clause. `transformExpr()` handles the same code path for `WHERE` predicates, `HAVING` clauses, `GROUP BY` keys, `ORDER BY` keys, `SELECT` target expressions, index predicates, generated-column expressions, trigger `WHEN` clauses, and policy expressions, among others.

Different clause contexts impose different restrictions: aggregates are illegal in `WHERE` but legal in `HAVING`; set-returning functions are illegal in most clauses but legal in the `SELECT` target list; volatile functions are illegal in index predicates. Rather than duplicating `transformExpr()` for each context, PostgreSQL passes a `ParseExprKind` enum value that names the context. `transformExpr()` checks this value at decision points. It raises an error when it encounters a disallowed construct.

The `ParseExprKind` enum (defined in `parse_node.h`) has over 40 values, including `EXPR_KIND_WHERE`, `EXPR_KIND_HAVING`, `EXPR_KIND_SELECT_TARGET`, `EXPR_KIND_INSERT_TARGET`, `EXPR_KIND_GROUP_BY`, `EXPR_KIND_ORDER_BY`, `EXPR_KIND_WINDOW_PARTITION`, `EXPR_KIND_INDEX_EXPRESSION`, `EXPR_KIND_GENERATED_COLUMN`, `EXPR_KIND_TRIGGER_WHEN`, `EXPR_KIND_POLICY`, and `EXPR_KIND_PARTITION_EXPRESSION`. Adding a new clause context means adding a new enum value. It also means auditing the restriction checks inside `transformExpr()`.

## Extension hooks for name resolution

`ParseState` exposes four function-pointer hooks that allow extensions and procedural languages to intercept the name-resolution steps of expression analysis:

- `p_pre_columnref_hook`: called before the parser attempts to resolve a `ColumnRef`. If it returns a non-NULL node, the parser uses that node. It skips normal lookup.
- `p_post_columnref_hook`: called after normal lookup. The hook can substitute or wrap the resolved node. It can also raise an error when it detects a disallowed reference.
- `p_paramref_hook`: called to resolve `$N` parameter references. Without this hook, parameters are only legal in prepared statements with a known parameter-type array. With it, a caller such as PL/pgSQL can supply its own `Param` nodes.
- `p_coerce_param_hook`: called when a parameter needs to be coerced to a target type, giving the caller control over how the coercion is expressed.

PL/pgSQL installs all four hooks before calling back into `parse_analyze` to analyse function bodies. The hooks give it access to the function's local variable namespace and `$N`-style parameter bindings without modifying the core parser. Foreign data wrapper extensions can similarly use the column-reference hooks to redirect column lookups to remote column metadata.

## Literal type inference in make_const

When the scanner produces an `A_Const` node, `make_const()` in `parse_node.c` converts it into a typed `Const` node. The rules are:

- **Integer literals** become `INT4` by default. If the value exceeds the `INT4` range, it becomes `INT8`. If it exceeds `INT8` range, it becomes `NUMERIC`.
- **Float literals** become `NUMERIC`, not `FLOAT8`. This preserves precision and avoids silent rounding during analysis. If the context requires a `FLOAT8`, PostgreSQL applies an implicit cast later.
- **String literals** get type `UNKNOWN`. PostgreSQL stores the string verbatim. It resolves the actual type later by context — typically via `coerce_type()`, when the literal appears next to a typed operand or column. If no context resolves it, `p_resolve_unknowns` controls whether a final pass attempts inference.
- **NULL** also gets type `UNKNOWN` for the same reason.

This deferred typing is intentional. A bare string literal `'2024-01-01'` might be a `date`, a `timestamp`, a `text`, or something else entirely, depending on how it is used. Forcing it to `text` at parse time would require an explicit cast in many valid SQL expressions.

## Container subscript transformation

Two functions in `parse_node.c` handle array subscript expressions — `arr[1]`, `mat[i][j]`. `transformContainerType()` normalises the container's type. It smashes domain types down to their base type. It treats the special internal types `int2vector` and `oidvector` as if they were domains over the ordinary `int2[]` and `oid[]` array types. This lets subscript logic operate uniformly on plain arrays without knowing about every domain or legacy vector type.

`transformContainerSubscripts()` builds a `SubscriptingRef` node that captures the container expression, the element type, the subscript list, and whether the expression is an assignment target (`arr[1] = x`) or a fetch (`x = arr[1]`). The same function handles both read and write cases. The `isAssignment` flag distinguishes them. For multi-dimensional arrays, `transformContainerSubscripts()` collects each dimension's subscript into the indirection list before it creates the single `SubscriptingRef`.

## See also

- [[subsystems/parser/semantic-analysis|Semantic Analysis (parse_analyze)]] — the overall analysis pipeline and how ParseState is used
- [[subsystems/parser/type-resolution|Type Resolution]] — how UNKNOWN literals and coercions are resolved
- [[subsystems/parser/query-parameters|Query Parameters]] — prepared statement parameter handling
- [[subsystems/planner/overview|Planner]] — consumes the Query node produced by semantic analysis
- [[subsystems/memory/contexts|Memory Contexts]] — the memory context within which ParseState is allocated

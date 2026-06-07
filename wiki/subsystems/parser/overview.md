---
title: Parser and Semantic Analysis
aliases:
  - Parser
  - Semantic Analysis
  - Parse Analysis
source_files:
  - src/backend/parser/scan.l
  - src/backend/parser/gram.y
  - src/backend/parser/analyze.c
  - src/include/nodes/parsenodes.h
  - src/include/nodes/primnodes.h
  - src/include/parser/parse_node.h
symbols:
  - RawStmt
  - Query
  - ParseState
  - RangeTblEntry
  - RTEKind
  - ColumnRef
  - A_Expr
  - FuncCall
  - Var
  - transformStmt
  - transformTopLevelStmt
  - parse_analyze_fixedparams
  - transformExpr
  - transformSelectStmt
  - transformInsertStmt
  - transformUpdateStmt
  - transformDeleteStmt
  - transformAggregateCall
  - transformWindowFuncCall
  - parseCheckAggregates
  - addRangeTableEntry
  - parse_sub_analyze
---

# Parser and Semantic Analysis

SQL text arriving at a PostgreSQL backend is opaque — a byte string with no semantic content until the system transforms it into something the planner and executor can work with. That transformation happens in two distinct phases. First, the parser turns SQL text into a raw parse tree that mirrors the syntactic structure of the statement but resolves nothing. Second, semantic analysis walks that raw tree. It builds a `Query` node that has looked up every name, assigned every type, and recorded every catalog dependency. The planner receives only `Query` nodes. It never sees raw SQL.

This separation matters because the grammar must be completely stateless. The gram.y comment states this explicitly: nothing in the grammar should initiate database accesses or depend on changeable state. This is because, in a multi-statement string, gram.y parses the entire string before any command executes. Schema lookups belong to the analysis phase, where they run under a proper transaction snapshot.

## Lexing and Grammar

The lexer (`scan.l`) is a Flex-generated scanner that turns the input byte stream into a stream of tokens. It tracks a byte offset (`yylloc`) for every token, which later becomes the position reported in error messages. The scanner is stateless between calls: all mutable state lives in a `core_yy_extra_type` structure threaded through as `yyextra`, never in global variables. This is a reentrant scanner — the same code serves both the main backend parser and PL/pgSQL.

A sorted lookup table in `kwlist.h` handles keywords. The scanner does not hard-code keyword recognition in its rules. Instead, it looks up any sequence of characters matching the identifier pattern in the keyword table. It returns the resulting Bison token number. Identifiers that are not keywords simply return `IDENT`. The grammar lists four keyword categories — reserved, unreserved, type-function-name, and column-name — controlling where each keyword may appear without quoting.

The grammar (`gram.y`) is a LALR(1) grammar processed by Bison. The top-level production collects a list of `RawStmt` nodes, each wrapping a single statement with its byte-offset position (`stmt_location`) and length. The analyzer propagates these positions all the way into `Query.stmt_location` and `Query.stmt_len`, enabling the statistics collector and `pg_stat_activity` to display accurate query text.

The raw parse tree uses "raw" node types declared in `parsenodes.h`: `ColumnRef` for column references (a list of string fields, nothing resolved), `A_Expr` for operator expressions (operator name as a string list, operands as raw nodes), `FuncCall` for function calls (function name as a string list), `A_Const` for literals, `TypeCast` for explicit casts, and `ResTarget` for target list entries. None of these carry type information or OIDs. A column reference like `t.c` is just a `ColumnRef` with `fields = ["t", "c"]`.

## The Parse State

Semantic analysis drives through a `ParseState` struct (`parse_node.h`) that accumulates everything discovered about a single query scope as analysis proceeds:

| Field | Purpose |
|---|---|
| `p_rtable` | Range table entries gathered so far (list of `RangeTblEntry`) |
| `p_namespace` | Currently visible RTEs for name resolution |
| `p_joinexprs` | `JoinExpr` nodes parallel to `p_rtable` for join RTEs |
| `p_parent_cte` | The enclosing `CommonTableExpr`, if any |
| `p_hasAggs` | Set true when an aggregate call is found |
| `p_hasWindowFuncs` | Set true when a window function is found |
| `p_resolve_unknowns` | Whether to resolve untyped SELECT outputs as `text` |
| `p_sourcetext` | The original SQL string, for error cursor positions |

Each subquery and CTE gets its own `ParseState`, linked to its parent via `make_parsestate(parentParseState)`. The parent link lets aggregate level-up logic (`agglevelsup`) walk outward to find the correct query level.

## From Raw Tree to Query

The entry point is `parse_analyze_fixedparams()` (or the varparams and callback variants). It constructs a `ParseState`. It calls `transformTopLevelStmt()`. It optionally computes a query jumble for `pg_stat_statements`. It invokes any `post_parse_analyze_hook`. It returns a `Query`.

`transformStmt()` dispatches on the node tag of the raw statement. Optimizable statements — `SelectStmt`, `InsertStmt`, `UpdateStmt`, `DeleteStmt`, `MergeStmt` — each have a dedicated transform function that performs full semantic analysis. `transformStmt()` simply wraps utility statements (DDL, `COPY`, `VACUUM`, and so on) in a `CMD_UTILITY` `Query` with `utilityStmt` pointing at the unmodified raw tree. Three utility-adjacent statements are exceptions: `DECLARE CURSOR`, `EXPLAIN`, and `CREATE TABLE AS` each contain an inner optimizable statement that must be fully analyzed.

The resulting `Query` struct carries every clause the planner needs:

```c
typedef struct Query {
    CmdType   commandType;     /* CMD_SELECT, CMD_INSERT, ... */
    int       resultRelation;  /* rtable index of target rel; 0 for SELECT */
    List     *rtable;          /* list of RangeTblEntry */
    FromExpr *jointree;        /* FROM and WHERE as a tree */
    List     *targetList;      /* TargetEntry list */
    List     *cteList;         /* WITH clause */
    Node     *setOperations;   /* UNION/INTERSECT/EXCEPT tree */
    bool      hasAggs;
    bool      hasWindowFuncs;
    bool      hasSubLinks;
    ...
} Query;
```

## The Range Table

The range table (`Query.rtable`) is the authoritative list of every data source a query touches. Each entry is a `RangeTblEntry` with an `RTEKind` tag:

| `RTEKind` | Meaning |
|---|---|
| `RTE_RELATION` | A real table, view, sequence, or foreign table (resolved to `relid` OID and `relkind`) |
| `RTE_SUBQUERY` | A subquery in the FROM clause; the subquery is a nested `Query` |
| `RTE_JOIN` | An explicit JOIN; `joinaliasvars` maps join output columns back to input columns |
| `RTE_FUNCTION` | A set-returning function in FROM (e.g. `generate_series`) |
| `RTE_TABLEFUNC` | A table function with a declared column list (e.g. `XMLTABLE`) |
| `RTE_VALUES` | A `VALUES(...)` list |
| `RTE_CTE` | A reference to a WITH clause CTE |
| `RTE_NAMEDTUPLESTORE` | A named transition table, used by AFTER triggers |

The planner adds `RTE_RESULT` for queries with an empty FROM clause. It is never present in parser output.

`addRangeTableEntry()` (`parse_relation.c`) creates every `RTE_RELATION` entry. It calls `RangeVarGetRelid()` to resolve the table name to an OID. It opens the relation to acquire the appropriate lock. It records permission requirements in a parallel `RTEPermissionInfo` list. `addRangeTableEntry()` obtains the lock here and holds it until the end of the transaction, so subsequent plan phases can rely on the schema not changing.

`Var` nodes in expressions reference range table entries by `varno` (1-based index into `rtable`) and `varattno` (attribute number within the relation). This is how a column reference like `t.c` becomes a `Var(varno=1, varattno=3)` once the analyzer knows `t` maps to rtable entry 1 and `c` is the third attribute.

## Expression Analysis

`transformExpr()` (`parse_expr.c`) is the recursive heart of semantic analysis. It walks a raw expression tree. It returns a fully-resolved expression tree in which every node carries a type OID and, for column references, a specific `Var`. The `ParseExprKind` argument tells it the context — `EXPR_KIND_WHERE`, `EXPR_KIND_SELECT_TARGET`, `EXPR_KIND_HAVING`, and so on — which controls which constructs are legal in that position (aggregates are forbidden in WHERE, for instance).

Key transformations performed by `transformExpr()`:

- A `ColumnRef` becomes a `Var` after the analyzer searches `p_namespace` to find which RTE the column belongs to, then looks up the attribute number.
- An `A_Expr` (operator expression) becomes an `OpExpr` or similar node after `parse_oper.c` resolves the operator name to a specific operator OID in `pg_operator`.
- A `FuncCall` becomes an `Aggref`, `WindowFunc`, or `FuncExpr` depending on context, after `parse_func.c` resolves the function name to a procedure OID in `pg_proc`.
- An `A_Const` becomes a `Const` node with the appropriate type and value.
- A `TypeCast` triggers coercion analysis.

## Type Coercion

PostgreSQL distinguishes three coercion strengths, represented by `CoercionContext` (`primnodes.h`):

| Context | When used |
|---|---|
| `COERCION_IMPLICIT` | The source type can be silently converted when used in an expression |
| `COERCION_ASSIGNMENT` | Conversion is allowed when storing into a column of the target type |
| `COERCION_EXPLICIT` | Only happens with an explicit `CAST` or `::` syntax |

When two operands of an operator have different types, `parse_coerce.c` consults `pg_cast` to find a cast path. It determines whether an implicit cast is available. If the types are incompatible even with explicit casting, analysis fails with a type error. The coercion context recorded in cast nodes (`CoercionForm`) controls whether the cast is shown when decompiling a query back to SQL: `COERCE_IMPLICIT_CAST` nodes are hidden, while `COERCE_EXPLICIT_CAST` nodes appear as `CAST(...)`.

Unknown-type string literals (bare `'foo'`) receive type `UNKNOWN` from the lexer. The analyzer defers resolution, using the surrounding operator or function signatures to drive the final type assignment. When no context resolves the type — SELECT output columns in certain situations — `p_resolve_unknowns` resolves them as `text`.

## Aggregates and Window Functions

Aggregate and window function detection happens during expression analysis, not as a separate pass. When `transformExpr()` encounters a `FuncCall` that resolves to an aggregate function in `pg_proc`, `transformAggregateCall()` (`parse_agg.c`) constructs an `Aggref` node. It sets `pstate->p_hasAggs = true`. It records the aggregate's level (outward query scope) in `agglevelsup`. An outer-level aggregate increments `p_hasAggs` on the appropriate ancestor `ParseState`.

`transformWindowFuncCall()` handles window functions. It links the `WindowFunc` to a `WindowClause` entry in `Query.windowClause`. It sets `p_hasWindowFuncs`. At this stage, `transformExpr()` does not lift aggregates or window functions out of expressions. They remain in place in the expression tree. After the analyzer assembles the full query, `parseCheckAggregates()` validates that aggregates do not appear in illegal positions (e.g., WHERE, JOIN ON). It also checks for correct nesting.

At the end of each `transformSelectStmt()` or equivalent call, the analyzer sets the `Query` flags `hasAggs` and `hasWindowFuncs` from the corresponding `ParseState` flags. The planner uses these flags to decide whether to introduce `Agg` or `WindowAgg` nodes into the plan.

## Subqueries

Each subquery in the FROM clause or in a sublink expression becomes a nested `Query`. `parse_sub_analyze()` creates a new `ParseState` with the parent as its `parentParseState`. It runs full analysis on the subquery. It returns a `Query` that becomes part of an `RTE_SUBQUERY` range table entry or a `SubLink`/`SubPlan` node. The parent `ParseState` remains accessible, so that correlated references can set `varlevelsup` to indicate the correct query depth. Correlated references are column references in the subquery that resolve to the parent query's range table.

The analyzer processes CTEs in WITH clauses similarly: `parse_sub_analyze()` analyzes each CTE body. It stores the body in `Query.cteList` as a `CommonTableExpr`. References to the CTE name produce `RTE_CTE` range table entries.

## Statement-Specific Structures

The four DML command types produce `Query` nodes with different semantics:

**SELECT** — `resultRelation` is 0, `targetList` carries the output expressions as `TargetEntry` nodes, `jointree` carries the FROM/WHERE tree. UNION/INTERSECT/EXCEPT produce a `setOperations` tree of `SetOperationStmt` nodes.

**INSERT** — `resultRelation` is the index of the target relation in `rtable`. `targetList` maps source expressions to destination column attribute numbers. An optional `onConflict` field holds the ON CONFLICT DO UPDATE or DO NOTHING specification.

**UPDATE** — `resultRelation` is the target relation index. `targetList` pairs update expressions with target attribute numbers. The relation appears in `rtable` once as the update target.

**DELETE** — `resultRelation` is the target relation index. `targetList` is empty unless RETURNING is present, in which case it holds the returning expressions.

All four statements support a `returningList` field (populated from a RETURNING clause). The analyzer treats it as if it were a SELECT target list over the modified row.

## Error Reporting

Every raw parse tree node contains a `location` field storing the byte offset of the relevant token in the source text. The analyzer threads the original source text through `ParseState.p_sourcetext`. When analysis fails, `parser_errposition()` uses the node's location together with the source text to produce an error cursor that points at exactly the right position in the query. The grammar uses `YYLLOC_DEFAULT` to propagate location information through grammar reductions, so even nodes constructed from multiple tokens carry a meaningful starting position.

## Key Files

| File | Role |
|---|---|
| `src/backend/parser/scan.l` | Flex lexer; produces tokens with byte-offset locations |
| `src/backend/parser/gram.y` | Bison LALR(1) grammar; produces raw parse trees |
| `src/backend/parser/analyze.c` | Top-level semantic analysis; `transformStmt()` dispatch |
| `src/backend/parser/parse_expr.c` | Expression analysis; `transformExpr()` |
| `src/backend/parser/parse_relation.c` | Range table construction; `addRangeTableEntry()` |
| `src/backend/parser/parse_agg.c` | Aggregate and window function analysis |
| `src/backend/parser/parse_coerce.c` | Type coercion and cast resolution |
| `src/backend/parser/parse_func.c` | Function and operator name resolution |
| `src/include/nodes/parsenodes.h` | Raw parse tree node types (`RawStmt`, `ColumnRef`, `A_Expr`, …) |
| `src/include/nodes/primnodes.h` | Analyzed expression node types (`Var`, `Const`, `FuncExpr`, `CoercionContext`, …) |
| `src/include/parser/parse_node.h` | `ParseState` definition |

## Related Topics

- [[subsystems/parser/semantic-analysis|Semantic Analysis]] — detailed coverage of the name-resolution, scope, and namespace machinery that backs `transformExpr()` and range-table construction.
- [[subsystems/parser/parse-tree-nodes|Parse Tree Nodes]] — a reference for the raw node types (`RawStmt`, `ColumnRef`, `A_Expr`, `FuncCall`) produced by the grammar before semantic analysis runs.
- [[subsystems/parser/parse-state|Parse State]] — in-depth look at `ParseState` lifecycle, parent linking, and how subquery scopes are managed.
- [[subsystems/parser/operator-resolution|Operator Resolution]] — how `parse_oper.c` selects a specific `pg_operator` entry from an operator name and operand types during expression analysis.
- [[subsystems/parser/type-resolution|Type Resolution]] — how unknown-type literals and mismatched operand types are resolved, covering the coercion rules and `pg_cast` lookups in `parse_coerce.c`.
- [[subsystems/rewriter/overview|Rewriter]] — the next stage in the pipeline: the rewriter receives `Query` trees from the parser and applies view expansion and rule rewriting before planning.
- [[subsystems/planner/overview|Planner Overview]] — the stage that consumes the fully-analyzed `Query` and produces a `PlannedStmt`; understanding the parser output is prerequisite to reading plan trees.
- [[subsystems/executor/overview|Executor Overview]] — the executor never sees `Query` trees directly; it operates only on the `PlannedStmt` produced downstream of parse analysis and planning.
- [[subsystems/transactions/mvcc|MVCC]] — catalog lookups performed during analysis require an active MVCC snapshot, which is why `analyze_requires_snapshot()` returns true for all optimizable statements.

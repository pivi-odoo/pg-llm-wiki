---
title: "Rule Deparser (ruleutils / pg_get_expr)"
aliases:
  - ruleutils
  - pg_get_expr
  - pg_get_viewdef
  - pg_get_ruledef
  - deparse_expression
  - query deparsing
source_files:
  - src/backend/utils/adt/ruleutils.c
  - src/include/utils/ruleutils.h
symbols:
  - pg_get_expr
  - pg_get_ruledef
  - pg_get_viewdef
  - pg_get_indexdef
  - pg_get_constraintdef
  - deparse_expression
  - deparse_context_for
  - deparse_context_for_plan_tree
  - get_query_def
  - get_rule_expr
  - deparse_context
  - deparse_namespace
  - deparse_columns
---

PostgreSQL stores query trees, expression trees, and rule definitions as serialised node structures in catalog columns of type `pg_node_tree`. The rule deparser in `ruleutils.c` is the facility that converts those serialised trees back into human-readable SQL text. It is the engine behind the `pg_get_expr()`, `pg_get_viewdef()`, `pg_get_ruledef()`, `pg_get_indexdef()`, and `pg_get_constraintdef()` SQL functions. `EXPLAIN` also calls it internally to render qual expressions and target lists in plan output.

## What the deparser reconstructs

The deparser does not have access to the original SQL text. It works from the post-analysis node tree, which has already been through semantic analysis: column names are replaced by `Var` nodes carrying relation and attribute numbers, operator names are resolved to OIDs, implicit casts are represented explicitly in the tree, and so on. The output SQL is therefore a normalised reconstruction rather than a verbatim replay of what the user typed. Implicit casts may appear as explicit ones. Operator expressions may render differently from the original syntax if the parser chose them via implicit resolution.

This matters in several practical ways. `pg_get_viewdef()` produces the canonical form of a view definition, which may look different from the `CREATE VIEW` statement used to define it. `pg_get_expr()` renders stored expressions from `pg_index.indexprs`, `pg_attrdef.adbin`, and similar catalog columns. `pg_dump` uses both heavily to reconstruct DDL that is semantically equivalent to, but not textually identical to, the original statements.

## Two entry points: query trees and expression trees

The deparser handles two distinct kinds of input. They require different calling paths.

**Expression trees** are deparsed by `deparse_expression()` (and its internal counterpart `deparse_expression_pretty()`), which calls `get_rule_expr()` on a single `Node *`. This path is used for index expressions, check constraints, default expressions, partition key definitions, and the `pg_get_expr()` SQL function. The caller provides a `List *` of `deparse_namespace` structures that supply the context needed to resolve `Var` references to column names. For the common case of a single-relation expression — a partial index predicate, for instance — `deparse_context_for()` builds that context from a relation OID (`deparse_context_for()`, ruleutils.c).

**Query trees** are deparsed by `get_query_def()`, which dispatches to command-specific helpers (`get_select_query_def()`, `get_insert_query_def()`, `get_update_query_def()`, `get_delete_query_def()`, `get_merge_query_def()`). `pg_get_viewdef()` and `pg_get_ruledef()` use this path, as does `pg_get_querydef()` for internal callers that already hold a `Query *`. The `pg_get_expr()` SQL function explicitly rejects query trees as input and raises an error if the caller accidentally passes the text of a `pg_rewrite.ev_action` column rather than a single expression (pg_get_expr_worker(), ruleutils.c).

## The deparse context stack

Every deparsing call carries a `deparse_context` struct that accumulates output into a `StringInfo` buffer and carries a stack of `deparse_namespace` frames. Each frame describes one query level's range table and the column alias names assigned to it. The deparser resolves a `Var` with `varlevelsup = N` against the Nth entry on the namespace stack, making it possible to correctly deparse subqueries and correlated expressions in a single traversal.

The stack is populated differently depending on the scenario:

- For a standalone expression with one relation, `deparse_context_for()` creates a single-frame stack with a synthetic `RangeTblEntry`.
- For a full query tree, `set_deparse_for_query()` initialises the frame from the `Query`'s actual range table.
- For a plan tree (used by `EXPLAIN`), `deparse_context_for_plan_tree()` initialises from the `PlannedStmt`'s range table. `EXPLAIN` calls `set_deparse_context_plan()` before each expression to point the frame at the correct `Plan` node, so that it can resolve `OUTER_VAR`, `INNER_VAR`, and `INDEX_VAR` references.

The `deparse_namespace` struct holds not just the range table and alias names but also parallel arrays of `deparse_columns` structs (one per RTE) that carry per-column alias assignments. Computing those alias assignments is a non-trivial phase of its own, because columns may have been dropped, renamed, or added since the query was originally parsed. Even so, the output must still be parseable and produce the same results.

## Column alias assignment and schema evolution

One of the harder problems in deparsing stored queries is that the underlying tables may have changed since the query was parsed. A view defined over a table that later gained or lost columns must still deparse into SQL. When re-parsed, that SQL must produce the same query plan. The `deparse_columns` struct (ruleutils.c) addresses this with two arrays:

- `colnames[]`: aliases for columns that existed when the query was parsed (indexed by `varattno`; dropped columns have `NULL` entries).
- `new_colnames[]` / `is_new_col[]`: the column alias list as it would look if the query were re-parsed against the current table definition, including columns added after parsing. This is what gets printed as the column alias list in the `FROM` clause.

Aliases must be unique within each RTE to avoid ambiguity in the deparsed text. For `JOIN USING` columns, the deparser must guarantee uniqueness across the whole query, not just per-RTE, because the merged column can be referenced by name from either side. A two-pass algorithm (`set_using_names()`, ruleutils.c) handles `JOIN USING` before the per-column uniqueness logic runs for ordinary columns.

## Locking during deparsing

When `get_query_def()` is called with a query tree, it calls `AcquireRewriteLocks()` before examining the range table (get_query_def(), ruleutils.c). This ensures that any relation referenced in the query still exists and is locked at `AccessShareLock` for the duration of the deparsing call. Without this, a concurrent `DROP TABLE` could invalidate the range table entry while the deparser is reading column names from the relcache. The same lock acquisition happens in `make_ruledef()` when rendering a rule's condition, which can contain `Var` references to `OLD` and `NEW`.

For `pg_get_expr()`, `pg_get_expr_worker()` opens the relation with `try_relation_open()` rather than a hard open (ruleutils.c). If the relation has been dropped since the expression was stored, the function returns `NULL` rather than raising an error. This tolerance is intentional: callers frequently use `pg_get_expr()` to inspect catalog entries for objects that may be in the process of being dropped.

## SPI access to pg_rewrite

`pg_get_ruledef()` and `pg_get_viewdef()` fetch the rule tuple from `pg_rewrite` via SPI rather than the syscache. PostgreSQL prepares the queries once per session and keeps them as `SPIPlanPtr` globals (`plan_getrulebyoid`, `plan_getviewrule`). Using SPI instead of the syscache enforces a read-access check on `pg_rewrite` itself, so unprivileged users cannot use `pg_get_ruledef()` to read rules they do not have permission to see directly (pg_get_ruledef_worker(), pg_get_viewdef_worker(), ruleutils.c).

## Pretty-printing flags

Most entry points accept a `pretty` boolean or an integer `prettyFlags` bitmask. Three flags are defined (ruleutils.c):

| Flag | Value | Effect |
|---|---|---|
| `PRETTYFLAG_PAREN` | 0x0001 | Emit parentheses around subexpressions to improve readability |
| `PRETTYFLAG_INDENT` | 0x0002 | Insert newlines and indent clauses (FROM, WHERE, etc.) |
| `PRETTYFLAG_SCHEMA` | 0x0004 | Schema-qualify relation names only when needed for disambiguation, not always |

When pretty-printing is off, `generate_qualified_relation_name()` always schema-qualifies relation names. This is the safe default used by `pg_dump`, which requires unambiguous output regardless of `search_path`. The `quote_all_identifiers` GUC, when true, forces all identifiers through `quote_identifier()` regardless of whether quoting is necessary.

The `wrapColumn` parameter, exposed via `pg_get_viewdef_wrap()`, sets a maximum line length after which the deparser inserts a newline. Zero means wrap after every clause; `-1` disables wrapping entirely (ruleutils.c).

## get_rule_expr: the expression dispatcher

The core recursive function is `get_rule_expr()`, which switches on the node tag and calls a specialised handler for each expression type. It handles every node type that can legally appear in a catalog-stored expression: `Var`, `Const`, `Param`, `Aggref`, `WindowFunc`, `FuncExpr`, `OpExpr`, `BoolExpr`, `CaseExpr`, `SubLink`, array constructors, coercion nodes, and so on. Each handler appends its contribution to the `StringInfo` buffer in the context.

A design invariant is that each call to `get_rule_expr()` must produce an *indivisible term*: an expression that, when re-parsed, produces the same tree node originally rendered. To enforce this, operators and boolean expressions wrap their operands in parentheses when the expression structure could otherwise be ambiguous. The `isSimpleNode()` helper checks whether a node already provides enough syntactic structure that extra parentheses are unnecessary (ruleutils.c).

Implicit casts present a specific challenge. The deparser has a `showimplicit` parameter that, when true, renders implicit casts as explicit `CAST(... AS type)` or `::type` syntax. This is important for function and operator arguments where the implicit resolution must be preserved across a dump-reload cycle: if the target type were not shown, the parser might resolve the function differently after reloading.

## Public C API

The header `src/include/utils/ruleutils.h` exposes the following C-callable functions for use by the planner, executor, and EXPLAIN:

| Function | Purpose |
|---|---|
| `deparse_expression(expr, dpcontext, forceprefix, showimplicit)` | Deparse a single expression node to text |
| `deparse_context_for(aliasname, relid)` | Build a single-relation deparse context |
| `deparse_context_for_plan_tree(pstmt, rtable_names)` | Build context for an entire plan tree |
| `set_deparse_context_plan(dpcontext, plan, ancestors)` | Focus context on a specific plan node |
| `select_rtable_names_for_explain(rtable, rels_used)` | Compute RTE aliases for EXPLAIN output |
| `pg_get_indexdef_string(indexrelid)` | Full index definition including tablespace |
| `pg_get_constraintdef_command(constraintId)` | Constraint definition as an `ADD CONSTRAINT` command |

## Related Topics

- [[subsystems/rewriter/overview|Query Rewriter Overview]] — how rule bodies stored in `pg_rewrite` are applied at rewrite time
- [[subsystems/rewriter/rules-vs-triggers|Rules vs Triggers]] — where rules fit relative to triggers in the query pipeline
- [[subsystems/planner/overview|Planner Overview]] — the planner uses `deparse_expression` to render qual expressions in `EXPLAIN` output

---
title: Parse Tree Serialisation and Debug Inspection
aliases:
  - node serialisation
  - nodeToString
  - debug_print_parse
  - parse tree debugging
tags:
  - theme/observability
source_files:
  - src/backend/nodes/print.c
  - src/backend/nodes/outfuncs.c
  - src/backend/nodes/read.c
  - src/backend/nodes/nodes.c
  - src/include/nodes/nodes.h
  - src/backend/tcop/postgres.c
symbols:
  - nodeToString
  - stringToNode
  - outNode
  - nodeRead
  - pg_strtok
  - elog_node_display
  - pprint
  - format_node_dump
  - pretty_format_node_dump
  - Debug_print_parse
  - Debug_print_rewritten
  - Debug_print_plan
  - Debug_pretty_print
---

Every internal structure that PostgreSQL builds during query processing — raw parse trees, analysed query trees, rewritten query trees, and plan trees — is a node: a C struct whose first field is a `NodeTag` enum value that identifies its type at runtime. This tagging convention makes it possible to traverse, copy, compare, and serialise any tree without knowing its concrete type at compile time. The serialisation layer (`nodeToString` / `stringToNode`) carries query trees across process boundaries, and it also makes the debug GUCs possible.

## The Node System

Every struct used in parse and plan trees embeds `NodeTag` as its first field. The `makeNode(T)` macro assigns the tag at allocation time: it palloc-zeroes the struct, then writes `T_<typename>` into `type`. The global enum `NodeTag` (generated from `nodes/nodetags.h`) lists every concrete node type. The values are never persisted to disk, so they can be renumbered freely between major releases. Renumbering is not safe within a stable branch, though, because it would break extension ABI.

The `IsA(ptr, T)` macro compares the tag against `T_<T>`. Generic code (copiers, equality checkers, outfuncs, readfuncs) switches on `nodeTag(obj)` to dispatch to per-type handlers. Because every tree-node struct starts with the same field, code can safely cast a `Node *` pointer to the concrete type once it knows the tag.

Leaf value nodes — `Integer`, `Float`, `Boolean`, `String`, and `BitString` — are lightweight wrappers that carry scalar constants inside list structures. `outNode` and `nodeRead` handle them specially, without the `{ }` braces used for compound nodes.

## Serialisation: outfuncs and nodeToString

`nodeToString(obj)` is the public entry point for serialisation. It allocates a `StringInfo` buffer and calls `outNode(str, obj)`. `outNode` dispatches on the node's tag:

- `outNode` writes lists and typed lists (`IntList`, `OidList`, `XidList`) as parenthesised sequences: `(i 1 2 3)` for an integer list, `(node node ...)` for a node list.
- `outNode` writes value nodes (`Integer`, `Float`, `Boolean`, `String`, `BitString`) as bare tokens.
- `outNode` wraps all other compound nodes in `{ }`. It writes the opening brace. Then a generated `outfuncs.switch.c` (included at compile time) dispatches to a per-type output function. Each output function writes field values in `:fieldname value` pairs. After the per-type function returns, `outNode` writes the closing `}`.

The per-type output functions live in `outfuncs.c`. `gen_node_support.pl` generates most of them from the node struct definitions. `outNode` skips struct fields annotated with `read_write_ignore`. It writes fields annotated `no_read`, but the reader does not expect to read them back. During ordinary serialisation, `outNode` normally writes location fields (source byte offsets) as `-1`. Restoring them requires a special debug build flag (`WRITE_READ_PARSE_PLAN_TREES`).

The result is a text encoding that looks like a simplified Lisp S-expression. A `Const` node for the integer 42 serialised as type `int4` looks something like:

```
{CONST :consttype 23 :consttypmod -1 :constcollid 0 :constlen 4
 :constbyval true :constisnull false :constvalue 4 [ 42 0 0 0 ]}
```

## Deserialisation: read.c and readfuncs

`stringToNode(str)` is the inverse of `nodeToString`. It sets a module-level pointer (`pg_strtok_ptr`) to the start of the string, then calls `nodeRead(NULL, 0)`. `nodeRead` drives the recursive descent.

The tokeniser `pg_strtok` reads one token at a time. It recognises four single-character structural tokens (`(`, `)`, `{`, `}`), bare scalars (integers, floats, booleans, bit strings), and double-quoted strings. The `<>` token represents a NULL pointer. Backslashes can escape whitespace or structural characters inside tokens.

`nodeRead` classifies each token with `nodeTokenType` and takes one of several paths:

- `{` — calls `parseNodeString()` from `readfuncs.c`, which reads the node type name and dispatches to a per-type read function, then expects a closing `}`.
- `(` — reads the list discriminator character (`i`, `o`, `x`, `b`, or a node) and builds the appropriate `List` or `Bitmapset`.
- Scalar tokens — constructs the matching `Integer`, `Float`, `Boolean`, `String`, or `BitString` value node.
- `<>` — returns `NULL`.

Re-entrancy is safe. `stringToNode` saves and restores the `pg_strtok_ptr` global, so nested calls work correctly. Nested calls occur when a node field itself holds a serialised sub-tree that is stored as a text column.

## Where Serialisation Is Used in Production

`nodeToString` and `stringToNode` are not only debug tools — they are load-bearing infrastructure:

**Parallel query.** When the executor launches parallel workers, it serialises the `PlannedStmt` with `nodeToString`. It places the result in shared memory. Each worker calls `stringToNode` to reconstruct a local copy of the plan before starting execution.

**Stored expressions.** PostgreSQL stores check constraints, index expressions, partial index predicates, and column defaults in `pg_constraint`/`pg_index`/`pg_attrdef` as the text output of `nodeToString`. The planner calls `stringToNode` to deserialise them when building access paths.

**Logical replication.** PostgreSQL stores and transmits row filters for publications as serialised node trees. The output plugin deserialises them with `stringToNode`.

## The Debug GUCs

Four GUCs control logging of intermediate trees during query processing. They write to the server log at `LOG` level, so the output is visible in the PostgreSQL log file or — in a session with `client_min_messages = log` — directly to the client.

| GUC | What it logs | When |
|-----|-------------|------|
| `debug_print_parse` | The `Query` node after semantic analysis | Before the rewriter |
| `debug_print_rewritten` | The list of `Query` nodes after rewriting | After the rewriter |
| `debug_print_plan` | The `PlannedStmt` after planning | After the planner |
| `debug_pretty_print` | Controls formatting for the above three | — |

All three printing GUCs call `elog_node_display`. `elog_node_display` calls `nodeToString` on the tree, then passes the result through either `format_node_dump` (compact, line-wrapped at 78 characters) or `pretty_format_node_dump` (indented, breaks on `{`, `}`, `:`, and `)`), depending on `debug_pretty_print`.

To activate them for a single session without restarting:

```sql
SET debug_print_parse    = on;
SET debug_print_rewritten = on;
SET debug_print_plan     = on;
SET debug_pretty_print   = on;
SET client_min_messages  = log;
```

The next query executed will emit the trees to the client. Turn on `debug_pretty_print`; otherwise the output is a single long line.

## Reading the Output

### Query node (parse and rewrite stages)

The root node after analysis is a `Query`. The most important fields:

- `:commandType` — an integer encoding the command: `1` = `SELECT`, `2` = `UPDATE`, `3` = `INSERT`, `4` = `DELETE`, `6` = `UTILITY`.
- `:rtable` — the range table: a list of `RangeTblEntry` nodes, one per table, subquery, join, or function reference. PostgreSQL numbers entries from 1. `Var` nodes reference them by this index via `:varno`.
- `:jointree` — a `FromExpr` that contains the FROM-list entries (as `RangeTblRef` or `JoinExpr` nodes) and the WHERE qual. This is the structure the planner uses to enumerate join orders.
- `:targetList` — a list of `TargetEntry` nodes, one per output column. Each `TargetEntry` wraps the expression (`expr`) with output metadata (`resname`, `resno`).
- `:havingQual`, `:groupClause`, `:sortClause`, `:limitOffset`, `:limitCount` — aggregation and result-ordering information.

### PlannedStmt node (plan stage)

After planning, the planner replaces the `Query` with a `PlannedStmt`:

- `:planTree` — the root plan node (e.g., `SeqScan`, `HashJoin`, `Agg`). Each plan node has `:lefttree` and `:righttree` children, forming the execution tree.
- `:rtable` — a new range table for the executor; similar structure to the query-level one but with executor-specific fields filled in.
- `:subplans` — list of plan trees for subqueries and CTEs that run as separate plan trees.

Plan nodes carry `:startup_cost`, `:total_cost`, `:plan_rows`, and `:plan_width` — the planner's cost estimates. These are useful for cross-checking `EXPLAIN` output.

## Formatting Details

`pretty_format_node_dump` works by scanning the flat string produced by `nodeToString` and re-emitting it with structural indentation. It tracks an indent level (`indentLev`) incremented on `{` and decremented on `}`, capped at 60 spaces. Field separators (`:`) always start a new line at the current indent. Closing braces and parentheses also force line breaks. This means the pretty output is deterministic given the same node tree — useful for diffing tree output across PostgreSQL versions or for comparing before/after a planner change.

## Debugging Workflow

A typical debugging session for an unexpected query plan:

```sql
SET debug_pretty_print   = on;
SET debug_print_parse    = on;
SET debug_print_plan     = on;
SET client_min_messages  = log;

EXPLAIN SELECT ...;
```

The parse tree confirms whether the parser and analyser interpreted the query as intended — checking `commandType`, the `rtable` entries, and `jointree` qual conditions. The plan tree shows which plan node types the planner chose, with cost estimates beside each node. Comparing `EXPLAIN (ANALYZE)` output against the plan tree's `:plan_rows` estimates pinpoints cardinality misestimation.

The `print_rt`, `print_tl`, and `print_expr` functions in `print.c` provide lower-level debugging helpers that decode range table and target list structures into human-readable name/relation pairs. These are available from a debugger session but not exposed as SQL functions.

## Related Topics

- [[subsystems/parser/semantic-analysis|parse analysis]] — how the raw parse tree becomes the analysed `Query` node that `debug_print_parse` shows
- [[subsystems/rewriter/overview|rewriter]] — transforms the `Query` list that `debug_print_rewritten` captures
- [[subsystems/planner/overview|planner overview]] — produces the `PlannedStmt` that `debug_print_plan` serialises
- [[subsystems/observability/pg-stat-statements]] — uses the query jumble infrastructure that also operates on node trees

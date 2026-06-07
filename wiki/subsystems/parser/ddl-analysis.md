---
title: "DDL Semantic Analysis"
aliases:
  - parse_utilcmd
  - transformCreateStmt
  - transformAlterTableStmt
  - DDL Parse Analysis
source_files:
  - src/backend/parser/parse_utilcmd.c
symbols:
  - transformCreateStmt
  - transformAlterTableStmt
  - transformIndexStmt
  - transformRuleStmt
  - transformStatsStmt
  - CreateStmtContext
  - CreateSchemaStmtContext
  - generateSerialExtraStmts
  - transformColumnDefinition
  - transformIndexConstraints
  - transformFKConstraints
---

DDL statements require a different kind of semantic analysis than DML. Unlike a `SELECT`, a `CREATE TABLE` cannot be analyzed once at parse time and cached safely — the database state it depends on may have changed between planning and execution. Ordinary `parse_analyze()` in `analyze.c` runs during query analysis. Its results can be cached in `pg_prepared_statements` or by PL/pgSQL, but a cached plan depends on catalog state that might become stale. Utility (DDL) commands have no infrastructure for lock retention or plan revalidation, so any catalog lookups they perform must happen at execution time under the locks the command is about to acquire. `parse_utilcmd.c` is the home for this deferred DDL analysis. `transformCreateStmt`, `transformAlterTableStmt`, `transformIndexStmt`, and the other functions in this file are invoked from `utility.c` during execution rather than from `analyze.c` during parse. They convert DDL parse trees into executable command sequences.

## The CreateStmtContext

All `CREATE TABLE` and `ALTER TABLE` analysis threads through a single working struct, `CreateStmtContext` (`parse_utilcmd.c`). It accumulates parsed elements as the statement is decomposed:

| Field | Purpose |
|---|---|
| `pstate` | Underlying `ParseState` for expression analysis and error location |
| `stmtType` | String literal for error messages (`"CREATE TABLE"`, `"ALTER TABLE"`, etc.) |
| `relation` | The target `RangeVar` |
| `rel` | Opened `Relation`, only set in the `ALTER TABLE` path |
| `isalter` / `isforeign` / `ispartitioned` | Context flags that gate certain validations |
| `columns` | Accumulated `ColumnDef` list |
| `ckconstraints` | `CHECK` constraint nodes to process |
| `fkconstraints` | `FOREIGN KEY` constraint nodes to process |
| `ixconstraints` | Index-creating constraints (`PRIMARY KEY`, `UNIQUE`, `EXCLUDE`) |
| `likeclauses` | `LIKE` clauses needing post-creation processing |
| `blist` | Commands to execute *before* the main `DefineRelation()` |
| `alist` | Commands to execute *after* `DefineRelation()` |
| `pkey` | The single `PRIMARY KEY` `IndexStmt`, if any |

The separation of `blist` and `alist` is a fundamental design choice. A serial or identity column requires a `CREATE SEQUENCE` to exist before the table is created (it goes into `blist`), while `ALTER SEQUENCE ... OWNED BY` and foreign key constraints must come after the table exists (they go into `alist`). `transformCreateStmt()` returns the full ordered list — `blist` + the `CreateStmt` itself + `alist`. The executor runs them in sequence.

## CREATE TABLE Analysis

`transformCreateStmt()` opens by resolving the target namespace with `RangeVarGetAndCheckCreationNamespace()`. This function locks the namespace against concurrent drops, checks permissions, and detects pre-existing relations (handling `IF NOT EXISTS`). It also schema-qualifies the relation name early to prevent later ambiguity if rewritten commands encounter other relations with the same unqualified name.

The main body iterates over `stmt->tableElts`, dispatching each element:

- `T_ColumnDef` → `transformColumnDefinition()` — resolves the column's type, expands serial pseudo-types, validates constraints, and dispatches index-creating constraints into `cxt.ixconstraints`.
- `T_Constraint` → `transformTableConstraint()` — routes table-level constraints into `ckconstraints`, `fkconstraints`, or `ixconstraints`.
- `T_TableLikeClause` → `transformTableLikeClause()` — opens the source relation under `AccessShareLock` and copies column definitions, constraints, indexes, and statistics according to the `INCLUDING` options specified.

After this pass, three postprocessing steps run in a fixed order: `transformIndexConstraints()`, `transformFKConstraints()`, and `transformCheckConstraints()`. Order matters. `LIKE ... INCLUDING INDEXES` clauses that might create a primary key must be processed after the primary index from the main column list. This lets LIKE-generated primary keys be detected as duplicates.

## Serial and Identity Column Expansion

`SERIAL`, `BIGSERIAL`, and `SMALLSERIAL` are not real types. `transformColumnDefinition()` detects them by name (e.g. `"bigserial"`, `"serial8"`). It replaces the type with the underlying integer OID (`INT8OID`, etc.). It calls `generateSerialExtraStmts()` (`parse_utilcmd.c`). That function builds:

1. A `CreateSeqStmt` pushed onto `cxt.blist` — the sequence must exist before the table.
2. An `AlterSeqStmt ... OWNED BY` pushed onto `cxt.alist` — this dependency link cannot be established until the table and its column attnums exist.

It also synthesizes a `DEFAULT nextval(...)` constraint and a `NOT NULL` constraint. It appends both to the column's constraint list. The SERIAL expansion is therefore entirely a parse-analysis concern. By execution time, the planner and executor see only ordinary integer columns with sequence-based defaults.

Identity columns (`GENERATED ALWAYS AS IDENTITY`) follow the same path through `generateSerialExtraStmts()`, but they use `for_identity = true`. This setting affects sequence ownership semantics and prevents the sequence from being used directly with `nextval()`.

## Index-Creating Constraints

`transformIndexConstraints()` walks `cxt.ixconstraints`. It converts each `PRIMARY KEY`, `UNIQUE`, or `EXCLUDE` constraint into an `IndexStmt`. For `PRIMARY KEY`, it additionally marks the relevant columns `is_not_null = true` inline (for columns defined in the same `CREATE TABLE`) or generates `AT_SetNotNull` `AlterTableCmd` nodes (for inherited or pre-existing columns). `transformIndexConstraints()` merges redundant index specifications — e.g. a column with both `UNIQUE` and `PRIMARY KEY` — rather than flagging them as errors.

The resulting `IndexStmt` nodes land in `cxt.alist`, ensuring `DefineIndex()` runs after `DefineRelation()` has created the heap. The `pkey` slot in `CreateStmtContext` tracks the primary key `IndexStmt` specifically, so that `transformIndexConstraints()` places it first among indexes, before any duplicate-detection pass.

## Foreign Key Constraint Handling

`transformFKConstraints()` has a deliberate ordering dependency: it must run after `transformIndexConstraints()`. The executor processes a FK constraint during `ADD CONSTRAINT`. At that point, it assumes that any supporting indexes — such as the primary key on the referenced column — already exist in `cxt.alist`. For `CREATE TABLE`, `transformFKConstraints()` wraps FK constraints into a separate `ALTER TABLE ADD CONSTRAINT` command. It appends the command to `cxt.alist`. For `ALTER TABLE ADD CONSTRAINT`, `transformFKConstraints()` leaves them for the caller to attach directly. New-table FK constraints have `skip_validation = true` since the table is empty.

## ALTER TABLE Analysis

`transformAlterTableStmt()` (`parse_utilcmd.c`) mirrors the `CREATE TABLE` path but runs under an open relation. The caller has already acquired the appropriate lock (via `AlterTableGetLockLevel()` in `tablecmds.c`; see [[subsystems/catalog/ddl-locking|DDL Locking]]). This function opens the relation with `NoLock` using the passed-in `relid` rather than `stmt->relation`, to avoid race conditions.

Most `AlterTableCmd` subtypes pass straight through unchanged. The subtypes that do require transformation:

| Subtype | Transformation |
|---|---|
| `AT_AddColumn` | Calls `transformColumnDefinition()` on the new `ColumnDef`; serial/identity columns generate sequence stmts |
| `AT_AddConstraint` | Routes via `transformTableConstraint()` into the appropriate constraint list |
| `AT_AlterColumnType` | Transforms the `USING` expression via `transformExpr()`; generates `ALTER SEQUENCE ... AS` for identity columns |
| `AT_AddIdentity` | Calls `generateSerialExtraStmts()` with `col_exists = true` |
| `AT_SetIdentity` | Splits sequence options from table options; generates `ALTER SEQUENCE` |
| `AT_AttachPartition` / `AT_DetachPartition` | Calls `transformPartitionCmd()` to evaluate and validate partition bounds |

After processing individual subcommands, the same postprocessing trio runs: `transformIndexConstraints()`, `transformFKConstraints()`, and `transformCheckConstraints()`. `transformAlterTableStmt()` folds index stmts from this pass back into the `AlterTableCmd` list as `AT_AddIndex` or `AT_AddIndexConstraint` subcommands, so `tablecmds.c` can schedule them appropriately relative to other subcommands.

## Index and Statistics Expression Analysis

`transformIndexStmt()` handles expression indexes and partial index predicates. It is a no-op if the index has no expressions and no `WHERE` clause (the common case). Callers that know they have only simple column indexes can skip it. For expression indexes, it constructs a minimal `ParseState` with only the indexed relation in the range table. It transforms each `IndexElem.expr` via `transformExpr()` with `EXPR_KIND_INDEX_EXPRESSION`. It assigns collations. `transformIndexStmt()` enforces the same single-relation restriction: any reference to a second table in an index expression is an error.

`transformStatsStmt()` follows the identical pattern for `CREATE STATISTICS` expressions, using `EXPR_KIND_STATS_EXPRESSION`. Both functions set a `transformed` flag on the statement node. If either is called again — for example, after a `LIKE ... INCLUDING INDEXES` post-creation expansion — it short-circuits immediately.

## Rule Statement Analysis

`transformRuleStmt()` is notably different from the other functions in this file: it acquires `AccessExclusiveLock` on the target relation itself, rather than relying on the caller to have done so. The comment explains this is to avoid deadlock: `DefineQueryRewrite()` needs `AccessExclusiveLock`. Acquiring a lesser lock first and then upgrading would be unsafe. The function sets up a two-entry range table with `OLD` at varno 1 and `NEW` at varno 2 (a fixed convention required by the rule system). It transforms the rule's `WHERE` qualification. It runs each action statement through a sub-`ParseState` via `parse_sub_analyze()`. `transformRuleStmt()` explicitly rejects OLD/NEW references in CTEs.

## CREATE SCHEMA Element Ordering

`transformCreateSchemaStmtElements()` does not perform deep semantic analysis. It classifies each element of a `CREATE SCHEMA` body into typed buckets (`sequences`, `tables`, `views`, `indexes`, `triggers`, `grants`). It emits them in that fixed order. This ordering ensures that sequences exist before tables that reference them. It also ensures that tables exist before indexes and triggers that reference them. `transformCreateSchemaStmtElements()` applies schema qualification to any element that lacks an explicit schema name.

## Related Topics

- [[subsystems/parser/semantic-analysis|Semantic Analysis (parse_analyze)]] — the DML counterpart; `transformStmt()` and `ParseState`
- [[subsystems/catalog/ddl-locking|DDL Locking]] — lock modes chosen before these analysis functions run
- [[subsystems/parser/overview|Parser Overview]] — the grammar and raw parse tree stage

---
title: CREATE TABLE
aliases:
  - create table internals
  - DefineRelation
  - heap_create_with_catalog
source_files:
  - src/backend/commands/tablecmds.c
  - src/backend/catalog/heap.c
  - src/backend/catalog/pg_constraint.c
symbols:
  - DefineRelation
  - heap_create
  - heap_create_with_catalog
  - AddNewRelationTuple
  - AddNewAttributeTuples
  - InsertPgAttributeTuples
  - InsertPgClassTuple
  - AddNewRelationType
  - StoreConstraints
  - StoreRelCheck
  - StoreAttrDefault
  - AddRelationNewConstraints
  - MergeAttributes
  - StoreCatalogInheritance
  - CreateConstraintEntry
  - CreateStmt
---

# CREATE TABLE

Creating a table is one of the most catalog-intensive operations PostgreSQL performs. A single `CREATE TABLE` statement populates at least three system catalogs, optionally allocates physical storage on disk, and may trigger index creation and trigger installation. All of this happens inside a single transaction. Understanding how it works reveals the relationship between the logical schema world (types, constraints, defaults) and the physical world (relation forks, files managed by the storage manager).

## Two-phase execution

The SQL text `CREATE TABLE foo (...)` reaches the executor as a `CreateStmt` parse node, the output of the parser's `transformCreateStmt()` in `parse_utilcmd.c`. By the time the node arrives at the dispatcher for utility commands, the parser has resolved column types, expanded `LIKE` clauses (to the extent possible without a live relation), and walked the column list for obvious conflicts like duplicate names. The parser leaves the raw constraint expressions and default expressions as unparsed trees at this stage, because they require a live relcache entry before `transformExpr()` can work on them.

The dispatcher hands execution of `CreateStmt` to `DefineRelation()` in `tablecmds.c`. `DefineRelation()` orchestrates everything. Its first job is resolving the inheritance list, resolving the tablespace, and deciding the `relkind`. Notably, if a `PARTITION BY` clause is present, the kind flips from `RELKIND_RELATION` to `RELKIND_PARTITIONED_TABLE`. This happens before any catalog work begins.

`DefineRelation()` then calls `MergeAttributes()` to fuse the explicit column list with any inherited columns. This builds a unified column list that becomes the `TupleDesc` passed downstream. Only after that flat descriptor is ready does the call to `heap_create_with_catalog()` happen. This call does the real catalog and storage work.

## Catalog anatomy

"Creating a table" in PostgreSQL means writing rows into several system catalogs. No single catalog entry is sufficient on its own; the relcache is assembled by joining all of them at open time.

### pg_class — the relation descriptor

Every relation, view, index, sequence, and materialized view has exactly one row in `pg_class`. For a new table, `heap_create_with_catalog()` (catalog/heap.c) builds this row via `AddNewRelationTuple()` and `InsertPgClassTuple()`. The row captures the relation's identity and statistics hint: the OID, namespace, owner, the OID of the access method (`relam`), persistence flag, relkind character, and the initial `relfrozenxid` / `relminmxid` used by vacuum's freeze tracking. Statistics fields (`relpages`, `reltuples`) start at zero and -1 respectively. The planner treats -1 as "unknown" and falls back to defaults until `ANALYZE` runs.

Key fixed fields of `pg_class` worth knowing:

| Field | Meaning |
|---|---|
| `relkind` | `'r'` regular table, `'p'` partitioned, `'i'` index, `'v'` view, etc. |
| `relpersistence` | `'p'` permanent, `'u'` unlogged, `'t'` temporary |
| `relam` | OID of the table access method (heap by default) |
| `relfilenode` | The physical file number (0 for mapped or partitioned relations) |
| `relfrozenxid` | Oldest XID in the table; vacuum advances this toward `datfrozenxid` |
| `relhasindex` | Set true the first time an index is created on this relation |
| `relchecks` | Count of CHECK constraints; kept in sync to avoid a full scan |
| `relnatts` | Number of user-visible columns |

### pg_attribute — one row per column

Each column of the new table gets a row in `pg_attribute`. `AddNewAttributeTuples()` inserts these rows in bulk, calling `InsertPgAttributeTuples()` (catalog/heap.c). `AddNewAttributeTuples()` also inserts the system columns (`ctid`, `xmin`, `xmax`, `cmin`, `cmax`, `tableoid`) here as rows with negative `attnum` values. This makes them first-class catalog citizens even though they are not user-visible.

The most semantically important fields:

| Field | Meaning |
|---|---|
| `attnum` | 1-based column position; negative for system columns |
| `atttypid` | OID of the column's data type |
| `atttypmod` | Type-specific modifier (e.g., `VARCHAR(n)` length) |
| `attnotnull` | True if a NOT NULL constraint applies |
| `atthasdef` | True if a default expression exists in `pg_attrdef` |
| `attisdropped` | Soft-delete flag; dropped columns keep their slot |
| `attislocal` | False for columns inherited from a parent |
| `attinhcount` | Number of parents that define this column |
| `attstattarget` | Statistics collection target (-1 = use system default) |

After insertion, `AddNewAttributeTuples()` also records `DEPENDENCY_NORMAL` links from each column to its type and collation, so that dropping a type cascades correctly.

### pg_type — the composite row type

Every regular table, view, and composite type gets an entry in `pg_type` because PostgreSQL's type system is unified. A table's row type is a first-class type that can be used as a column type in another table. `AddNewRelationType()` inside `heap_create_with_catalog()` creates the composite type entry. It calls `TypeCreate()` to write the `pg_type` row with `typtype = 'c'` (composite). The same function also registers an array type over the composite row type at this time. This is why `SELECT ARRAY[myrow]` works for any table.

Sequences, [[subsystems/storage/toast|toast]] tables, and indexes are the exceptions: they do not get composite type entries. This reflects that they are internal implementation details rather than user-facing types.

## Physical storage allocation

The catalog work and storage allocation happen in a deliberate order: the relcache entry is built first, then storage is created, then the catalog rows are inserted. This ordering matters for two reasons. `smgrcreate()` needs the `RelFileLocator` that the relcache entry provides. The catalog insert needs the OID chosen before the relcache is populated.

`heap_create()` (catalog/heap.c) is the function that bridges these two worlds. It calls `RelationBuildLocalRelation()` to construct an in-memory relcache entry (a `Relation` struct). Then, if the relkind has storage (`RELKIND_HAS_STORAGE`), it delegates to the table access method's `relation_set_new_filelocator` callback. For heap tables, this calls `RelationCreateStorage()` via the storage manager interface. `RelationCreateStorage()` ultimately invokes `smgrcreate()` to create the relation's fork files on disk.

The storage model uses three forks per heap relation:

- **main fork** — the actual tuple data pages
- **FSM fork** ([[subsystems/storage/fsm|free space map]]) — tracks free space per page for `INSERT` placement
- **VM fork** ([[subsystems/storage/visibility-map|visibility map]]) — one bit per page for all-visible and all-frozen status

At table creation only the main fork exists; PostgreSQL creates the FSM and VM on demand, when the first `VACUUM` or write touches them. For partitioned tables (`RELKIND_PARTITIONED_TABLE`), `create_storage` is forced false — there is no physical data file because all rows live in partitions.

The `relpersistence` flag determines how the storage manager behaves: permanent tables go to the default tablespace, unlogged tables skip WAL for their data pages, and temporary tables are placed in a per-session temporary tablespace and are automatically dropped at session end.

## Constraints

Constraints are not a single mechanism — PostgreSQL uses different storage and enforcement strategies depending on the constraint type.

### NOT NULL

The simplest constraint: a boolean flag `attnotnull` on the `pg_attribute` row. `MergeAttributes()` sets this when building the column descriptor. `InsertPgAttributeTuples()` writes it to disk. A basic NOT NULL constraint has no row in `pg_constraint`; the heap access method's tuple validation checks it at INSERT and UPDATE time. This is why `attnotnull` information is so cheap to check — no catalog join is needed.

### CHECK constraints

`StoreRelCheck()` (catalog/heap.c) stores check constraints in `pg_constraint` with `contype = 'c'`. `StoreConstraints()` calls `StoreRelCheck()` after the relation is in the catalog. `StoreRelCheck()` serializes the constraint expression to its `nodeToString()` form and stores it in the `conbin` column. At INSERT or UPDATE time, `ExecConstraints()` evaluates the stored expression tree using `ExecEvalExpr()`. PostgreSQL also maintains the count of CHECK constraints in `pg_class.relchecks`. This lets the executor skip constraint evaluation entirely when the count is zero — a significant optimization for bulk loads.

### PRIMARY KEY and UNIQUE

PostgreSQL enforces these constraints through indexes rather than per-row checks. `DefineRelation()` does not create the index directly. Instead, `AddRelationNewConstraints()` (catalog/heap.c) emits an `IndexStmt`. PostgreSQL queues this statement and executes it after the basic catalog work is done. The resulting `pg_constraint` row has `contype = 'p'` or `'u'`. Its `conindid` column points to the index relation. The dependency graph runs from the constraint to the index, not the other way. Dropping the constraint therefore cascades to drop the index. The index cannot be dropped independently while the constraint exists.

### FOREIGN KEY

PostgreSQL stores foreign key constraints in `pg_constraint` with `contype = 'f'`. The `conrelid` column names the referencing table. The `confrelid` column names the referenced table. `conkey` and `confkey` are int2 arrays of attribute numbers. `confupdtype` / `confdeltype` encode the ON UPDATE / ON DELETE action with single-character codes (`'a'` = NO ACTION, `'c'` = CASCADE, `'n'` = SET NULL, `'d'` = SET DEFAULT, `'r'` = RESTRICT).

Enforcement uses row-level triggers, not a post-INSERT scan. `ATAddForeignKeyConstraint()` in `tablecmds.c` installs four constraint triggers on the referencing table (for INSERT and UPDATE checking) and on the referenced table (for DELETE and UPDATE propagation). The trigger mechanism is what makes foreign key enforcement transactional and compatible with deferred constraints. A `DEFERRABLE INITIALLY DEFERRED` foreign key, for example, simply delays the trigger fire until transaction commit.

### pg_constraint fields reference

| Field | Meaning |
|---|---|
| `contype` | `'c'` CHECK, `'f'` FOREIGN KEY, `'p'` PRIMARY KEY, `'u'` UNIQUE, `'x'` EXCLUSION |
| `conrelid` | OID of the table owning this constraint (0 for domain constraints) |
| `conindid` | OID of the supporting index (for PK, UNIQUE, EXCLUSION, FK) |
| `conkey` | int2[] of attribute numbers of constrained columns |
| `confrelid` | For FK: OID of the referenced table |
| `confkey` | For FK: int2[] of referenced column attribute numbers |
| `conislocal` | False if constraint was inherited; true if defined on this table |
| `coninhcount` | Number of inheritance parents that contribute this constraint |
| `condeferrable` / `condeferred` | Whether constraint can/does defer to end of transaction |
| `convalidated` | False for constraints added with NOT VALID; validated separately |

## Default expressions

PostgreSQL does not store a column default in `pg_attribute`; it lives in `pg_attrdef`, one row per column that has a default. The separation exists because most columns have no default, and `pg_attribute` is wide enough already.

The `atthasdef` flag in `pg_attribute` is the fast path: if it is false, no lookup of `pg_attrdef` is needed. When it is true, the executor's `ExecInsert()` path fetches the default expression and evaluates it with `ExecEvalExpr()` at INSERT time.

There is a deliberate split in when defaults are processed during `CREATE TABLE`. `StoreConstraints()` can immediately store pre-cooked defaults — those inherited from a parent, already in expression tree form. Raw defaults — new defaults written directly in the `CREATE TABLE` statement — must wait until `CommandCounterIncrement()` makes the new relation visible to a catalog scan, because `transformExpr()` needs to resolve column references against the live relation. `AddRelationNewConstraints()` handles this second pass. It calls `StoreAttrDefault()` to write the `pg_attrdef` row once expression transformation is complete.

Generated columns (`GENERATED ALWAYS AS (expr) STORED`) follow the same `pg_attrdef` storage path, but PostgreSQL flags them with `attgenerated = 's'` in `pg_attribute`. The executor evaluates them on every INSERT and UPDATE, rather than only when the column value is absent.

## The LIKE clause

`LIKE other_table` is a compile-time copy, not a runtime relationship. The parser's `transformTableLikeClause()` in `parse_utilcmd.c` reads the source table's `pg_attribute` rows. It produces `ColumnDef` nodes. `transformTableLikeClause()` splices these nodes into the column list before `MergeAttributes()` runs. By the time `DefineRelation()` sees the `CreateStmt`, the liked columns are indistinguishable from explicitly declared columns.

The `INCLUDING` options control what else is copied:

- `INCLUDING DEFAULTS` — copies `pg_attrdef` expressions as cooked defaults
- `INCLUDING CONSTRAINTS` — copies CHECK constraint expressions
- `INCLUDING INDEXES` — defers index creation until after the table exists, since indexes need a live relation
- `INCLUDING STORAGE` — copies per-column storage settings (`attstorage`)
- `INCLUDING COMMENTS` — copies column comments

Notably, `LIKE` does not establish an inheritance relationship; there is no `pg_inherits` row. The two tables are entirely independent after creation. This makes `LIKE` useful for creating template-like table shapes without the behavioral coupling that inheritance implies.

## Inheritance

Table inheritance records the parent-child relationship in `pg_inherits`, one row per (child, parent) pair. Each row has an `inhseqno` sequence number. This number determines the ordering when a child has multiple parents. `StoreCatalogInheritance()` in `tablecmds.c` writes these rows. It also marks each parent with `relhassubclass = true` in `pg_class`. This flag triggers the planner to consider all children in queries against the parent.

Inherited columns arrive via `MergeAttributes()`. This function merges the parent's column list into the child's schema before the `TupleDesc` is built. The resulting `pg_attribute` rows for inherited columns have `attislocal = false`. Their `attinhcount` counts the number of contributing parents. If the child re-declares a column from a parent, `MergeAttributes()` verifies type compatibility and merges the definitions, with the child's default taking precedence.

By default, `StoreConstraints()` propagates parent CHECK constraints to the child's `pg_constraint` rows, with `conislocal = false` and the appropriate `coninhcount`. `MergeAttributes()` inherits NOT NULL simply by copying `attnotnull` into the merged attribute.

The `ONLY` keyword in queries (`SELECT ... FROM ONLY parent`) suppresses child scanning; the planner detects this and omits child relations from the plan. Inheritance queries that do not use `ONLY` result in append plans over all children. This is why `relhassubclass` is a critical flag: when it is false, the planner can short-circuit the child lookup entirely.

## Partitioned tables

`CREATE TABLE ... PARTITION BY range|list|hash (key)` creates a partitioned table: a logical container with a partition key but no physical storage of its own. The `relkind` is `RELKIND_PARTITIONED_TABLE`, and `create_storage` is false in the `heap_create()` call. As a result, no fork files are allocated.

PostgreSQL stores the definition of the partition key in `pg_partitioned_table`, one row per partitioned table. This row records the strategy (`'r'`, `'l'`, `'h'`), the attribute numbers of the key columns, and any expressions used in the partition key. Adding a partition (`CREATE TABLE child PARTITION OF parent FOR VALUES ...`) goes through `DefineRelation()` with a `partbound` clause. The child relation is a normal heap relation, but it has `relispartition = true` in its `pg_class` row and records its partition bounds in `pg_class.relpartbound`.

Because a partitioned table has no storage, queries route rows to a specific partition using routing logic in the executor (see [[code-paths/insert]]). The planner uses the partition descriptor to prune partitions that cannot contain the queried rows.

## Transactional DDL

All catalog insertions performed by `CREATE TABLE` — the `pg_class` row, the `pg_attribute` rows, the `pg_type` entry, any `pg_constraint` rows — are ordinary heap tuple inserts subject to [[subsystems/transactions/mvcc|MVCC]]. They are not visible to other transactions until the creating transaction commits. If the transaction aborts, the catalog rows disappear just like any other deleted tuple.

Physical storage is slightly more nuanced. The storage manager creates the files for each relation fork synchronously during the command, so they exist on disk before commit. It schedules their deletion at abort time, though, by registering `RelationDropStorage()` as a cleanup callback. This means an aborted `CREATE TABLE` leaves no orphaned files.

This property is what makes PostgreSQL's DDL transactional. `BEGIN; CREATE TABLE foo (...); ROLLBACK;` leaves no trace. `BEGIN; CREATE TABLE foo (...); CREATE INDEX ...; ALTER TABLE ...;` can be safely rolled back as a unit.

The creating transaction holds the `AccessExclusiveLock` taken on the new relation's OID until transaction end. Because the new relation is not yet visible to other backends (MVCC hides the catalog rows), this lock has no contention cost; its purpose is to protect against lock-order issues in the lock manager itself.

## See also

- [[subsystems/transactions/mvcc]] — how catalog visibility works for other transactions
- [[code-paths/insert]] — how default expressions and constraint checks fire at INSERT time
- [[subsystems/indexes/btree]] — the index structures created by PRIMARY KEY and UNIQUE constraints
- [[subsystems/storage/buffer-manager]] — how new relation pages enter the buffer pool
- [[subsystems/locking/overview]] — locking during DDL
- [[architecture/overview]] — where DDL fits in the overall query lifecycle

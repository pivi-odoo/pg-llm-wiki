---
title: "Operators and the pg_operator Catalog"
aliases:
  - "pg_operator"
  - "operator catalog"
  - "commutator"
  - "negator"
  - "operator family"
  - "operator class"
  - "opfamily"
  - "opclass"
  - "strategy number"
tags:
  - theme/extensibility
source_files:
  - src/backend/catalog/pg_operator.c
  - src/include/catalog/pg_operator.h
symbols:
  - FormData_pg_operator
  - Form_pg_operator
  - OperatorCreate
  - OperatorUpd
  - makeOperatorDependencies
  - OperatorShellMake
---

Every infix, prefix, or postfix symbol that SQL accepts — `=`, `<`, `&&`, `@>` — is a row in `pg_operator`. The catalog records not just the underlying implementation function but also semantic relationships between operators: which operator is the logical inverse, which one yields the same result when operands are swapped. The query planner reads these relationships to rewrite and simplify predicates without any explicit knowledge of individual operators. Custom types and extensions participate in exactly the same infrastructure as built-in types, making operator extensibility a first-class feature of the system.

## The pg_operator Catalog

`pg_operator` (OID 2617, `OperatorRelationId`) has one row for each defined operator. Its C struct is `FormData_pg_operator` (`Form_pg_operator` as a pointer). The key columns are:

| Column | Type | Meaning |
|---|---|---|
| `oprname` | `name` | Operator symbol, e.g. `<`, `=`, `&&`. Not unique by itself; uniqueness is on `(oprname, oprleft, oprright, oprnamespace)`. |
| `oprkind` | `char` | `'b'` for binary (infix), `'l'` for prefix (left unary). |
| `oprleft` | `Oid` | Left operand type OID. Zero for prefix operators — there is no left operand. |
| `oprright` | `Oid` | Right operand type OID. Always set. |
| `oprresult` | `Oid` | Return type OID. Most indexable operators return `bool`. |
| `oprcode` | `regproc` | The `pg_proc` OID of the function that implements the operator. A row with `oprcode = 0` is a "shell" — a forward declaration placeholder not yet backed by an implementation. |
| `oprcom` | `Oid` | Commutator operator OID, or 0. |
| `oprnegate` | `Oid` | Negator operator OID, or 0. |
| `oprcanmerge` | `bool` | True if this operator can drive a merge join. |
| `oprcanhash` | `bool` | True if this operator can drive a hash join. |
| `oprrest` | `regproc` | Restriction selectivity estimator function. |
| `oprjoin` | `regproc` | Join selectivity estimator function. |

Two unique indexes enforce the uniqueness constraints: `pg_operator_oid_index` on `oid`, and `pg_operator_oprname_l_r_n_index` on `(oprname, oprleft, oprright, oprnamespace)`. Operator lookup by the parser uses `SearchSysCache4(OPERNAMENSP, ...)` against the name+type key. The syscache identifiers are `OPEROID` (by OID) and `OPERNAMENSP` (by name and type OIDs).

SQL's `CREATE OPERATOR` creates operators. The command routes through `OperatorCreate()` in `src/backend/catalog/pg_operator.c`.

## Commutators and Negators

Two of the most planner-critical columns are `oprcom` (commutator) and `oprnegate` (negator). Both store OIDs of related operators; both are optional.

**Commutator.** An operator `OP` has commutator `COP` when swapping the operands gives the same result: `x OP y` is equivalent to `y COP x`. The canonical example is the pair `<` and `>`: `a < b` is the same predicate as `b > a`. Equality `=` is its own commutator. Commutators are only meaningful for binary operators. `oprcode` on the two operators will typically be different functions, since the argument order differs. The truth value, however, is identical.

The planner uses commutators to normalize join conditions. When the planner considers `t2.x < t1.y` but its join strategy requires the inner-table column on the left, it can rewrite to `t1.y > t2.x` using the commutator, without changing the predicate's meaning. Without a commutator annotation, many join orders would be unavailable or would require more expensive plans.

**Negator.** An operator `OP` has negator `NOP` when `NOT (x OP y)` is equivalent to `x NOP y`. The standard pair is `=` and `<>`. Negators must return `bool` — non-boolean operators cannot have them.

The planner uses negators primarily during constraint exclusion: if a table partition's constraint says `status <> 'active'` and a query filter says `status = 'active'`, the planner can detect via the negator relationship that the constraint logically excludes all rows satisfying the filter. It then prunes the partition without scanning it.

### Shell operators and mutual back-references

When two operators are commutators of each other, a bootstrapping problem exists: neither can refer to the other until both are created. PostgreSQL resolves this with shell operators. When `OperatorCreate()` processes a `COMMUTATOR` clause naming an operator that does not yet exist, it calls `OperatorShellMake()` to insert a minimal placeholder row with `oprcode = 0`. The new operator's `oprcom` field points at this shell OID. When the commutator operator is later fully defined, it fills in the shell row. `OperatorUpd()` then writes the back-pointer into the first operator's `oprcom` field. The same mechanism applies to negators, except that a negator cannot be its own negator (checked at creation time), while a commutator can be self-referential (e.g., `=`).

Dependencies between an operator and its commutator or negator are intentionally not expressed as hard catalog dependencies. If `<` is dropped, the system simply sets the `oprcom` field on `>` to zero rather than cascading a drop. This prevents accidental deletion of an entire operator family when removing one member.

## Operator Families and Operator Classes

Individual operator rows describe point-to-point semantics. A higher-level structure — the operator family (`pg_opfamily`) and operator class (`pg_opclass`) — organizes operators into coherent groups that an index access method can use to drive index scans.

**Operator family (`pg_opfamily`).** An opfamily groups a set of operators and support functions that together implement a coherent ordering or equality semantics for one or more data types. The key columns are `opfmethod` (the access method OID, e.g. B-tree, hash, GiST) and `opfname`. All integer comparison operators (`<`, `<=`, `=`, `>=`, `>`) for `int2`, `int4`, and `int8` belong to a single B-tree opfamily because they are mutually consistent. An index built on `int4` can satisfy a query condition involving `int8` values, since the ordering is compatible.

**Operator class (`pg_opclass`).** An opclass is a subset of an opfamily anchored to a specific data type (`opcintype`). When you run `CREATE INDEX`, PostgreSQL selects an opclass for each indexed column — either the explicitly specified one, or the type's default opclass for the chosen access method. The opclass determines which operators can drive index scans on that column and which support functions the AM can call.

The relationship between the three catalog layers:

```
pg_am (access method)
  └── pg_opfamily (groups operators for an AM)
        └── pg_opclass (binds an opfamily to a specific input type)
              └── pg_amop (operator member: opfamily + strategy number + operator OID)
              └── pg_amproc (support function member: opfamily + support number + proc OID)
```

`pg_amop` is the join table that records which operators belong to which opfamily under which strategy number. `pg_amproc` records the support functions (comparison routines, hash functions, etc.) that the AM calls internally.

## Strategy Numbers

Index access methods define a fixed vocabulary of numbered "strategies" that abstract over the concrete operator names. Each strategy number represents a predicate relationship the AM knows how to exploit.

For B-tree, the five strategies are hardcoded in `src/include/access/stratnum.h`:

| Strategy | Constant | Meaning |
|---|---|---|
| 1 | `BTLessStrategyNumber` | Less than (`<`) |
| 2 | `BTLessEqualStrategyNumber` | Less than or equal (`<=`) |
| 3 | `BTEqualStrategyNumber` | Equal (`=`) |
| 4 | `BTGreaterEqualStrategyNumber` | Greater than or equal (`>=`) |
| 5 | `BTGreaterStrategyNumber` | Greater than (`>`) |

When the executor evaluates `WHERE col < 42`, the planner looks up whether there exists an `pg_amop` row linking the `<` operator for `col`'s type to strategy 1 in the relevant B-tree opfamily. If such a row exists, the planner can use a B-tree index scan, with the index entry point determined by the `<` predicate. The AM never knows the operator's name or OID directly; it works entirely through strategy numbers.

Hash indexes use a single strategy (strategy 1 = equality). GiST and SP-GiST define their own strategy sets that vary by opclass; for example, the `point` GiST opclass defines strategies for "left of", "right of", "contained by", and similar geometric predicates.

The separation between strategy numbers and operator OIDs is what allows opfamilies to span multiple data types. The B-tree integer opfamily registers `int4 < int8` under strategy 1 with `opllefttype = int4` and `oprighttype = int8`. When the planner sees a mixed-type comparison `int4_col < int8_value`, it finds this cross-type `pg_amop` entry. This tells the planner the B-tree index is applicable.

## Related Topics

- [[subsystems/catalog/core-catalogs|Core system catalogs (pg_class, pg_type, pg_proc)]]
- [[subsystems/catalog/syscache|Syscache]]
- [[subsystems/indexes/index-am|Index access method interface]]
- [[subsystems/planner/overview|Planner overview]]

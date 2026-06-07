---
title: "Temporal Tables and Period Constraints"
aliases:
  - "WITHOUT OVERLAPS"
  - "temporal PRIMARY KEY"
  - "temporal FOREIGN KEY"
  - "PERIOD"
  - "bi-temporal"
source_files:
  - src/backend/catalog/heap.c
  - src/backend/commands/tablecmds.c
  - src/backend/optimizer/util/plancat.c
  - src/include/catalog/pg_constraint.h
  - src/include/nodes/parsenodes.h
symbols:
  - transformTableConstraint
---

PostgreSQL 18 added temporal PRIMARY KEY, UNIQUE, and FOREIGN KEY constraints using the SQL:2011 period syntax. These constraints enforce non-overlapping date or time ranges for the same entity key. This lets the database enforce bi-temporal or valid-time data models that previously required complex trigger or exclusion-constraint workarounds.

## The Problem They Solve

A common pattern in business applications is tracking the validity period of a fact: an employee's salary, a product's price, or a contract's terms change over time. The history must be preserved. The natural representation is a table with `(id, valid_from, valid_to)`. Each row must satisfy `valid_from <= valid_to`. No two rows for the same `id` may have overlapping ranges.

Before PG 18, enforcing non-overlap required either an exclusion constraint using the `&&` range operator and a `btree_gist` index, or complex trigger logic. The new syntax does the same thing with standard SQL:

```sql
CREATE TABLE prices (
    product_id   int,
    valid_at     daterange,
    price        numeric,
    PRIMARY KEY (product_id, valid_at WITHOUT OVERLAPS)
);
```

The `WITHOUT OVERLAPS` clause tells PostgreSQL that the `valid_at` column participates in the uniqueness check using range non-overlap semantics rather than simple equality.

## Temporal Primary Key

A temporal PRIMARY KEY (or UNIQUE constraint) with `WITHOUT OVERLAPS` creates an index that enforces:
1. No two rows with the same non-period key columns (e.g., `product_id`) have overlapping values in the period column (e.g., `valid_at`).
2. The period column must be a range or multirange type.

The constraint uses a GiST index (not btree) to implement the non-overlap check, because range containment and overlap are not equality tests. PostgreSQL creates the GiST index on the period column automatically as part of the constraint.

```sql
-- Also valid with UNIQUE
CREATE TABLE employee_salaries (
    employee_id  int,
    valid_at     tstzrange,
    salary       numeric,
    UNIQUE (employee_id, valid_at WITHOUT OVERLAPS)
);
```

PostgreSQL rejects inserts and updates that would create overlapping ranges with a constraint violation, just like a plain UNIQUE violation.

## Temporal Foreign Key

A temporal FOREIGN KEY references a temporal primary key using `PERIOD` syntax. It enforces that every row's period in the child table is fully contained within some row's period in the parent table for the same key.

```sql
CREATE TABLE price_adjustments (
    product_id   int,
    valid_at     daterange,
    discount     numeric,
    FOREIGN KEY (product_id, PERIOD valid_at)
        REFERENCES prices (product_id, PERIOD valid_at)
);
```

The semantics differ from a standard foreign key: the child's period does not need to exactly match a parent row — it needs to be covered (contained) by one. This models the common case where a child record is valid for a sub-period of the parent.

## Range Column Requirements

The period column must be a **range** or **multirange** type (`daterange`, `tsrange`, `tstzrange`, `int4range`, etc.). The constraint implementation uses the GiST operator class for that range type. The period column may be `NOT NULL`; temporal constraints do not enforce non-nullability automatically.

## Interaction with Other Features

**Partitioning**: temporal primary keys cannot currently serve as partition keys (partition routing uses equality, not range non-overlap).

**Exclusion constraints**: temporal PRIMARY KEY with `WITHOUT OVERLAPS` is internally implemented via the same exclusion-constraint mechanism as explicit `EXCLUDE USING GIST (... WITH &&)`. The syntax is cleaner, though. The constraint type in `pg_constraint` is `p` (primary) or `u` (unique), not `x` (exclusion).

**Standard exclusion constraints** (`EXCLUDE USING GIST`) still work as before and remain the right choice for custom overlap conditions that do not fit the temporal key model.

## Checking Temporal Constraints

```sql
-- See temporal constraints in the catalog
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'prices'::regclass
  AND conperiod = true;
```

The `pg_constraint.conperiod` column (added in PG 18) identifies constraints that use temporal/period semantics.

## Related Topics

- [[subsystems/types/range-types|Range and Multirange Types]] — the range types used as period columns
- [[subsystems/constraints|Constraint Internals]] — how CHECK, UNIQUE, FK, and exclusion constraints are stored
- [[subsystems/indexes/gist|GiST]] — the index type used to enforce non-overlap
- [[subsystems/locking/predicate-locking|Predicate Locking and Serializable Isolation]] — interaction with serializable transactions

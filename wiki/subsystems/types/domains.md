---
title: "Domain Types"
aliases:
  - "CREATE DOMAIN"
  - "Domain Constraints"
source_files:
  - src/backend/commands/typecmds.c
  - src/backend/utils/adt/domains.c
  - src/backend/utils/cache/typcache.c
  - src/include/catalog/pg_type.h
symbols:
  - DefineDomain
  - domain_check_input
  - domain_check
  - InitDomainConstraintRef
  - UpdateDomainConstraintRef
  - CoerceToDomain
---

# Domain Types

A domain is a user-defined type built on top of an existing base type, with optional `NOT NULL` and `CHECK` constraints that every value of the domain must satisfy. Domains give names and invariants to data shapes that recur throughout a schema — postal codes, email addresses, positive integers — without requiring application-layer validation.

## Catalog representation

A domain is a single row in `pg_type` with `typtype = 'd'`. Its `typbasetype` column holds the OID of the underlying type. Its `typtypmod` column holds the typmod passed through when the base type is parameterised (e.g., `DOMAIN d AS varchar(20)` stores `typmod = 20`). `DefineDomain()` (`typecmds.c`) inherits all physical storage properties — `typlen`, `typbyval`, `typalign`, `typstorage` — from the base type. It copies them into the domain's `pg_type` row at creation time. `DefineDomain()` similarly copies I/O functions. The domain uses the base type's `typinput`, `typoutput`, `typreceive`, and `typsend` OIDs, except that the special wrapper functions `domain_in` and `domain_recv` (`domains.c`) override `typinput` and `typreceive`. These wrappers apply constraint checking after calling the underlying input function.

PostgreSQL creates an implicit array type alongside every domain, just as with base types. `CREATE DOMAIN positive_int AS int CHECK (VALUE > 0)` creates both `positive_int` and `_positive_int` in `pg_type`.

PostgreSQL stores constraints attached to a domain in `pg_constraint` rows, with `contypid` set to the domain's OID and `contype` of `'c'` (CHECK) or `'n'` (NOT NULL). When a domain is based on another domain, both domains' constraints apply: the full constraint set is the union of constraints at every level of the chain.

## Constraint checking at input time

`domain_in()` is the input function for all domain types. After parsing the text representation using the base type's own input function, it calls `domain_check_input()` which:

1. Calls `UpdateDomainConstraintRef()` to ensure the cached constraint list is current.
2. For each `DOM_CONSTRAINT_NOTNULL` entry, raises an error if the value is NULL.
3. For each `DOM_CONSTRAINT_CHECK` entry, evaluates the CHECK expression in a standalone `ExprContext`. The expression sees the input value through a `CoerceToDomainValue` node. This node acts as a placeholder. The expression evaluator replaces it with the actual datum.

The `DomainConstraintRef` structure (`typcache.c`) holds a reference to the type cache entry's constraint list. It also tracks a generation counter. `UpdateDomainConstraintRef()` compares the cached generation against the type cache entry. The entry may have been invalidated, because a constraint was added or dropped via `ALTER DOMAIN`. If so, `UpdateDomainConstraintRef()` rebuilds the constraint list by re-reading `pg_constraint`. This makes constraint checking immune to concurrent schema changes without requiring a catalog lookup on every call.

## Constraint checking in expressions

When SQL assigns a value of the base type to a domain column or variable, the planner inserts a `CoerceToDomain` node. At execution time this node calls `ExecEvalCoerceToDomain()` (`execExprInterp.c`), which checks the domain's constraints in exactly the same loop as `domain_check_input()`. The planner compiles the check as part of the expression tree, rather than going through the text-input path. So it avoids the overhead of text parsing.

The planner also inserts `CoerceToDomain` when a value flows from one domain to another, or when a function argument or return value is declared as a domain type. The planner is responsible for inserting these coercion nodes wherever type-level constraints must be verified. Without them, values can enter storage without constraint evaluation.

## ALTER DOMAIN and constraint evolution

`ALTER DOMAIN` can add or drop constraints. Adding a `NOT NULL` or a `CHECK` constraint with `NOT VALID` skips validation of existing rows. It marks the constraint in `pg_constraint` with `convalidated = false`. A subsequent `ALTER DOMAIN ... VALIDATE CONSTRAINT` scans every column of every table that uses the domain. It checks each stored value. Only then does it set `convalidated` to `true`. This two-phase pattern mirrors `ALTER TABLE ... ADD CONSTRAINT NOT VALID ... VALIDATE CONSTRAINT`. It avoids long-running table scans while the schema evolves.

Dropping a constraint invalidates the type cache entry for the domain (via a catalog invalidation). This causes the next `UpdateDomainConstraintRef()` call in any backend to rebuild the constraint list without the dropped constraint.

## Domains over domains

PostgreSQL supports building a domain on top of another domain: `CREATE DOMAIN us_zip AS zip_code CHECK (VALUE ~ '^\d{5}$')`. The catalog chain is: `us_zip.typbasetype → zip_code`, `zip_code.typbasetype → text`. The type cache flattens this chain during `InitDomainConstraintRef()` by walking `typbasetype` links and collecting all `pg_constraint` rows at every level into a single list. Execution only ever iterates this flat list. So constraint checking cost is O(total constraints), regardless of how deep the domain hierarchy is.

## Domains and polymorphic functions

Domains participate in function resolution as their base type. A function declared `f(x int)` accepts a `positive_int` argument because `positive_int` is assignment-compatible with `int`. The coercion in the other direction — `int` to `positive_int` — requires an explicit cast. This cast inserts a `CoerceToDomain` node.

## See also

- [[subsystems/types/composite-types]] — another form of user-defined structural type
- [[subsystems/catalog/core-catalogs]] — pg_type layout and typtype values
- [[subsystems/transactions/mvcc]] — domain constraint violations and transaction rollback

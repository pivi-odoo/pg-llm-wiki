---
title: "Expression Collation Assignment"
aliases:
  - collation propagation
  - assign_query_collations
  - COLLATE clause internals
  - CollateStrength
  - collation conflict
source_files:
  - src/backend/parser/parse_collate.c
  - src/include/parser/parse_collate.h
symbols:
  - assign_query_collations
  - assign_collations_walker
  - merge_collation_state
  - CollateStrength
  - assign_collations_context
---

Collation assignment is a post-pass over the expression tree that runs after semantic analysis is complete. Its job is to attach two collation OIDs to every expression node: the collation of the node's output value (used by callers), and the collation the node should use when invoking collation-sensitive operations such as comparisons and pattern matching. Because propagating collation information requires tracking state across the entire expression, PostgreSQL defers it to a separate tree walk rather than computing it on the fly during parsing.

## Collation Strength

The core concept is *collation strength*, borrowed from the SQL standard (which calls it "derivation"). The implementation uses four levels:

| Strength | Meaning |
|---|---|
| `COLLATE_NONE` | Expression produces a non-collatable type (e.g., `integer`, `timestamp`) |
| `COLLATE_IMPLICIT` | Collation was inherited from an input (e.g., a column reference) |
| `COLLATE_CONFLICT` | Two implicit collations met and neither can win |
| `COLLATE_EXPLICIT` | An explicit `COLLATE` clause was applied |

An explicit collation always wins over any implicit one. When two implicit collations meet, neither can dominate, so the state becomes `COLLATE_CONFLICT`. Conflicts do not immediately raise an error. They are carried in the tree. They become an error only if the conflicted node ends up somewhere that needs a definite collation. A comparison like `col_en = col_fr` fails at runtime (or parse time for strict functions). `col_en || col_fr` does not, because string concatenation does not use collation.

## The Walk

`assign_query_collations()` is the entry point. It calls `query_tree_walker()` with flags that skip range-table entries and CTE subqueries. The parser already processed those entries when it parsed them, so re-walking them would be redundant. Re-walking could also produce wrong results if Vars referencing them were created with the correct collation.

Inside the walk, `assign_collations_walker()` processes each expression node bottom-up. For leaf nodes (column references, literals, parameters), the node's type, or a `COLLATE` clause attached directly, determines its collation. For operator and function calls, the walker collects the collations of all collatable inputs. It merges them with `merge_collation_state()`.

`merge_collation_state()` implements the strength ordering. An incoming explicit collation always replaces the context's current state. An incoming implicit collation updates the state only if the context is currently `COLLATE_NONE`. It transitions the state to `COLLATE_CONFLICT` if the context already holds a different implicit collation.

## What Gets Stored

Each expression node carries two collation fields:

- **`resultcollation`** (or `opcollid` / `inputcollid` depending on node type) — the collation of the output value, propagated upward for callers to use.
- **`inputcollid`** — the collation the node should use for its own collation-sensitive operations. For a comparison operator, this is what gets passed to the comparison function.

The two fields are necessary because they can differ. A function that takes two `text` arguments and returns `integer` has no output collation (integers are not collatable), yet it still needs an input collation to perform any internal text comparisons.

## Aggregates and Window Functions

Aggregate functions require special handling because they can have an `ORDER BY` clause inside the aggregate and hypothetical-set arguments. `assign_aggregate_collations()`, `assign_ordered_set_collations()`, and `assign_hypothetical_collations()` handle these cases. The complication is that the aggregate's arguments, its sort keys, and its hypothetical arguments all contribute collations. These collations must be merged and checked for consistency.

## Practical Implications for Web Applications

Collation conflicts surface most often when a query mixes string columns from tables created with different collations. They also surface when a query compares `text` parameters from the client (which carry the type's default collation) against columns with explicit collations. The error message "could not determine which collation to use for string comparison" comes from a `COLLATE_CONFLICT` state reaching a function that requires a definite collation. The fix is to add an explicit `COLLATE` clause to one side of the expression.

## Related Topics

- [[subsystems/parser/type-resolution|Type Resolution]] — the earlier phase that assigns types before collations are propagated
- [[subsystems/parser/semantic-analysis|Semantic Analysis]] — the overall post-parse analysis phase that calls `assign_query_collations()`
- [[subsystems/types/locale|Locale and Collation]] — how collation OIDs map to ICU and libc collators

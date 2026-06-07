---
title: "Generic Object Definition Infrastructure (define.c)"
aliases:
  - "define.c"
  - "DefElem"
  - "CREATE AGGREGATE"
  - "CREATE CAST"
  - "CREATE COLLATION"
  - "CREATE CONVERSION"
  - "DefineAggregate"
  - "DefineCollation"
  - "CreateCast"
  - "CreateConversionCommand"
source_files:
  - src/backend/commands/define.c
  - src/include/commands/defrem.h
  - src/backend/commands/aggregatecmds.c
  - src/backend/commands/collationcmds.c
  - src/backend/commands/conversioncmds.c
  - src/backend/commands/functioncmds.c
symbols:
  - DefElem
  - defGetString
  - defGetBoolean
  - defGetTypeName
  - defGetQualifiedName
  - errorConflictingDefElem
  - DefineAggregate
  - AggregateCreate
  - DefineCollation
  - CollationCreate
  - CreateConversionCommand
  - ConversionCreate
  - CreateCast
  - CastCreate
---

PostgreSQL's extensibility rests on a handful of DDL commands — `CREATE AGGREGATE`, `CREATE CAST`, `CREATE COLLATION`, `CREATE CONVERSION` — that let extensions plug new behaviour into the type system, the expression analyser, and the multibyte string layer without modifying core code. The command-processing layer for these objects follows a uniform two-phase pattern. A `DefineFoo` or `CreateFoo` function in `src/backend/commands/` unpacks the parse tree, validates arguments, and resolves names against the catalogs. Then a catalog-layer function (typically in `src/backend/catalog/`) writes the actual rows. The plumbing that makes this pattern uniform is `define.c`, a short but pervasive file that provides typed accessors for the generic `DefElem` key-value list emitted by the parser.

## The DefElem pattern

Every `CREATE` statement with named options — `sfunc = mystate`, `locale = 'en-US'`, `castcontext = implicit` — reaches the command layer as a `List` of `DefElem` nodes. A `DefElem` carries a `defname` string (the option keyword) and an `arg` node (the value, which can be a string, integer, float, boolean, type name, or qualified name). The grammar is deliberately untyped: it accepts almost any syntactic form and defers interpretation to the command function.

`define.c` provides the typed extraction layer:

| Accessor | Returns | Accepts |
|---|---|---|
| `defGetString` | `char *` | Integer, Float, Boolean, String, TypeName, List, `*` |
| `defGetNumeric` | `double` | Integer, Float |
| `defGetBoolean` | `bool` | Integer (0/1), "true"/"false"/"on"/"off", bare keyword |
| `defGetInt32` / `defGetInt64` | `int32` / `int64` | Integer, Float (for large values) |
| `defGetObjectId` | `Oid` | Integer, Float |
| `defGetQualifiedName` | `List *` of strings | TypeName, List, quoted String |
| `defGetTypeName` | `TypeName *` | TypeName, quoted String |
| `defGetTypeLength` | `int` | Integer or the string "variable" |

Each accessor raises `ERRCODE_SYNTAX_ERROR` if the node type is wrong, producing a user-visible message that names the option. The pattern in every `DefineFoo` function is the same: iterate the list, `strcmp` on `defname`, call the appropriate accessor, and store the result in a local variable. A caller calls the helper `errorConflictingDefElem` whenever it detects that an option appeared twice. This produces the standard "conflicting or redundant options" error with an accurate source position.

This design lets the parser remain oblivious to the semantics of each option. The grammar for `CREATE AGGREGATE` and `CREATE COLLATION` emit the same `DefElem` list structure. Only the `DefineFoo` function knows which keys are required, which are optional, and what combinations are forbidden.

## CREATE AGGREGATE: composable aggregation

An aggregate is more than a function — it is a protocol for combining values incrementally. `DefineAggregate` in `aggregatecmds.c` decodes up to twenty named parameters from the `DefElem` list and eventually calls `AggregateCreate` in the catalog layer. The required parameters are `sfunc` (the state transition function) and `stype` (the state type). Everything else is optional.

The core aggregation model requires two things: a transition function with the signature `(stype, input_type) → stype`, and an initial state value (`initcond`) stored as text. At query execution time, the executor repeatedly calls the transition function, folding each input row into the accumulated state. When all rows are consumed, an optional final function (`finalfunc`) converts the state into the result type.

For parallel query, the aggregate can also declare a `combinefunc` of type `(stype, stype) → stype`. When parallel workers aggregate partial results independently, the combine function merges two partial states. Without a combine function the aggregate is not parallel-safe regardless of the `parallel` attribute.

For window functions and sliding aggregates, there is a second complete state machine: `msfunc`, `minvfunc`, `mstype`, and `minitcond`. The "moving-aggregate" protocol maintains a secondary state that the inverse function (`minvfunc`) can unwind as rows leave the window frame. This is the mechanism behind efficient frame-moving for aggregates like `sum` and `avg` in window queries — rather than recomputing the full window from scratch, the inverse function subtracts outgoing rows from the secondary state.

Ordered-set aggregates (`AGGKIND_ORDERED_SET`) add the concept of direct arguments — parameters evaluated once per aggregate call rather than once per row — and optionally `HYPOTHETICAL` mode, where the direct arguments represent a hypothetical row inserted into the sorted set. The grammar distinguishes ordered-set aggregates by the presence of a `WITHIN GROUP` clause. `DefineAggregate` receives this via the second element of the `args` pair.

For serial/deserial functions: if the transition type is `internal` (a pointer to private C memory), the aggregate can declare `serialfunc` and `deserialfunc` to support parallel aggregation across process boundaries. Both must be specified together. Specifying only one is an error.

```mermaid
TD
    A[CREATE AGGREGATE] --> B["DefineAggregate<br/>(aggregatecmds.c)"]
    B --> C["required:<br/>sfunc + stype"]
    B --> D["optional:<br/>finalfunc, combinefunc,<br/>serialfunc, deserialfunc"]
    B --> E["optional moving-agg:<br/>msfunc, minvfunc, mstype"]
    C --> F["AggregateCreate<br/>(catalog layer)"]
    D --> F
    E --> F
    F --> G["pg_proc row<br/>+ pg_aggregate row"]
```

PostgreSQL stores the `initval` string as text in `pg_aggregate` rather than interpreting it at definition time. This matters for time-sensitive initial conditions: if someone declared `initcond = 'now'` for a `timestamp` aggregate, the value must be interpreted at query time, not at `CREATE AGGREGATE` time. `DefineAggregate` does call the type's input function on the string to validate it early, but it discards the parsed result. PostgreSQL stores only the text.

## CREATE CAST: the coercion graph

A cast defines how PostgreSQL moves a value of one type to another. The expression analyser maintains an in-memory coercion graph rooted in `pg_cast`. Every time it needs to coerce an expression, it searches this graph for the shortest applicable path. `CreateCast` in `functioncmds.c` adds one directed edge to this graph.

Three cast methods exist:

- **Function cast** (`WITH FUNCTION`): PostgreSQL calls the nominated function at runtime. The function must take the source type (or a binary-coercible variant) as its first argument and return the target type. It may optionally take a second `integer` argument (a type modifier, e.g. `varchar(20)` precision) and a third `boolean` argument (indicating explicit vs. implicit context).
- **Binary-compatible cast** (`WITHOUT FUNCTION`): no function is called. The system reinterprets the value in place. PostgreSQL verifies that source and target types have identical physical layout — same length, alignment, and pass-by-value flag. This method requires superuser privileges, because a mistake here can crash the backend. PostgreSQL unconditionally rejects composite, enum, array, and domain types.
- **I/O conversion cast** (`WITH INOUT`): the source type's output function formats the value as text, and the target type's input function parses it. This method is slower but always available between any two types with text representations.

The cast context controls when the coercion is applied automatically:

| Context | Meaning |
|---|---|
| `IMPLICIT` | Applied silently anywhere, including inside expressions and function argument coercion |
| `ASSIGNMENT` | Applied silently only in INSERT/UPDATE target-column coercion |
| `EXPLICIT` | Applied only when the user writes an explicit `CAST(x AS y)` or `x::y` |

The analyser searches implicit casts greedily. Marking too many casts implicit causes ambiguity errors when the analyser finds multiple paths with equal cost. The PostgreSQL convention is that only casts within a type family — between integer widths, between character types — should be implicit.

Permission: the user must own either the source or the target type. This prevents a user from silently intercepting coercions involving types they do not control.

## CREATE COLLATION: naming a locale

A collation is a named set of rules for comparing and ordering text. `DefineCollation` in `collationcmds.c` wraps either a libc locale (specified via `LC_COLLATE` / `LC_CTYPE` or the combined `LOCALE` shorthand) or an ICU locale (specified via `LOCALE` with `PROVIDER = icu`) into a row in `pg_collation`.

For libc collations, `collcollate` and `collctype` name the POSIX locale used for sort order and character classification respectively. For ICU collations, `colliculocale` names the ICU locale, canonicalised to a BCP 47 language tag during creation. If the user supplies a non-canonical form — `en_US` instead of `en-US` — PostgreSQL emits a `NOTICE` and stores the canonical form.

The `deterministic` flag (ICU only) controls whether strings that collate as equal are also considered identical for equality purposes. A non-deterministic collation makes `'café'` and `'cafe'` compare equal under case-insensitive or accent-insensitive rules. This changes the behaviour of `=` operators, `GROUP BY`, and uniqueness constraints. libc does not support non-deterministic collations, because it provides no mechanism for decomposed-and-recomposed comparison.

**Collation versioning** is the other key design in `DefineCollation`. The `version` column in `pg_collation` captures a string (typically a library version number) describing the collation rules in effect at creation time. At creation time, `get_collation_actual_version` queries the underlying library. `ALTER COLLATION ... REFRESH VERSION` (implemented in `AlterCollation`) re-queries the library and updates the stored version if it has changed. The version mismatch warning exists because changing collation rules mid-deployment silently invalidates any index built with that collation. Sorted pages that were correct under the old rules may be wrong under the new rules. Applications that track index consistency rely on `pg_collation.collversion` to detect this situation.

When a user specifies `FROM existing_collation`, PostgreSQL copies all attributes directly from the source row without canonicalisation. PostgreSQL cannot copy the `default` collation, because code throughout PostgreSQL checks for `DEFAULT_COLLATION_OID` rather than inspecting the provider. Those checks would silently ignore a second row with `COLLPROVIDER_DEFAULT`.

## CREATE CONVERSION: encoding translation

A conversion describes how to translate text from one database encoding to another. `CreateConversionCommand` in `conversioncmds.c` validates the encoding names, looks up the nominated function, and verifies its signature and return type. It then calls the function once with an empty string, to confirm it accepts the requested encoding pair, before writing to `pg_conversion` via `ConversionCreate`.

The conversion function must have this exact signature:

```c
int conv_func(int src_encoding, int dest_encoding,
              const char *src, char *dest, int len, bool noError);
```

The `noError` flag allows the function to return the number of bytes converted rather than raising an error, supporting partial-conversion use cases. PostgreSQL enforces this signature at `CREATE CONVERSION` time rather than at call time, preventing a class of crashes from mismatched function pointers. There is one additional check: `CreateConversionCommand` calls the function with an empty string at definition time. If it returns nonzero, `CreateConversionCommand` rejects the definition.

PostgreSQL rejects conversions to or from `SQL_ASCII`. The encoding layer has hard-wired fast paths that bypass any registered conversion function when either encoding is `SQL_ASCII`. As a result, a registered function would never execute.

The `DEFAULT` keyword in `CREATE DEFAULT CONVERSION` marks a conversion as the preferred route for its encoding pair. When the multibyte layer needs to convert a string, it searches `pg_conversion` for a default conversion. Non-default conversions are available by name, but the layer does not select them automatically.

## Layered validation

All four commands follow the same validation sequence:

1. Resolve the object name to a namespace OID using `QualifiedNameGetCreationNamespace`.
2. Check `ACL_CREATE` on the target namespace.
3. Decode the option list, raising errors for unknown keys or duplicate keys via `errorConflictingDefElem`.
4. Resolve all function and type references to OIDs, validating signatures.
5. Call the catalog-layer `FooCreate` function, which writes system catalog rows and records dependency edges.

The dependency edges written by the catalog layer are what makes `DROP` cascades correct. A cast that depends on a function, a collation that wraps an external library, a conversion that depends on a function — PostgreSQL records all of these in `pg_depend`, so dropping a function automatically drops any cast or conversion that uses it.

## Related Topics

- [[subsystems/extensions/custom-operators|Custom Operators and Operator Classes]]
- [[code-paths/create-function|CREATE FUNCTION]]
- [[subsystems/extensions/procedural-languages|Procedural Languages]]
- [[subsystems/extensions/overview|Extension Overview]]

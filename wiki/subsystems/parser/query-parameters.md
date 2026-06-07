---
title: "Query Parameters ($1, $2, ...)"
aliases:
  - query parameters
  - bind parameters
  - prepared statement parameters
  - ParamRef
  - PARAM_EXTERN
  - parameter type inference
tags:
  - theme/wire-protocol
source_files:
  - src/backend/parser/parse_param.c
  - src/include/parser/parse_param.h
symbols:
  - setup_parse_fixed_parameters
  - setup_parse_variable_parameters
  - fixed_paramref_hook
  - variable_paramref_hook
  - variable_coerce_param_hook
  - check_variable_parameters
  - query_contains_extern_params
  - FixedParamState
  - VarParamState
  - ParamRef
  - Param
---

Every `$1`, `$2`, and `$N` placeholder in a PostgreSQL query is a `ParamRef` in the raw parse tree and a `Param` node with `paramkind = PARAM_EXTERN` in the analyzed tree. The parser itself does not know how to resolve these references. It delegates that job to a hook in `ParseState`, implemented in `parse_param.c`. This hook-based design lets the same parser handle two fundamentally different use cases: prepared statements with pre-declared types, and the extended query protocol where parameter types must be inferred from context.

## Two Resolution Modes

PostgreSQL uses **fixed-parameter mode** when all parameter types are known before parsing begins — for example, when a client sends a `Parse` message in the extended query protocol with explicit type OIDs, or when a PL/pgSQL function calls a prepared statement whose types are fixed by its signature. `setup_parse_fixed_parameters()` installs `fixed_paramref_hook`. It hands the hook a `FixedParamState` containing the caller-supplied OID array. The hook validates that the `$N` reference is within range and that the OID is valid. Then it creates a `Param` node directly.

PostgreSQL uses **variable-parameter mode** when types must be inferred — the common case for drivers that send parameter type OIDs as zero (meaning "figure it out"). `setup_parse_variable_parameters()` installs both `variable_paramref_hook` and `variable_coerce_param_hook`, along with a `VarParamState`. `VarParamState` holds a pointer-to-pointer to the type array, which lets PostgreSQL re-`palloc` the array as new `$N` references appear. A parameter first seen in this mode gets `UNKNOWNOID` as a placeholder.

## Type Inference for Variable Parameters

The interesting work happens in `variable_coerce_param_hook`, which fires whenever the semantic analyzer needs to coerce an `UNKNOWNOID` parameter to a specific type. The hook updates the type array entry for that parameter number. It also sets `param->paramtype` to the resolved type. Three outcomes are possible:

- **First resolution**: the slot held `UNKNOWNOID`, so the target type is accepted and recorded.
- **Consistent re-resolution**: the slot already holds the target type (the same `$N` appears twice in compatible positions), so nothing changes.
- **Conflict**: the slot holds a different type from a previous resolution, which raises `ERRCODE_AMBIGUOUS_PARAMETER` ("inconsistent types deduced for parameter $N").

The hook intentionally always sets `paramtypmod` to `-1`, even when the context would imply a specific type modifier. There is no mechanism to enforce that the runtime value of a parameter matches a particular typmod, so storing a non-generic typmod would create a false promise. Any necessary length coercions happen at runtime instead.

PostgreSQL always derives a parameter's collation from the type's default collation (`get_typcollation()`). Applications that need a different collation must apply an explicit `COLLATE` expression around the parameter.

## Post-Analysis Consistency Check

After parsing completes, `check_variable_parameters()` walks the entire query tree to verify that every `PARAM_EXTERN` node holds the resolved type rather than `UNKNOWNOID`. This catches a parameter that never went through coercion — for example, in `SELECT $1` with no other type hint. The error is "could not determine data type of parameter $N" with `ERRCODE_AMBIGUOUS_PARAMETER`.

The check does not require every parameter slot to have been used. A query can reference `$1` and `$3` without referencing `$2`. Slot 2 remains zero in the type array. Zero is distinct from `UNKNOWNOID`, and PostgreSQL accepts it.

## The JDBC void Hack

When the paramtype in a variable-parameter slot is `VOIDOID` and the expression is a call argument (`EXPR_KIND_CALL_ARGUMENT`), the parser treats it as `UNKNOWNOID`. This accommodates JDBC drivers that pass `void` as the type for output parameters in stored procedure calls. The driver cannot easily distinguish function calls (which have a return value) from procedure calls (which do not), so it uses `void` as a sentinel. Treating `void` as unknown lets the type-inference machinery handle these parameters normally.

## Related Topics

- [[code-paths/extended-query|Extended Query Protocol]] — the protocol layer that delivers parameter values and type hints
- [[subsystems/parser/type-resolution|Type Resolution]] — how the analyzer resolves type ambiguities in expressions generally
- [[subsystems/parser/semantic-analysis|Semantic Analysis]] — the broader context in which parameter resolution hooks are invoked

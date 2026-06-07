---
title: "Stack Depth Checking"
aliases:
  - "check_stack_depth"
  - "stack_is_too_deep"
  - "max_stack_depth"
  - "set_stack_base"
source_files:
  - src/backend/utils/misc/stack_depth.c
  - src/include/miscadmin.h
symbols:
  - check_stack_depth
  - stack_is_too_deep
  - set_stack_base
  - restore_stack_base
  - get_stack_depth_rlimit
  - max_stack_depth
---

# Stack Depth Checking

PostgreSQL detects excessively deep call stacks before the operating system does, converting what would be an unrecoverable `SIGSEGV` (stack overflow signal) into an ordinary SQL error that the client can catch. Many parts of the executor, planner, and procedural language engines are recursive. Recursive SQL queries (`WITH RECURSIVE`), deeply nested PL/pgSQL function calls, complex expression trees, and partition hierarchies can all drive call-stack depth well beyond what is safe. Most Unix kernels treat stack overflow as a fatal signal that kills the process. Detecting the condition inside PostgreSQL first means the connection survives, and the client receives an error rather than a connection drop.

## How the Limit Is Measured

At backend startup, `set_stack_base()` (called from `main()`) records the address of a stack frame near the bottom of the stack into `stack_base_ptr`. That pointer serves as the reference point for all subsequent depth measurements.

When PostgreSQL calls `stack_is_too_deep()`, the function takes the address of a local variable in its own frame (`stack_top_loc`) and computes the absolute difference from `stack_base_ptr`. This gives an approximation of the current stack depth in bytes, without any OS calls. PostgreSQL ignores the sign of the difference, because some architectures grow the stack upward.

```c
stack_depth = (ssize_t) (stack_base_ptr - &stack_top_loc);
if (stack_depth < 0)
    stack_depth = -stack_depth;
```

PostgreSQL skips the guard if `stack_base_ptr` is still `NULL` (that is, if code calls it before `set_stack_base()`). This way, the check does not accidentally abort early-startup code.

## The `max_stack_depth` GUC

The GUC `max_stack_depth` (default 100 kB) specifies the maximum call-stack depth PostgreSQL will allow, measured in kilobytes. Internally, PostgreSQL keeps it as `max_stack_depth_bytes` (bytes) for cheap comparisons on every call.

The GUC check hook (`check_max_stack_depth`) reads the platform's hard limit via `getrlimit(RLIMIT_STACK, ...)` and rejects any value that would leave less than `STACK_DEPTH_SLOP` (512 kB) of headroom between the configured limit and the OS limit. This safety margin ensures that PostgreSQL signals the error first. The kernel would otherwise kill the process.

On Windows, where `getrlimit` is unavailable, `get_stack_depth_rlimit()` returns `WIN32_STACK_RLIMIT` instead.

Raising `max_stack_depth` requires first raising the OS stack limit (`ulimit -s` or equivalent), otherwise the GUC assignment is rejected with:

```
ERROR: "max_stack_depth" must not exceed NkB.
HINT: Increase the platform's stack depth limit via "ulimit -s" or local equivalent.
```

## Enforcement API

| Function | Behaviour |
|---|---|
| `check_stack_depth()` | Calls `stack_is_too_deep()` and immediately raises `ERROR` (ERRCODE_STATEMENT_TOO_COMPLEX) if the limit is exceeded. |
| `stack_is_too_deep()` | Returns `true` if the limit is exceeded; callers that want to handle the condition themselves use this form. |

The error message produced by `check_stack_depth()` reads:

```
ERROR:  stack depth limit exceeded
HINT:   Increase the configuration parameter "max_stack_depth" (currently NkB),
        after ensuring the platform's stack depth limit is adequate.
```

Because this is a regular `ERROR`, it unwinds the current transaction normally. The backend process is not killed and the connection stays open.

## Where It Is Called

`check_stack_depth()` is called at the entry of any backend function that might recurse to arbitrary depth, including:

- Expression evaluation and recursive query execution in the executor.
- Planner routines for partition-wise join (`try_partitionwise_join()`).
- Catalog dependency traversal (`findDependentObjects()`).
- Procedural language interpreters (PL/pgSQL, PL/Perl, etc.).
- The radix-tree library (`lib/radixtree.h`).
- Bipartite matching DFS (`hk_depth_search()` in `bipartite_match.c`).

## PG 18: Extracted into Its Own File

Prior to PostgreSQL 18, the stack-depth routines lived inside `src/backend/utils/misc/guc.c` alongside the GUC machinery. In PG 18, PostgreSQL extracted them into the dedicated file `src/backend/utils/misc/stack_depth.c`, making the module self-contained and easier to audit independently of the large GUC source file.

## Thread-Safety Note

`restore_stack_base()` exists specifically to support PL/Java, which can call backend functions from threads other than the main thread. Because a different thread has its stack at a completely different memory location, PL/Java saves the current `stack_base_ptr` before the cross-thread call and restores it afterwards using `restore_stack_base()`.

## Related Topics

- [[subsystems/guc|GUC]] — `max_stack_depth` is a GUC parameter; understanding the GUC machinery explains how the check hook enforces the OS-limit constraint at assignment time.
- [[subsystems/error-handling|Error Handling]] — stack depth violations are raised as ordinary `ERROR`s that unwind the transaction normally, relying on PostgreSQL's error-handling infrastructure.
- [[architecture/backend-startup|Backend Startup]] — `set_stack_base()` is called during backend startup to record the reference stack frame address used by all subsequent depth checks.
- [[subsystems/executor/expression-eval|Expression Evaluation]] — the expression evaluator is one of the primary recursive call sites that calls `check_stack_depth()` to guard against deeply nested expressions.
- [[subsystems/planner/bipartite-match|Bipartite Match]] — the DFS routine `hk_depth_search()` in the bipartite matching algorithm is an explicit caller of `check_stack_depth()`.
- [[subsystems/plpgsql/overview|PL/pgSQL]] — the PL/pgSQL interpreter is a key recursive consumer of `check_stack_depth()`, particularly for nested function calls and exception handlers.
- [[subsystems/partitioning/partition-wise-join|Partition-Wise Join]] — `try_partitionwise_join()` calls `check_stack_depth()` to guard against deeply nested partition hierarchies during planning.

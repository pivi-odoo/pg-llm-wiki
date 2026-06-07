---
title: "Scalar Numeric Types"
aliases:
  - integer types
  - float types
  - smallint
  - bigint
  - int4
  - int8
  - float4
  - float8
  - real
  - double precision
source_files:
  - src/backend/utils/adt/int.c
  - src/backend/utils/adt/int8.c
  - src/backend/utils/adt/float.c
symbols:
  - int2in
  - int4in
  - int8in
  - float4in
  - float8in
  - float4in_internal
  - float8in_internal
  - float8out_internal
  - float8_cmp_internal
  - float8_eq
  - float8_lt
  - float8_gt
  - in_range_int4_int4
  - in_range_float8_float8
  - int4gcd_internal
  - int8gcd_internal
  - generate_series_fctx
  - extra_float_digits
---

PostgreSQL's scalar numeric types — `smallint` (`int2`), `integer` (`int4`), `bigint` (`int8`), `real` (`float4`), and `double precision` (`float8`) — are the most fundamental fixed-width types in the system. Each is passed by value in the `Datum` representation on 64-bit platforms and requires no TOAST. Each also has a dense operator set covering arithmetic, comparison, bitwise manipulation, and window function support. Understanding their implementation clarifies why certain edge cases around overflow, NaN ordering, and floating-point output behave the way they do.

## Physical Representation and Pass-by-Value

All five types are fixed-width and fit within a `Datum` on 64-bit platforms. `int2`, `int4`, and `float4` are 2, 4, and 4 bytes respectively. `int8` and `float8` are 8 bytes. Whether `int8` and `float8` are passed by value is controlled at compile time by the `USE_FLOAT8_BYVAL` macro, which defaults to true on 64-bit builds. On 32-bit builds, these types are 8-byte pass-by-reference. The `int8inc()` function in `int8.c` has a special aggregate fast path that mutates the state value in-place to avoid palloc overhead on every `COUNT()` increment.

Because all five types are fixed-width and pass-by-value (on 64-bit), none of them can be TOASTed. The `pg_type` catalog marks them with `typlen` equal to their byte width and `typbyval = true`. The type cache propagates this to callers, so they never attempt to detoast these values.

| SQL name | Internal name | C type | Bytes | Range |
|----------|---------------|--------|-------|-------|
| `smallint` | `int2` | `int16` | 2 | −32,768 to 32,767 |
| `integer` | `int4` | `int32` | 4 | −2,147,483,648 to 2,147,483,647 |
| `bigint` | `int8` | `int64` | 8 | −9,223,372,036,854,775,808 to 9,223,372,036,854,775,807 |
| `real` | `float4` | `float4` | 4 | ~1.18e-38 to ~3.4e38, 6 decimal digits |
| `double precision` | `float8` | `float8` | 8 | ~2.23e-308 to ~1.8e308, 15 decimal digits |

## Integer Overflow Detection

PostgreSQL deliberately never silently wraps integers. Every arithmetic operator for `int2`, `int4`, and `int8` uses the overflow-detecting helpers from `src/include/common/int.h` — `pg_add_s32_overflow`, `pg_sub_s32_overflow`, `pg_mul_s32_overflow`, and their 16-bit and 64-bit counterparts. When these detect overflow, the operator raises `ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE` before the result is produced (int4pl(), int4mi(), int4mul(), `int.c`).

Division has two special cases that require explicit handling beyond overflow checks. Division by zero raises `ERRCODE_DIVISION_BY_ZERO`. Division of the minimum value by −1 — for example `(-2147483648)::integer / -1` — is undefined behaviour on two's-complement hardware: some CPUs signal a hardware exception, others silently produce `INT_MIN`, others zero. PostgreSQL special-cases this by recognising division by −1 as negation and checking whether the dividend is `INT_MIN` before proceeding (int4div(), `int.c`). The same pattern appears identically in `int8div()` (`int8.c`).

Unary negation has the same hazard: `(-32768)::smallint` cannot be negated because `32768` overflows the type. The `int2um()`, `int4um()`, and `int8um()` functions all guard against this with an explicit `INT_MIN` check before applying the negation (int.c, int8.c).

The `abs()` function must guard against the same case: `abs(INT_MIN)` overflows because the positive counterpart cannot be represented. Cross-type arithmetic — for example `int2 + int4` — widens both operands to the larger type before computing. So the result type is the larger of the two. Overflow detection also uses the wider type's range.

## Integer Division Semantics

Integer division truncates toward zero, matching the SQL standard and the behaviour of C's `/` operator on signed integers. This means `(-7) / 2 = -3`, not `-4` (which would be floor division). The modulo operator (`%`) is consistent with this: the result has the same sign as the dividend. Both division and modulo special-case `divisor == -1` to avoid undefined hardware behaviour. Modulo by −1 always returns zero (int4mod(), int2mod(), int8mod()).

## Overflow-Safe Increment for Aggregates

`COUNT()` and similar aggregates that increment a running `int8` counter use `int8inc()` and `int8dec()` from `int8.c`. On pass-by-reference builds, when called from an aggregate context, these functions detect via `AggCheckCallContext()` that the input is modifiable local storage, and update it in-place rather than allocating a new value. This optimisation is significant for large aggregates: without it, every row would require a palloc for the incremented counter value.

## Floating-Point Output and extra_float_digits

The GUC `extra_float_digits` controls how many digits beyond the platform default are used when outputting `float4` and `float8` values as text (float8out_internal(), `float.c`). The default value of `1` activates the *shortest-decimal* algorithm, which produces the fewest decimal digits needed to uniquely identify the IEEE 754 binary value — guaranteeing lossless round-trip through text. When `extra_float_digits` is set to `0` or negative, the output falls back to `printf`-style formatting with `DBL_DIG + extra_float_digits` significant digits, which is the pre-PostgreSQL-12 behaviour.

`float8out_internal()` calls the shortest-decimal algorithm (`double_to_shortest_decimal_buf()` from `src/common/shortest_dec.c`) directly. For `float4`, `float4out()` calls `float_to_shortest_decimal_buf()`. The result is always a string that, when parsed back by `strtod()` or `strtof()`, returns the exact same binary IEEE 754 value.

## Float Input: strtof vs strtod

`float4in_internal()` uses `strtof()` directly rather than first converting to `double` via `strtod()` and then narrowing. The reason is a subtle double-rounding hazard: some decimal values round to a different `float` when converted directly than when converted through `double`. For example, `7.038531e-26` rounds to a different 32-bit IEEE value depending on whether the intermediate conversion is through `double` (float4in_internal(), `float.c`). Using `strtof()` avoids one rounding step and ensures the result matches what a correct decimal-to-float conversion should produce.

`float8in_internal()` uses `strtod()` and is exposed as a shared utility for composite types like `point` and `box` that need to parse floating-point substrings. Both input functions accept `NaN`, `Infinity`, `+Infinity`, `-Infinity`, `inf`, `+inf`, and `-inf` as case-insensitive literals, handled explicitly because platform `strtod()` support for these is inconsistent across C99 implementations.

## NaN Ordering in Float Types

IEEE 754 defines that any comparison involving NaN is false, including `NaN = NaN`. PostgreSQL cannot use this semantics for sorting, hashing, and `GROUP BY` — those operations require a total order. The comparison functions in `src/include/utils/float.h` implement a custom total order, defining NaN to be greater than every finite value and greater than infinity (float8_lt(), float8_gt(), float8_eq()).

The inline implementation is exact: `float8_lt(a, b)` returns `!isnan(a) && (isnan(b) || a < b)`, meaning NaN is never less than anything. Everything is less than NaN. The symmetric form applies for `float8_gt`. The equality function `float8_eq` returns `isnan(a) ? isnan(b) : !isnan(b) && a == b`, so two NaN values compare as equal — a necessary invariant for hashing and deduplication to work correctly.

PostgreSQL applies this NaN-at-the-top ordering everywhere it establishes a total order over floats: B-tree indexes, `ORDER BY`, `MIN`/`MAX`, and window function frame evaluation. The `in_range_float8_float8()` function (`float.c`) used for `RANGE`-mode window frames also explicitly handles NaN operands following the same rule.

## Window Function Frame Support: in_range

All three integer types and both float types provide `in_range` support functions, which the executor calls to determine whether a row falls within a `RANGE BETWEEN ... PRECEDING AND ... FOLLOWING` window frame. The function receives the row's value, the frame reference value (the current peer group's value), the offset, and booleans indicating direction.

For integers, the implementation adds the offset to the base using overflow-checked arithmetic and then compares (in_range_int4_int4(), `int.c`). When addition overflows, the sign of the true sum is known from the direction, so the function returns the correct boolean without needing the actual value. For floats, the implementation delegates the addition to the FPU. The FPU produces a correctly-signed infinity on overflow, which then compares correctly against any finite or infinite `val` (in_range_float8_float8(), `float.c`).

For `int4` with an `int8` offset, `in_range_int4_int8()` widens all arithmetic to `int64` to avoid overflow in intermediate computations. The `int2` variants delegate to the `int4` variants with a cast, rather than duplicating code.

## GCD, LCM, and Bitwise Operations

Integers expose `gcd()` and `lcm()` functions. The GCD implementation uses the Euclidean algorithm computed in *negative space* — the implementation converts both operands to their negative absolute values to avoid an asymmetry at `INT_MIN`, which has no positive counterpart (int4gcd_internal(), int8gcd_internal()). The special case `gcd(INT_MIN, 0)` would produce `abs(INT_MIN)`, which is unrepresentable. So it raises an overflow error. `lcm()` is computed as `abs(a / gcd(a, b) * b)` with overflow checks on the multiplication step.

This file provides bitwise operators (`&`, `|`, `#`, `~`, `<<`, `>>`) for `int2` and `int4` only — not for `int8`, though the catalog exposes them. Shift operators accept an `int4` shift count regardless of the value type.

## Cross-Type Comparison and Implicit Casts

The numeric types form an implicit cast hierarchy: `int2` → `int4` → `int8` → `float8`, and `float4` → `float8`. Each adjacent pair has dedicated cross-type comparison functions — for example `int24eq()`, `int42eq()`, `int84eq()`, and their ordered counterparts — to avoid unnecessary casts when comparing across types. For the integer/integer cross-type operators, these functions always compute the comparison in the wider type's domain (int24eq(), int84eq(), `int.c`, `int8.c`). Cross-type float comparisons widen `float4` to `float8` before comparing (btfloat48cmp(), `float.c`).

## Related Topics

- [[subsystems/types/base-types|Base Types (CREATE TYPE)]] — how fixed-width pass-by-value types are declared and registered in the type system
- [[subsystems/types/range-types|Range and Multirange Types]] — range types that use integer and float scalars as subtypes, including the `in_range` protocol used by range canonicalization
- [[subsystems/storage/toast|TOAST]] — the out-of-line storage mechanism that scalar numerics explicitly bypass due to their fixed width

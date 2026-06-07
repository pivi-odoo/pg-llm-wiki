---
title: "Money Type (Cash)"
aliases:
  - money
  - Cash
  - cash type
  - monetary type
source_files:
  - src/backend/utils/adt/cash.c
  - src/include/utils/cash.h
symbols:
  - Cash
  - cash_in
  - cash_out
  - cash_pl
  - cash_mi
  - cash_div_cash
  - cash_mul_flt8
  - cash_numeric
  - numeric_cash
  - int4_cash
  - cash_cmp
  - cashlarger
  - cashsmaller
  - cash_words
---

# Money Type (Cash)

PostgreSQL's `money` type stores a currency amount as a signed 64-bit integer, denominated in the smallest unit of the session's locale (cents for USD). All display formatting — currency symbol, decimal separator, thousands grouping, sign placement — is driven by the `lc_monetary` locale at I/O time. The binary representation is a raw `int64`, making arithmetic exact and compact. But the tight coupling to locale makes `money` a poor choice whenever data must survive a locale change.

## Physical representation

`Cash` is `typedef int64` (`src/include/utils/cash.h`). The integer value represents the amount in the smallest currency unit: the value `12345` displayed under `en_US.UTF-8` locale is `$123.45`. There is no compile-time scale factor. The number of fractional digits is read at runtime from `lconv->frac_digits` (via `PGLC_localeconv()`, `cash.c`). If `frac_digits` is outside the plausible range `[0, 10]` — as happens in the `C` locale where it is `CHAR_MAX` — the code falls back to 2, matching the common two-decimal-place convention for currencies like USD and EUR.

The range of representable values is approximately ±92.2 quadrillion in the smallest unit, or ±$922 trillion for a two-decimal-place currency.

## Input parsing

`cash_in()` (`cash.c`) interprets a text string using six locale parameters obtained from `lconv`:

| Parameter | Source field | Role |
|---|---|---|
| Currency symbol | `currency_symbol` | Stripped from input before or after the sign |
| Decimal point | `mon_decimal_point` | Separates integer from fractional digits (must be single-byte; defaults to `.`) |
| Thousands separator | `mon_thousands_sep` | Silently ignored during parsing |
| Positive sign | `positive_sign` | Stripped if present |
| Negative sign | `negative_sign` | Sets sign to −1 if found |
| Fractional digits | `frac_digits` | How many digits after the decimal point to consume |

The parser accumulates digits as a negative integer (to avoid overflow at `INT64_MIN`) and flips the sign at the end. Parentheses around a value also denote a negative amount, matching accounting convention. After consuming `frac_digits` digits past the decimal point, any additional digit triggers a round-half-up correction. If fewer than `frac_digits` fractional digits appear, the parser multiplies the value by the appropriate power of 10 to normalize it.

## Output formatting

`cash_out()` reconstructs the numeric string right-to-left in a local buffer — digits, then decimal point, then thousands separators — before prepending or appending the currency symbol and sign according to the POSIX `sign_posn` rules (0 = parentheses, 1–4 = various prefix/suffix placements for symbol and sign). The `p_*` / `n_*` `lconv` fields govern positive and negative amounts separately, so the positive `$1.00` and negative `($1.00)` formats can differ in symbol position and spacing.

## Arithmetic

The operator set is deliberately asymmetric: you can multiply or divide `money` by a scalar, but you cannot add a scalar to it.

| Expression | Result type | Notes |
|---|---|---|
| `money + money` | `money` | Overflow checked via `pg_add_s64_overflow` |
| `money - money` | `money` | Overflow checked via `pg_sub_s64_overflow` |
| `money * float8` | `money` | Result rounded with `rint()`; overflow checked |
| `money / float8` | `money` | Result rounded with `rint()`; NaN and overflow checked |
| `money * float4` | `money` | Same as float8 path |
| `money * int8/int4/int2` | `money` | Integer multiply, overflow checked |
| `money / int8/int4/int2` | `money` | Integer divide; raises `division_by_zero` if divisor is 0 |
| `money / money` | `float8` | Returns a dimensionless ratio |

There is no `money + float8` or `money + integer` operator. This forces callers to be explicit: computing a price increase requires multiplying by a factor, not adding a float. The `money / money` case returns `float8` to represent a dimensionless ratio (e.g., what fraction of a budget is spent).

Overflow for integer operations uses `pg_add_s64_overflow` and `pg_mul_s64_overflow` from `src/include/common/int.h`. For float operations, the code first computes the result as `float8`, then validates it with `FLOAT8_FITS_IN_INT64` and `isnan()` before casting to `int64`.

## Comparison and aggregates

Six comparison operators (`=`, `<>`, `<`, `<=`, `>`, `>=`) delegate to direct integer comparisons. `cash_cmp()` provides a three-way comparator for the B-tree operator class, enabling `ORDER BY` and index scans on `money` columns. `cashlarger()` and `cashsmaller()` support the `max` and `min` aggregate functions.

`cash_words()` converts a money value to its English-language description ("One hundred twenty three dollars and forty five cents"). It is hardcoded to USD conventions and is not locale-aware.

## Type conversions

All explicit casts between `money` and other numeric types go through the locale scale factor at conversion time:

- `money::numeric` (`cash_numeric()`): divides the raw int64 by `10^frac_digits` to produce a scaled numeric value.
- `numeric::money` (`numeric_cash()`): multiplies by `10^frac_digits` and rounds to the nearest integer.
- `int4::money` / `int8::money`: multiply the integer by `10^frac_digits`, treating the input as a whole-unit amount (e.g., `5::money` becomes `$5.00` under a two-decimal-place locale).

There are no implicit casts between `money` and any other type. All conversions require an explicit `CAST` or `::` syntax.

## Locale portability hazard

The central weakness of the `money` type is that the system reads `frac_digits` from the session locale at every I/O boundary. If a database is dumped under a locale with `frac_digits = 2` and restored under one with `frac_digits = 0`, the binary integer values are preserved exactly. But every value is misinterpreted by a factor of 100 at display time — silently. The same hazard applies to `numeric_cash` and `int_cash` casts. For applications that must be portable across locales or that need auditable, locale-independent storage, `numeric` with an explicit scale is a safer choice.

## See also

- [[subsystems/types/numeric-scalar-types]] — numeric and integer types without locale coupling
- [[subsystems/types/locale]] — how lc_monetary and other locale categories work in PostgreSQL

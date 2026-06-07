---
title: "Integer I/O and Number Utilities"
aliases:
  - integer parsing
  - pg_strtoint32
  - pg_strtoint64
  - pg_ltoa
  - pg_lltoa
  - numutils
  - integer formatting
source_files:
  - src/backend/utils/adt/numutils.c
  - src/include/utils/builtins.h
symbols:
  - pg_strtoint16
  - pg_strtoint32
  - pg_strtoint64
  - pg_strtouint64
  - pg_ltoa
  - pg_lltoa
  - pg_ulltoa_n
  - pg_ultoa_n
  - decimalLength32
  - decimalLength64
---

`src/backend/utils/adt/numutils.c` provides the low-level integer parsing and formatting routines that underpin the `int2`, `int4`, and `int8` type I/O functions. These routines also handle OID parsing, and anywhere else PostgreSQL must convert between integers and their decimal text representations. The code is optimised for the common case of small positive numbers. It is also designed to produce predictable, locale-independent output regardless of the server's `LC_NUMERIC` setting.

## Parsing

The public parsing entry points are `pg_strtoint16`, `pg_strtoint32`, `pg_strtoint64`, and `pg_strtouint64`. All four share the same design:

- They accept an optional leading sign and a run of ASCII decimal digits.
- They detect overflow by tracking the running value against type-specific limits before each digit is incorporated. This catches overflow without undefined behaviour.
- They reject leading whitespace and trailing non-digit characters (unlike `strtol`/`strtoll`, which silently stop at the first non-digit).
- They support *soft errors* via the `escontext` parameter. When `escontext` is non-NULL and the caller opts in, a parse failure sets an error in the context. It returns 0, rather than calling `ereport(ERROR)`. This lets callers like `pg_input_is_valid()` probe types safely.

For `int4` and `int8`, a fast path handles the common case of a non-empty run of digits with no sign character and no risk of overflow (strings shorter than the maximum decimal length of the type). The fast path avoids branch-heavy overflow checking for the vast majority of input.

## Formatting

The integer-to-string formatters are written for speed because they are called on every row output for integer columns. Two techniques drive most of the performance:

### Two-digit lookup table

`DIGIT_TABLE` is a 200-byte array of precomputed two-digit strings `"00"` through `"99"`. Instead of formatting one digit at a time with a modulo and divide, the formatters consume two digits per iteration by indexing into this table and copying 2 bytes:

```c
while (value >= 100)
{
    /* value % 100 gives the index into DIGIT_TABLE */
    dst -= 2;
    memcpy(dst, DIGIT_TABLE + (value % 100) * 2, 2);
    value /= 100;
}
```

This halves the number of divisions. It typically outperforms `sprintf` on integer formatting benchmarks.

### Pre-computing decimal length

`decimalLength32()` and `decimalLength64()` determine how many decimal digits an integer will produce without trial formatting. They use the identity:

```
floor(log10(v)) ≈ (floor(log2(v)) + 1) * 1233 / 4096
```

A single `pg_leftmost_one_pos32()`/`pg_leftmost_one_pos64()` intrinsic finds the bit-length of `v` (this maps to `BSR` on x86 or `CLZ` on ARM). The formatter uses the result to allocate exactly the right buffer size, and to position itself to write directly into the output without reversing digits.

## Output Conventions

All integer formatters produce:

- Signed decimal for `int2`, `int4`, `int8` (with a leading `-` for negative values).
- Unsigned decimal for `oid` and `uint64` variants.
- No thousands separator, no locale-specific formatting — always pure ASCII digits.
- No leading zeros except when the value itself is zero.

The formatter always fills the output buffer right-to-left (most-significant digit last). It returns the pointer to the start of the result. Callers must allocate a buffer of sufficient size (at most 21 characters for a 64-bit signed integer including sign and NUL).

## Usage in Type I/O

The standard type I/O functions call these utilities:

| SQL Type | Parse | Format |
|---|---|---|
| `int2` (`smallint`) | `pg_strtoint16` | `pg_ltoa` (via cast) |
| `int4` (`integer`) | `pg_strtoint32` | `pg_ltoa` |
| `int8` (`bigint`) | `pg_strtoint64` | `pg_lltoa` |
| `oid` | `uint32in_subr` (calls `pg_strtouint64`) | `pg_ultoa_n` |

The backend also uses them wherever it converts internal counter values, OIDs, or tuple offsets to text for display or logging. This makes them among the most frequently called functions in a busy system.

## Related Topics

- [[subsystems/types/numeric-scalar-types|Numeric Scalar Types]] — the SQL-facing `int2`/`int4`/`int8` type layer
- [[subsystems/types/oid-type|OID Type]] — uses `uint32in_subr` for parsing
- [[subsystems/types/variable-length-types|Variable-Length Types]] — text representation of varlena types

---
title: "Oracle-Compatible String Functions"
aliases:
  - lower upper initcap
  - lpad rpad
  - btrim ltrim rtrim
  - translate
  - ascii chr
  - repeat
  - oracle_compat
source_files:
  - src/backend/utils/adt/oracle_compat.c
symbols:
  - lower
  - upper
  - initcap
  - lpad
  - rpad
  - btrim
  - btrim1
  - ltrim
  - ltrim1
  - rtrim
  - rtrim1
  - dotrim
  - translate
  - ascii
  - chr
  - repeat
  - dobyteatrim
---

A cluster of everyday string functions — `lower`, `upper`, `initcap`, `lpad`, `rpad`, `btrim`, `ltrim`, `rtrim`, `translate`, `ascii`, `chr`, and `repeat` — live in `oracle_compat.c`. The name reflects their origin: these functions were added in the mid-1990s to ease migration from Oracle databases. They are now standard SQL or de facto standard across most database systems. They are also among the most commonly called functions in web application queries.

## Case Functions: lower, upper, initcap

All three delegate entirely to the locale layer: `str_tolower()`, `str_toupper()`, and `str_initcap()` from `formatting.c`. Each function accepts the current collation via `PG_GET_COLLATION()` and passes it through. On databases with ICU collations, this means case folding is locale-aware — `upper('ß')` returns `'SS'` in German locales under ICU.

`initcap` defines a "word" as any sequence of alphanumeric characters delimited by non-alphanumeric characters. It capitalises the first character of each word and lowercases the rest. This is not the same as title case in typography (which handles articles, prepositions, etc. differently) — it is a mechanical per-word operation.

## Padding: lpad and rpad

`lpad(string, length, fill)` and `rpad(string, length, fill)` pad `string` to exactly `length` characters, left or right respectively. If `string` is already longer than `length`, the function truncates it on the right to fit. The fill string wraps around cyclically if it is shorter than the required padding.

`lpad` and `rpad` handle multibyte encodings correctly: they compute lengths in characters, not bytes, using `pg_mbstrlen_with_len()`. The worst-case output allocation uses `pg_database_encoding_max_length() * length` bytes to account for encodings where a single character can be multiple bytes. Overflow checks using `pg_mul_s32_overflow` / `pg_add_s32_overflow` guard against integer overflow on enormous requested lengths.

## Trimming: btrim, ltrim, rtrim

All three trim functions share the `dotrim()` implementation, parameterised by two booleans (`doltrim`, `dortrim`). The function removes characters from the front and back of the string as long as they appear anywhere in the set string — not in sequence, but as a set membership test. `btrim('xxABCxx', 'xB')` returns `'ABC'` because `x` and `B` are both members of the trim set. But trimming stops at `A` and `C`, which are not members of the trim set.

For multibyte encodings, `dotrim()` pre-builds arrays of character pointers and byte-lengths for both strings to avoid re-parsing the encoding on every inner-loop iteration. For single-byte encodings it uses the simpler byte-at-a-time approach. `btrim1` and similar `1`-suffixed variants hardcode the trim set to a single space, bypassing argument parsing.

`byteatrim`, `bytealtrim`, and `byteartrim` are byte-oriented variants that operate on `bytea`, trimming by byte value rather than character.

## translate

`translate(string, from, to)` performs character-by-character substitution: each character in `string` that appears in `from` is replaced by the corresponding character in `to`. If `from` is longer than `to`, characters past the end of `to` are deleted rather than substituted. All characters not in `from` are copied unchanged.

The implementation walks the string character by character, searching `from` for each source character using `memcmp`. This is O(n×m), where n is the string length and m is the length of `from`. That complexity is acceptable for the typical use case of small `from` sets (e.g., removing a handful of punctuation characters). The implementation allocates the output buffer at worst-case size upfront (one maximum-length character per input character) and does not reallocate it — the `SET_VARSIZE` at the end sets the true length.

## ascii and chr

`ascii(string)` returns the code point of the first character. For UTF-8 databases, it decodes the full Unicode code point from the multi-byte sequence. For other multi-byte encodings, it requires the first byte to be in the ASCII range (1–127) and returns that byte value. For single-byte encodings it returns the byte value directly (1–255). An empty string returns 0.

`chr(integer)` is the inverse: it returns the character with the given code point. For UTF-8, `chr()` encodes code points from 128 to 1,114,111 (U+10FFFF) as UTF-8 sequences. It rejects surrogate pairs (U+D800 to U+DFFF) because they are not valid Unicode scalars. For other multi-byte encodings, it accepts only the ASCII range (1–127). It always rejects zero because null bytes cannot appear in text values.

## repeat

`repeat(string, n)` returns `string` concatenated `n` times. `repeat()` treats negative `n` as zero. It pre-allocates the output at `len * n` bytes and fills it in a loop with `memcpy`. It calls `CHECK_FOR_INTERRUPTS()` on each iteration, so that repeating a string billions of times can be cancelled.

## Related Topics

- [[subsystems/types/locale|Locale and Collation]] — the collation-aware lower/upper/initcap functions delegate here
- [[subsystems/types/formatting-functions|Formatting Functions]] — `to_char`, `to_date`, and related Oracle-origin format functions
- [[subsystems/types/variable-length-types|Variable-length types]] — how `text` and `bytea` are represented in memory

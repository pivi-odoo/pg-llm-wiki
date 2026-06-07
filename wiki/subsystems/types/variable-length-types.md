---
title: "Variable-Length Text Types"
aliases:
  - varlena types
  - text type
  - varchar
  - bpchar
  - bytea
  - bit varying
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/varlena.c
  - src/backend/utils/adt/varchar.c
  - src/backend/utils/adt/varbit.c
symbols:
  - cstring_to_text
  - cstring_to_text_with_len
  - text_to_cstring
  - text_substring
  - text_length
  - varstr_sortsupport
  - VarStringSortSupport
  - TextPositionState
  - bpchar_input
  - varchar_input
  - bpchartruelen
  - VarBit
  - VARBITLEN
  - VARBITTOTALLEN
  - VARBIT_PAD
---

PostgreSQL's variable-length text family — `text`, `character varying(n)`, `character(n)`, `bytea`, `bit`, and `bit varying` — all share the same `varlena` physical representation on disk. The diversity of SQL semantics (character counting in a multibyte encoding, blank-padding rules, bit-level operators) sits on top of a single underlying storage model. The I/O, comparison, and string-operation functions live almost entirely in `varlena.c`, `varchar.c`, and `varbit.c`.

## The varlena representation

PostgreSQL stores every value in this family as a contiguous byte sequence beginning with a variable-length header that encodes the total allocation size. `src/include/varatt.h` defines the header format. All other varlena types share this format. Functions that construct values allocate `VARHDRSZ + payload_bytes` and call `SET_VARSIZE()` to fill the header. Functions that read values use `VARSIZE_ANY_EXHDR()` to obtain the payload size and `VARDATA_ANY()` to obtain a pointer past the header, without caring which header variant is in use.

A key discipline in the function implementations is the distinction between two detoasting levels:

- `PG_DETOAST_DATUM()` / `DatumGetTextPP()`: accepts short-header and packed forms without reallocating, avoiding a copy for values that are already in memory in a usable layout. Functions that only read payload bytes use this form.
- `PG_DETOAST_DATUM_COPY()`: always returns a fresh palloc'd copy with a full-size 4-byte header. Needed by functions that will modify the value or pass it somewhere that expects a plain pointer.

`text_to_cstring()` handles the common C-code pattern of obtaining a null-terminated string. It calls `pg_detoast_datum_packed()` to get the unpacked form, copies the payload into a freshly palloc'd buffer, and appends `\0` (varlena.c). The inverse, `cstring_to_text_with_len()`, allocates a varlena of the right size and does a single `memcpy`.

## The `text` type

`text` is the simplest member of the family: it stores an arbitrary byte sequence that happens to be a valid encoding of the server's character set. There is no declared maximum length. The SQL type modifier is absent (`typmod = -1`). The input function `textin()` is a trivial wrapper around `cstring_to_text()`; the output function calls `TextDatumGetCString()` (varlena.c).

Character counting versus byte counting is a persistent concern throughout the text implementation. `textlen()` returns the logical character count. For single-byte encodings this is the same as `toast_raw_datum_size(str) - VARHDRSZ`, which can be computed without detoasting the value at all. For multibyte encodings, the function must detoast and walk the byte sequence with `pg_mbstrlen_with_len()` (varlena.c, `text_length()`). `octet_length()` separately provides the byte length by calling `toast_raw_datum_size()` directly.

Substring extraction via `text_substring()` is TOAST-aware. For single-byte encodings where character offsets equal byte offsets, the function uses `DatumGetTextPSlice()` to fetch only the needed bytes from an out-of-line [[subsystems/storage/toast|TOAST]] value. For multibyte encodings, the function cannot know the required byte range without scanning from the start. It therefore fetches a conservatively large slice and then walks it character by character (varlena.c).

Pattern search with `position()` and `strpos()` uses a Boyer-Moore-Horspool algorithm via `TextPositionState`. The state struct holds the precomputed skip table indexed by the 256 possible mismatched bytes, the haystack and needle pointers, and a reference point for converting byte positions back to character positions. For multibyte encodings, `text_position_setup()` explicitly rejects non-deterministic ICU collations: it raises an error if `pg_locale_deterministic()` returns false. The byte-level skip table cannot be constructed for collations that may treat multiple byte sequences as equivalent (varlena.c).

## `character varying(n)` and `character(n)`

Both types are physically identical to `text` — `VarChar` and `BpChar` are typedef aliases for `struct varlena` — but they impose a length limit via the type modifier. The typmod encoding is `VARHDRSZ + max_characters`, so `varchar(10)` stores typmod 14 on a 32-bit system. This offset-by-VARHDRSZ convention is historical and documented inline (`anychar_typmodin()`, varchar.c).

**`varchar(n)`** (`character varying`): on input, `varchar_input()` counts characters with `pg_mbcharcliplen()` and raises `ERRCODE_STRING_DATA_RIGHT_TRUNCATION` if the string is too long. Per SQL, `varchar_input()` silently drops trailing spaces that would be truncated. Once accepted, PostgreSQL stores the value with no padding — there is no length difference between a `varchar(50)` column holding "abc" and a `text` column holding "abc". The planner support function `varchar_support()` can eliminate a `varchar(N)` length-coercion call at plan time when the input is already declared at most N characters wide (varchar.c).

**`character(n)`** (`bpchar`, blank-padded char): `bpchar_input()` always pads the stored value with ASCII spaces to reach exactly the declared character length. The padding is physical — the varlena payload contains the trailing spaces. Comparison functions must strip trailing spaces before comparing. `bpchartruelen()` walks the byte array from the right to find the last non-space byte. All equality and ordering operators apply this stripping. The character count returned by `char_length()` / `bpcharlen()` also strips trailing spaces (varchar.c).

Because `varchar` and `bpchar` share all their operators and most functions with `text`, `varcharsend()` directly delegates to `textsend()`, and `bpcharsend()` does the same. The binary wire format is identical for all three (varchar.c).

## Comparison and sort support

String comparison across the family routes through `text_cmp()` for `text` and `varchar`, and through a trailing-space-stripped equivalent for `bpchar`. Both take a collation OID. For `lc_collate = C` (or the equivalent ICU rule), comparison reduces to `memcmp()` on the raw bytes, which is fast and avoids locale overhead. For locale-aware collations, `varstr_cmp()` calls into `strcoll()` or the ICU collation APIs.

`varstr_sortsupport()` is the central entry point for accelerated sorting (varlena.c). The executor's sort path invokes it via `bttextsortsupport()` and equivalent functions. The logic selects a direct comparison function — `varstrfastcmp_c` for C locale, `varlenafastcmp_locale` for locale-aware — and, when conditions allow, enables abbreviated key generation. Abbreviated keys are fixed-width `Datum`-sized values derived from the full string that preserve sort order; the sort pass compares abbreviated keys first, falling back to the full string only for ties. This avoids repeated detoasting of long strings during sort. `strxfrm()` computes the abbreviation into a `VarStringSortSupport` scratch buffer. HyperLogLog cardinality estimation (the `abbr_card` and `full_card` fields) monitors whether abbreviation is actually reducing comparisons. `varstr_abbrev_abort()` can disable it mid-sort if the cardinality is too low to help.

One important constraint: non-deterministic ICU collations — those where `pg_locale_deterministic()` is false — disable abbreviation and also refuse to participate in substring searches. For equality tests with such collations, the code must fall through to `varstr_cmp()` regardless of the optimisation status (varlena.c).

## `bytea`

`bytea` is semantically a raw byte array with no encoding interpretation. Its SQL representation accepts two input formats: the hex format (`\xDEADBEEF`) and the traditional octal escape format (`\012` for a newline). The `bytea_output` GUC controls the output format (default `hex`). The implementation shares the same `varlena` header as text, but the payload is opaque bytes: no character-set validation, no multibyte awareness, no collation (varlena.c, `byteain()` / `byteaout()`).

Substring operations on `bytea` use byte offsets directly. `bytea_substring()` calls `DatumGetByteaPSlice()`, which in turn calls the TOAST slice interface. This enables efficient partial retrieval of out-of-line values stored in EXTERNAL (uncompressed) storage.

## `bit(n)` and `bit varying(n)`

The bit string types use a distinct `VarBit` struct rather than a plain `varlena` alias. The layout is:

```
varlena header  (VARHDRSZ bytes)
bit_len         (int32 — number of valid bits)
bit_dat[]       (bits8 array, most-significant byte first)
```

The total allocation is `VARBITTOTALLEN(bitlen)` bytes. A critical invariant is that any low-order padding bits in the final byte of `bit_dat` must be zero; `VARBIT_PAD()` enforces this after any operation that might leave stale bits. Comparison operations can then work byte-by-byte without masking the last byte, provided the invariant holds (varbit.c).

`bit(n)` requires exact length: `bit_in()` raises `ERRCODE_STRING_DATA_LENGTH_MISMATCH` if the input string length does not equal the declared typmod. `bit varying(n)` (varbit) silently truncates to the declared maximum. Both types accept binary (`b'1010'`) and hex (`x'A'`) input literals. PostgreSQL stores the typmod for bit types as the raw bit count, unlike the `VARHDRSZ + n` convention for character types (varbit.c, `anybit_typmodin()`).

Bitwise operators (AND, OR, XOR, NOT, shift) operate on full byte words using standard C integer operations, with a final `VARBIT_PAD()` call to zero any fractional tail bits. Concatenation (`bit_catenate()`) and substring extraction (`bitsubstring()`) handle the non-byte-aligned case by shifting bits across byte boundaries.

## Type modifier summary

| Type | Typmod encoding | Stored length | Padding |
|---|---|---|---|
| `text` | none (−1) | actual bytes | none |
| `varchar(n)` | `VARHDRSZ + n` chars | actual bytes ≤ n chars | none |
| `char(n)` / `bpchar(n)` | `VARHDRSZ + n` chars | always n chars | space-padded to n |
| `bytea` | none (−1) | actual bytes | none |
| `bit(n)` | n bits | ⌈n/8⌉ bytes | zero-padded last byte |
| `bit varying(n)` | n bits max | actual ⌈bitlen/8⌉ bytes | zero-padded last byte |

## Related Topics

- [[subsystems/storage/toast|TOAST]] — how values exceeding the tuple size limit are compressed and stored out-of-line
- [[subsystems/types/base-types|Base Types]] — how custom base types declare their own varlena layout and I/O functions

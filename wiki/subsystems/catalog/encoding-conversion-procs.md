---
title: "Encoding Conversion Procedures"
aliases:
  - encoding conversion procs
  - character encoding conversion
  - conv.c
  - pg_mb conversion
source_files:
  - src/backend/utils/mb/conv.c
  - src/backend/utils/mb/iso.c
  - src/backend/utils/mb/stringinfo_mb.c
  - src/backend/utils/mb/win1251.c
  - src/backend/utils/mb/win866.c
  - src/backend/utils/mb/wstrcmp.c
  - src/backend/utils/mb/wstrncmp.c
  - src/backend/utils/mb/conversion_procs/cyrillic_and_mic/cyrillic_and_mic.c
  - src/backend/utils/mb/conversion_procs/euc2004_sjis2004/euc2004_sjis2004.c
  - src/backend/utils/mb/conversion_procs/euc_cn_and_mic/euc_cn_and_mic.c
  - src/backend/utils/mb/conversion_procs/euc_jp_and_sjis/euc_jp_and_sjis.c
  - src/backend/utils/mb/conversion_procs/euc_kr_and_mic/euc_kr_and_mic.c
  - src/backend/utils/mb/conversion_procs/euc_tw_and_big5/big5.c
  - src/backend/utils/mb/conversion_procs/euc_tw_and_big5/euc_tw_and_big5.c
  - src/backend/utils/mb/conversion_procs/latin2_and_win1250/latin2_and_win1250.c
  - src/backend/utils/mb/conversion_procs/latin_and_mic/latin_and_mic.c
  - src/backend/utils/mb/conversion_procs/utf8_and_big5/utf8_and_big5.c
  - src/backend/utils/mb/conversion_procs/utf8_and_cyrillic/utf8_and_cyrillic.c
  - src/backend/utils/mb/conversion_procs/utf8_and_euc2004/utf8_and_euc2004.c
  - src/backend/utils/mb/conversion_procs/utf8_and_euc_cn/utf8_and_euc_cn.c
  - src/backend/utils/mb/conversion_procs/utf8_and_euc_jp/utf8_and_euc_jp.c
  - src/backend/utils/mb/conversion_procs/utf8_and_euc_kr/utf8_and_euc_kr.c
  - src/backend/utils/mb/conversion_procs/utf8_and_euc_tw/utf8_and_euc_tw.c
  - src/backend/utils/mb/conversion_procs/utf8_and_gb18030/utf8_and_gb18030.c
  - src/backend/utils/mb/conversion_procs/utf8_and_gbk/utf8_and_gbk.c
  - src/backend/utils/mb/conversion_procs/utf8_and_iso8859/utf8_and_iso8859.c
  - src/backend/utils/mb/conversion_procs/utf8_and_iso8859_1/utf8_and_iso8859_1.c
  - src/backend/utils/mb/conversion_procs/utf8_and_johab/utf8_and_johab.c
  - src/backend/utils/mb/conversion_procs/utf8_and_sjis/utf8_and_sjis.c
  - src/backend/utils/mb/conversion_procs/utf8_and_sjis2004/utf8_and_sjis2004.c
  - src/backend/utils/mb/conversion_procs/utf8_and_uhc/utf8_and_uhc.c
  - src/backend/utils/mb/conversion_procs/utf8_and_win/utf8_and_win.c
symbols:
  - local2local
  - UtfToLocal
  - LocalToUtf
  - latin2mic
  - mic2latin
  - appendStringInfoStringQuoted
  - pg_char_and_wchar_strcmp
  - pg_wchar_strncmp
  - CHECK_ENCODING_CONVERSION_ARGS
  - pg_mb_radix_tree
  - pg_utf_to_local_combined
  - pg_local_to_utf_combined
---

PostgreSQL's encoding conversion infrastructure translates text between the database server encoding and the client encoding whenever they differ. PostgreSQL splits the work across two layers. `src/backend/utils/mb/conv.c` provides generic byte-level translation primitives. Each subdirectory under `src/backend/utils/mb/conversion_procs/` compiles to a separate shared library that registers one or more conversion functions in `pg_proc`. The [[subsystems/catalog/encoding-conversions|pg_conversion catalog]] ties everything together by mapping encoding pairs to the function OIDs that implement them.

## Conversion functions as loadable procedures

Each `conversion_procs/` subdirectory is an independent shared library. A directory named `utf8_and_euc_jp` compiles to a library that registers two `pg_proc` entries: `euc_jp_to_utf8` and `utf8_to_euc_jp`. Conversion is directional: knowing how to convert in one direction does not imply the reverse. The two functions share the same mapping data (imported from `Unicode/euc_jp_to_utf8.map` and `Unicode/utf8_to_euc_jp.map`) but traverse it in opposite directions.

Every conversion function has the same fixed signature expected by the catalog machinery:

```sql
function_name(integer, integer, cstring, internal, integer, boolean) RETURNS integer
```

The arguments are: source encoding ID, destination encoding ID, source C string, destination buffer, source length in bytes, and a `noError` flag. The return value is the number of source bytes successfully consumed. When `noError` is true, the function stops at the first unmappable character and returns the count consumed so far rather than raising an error. This supports partial conversions used in error-recovery and input validation paths.

Every conversion function calls the `CHECK_ENCODING_CONVERSION_ARGS` macro (defined in `src/include/mb/pg_wchar.h`) at its top. It delegates to `check_encoding_conversion_args()`. That function verifies that the runtime-passed encoding IDs match the encoding pair the function was written for. This guard exists because the same shared library can register multiple functions. A misconfigured `pg_conversion` row could otherwise route a conversion to the wrong function.

## Generic single-byte conversion primitives

`conv.c` provides three primitives that most single-byte conversion functions delegate to rather than implementing their own byte loops.

`local2local()` handles single-byte to single-byte conversions driven by a 128-entry lookup table. The table covers only the high-bit range (0x80–0xFF); ASCII bytes pass through unchanged. A zero entry in the table means the source byte has no equivalent in the target encoding; if `noError` is false, the function calls `report_untranslatable_char()`, which raises an error with the precise byte position. The Cyrillic conversion library (`cyrillic_and_mic.c`) makes heavy use of this: the inline arrays `iso2koi`, `koi2iso`, `win2koi`, and so on are all 128-element tables passed directly to `local2local()`.

`UtfToLocal()` and `LocalToUtf()` handle conversions between UTF-8 and any legacy encoding whose mapping is expressed as a `pg_mb_radix_tree`. The radix tree structure (`src/include/mb/pg_wchar.h`) organises Unicode-to-local and local-to-Unicode mappings by input byte length (1–4 bytes), with separate root offsets and byte-range bounds for each input width. This avoids scanning the whole table for each character: the lookup follows the radix trie directly. Both functions also accept a `pg_utf_to_local_combined` or `pg_local_to_utf_combined` array for handling Unicode combining character sequences that map to a single legacy code point, plus an optional `utf_local_conversion_func` callback for algorithmic conversions that cannot be expressed as a static table (certain Chinese encoding extensions use this).

The identity case in both functions always handles ASCII bytes without a table lookup: `UtfToLocal` passes single-byte UTF-8 sequences through directly, and `LocalToUtf` passes bytes below 0x80 through directly. This means ASCII-only strings pass through both functions at near-memcpy speed.

## Encoding table files

`iso.c` and `win1251.c` and `win866.c` in `src/backend/utils/mb/` are code generators, not runtime files. Each is a standalone C program that reads a two-column mapping table from stdin and emits a C source file containing two 128-element `static char` arrays (one for each conversion direction). The conversion proc libraries then include the generated arrays at compile time.

`win1251.c` generates the KOI8-R ↔ CP1251 tables. `win866.c` generates the KOI8-R ↔ CP866 (DOS Cyrillic, also called Alternativny Variant) tables. `iso.c` generates the KOI8-R ↔ ISO-8859-5 tables. All three encodings represent the Cyrillic character repertoire with different byte assignments; `cyrillic_and_mic.c` embeds the resulting arrays directly, so there is no runtime file I/O for encoding lookup.

## The MIC pivot encoding

Several older conversion libraries route conversions through MULE_INTERNAL (`PG_MULE_INTERNAL`), a multibyte encoding derived from the Mule (Multilingual Emacs) project. The `latin_and_mic.c`, `euc_jp_and_sjis.c`, `euc_kr_and_mic.c`, and `euc_cn_and_mic.c` libraries all register both direct conversions and MIC-mediated paths.

In `conv.c`, `latin2mic()` and `mic2latin()` handle the Latin ↔ MIC conversion for encodings whose local byte values map directly to MIC code positions. For example, `latin1_to_mic` in `latin_and_mic.c` calls `latin2mic(src, dest, len, LC_ISO8859_1, PG_LATIN1, noError)`, which prefixes each high-byte character with the Mule character-set ID byte. The inverse strips that prefix byte.

MIC's role is largely historical. In early PostgreSQL, UTF-8 was not yet the default. MIC served as the common intermediate for East Asian encoding pairs that lacked a direct mapping. UTF-8 databases are now the default, and UTF-8-pivoted conversion paths are registered for all major encodings. As a result, conversion code rarely takes MIC paths. The libraries retain them because they are still registered in `pg_conversion` for databases that use legacy server encodings.

## Multibyte-aware string building

`stringinfo_mb.c` extends the standard `StringInfo` API with `appendStringInfoStringQuoted()`. When appending a string to an error message or log entry with a byte-length limit, a naive truncation at an arbitrary byte offset can split a multibyte character, producing an invalid string in the target encoding. `appendStringInfoStringQuoted` calls `pg_mbcliplen()` to find the last character boundary at or before the requested byte limit before copying, then appends an ellipsis if the string was truncated. This function is backend-only (not in `common/stringinfo.c`) because `pg_mbcliplen` depends on encoding metadata. That metadata is unavailable in frontend code.

## Encoding-aware string comparison

`wstrcmp.c` and `wstrncmp.c` provide `pg_char_and_wchar_strcmp()`, `pg_wchar_strncmp()`, and `pg_wchar_strlen()`. These operate on `pg_wchar` (a 32-bit Unicode code point type) rather than raw bytes, so comparison is code-point ordered regardless of the underlying byte encoding. PostgreSQL uses them in identifier resolution and parsing contexts where the input has already been decoded to `pg_wchar` form. In a UTF-8 database, byte-order and code-point order coincide for the Basic Multilingual Plane. But in databases using non-UTF8 server encodings (EUC_JP, SJIS, etc.), the byte representation can be in an order incompatible with dictionary order. The `pg_wchar` layer normalises this.

## How the conversion path avoids redundant work

When a session connects with the same encoding as the database server, `PrepareClientEncoding()` in `mbutils.c` detects the match and stores null function pointers for the conversion path. Every subsequent `pg_client_to_server()` and `pg_server_to_client()` call checks this first and returns immediately, so it never invokes a conversion proc. For a UTF-8 client connecting to a UTF-8 database — the common case — the overhead of the encoding layer is a single pointer check per text value, not a function call.

When encodings differ, see [[subsystems/catalog/encoding-conversions|pg_conversion catalog]] for how the session locates and caches the conversion function via `FindDefaultConversionProc()` and `ConvProcInfo` so that the rest of the session needs no further catalog access.

Encoding mismatches that have no registered conversion produce an error at `PrepareClientEncoding` time (for session-level mismatches) or at `pg_do_encoding_conversion()` time (for explicit `CONVERT` calls). The error indicates the missing encoding pair by name. That name is the first place to look when a client connection fails with "conversion not supported".

## See also

- [[subsystems/catalog/encoding-conversions|pg_conversion catalog]] — catalog schema, `FindDefaultConversionProc`, session encoding setup
- [[subsystems/catalog/core-catalogs|core system catalogs]] — syscache lookup patterns used by encoding catalog access

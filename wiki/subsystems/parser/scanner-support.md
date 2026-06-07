---
title: "Lexer and Scanner Support Routines"
aliases:
  - scansup
  - identifier downcasing
  - identifier truncation
source_files:
  - src/backend/parser/scansup.c
  - src/include/parser/scansup.h
symbols:
  - downcase_truncate_identifier
  - downcase_identifier
  - truncate_identifier
  - scanner_isspace
---

The scanner support routines in `scansup.c` are a small but critical layer between the Flex-generated lexer and the rest of the parser. They handle two concerns: normalising identifier case and enforcing the system-wide name length limit. Every code path that processes SQL identifiers or whitespace must handle both consistently. Because these routines are called both inside the lexer and from utility code throughout the backend, they serve as the single canonical definition of what "a valid, normalised identifier" means in PostgreSQL.

## Identifier Case Folding

SQL is case-insensitive for unquoted identifiers. A table named `MyTable` is the same as `mytable` or `MYTABLE`. PostgreSQL implements this by folding unquoted identifiers to lowercase during lexing, before it stores or looks up any name in the catalog.

The workhorse is `downcase_identifier()` (`scansup.c`). Its character-level logic reflects a careful compromise between SQL standards, locale correctness, and multi-byte encoding safety:

- For 7-bit ASCII letters (`A`–`Z`), `downcase_identifier()` folds them with an arithmetic shift (`ch += 'a' - 'A'`), completely bypassing the C locale. This is intentional: locales like Turkish map uppercase `I` to a dotless lowercase `ı` rather than `i`. That mapping would make SQL identifiers locale-dependent and break portability. The comment in the source explicitly calls out Turkish as the problematic case.
- For bytes with the high bit set (non-ASCII), `downcase_identifier()` uses `tolower()`, but **only** in single-byte database encodings. The check `pg_database_encoding_max_length() == 1` guards this path. In a multi-byte encoding such as UTF-8, a byte with the high bit set may be a continuation byte of a multi-byte character, not a standalone character, so applying `tolower()` to it would corrupt the string.
- `downcase_identifier()` passes multi-byte characters in multi-byte encodings through unchanged. PostgreSQL does not implement full Unicode case mapping (as required by SQL:1999). The comment in `scansup.c` acknowledges this gap explicitly.

The public API presented to the lexer is `downcase_truncate_identifier()`. It is a thin wrapper that calls `downcase_identifier()` with `truncate=true`. A separate `downcase_identifier()` entry point with an explicit `truncate` flag exists for callers that need downcasing without truncation, such as internal code paths that construct names.

The lexer (`scan.l`) calls `downcase_truncate_identifier()` at two points: once for bare unquoted identifiers and once for keywords that the parser subsequently treats as identifiers. Quoted identifiers skip this path entirely; the lexer preserves their case verbatim.

## Identifier Length Truncation

PostgreSQL limits the storage size of any identifier to `NAMEDATALEN - 1` bytes (63 bytes in a standard build, where `NAMEDATALEN = 64` as defined in `pg_config_manual.h`). This limit exists because PostgreSQL stores identifiers as fixed-size `NameData` fields (`char[NAMEDATALEN]`) in catalog tuples such as `pg_class.relname` and `pg_attribute.attname`.

`truncate_identifier()` (`scansup.c`) enforces this limit in-place. If the identifier's byte length meets or exceeds `NAMEDATALEN`, it clips the string using `pg_mbcliplen()`. `pg_mbcliplen()` is multi-byte aware: rather than blindly cutting at byte 63, it finds the largest number of complete multi-byte characters that fit within the limit. Without this, a naive truncation could split a multi-byte character. That would leave the string in an invalid encoding state. After clipping, if the `warn` flag is true, `truncate_identifier()` emits a `NOTICE` with both the original and truncated forms (error code `ERRCODE_NAME_TOO_LONG`).

The comment in `downcase_identifier()` notes a design constraint for future work: the current API guarantees that downcasing never increases string length. Languages with length-changing case mappings (such as the German sharp-S `ß` → `SS`) would require revisiting this. `SplitIdentifierString()` in `varlena.c` would also need updating.

## Whitespace Matching

`scanner_isspace()` (`scansup.c`) returns true for exactly the five characters the Flex scanner treats as whitespace: space (` `), tab (`\t`), newline (`\n`), carriage return (`\r`), and form-feed (`\f`). Its purpose is to let non-lexer code perform whitespace tests that match the lexer's behaviour precisely, without depending on the locale-sensitive C library `isspace()`.

The `scan.l` source carries an explicit cross-reference comment: "if you change the set of whitespace characters, fix `scanner_isspace()`". The two definitions must stay in sync. Callers outside the lexer include identifier string splitters (`SplitIdentifierString()`, `varlena.c`), type input functions, regex proc parsing (`regproc.c`), and `LIKE` escape validation in `parser.c`. All of these need to split or validate strings in a way consistent with what the SQL lexer would accept.

## Function Reference

| Function | Signature | In-place? | Notes |
|---|---|---|---|
| `downcase_truncate_identifier` | `(const char *ident, int len, bool warn) → char *` | No (palloc) | Main lexer entry point; downcases and truncates |
| `downcase_identifier` | `(const char *ident, int len, bool warn, bool truncate) → char *` | No (palloc) | Lower-level variant; truncation is optional |
| `truncate_identifier` | `(char *ident, int len, bool warn)` | Yes | Clips to `NAMEDATALEN-1` bytes, multi-byte safe |
| `scanner_isspace` | `(char ch) → bool` | — | Matches Flex whitespace definition exactly |

## Design Invariants

Several invariants hold across all paths that produce or consume PostgreSQL identifiers:

1. An unquoted identifier that enters the system through the lexer will always be lowercase and at most `NAMEDATALEN - 1` bytes long. This is true by the time it reaches the parser's grammar rules.
2. Quoted identifiers bypass `downcase_truncate_identifier()` in the lexer. They still pass through `truncate_identifier()`, so the length limit still applies.
3. Identifiers constructed programmatically (for example, by catalog bootstrapping or internal DDL code) should call `downcase_truncate_identifier()` to stay consistent with what the lexer would produce from the same input.
4. The downcasing is ASCII-only for the 7-bit range. As a result, `downcase_identifier()` always folds two identifiers that differ only in ASCII case to the same result, regardless of database locale or encoding.

## Related Topics

- [[subsystems/parser/overview|Parser and Semantic Analysis]] — the broader parser pipeline that consumes the normalised identifiers produced here

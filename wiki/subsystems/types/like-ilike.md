---
title: "LIKE and ILIKE Pattern Matching"
aliases:
  - LIKE
  - ILIKE
  - pattern matching
  - wildcard matching
  - text_pattern_ops
tags:
  - theme/query-optimization
source_files:
  - src/backend/utils/adt/like.c
  - src/backend/utils/adt/like_match.c
  - src/backend/utils/adt/like_support.c
symbols:
  - GenericMatchText
  - Generic_Text_IC_like
  - SB_MatchText
  - MB_MatchText
  - UTF8_MatchText
  - SB_IMatchText
  - SB_lower_char
  - like_fixed_prefix
  - pattern_fixed_prefix
  - patternsel_common
  - like_selectivity
  - likesel
  - nlikesel
  - iclikesel
  - textlike
  - texticlike
  - textlike_support
  - texticlike_support
---

`LIKE` and `ILIKE` are PostgreSQL's SQL-standard wildcard operators. They offer a restricted but fast alternative to regular expressions for the common cases of prefix matching (`foo%`), suffix matching (`%bar`), and substring containment (`%baz%`). Because patterns that start with a literal prefix can be rewritten as a range condition, the planner can drive a B-tree index scan — something neither regular expressions nor `SIMILAR TO` can do directly.

## Pattern syntax

There are two wildcard metacharacters. `%` matches zero or more characters of any kind. `_` matches exactly one character. Everything else in a pattern is a literal that must appear verbatim in the string.

```sql
'foo%'    -- matches 'foo', 'foobar', 'foo123', …
'_oo'     -- matches 'boo', 'zoo', 'foo', …
'%a%b%'   -- matches any string containing at least one 'a' before a 'b'
```

To match a literal `%` or `_`, prefix it with the escape character. The default escape character is `\`; the ESCAPE clause lets you choose another single character or suppress escaping entirely:

```sql
'100\%'                     -- matches the literal string '100%'
'100%' ESCAPE ''            -- no escaping; % is always a wildcard
'100!%' ESCAPE '!'          -- '!' escapes the following character
```

Internally, `like_escape()` (`like.c`) normalises any user-supplied escape character to `\` before the pattern reaches the matcher, so the matching loop only ever needs to recognise `\` as the escape sentinel.

## LIKE vs ILIKE

`LIKE` is case-sensitive: the pattern character and the string character must be identical bytes. `ILIKE` is the PostgreSQL extension for case-insensitive matching. The SQL standard does not define it.

The case-insensitive path is more expensive. For **single-byte encodings**, `SB_IMatchText` folds characters on the fly using `SB_lower_char()` (`like.c`), which calls `pg_ascii_tolower`, `tolower_l`, or `pg_tolower` depending on the active locale. This avoids allocating a downcased copy of the string but pays one function call per character comparison.

For **multibyte encodings and ICU**, folding individual characters cheaply is not possible. `tolower()` has a single-byte API. ICU does not expose a single-character fold. `Generic_Text_IC_like()` therefore calls the full `lower()` function on both the string and the pattern before matching (`like.c`). That converts the entire input to a new palloc'd buffer in the current collation before the matching loop even starts. The cost is proportional to the string length. The allocation happens on every call. This is the primary performance penalty of `ILIKE` on multibyte databases.

`ILIKE` also requires a deterministic collation. PostgreSQL explicitly rejects nondeterministic collations (e.g. accent-insensitive ICU collations) with `ERRCODE_FEATURE_NOT_SUPPORTED`, because the optimised prefix-bound logic relies on byte-level ordering.

## The matching algorithm

The matching engine lives in `like_match.c`, a C source file that is not compiled directly. Instead it is `#include`d into `like.c` **four times**, each time with a different set of macros defining:

- `MatchText` — the name the compiled function gets
- `NextChar` — how to advance one character (byte step for single-byte, UTF-8-aware step for UTF-8, full `pg_mblen` call for other multibyte)
- `CHAREQ` — byte equality or wide-character equality
- `MATCH_LOWER` — defined only for the case-insensitive single-byte instantiation

The four variants produced are `SB_MatchText`, `MB_MatchText`, `UTF8_MatchText`, and `SB_IMatchText`. The dispatcher `GenericMatchText()` picks the right one at runtime based on `pg_database_encoding_max_length()` and the database encoding. Because each variant has its character-advance logic inlined, the hot loop carries no branching on encoding type.

The algorithm itself is a classic backtracking scan:

- **Literal characters** are compared directly (via `GETCHAR`, which applies `MATCH_LOWER` when case-folding is active). A mismatch returns `LIKE_FALSE` immediately.
- **`_`** advances one character in the text and one byte in the pattern.
- **`%`** collapses adjacent wildcards first (consecutive `%` and `_` characters are simplified to a single `%` followed by the required number of `_`s), then scans forward in the text looking for a position where the first non-wildcard pattern character matches. At each candidate position it recurses to check the remainder of the pattern. If the recursive call returns `LIKE_ABORT` (meaning the text is too short to ever match), the outer loop also aborts rather than continuing to scan.

The three-value return — `LIKE_TRUE`, `LIKE_FALSE`, `LIKE_ABORT` — allows the recursion to short-circuit. When a `%` scan recurses and receives `LIKE_ABORT`, it knows that no further position in the text can match. It can exit immediately. This limits the worst-case behaviour on patterns with multiple `%` wildcards.

A fast path exists for the single-character pattern `%`: it returns `LIKE_TRUE` immediately without entering the loop.

## B-tree index acceleration

The most important performance feature of `LIKE` is that patterns anchored on the left — those whose first character is a literal, not `%` or `_` — can be turned into a range query that a B-tree index can evaluate. The planner support function `textlike_support()` (`like_support.c`) intercepts `SupportRequestIndexCondition` requests and calls `match_pattern_prefix()`. That function tries to extract the fixed prefix of the pattern.

`like_fixed_prefix()` walks the pattern character by character until it hits `%`, `_`, a backslash escape, or (for `ILIKE`) a letter that could fold differently in the active locale. The prefix up to that point becomes the lower bound. `match_pattern_prefix()` then synthesises a "greater string" by incrementing the last character of the prefix, giving an upper bound:

```
name LIKE 'foo%'
→  name >= 'foo' AND name < 'fop'
```

For an exact pattern (no wildcards), the rewrite is a single equality:

```
name LIKE 'foobar'
→  name = 'foobar'
```

This rewrite is purely a planner optimisation — the executor still evaluates the original `LIKE` condition as a filter (a *qpqual*) to eliminate any false positives the range scan might admit. The planner support infrastructure handles both the restriction selectivity estimate (`SupportRequestSelectivity`) and the index condition generation (`SupportRequestIndexCondition`) from the same entry point (`like_regex_support()`, `like_support.c`).

## Operator class requirement for prefix scans

The range-bound rewrite only works correctly when the index uses byte-level (C-locale) ordering, because `make_greater_string()` increments bytes. The resulting string must be strictly greater than all extensions of the prefix under the index's collation.

Standard `text` and `varchar` B-tree indexes use the database's default collation. That collation may reorder strings in locale-specific ways that break the bound arithmetic. PostgreSQL therefore ships three dedicated operator classes:

| Operator class | Type | Ordering |
|---|---|---|
| `text_pattern_ops` | `text` | byte-level (C) |
| `varchar_pattern_ops` | `varchar` | byte-level (C) |
| `bpchar_pattern_ops` | `char(n)` | byte-level (C) |

An index created with one of these operator classes can drive a prefix scan for `LIKE` regardless of the database collation. Without them, the planner will only use a normal B-tree index for prefix scans when `lc_collate` is already `C`.

```sql
CREATE INDEX ON products (name text_pattern_ops);
-- Now the planner can use this index for:  name LIKE 'foo%'
```

The collation check in `match_pattern_prefix()` is on the *index* collation, not the expression collation. So an index built with `text_pattern_ops` is eligible even if the column itself is defined with a non-C collation.

sp_GiST indexes on `text` have native prefix-operator support (`TextPrefixOperator`) and do not need the range-bound workaround; `like_support.c` detects the `TEXT_SPGIST_FAM_OID` opfamily and generates a direct prefix condition instead.

## Substring patterns and pg_trgm

Patterns of the form `%foo%` have no fixed prefix and cannot use a B-tree index at all. The [[subsystems/pg-trgm|pg_trgm]] extension provides GIN and GiST trigram indexes that support arbitrary substring `LIKE` and `ILIKE` patterns by decomposing strings into overlapping three-character sequences. Any `LIKE` pattern can be decomposed into a set of required trigrams; the index returns a superset of matching rows and the executor applies the full `LIKE` check as a filter. For search-box style contains queries, a GIN trigram index is typically the right choice.

## SIMILAR TO

`SIMILAR TO` is the SQL-standard pattern language. It accepts a pattern syntax that is a hybrid of `LIKE` and regular expressions: `%` and `_` retain their `LIKE` meanings, but the pattern may also use `|`, `*`, `+`, `?`, `{m,n}`, and character classes. Internally, `SIMILAR TO` translates its pattern into a POSIX regular expression and hands it to the [[subsystems/types/regex-matching|regular expression engine]]. The translation happens at parse time in `similar_to_regexp()`.

Because the execution path goes through the full regex engine, `SIMILAR TO` is never accelerated by B-tree prefix optimisation. A `pg_trgm` GIN index can still help if the pattern contains enough fixed trigrams.

## Selectivity estimation

The planner needs a row-count estimate for `LIKE` conditions to choose between a sequential scan and an index scan. `likesel()` and `iclikesel()` in `like_support.c` both delegate to `patternsel_common()`, which takes the following approach:

1. Extract the fixed prefix of the pattern (using the same `like_fixed_prefix()` logic as the index optimisation).
2. If the prefix is non-empty, estimate the fraction of rows in the histogram range `[prefix, greaterstr)` using `ineq_histogram_selectivity()`. This can be quite accurate when the column has a useful MCV or histogram in `pg_statistic`.
3. Multiply the prefix selectivity by a heuristic estimate for the remainder of the pattern (`like_selectivity()`), which assigns `FIXED_CHAR_SEL = 0.20` per literal character and `FULL_WILDCARD_SEL = 5.0` per `%`.
4. Most-common-values entries are evaluated exactly by running the actual operator, then merged with the histogram estimate.
5. If no statistics are available, or for join selectivity, the estimate falls back to `DEFAULT_MATCH_SEL` (a small constant).

The critical implication is that a leading `%` eliminates the prefix entirely, leaving the planner with only the low-precision heuristic. Patterns anchored on the left get meaningfully better estimates than floating substring patterns.

## Practical guidance

- **Anchor patterns on the left** whenever the query allows it. `name LIKE 'Smith%'` can use a B-tree index; `name LIKE '%Smith%'` cannot.
- **Create a `text_pattern_ops` index** on columns used with `LIKE` when the database is not in C locale. Without it, the index will be ignored for pattern queries even if the collation would happen to produce the right ordering.
- **Use `pg_trgm` for search-box queries**. If the application needs `LIKE '%keyword%'` style full-text-search, install `pg_trgm` and create a GIN index. It will handle `ILIKE` as well since `pg_trgm` is case-insensitive by default.
- **Prefer `LIKE` over `ILIKE`** in hot paths. On multibyte databases `ILIKE` allocates and downcases both string and pattern on every call. If case-insensitive lookup is the primary use case, consider storing data in a normalised (already-lowercased) form and matching with `LIKE`.
- **Avoid `SIMILAR TO`** unless the SQL-standard syntax is a hard requirement. Its power over `LIKE` is rarely needed, and it is never index-accelerated via B-tree.

## Related Topics

- [[subsystems/types/regex-matching|Regular Expression Matching]] — the Spencer regex engine that backs `~`, `~*`, and `SIMILAR TO`
- [[subsystems/pg-trgm|pg_trgm: Trigram Similarity Search]] — GIN/GiST trigram indexes for substring LIKE and fuzzy matching
- [[subsystems/types/locale|Locale and Collation]] — how `lc_ctype` and ICU collations interact with case folding

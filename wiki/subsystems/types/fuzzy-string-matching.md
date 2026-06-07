---
title: "Fuzzy String Matching: Levenshtein and Phonetic Functions"
aliases:
  - levenshtein
  - edit distance
  - fuzzy string matching
  - fuzzystrmatch
  - soundex
  - metaphone
source_files:
  - src/backend/utils/adt/levenshtein.c
  - src/backend/utils/adt/varlena.c
  - contrib/fuzzystrmatch/fuzzystrmatch.c
symbols:
  - varstr_levenshtein
  - varstr_levenshtein_less_equal
  - levenshtein
  - levenshtein_less_equal
  - soundex
  - metaphone
  - MAX_LEVENSHTEIN_STRLEN
---

PostgreSQL's fuzzy string matching capabilities live in two places. Core PostgreSQL builds in Levenshtein edit-distance functions, available without any extension via `levenshtein()` and `levenshtein_less_equal()`. The `fuzzystrmatch` contrib extension exclusively provides phonetic algorithms — soundex, metaphone, Double Metaphone, and Daitch-Mokotoff Soundex. Together they cover the two dominant approaches to approximate matching: counting the minimum character-level edits between strings, and mapping strings onto a phonetic encoding where differently-spelled words that sound alike become identical.

## Edit distance: what Levenshtein measures

The Levenshtein distance between two strings is the minimum number of single-character operations — insertions, deletions, and substitutions — needed to transform one string into the other. "kitten" → "sitting" requires three operations (substitute k→s, substitute e→i, insert g). So `levenshtein('kitten', 'sitting')` returns 3.

The algorithm fills a notional (m+1)×(n+1) matrix where cell (i, j) holds the minimum cost to transform the first i characters of the source into the first j characters of the target. In `levenshtein.c`, the algorithm never fully materialises this matrix: it keeps only two rows in memory at once (`prev` and `curr`), making memory consumption O(m) rather than O(m·n). Time complexity remains O(m·n).

The three-argument overload `levenshtein(source, target, ins_cost, del_cost, sub_cost)` assigns independent integer costs to each operation type. The default `(1, 1, 1)` weighting treats all edits as equally costly. Real-world applications sometimes differ: an OCR pipeline might weight substitutions lower than insertions, because adjacent keys on a QWERTY keyboard are frequently swapped, not inserted. The same `levenshtein.c` file implements the weighted variant. The cost arguments simply replace the literal `1` constants in the DP recurrence.

Both forms operate on character positions, not byte positions. The algorithm handles multi-byte UTF-8 strings correctly. `pg_mbstrlen_with_len()` converts byte lengths to character counts before the DP loop. A pre-computed `s_char_len` array caches per-character byte widths for the source string, so the inner loop avoids repeated multibyte scanning. When both strings are entirely ASCII (byte length equals character length), a fast path skips the `s_char_len` array entirely.

## The 255-character limit

`levenshtein.c` defines `MAX_LEVENSHTEIN_STRLEN 255`. Any call with an input exceeding 255 characters raises an error unless the internal `trusted` flag is set. The limit is a deliberate CPU and memory guard. The DP algorithm is O(m·n), so two 255-character strings require roughly 65,000 cell evaluations. That is already enough to make careless table-wide calls expensive. The `trusted` path exists only for internal callers like `updateClosestMatch()` in `varlena.c`, which drives the "did you mean?" hints in error messages. SQL-callable functions always pass `trusted = false`.

## levenshtein_less_equal: early termination for filtering

`levenshtein_less_equal(source, target, max_d)` computes the edit distance. But it returns `max_d + 1` immediately when the true distance would exceed `max_d`, avoiding unnecessary work. It is the performance-critical form for filtering large candidate sets.

The optimisation works by tracking a `start_column` and `stop_column` window within each DP row. Any cell's contribution to the final answer is bounded by the minimum residual distance from that cell to the bottom-right corner. This value grows as you move away from the diagonal. Because of this, the algorithm can skip cells far from the diagonal once it becomes clear they cannot produce a result within `max_d`. After each row, the window shrinks from both ends by re-evaluating whether the boundary columns can still affect the answer. If `start_column` meets or passes `stop_column`, the bound has been exceeded. The function returns `max_d + 1` immediately.

There is also a pre-computation shortcut: if the difference in string lengths alone exceeds `max_d` (because the minimum cost to equalise lengths already overshoots the budget), the function returns `max_d + 1` without allocating any memory at all.

```sql
-- Efficient: scans candidates and discards anything farther than 2 edits
SELECT name FROM users WHERE levenshtein_less_equal(name, 'Jon', 2) <= 2;

-- Accurate count only when actually needed:
SELECT levenshtein('Jon', 'Joan');  -- returns 1
```

The four-argument form `levenshtein_less_equal(source, target, ins_cost, del_cost, sub_cost, max_d)` combines custom weights with early termination.

## Phonetic algorithms: fuzzystrmatch extension only

Soundex, metaphone, Double Metaphone (dmetaphone / dmetaphone_alt), and Daitch-Mokotoff Soundex are all part of the `fuzzystrmatch` contrib extension. None are available in a stock PostgreSQL installation without `CREATE EXTENSION fuzzystrmatch`. The grep confirms: the source files for these functions (`fuzzystrmatch.c`, `dmetaphone.c`, `daitch_mokotoff.c`) live under `contrib/fuzzystrmatch/`, not under `src/backend/utils/adt/`.

Phonetic algorithms work differently from edit distance: they reduce a word to a code based on how it sounds in English. So "Smith" and "Smythe" both produce `S530`. This makes them well-suited for matching names across spelling variations in historical records or legacy data where phonetic consistency matters more than exact spelling. The weakness is the opposite of Levenshtein: two strings can sound identical and encode identically, even when their meaning is entirely different. This produces false positives that edit distance would reject.

```sql
CREATE EXTENSION fuzzystrmatch;

SELECT soundex('Smith'), soundex('Smythe');  -- both: S530
SELECT metaphone('PostgreSQL', 8);           -- PSTRKSKL
SELECT dmetaphone('Smith');                  -- SM0
```

## Levenshtein vs pg_trgm

[[subsystems/pg-trgm|pg_trgm]] and Levenshtein both measure how alike two strings are, but they suit different problems.

Levenshtein gives an exact, interpretable integer: "these two strings differ by exactly 2 edits." That precision is valuable when the threshold has semantic meaning — rejecting passwords too similar to the old one, or flagging records that differ by at most one character in a deduplication pass. It requires no extension and no index. Its weakness is scalability: with a 255-character cap and O(m·n) time, applying it to millions of rows without pre-filtering is slow.

`pg_trgm` trades exactness for scale. It produces a continuous similarity score based on shared trigrams. Crucially, it also supports [[subsystems/indexes/gin|GIN]] and [[subsystems/indexes/gist|GiST]] indexes that can pre-filter rows before any per-pair computation. A `WHERE name % 'Jon'` query with a GIN index touches only the fraction of the table whose trigram sets overlap, making full-table fuzzy lookups practical. `pg_trgm` also has no 255-character ceiling. So it applies naturally to paragraph-length text. The trade-off is that trigram similarity does not directly correspond to edit distance. So "at most 2 edits away" cannot be expressed directly as a `pg_trgm` threshold.

The practical rule: use Levenshtein for short strings where edit count is the meaningful metric; use `pg_trgm` when you need index-accelerated fuzzy search over a large table.

## Use cases

**"Did you mean?" suggestions.** PostgreSQL's own error messages use Levenshtein internally through `updateClosestMatch()` in `varlena.c`. When you mistype a column name, the planner scans known column names. It uses `varstr_levenshtein_less_equal` to find the closest match within a fixed budget, then appends it to the error. The same pattern applies in applications: collect a candidate list, filter with `levenshtein_less_equal`, surface the closest.

**Name deduplication.** Customer or contact records imported from multiple sources often contain spelling variants of the same name. A query joining on `levenshtein(a.name, b.name) <= 2` (combined with length filtering to limit cost) identifies likely duplicates for human review.

**Typo-tolerant input validation.** You can validate form fields for product codes, discount codes, or identifiers loosely: if the entered code has edit distance 1 from a known valid code, prompt the user to confirm rather than refusing outright.

**Password similarity checks.** The PostgreSQL `passwordcheck` extension and various application-level policies use Levenshtein to reject new passwords that are too close to the current one. Because password hashing makes direct comparison impossible, the check must happen before hashing. The 255-character limit is not a constraint in practice for passwords.

**Historical name matching.** Soundex and Metaphone (from `fuzzystrmatch`) are the right tool for matching names across datasets where phonetic variation dominates spelling variation — genealogy records, census data, or transliterated names. Systematic transliteration differences can fool edit distance. Phonetic encoding handles them naturally.

## Related Topics

- [[subsystems/pg-trgm|pg_trgm]] — trigram similarity with index acceleration
- [[subsystems/indexes/gin|GIN]] — inverted index used by pg_trgm for text search
- [[subsystems/indexes/gist|GiST]] — lossy bitmap index used by pg_trgm for kNN
- [[subsystems/types/regex-matching|regex matching]] — pattern-based string matching
- [[subsystems/types/like-ilike|LIKE/ILIKE]] — prefix and wildcard string matching

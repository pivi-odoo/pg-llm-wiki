---
title: "Regular Expression Matching"
aliases:
  - regex
  - regexp
  - Spencer regex
source_files:
  - src/backend/utils/adt/regexp.c
  - src/backend/regex/regcomp.c
  - src/backend/regex/regc_nfa.c
  - src/backend/regex/regc_color.c
  - src/backend/regex/regc_pg_locale.c
  - src/backend/regex/regexec.c
  - src/backend/regex/rege_dfa.c
  - src/include/regex/regguts.h
symbols:
  - RE_compile_and_cache
  - RE_compile_and_execute
  - setup_regexp_matches
  - pg_regcomp
  - pg_regexec
  - cached_re_str
  - regexp_matches_ctx
  - struct nfa
  - struct cnfa
  - struct subre
  - struct dfa
  - struct colormap
  - pg_set_regex_collation
  - pg_wc_isdigit
  - pg_wc_isalpha
  - pg_wc_isupper
  - pg_wc_islower
  - pg_wc_isspace
  - pg_wc_isgraph
  - pg_wc_isprint
  - pg_wc_ispunct
  - pg_wc_iscntrl
  - pg_wc_tolower
  - pg_wc_toupper
  - pg_ctype_get_cache
---

PostgreSQL's regular expression engine is a self-contained implementation derived from Henry Spencer's "advanced" regex library, embedded directly into the backend rather than relying on any system regex library. It underpins the `~`, `~*`, `!~`, and `!~*` operators, all `regexp_*` functions, `SIMILAR TO`, and the `SUBSTRING(... SIMILAR ... ESCAPE ...)` SQL form. The engine compiles patterns into nondeterministic finite automata (NFA). It optimises them into compacted DFA-like structures. It executes matches entirely in wide-character (`pg_wchar`) space, to handle multibyte encodings correctly.

## Pattern Flavours and Compile Flags

Three regex flavours coexist under the same engine. The `REG_ADVANCED` flag (the default for all SQL-exposed operators) enables Perl-like extensions: lookahead/lookbehind, non-capturing groups `(?:...)`, and character-class escapes like `\d`. `REG_EXTENDED` gives plain POSIX EREs without those extensions. `REG_QUOTE` treats the entire pattern as a literal string. The `~` and `~*` operators hard-wire `REG_ADVANCED`. User-facing functions like `regexp_match()` and `regexp_like()` accept an optional flags string, parsed by `parse_re_flags()` (regexp.c), which maps single-letter codes (`i`, `n`, `x`, `g`, ...) to the corresponding `cflags` bitmask.

PostgreSQL does not handle `SIMILAR TO` natively. `similar_escape_internal()` (regexp.c) rewrites the SQL pattern into a POSIX regexp: it translates `%` to `.*`, translates `_` to `.`, and converts parentheses to non-capturing form. It then wraps the whole thing in `^(?:...)$`. The transformation supports the SQL `ESCAPE` character and the two-escape-double-quote syntax required by `SUBSTRING`.

## Locale-Aware Character Classification

The Henry Spencer regex engine knows nothing about PostgreSQL's collation system. The bridge is `regc_pg_locale.c`. It is `#include`d directly into `regcomp.c`, and it provides a suite of locale-aware character-classification and case-conversion callbacks. Before any compilation begins, `pg_regcomp()` calls `pg_set_regex_collation(collation)` to record the active collation in module-level static variables (`pg_regex_strategy`, `pg_regex_locale`, `pg_regex_collation`). Every subsequent callback — `pg_wc_isalpha()`, `pg_wc_isdigit()`, `pg_wc_isupper()`, `pg_wc_islower()`, `pg_wc_isspace()`, `pg_wc_isgraph()`, `pg_wc_isprint()`, `pg_wc_ispunct()`, `pg_wc_iscntrl()` — dispatches through a `PG_Locale_Strategy` enum that was chosen at collation setup time. This keeps the per-character hot path to a single switch, with no repeated collation lookup.

This wiring is what makes bracket expressions locale-sensitive. A pattern like `[[:alpha:]]` does not consult a hard-wired Unicode table. Instead, `cclasscvec()` (regc_locale.c) calls `pg_ctype_get_cache(pg_wc_isalpha, ...)`, which probes the active collation's classification for every code point up to `MAX_SIMPLE_CHR`. The high colormap mechanism handles characters above that threshold at match time. The result is that `[[:alpha:]]` in a `tr_TR` collation recognises Turkish letters that ASCII-only code would miss, while `[[:alpha:]]` in the C collation is restricted to the 52 ASCII letters regardless of database encoding.

### Locale Strategy Selection

`pg_set_regex_collation()` selects one of six locale-dispatch strategies at the start of each compile or execute, ranging from a hard-wired ASCII bitmask under C locale through libc wide/narrow classification for default collations to ICU classification functions for an ICU collation provider. [[subsystems/types/regex-engine|Henry Spencer ARE Regex Engine]] has the full strategy table and the character-class caching machinery that backs it.

The C-locale path uses a 128-entry bitmask array (`pg_char_properties[]`) that is independent of `LC_CTYPE`. This ensures C-locale behavior is stable regardless of the process environment. The WIDE and WIDE_L paths apply `pg_ascii_toupper`/`pg_ascii_tolower` to ASCII characters even in the default collation. This deliberately overrides any libc behavior that might treat `i`/`I` differently (as Turkish locales do for `upper()`/`lower()`). PostgreSQL rejects nondeterministic collations outright — those where string equality is not character-by-character — with `ERRCODE_FEATURE_NOT_SUPPORTED`, because the regex engine's DFA construction assumes stable character equivalences.

### Case Folding and REG_ICASE

When `REG_ICASE` is set, the engine must know which characters are case-equivalent. The two callbacks `pg_wc_toupper()` and `pg_wc_tolower()` perform this folding under the active locale strategy. `allcases()` (regc_locale.c) calls both and accumulates lowercase and uppercase variants into a `cvec`. The engine then uses this `cvec` to expand each literal character in the pattern into its full case-equivalent set before coloring. For character ranges under `REG_ICASE`, `range()` iterates the specified range and appends case-equivalents that fall outside the range as individual characters, since contiguous range representation cannot capture arbitrary case-equivalence gaps.

For back-references under `REG_ICASE`, the compiled pattern stores a `casecmp()` function pointer in `guts.compare`. `casecmp()` uses `pg_wc_tolower()` on each character pair, so a back-reference comparison respects the collation rather than performing a locale-agnostic fold.

### Character-Class Caching

Probing the classification function for every code point on every compile would be prohibitively expensive. So `pg_ctype_get_cache()` (regc_pg_locale.c) expands a POSIX bracket class like `[[:alpha:]]` into a `cvec` once per `(probefunc, collation OID)` combination, and caches it for the life of the process. See the character-class caching discussion on the regex engine page for how the cache is built, keyed, and reused across compilations.

### Equivalence Classes

`eclass()` (regc_locale.c) dispatches POSIX equivalence classes (`[=a=]`). It treats each character as its own equivalence class, delegating to `allcases()` when `REG_ICASE` is active. So `[=a=]` matches only `a` (and `A` in case-insensitive mode), not locale-defined canonical equivalents. The regex engine page covers `eclass()`/`allcases()` in more depth.

## Compiled Pattern Representation

Compilation produces a `regex_t` whose opaque `re_guts` pointer leads to a `struct guts` (regguts.h). The guts contain:

- A `struct colormap` — the character equivalence-class map.
- A `struct subre` tree — the subexpression tree tracking capturing groups, alternations, concatenations, and iterations.
- A `struct cnfa search` — a fast, stripped-down compacted NFA used for preliminary search without subexpression tracking.
- An array of `struct subre` nodes for lookaround constraints (`lacons`).

Each `subre` node carries its own `struct cnfa`, which is the compacted NFA for just that subexpression. The op codes distinguish plain DFA-able sub-REs (`'='`), captures (`'('`), concatenation (`'.'`), alternation (`'|'`), iteration (`'*'`), and back-references (`'b'`).

### Color Maps

The engine maps Unicode code points to integer *colors* — equivalence classes of characters that are indistinguishable within the pattern — before building any NFA (regc_color.c, included by regcomp.c). For code points up to `MAX_SIMPLE_CHR` this is a direct array lookup. For higher code points, the engine uses a two-dimensional `hicolormap` array, indexed by range membership and locale character-class membership. This color abstraction reduces the number of NFA arcs dramatically: an arc labelled with a color matches any character in that class, and `RAINBOW` arcs match all colors at once. A `colordesc` records how many characters share each color and chains all arcs that carry it.

## Compilation Pipeline

`pg_regcomp()` (regcomp.c) drives compilation from lexing through NFA construction, optimisation, and compaction into the flat `cnfa` representation the executor runs against, plus a second stripped-down `cnfa` (`guts.search`) used to locate candidate match windows quickly. The regex engine page covers the four-phase pipeline (parse, optimise, compact, build search NFA) in detail.

## Compiled-Pattern Cache

Compiling a regex is expensive relative to a single match. `RE_compile_and_cache()` (regexp.c) maintains a process-global, self-organising list of up to `MAX_CACHED_RES` (default 32) previously compiled patterns. `RE_compile_and_cache()` keeps the list in most-recently-used order. On a cache hit, the entry moves to position 0. On a miss, `RE_compile_and_cache()` inserts the new entry at position 0 and evicts the oldest entry. It keys cache entries by (raw pattern bytes, `cflags`, collation OID). Each entry lives in its own [[subsystems/memory/contexts|memory context]] whose parent is the long-lived `RegexpCacheMemoryContext` (a child of `TopMemoryContext`). This ensures the entries survive across transactions. The pattern text identifies the context. This makes it visible in `pg_backend_memory_contexts`.

## Execution Strategy

`pg_regexec()` (regexec.c) builds a lazy DFA at runtime. `pg_regexec()` does not enumerate the compacted NFA's states upfront. Instead, a `struct dfa` maintains a cache of *state sets* (`struct sset`). Each represents a subset of NFA states reachable at a given input position. The `miss()` function computes new state sets on demand and inserts them into the cache. For small NFAs, `pg_regexec()` allocates the DFA cache inline in a `struct smalldfa` on the stack, to avoid malloc.

Execution has two layers:

- **Preliminary search** — `find()` first runs the stripped `guts.search` NFA via `shortest()` to identify a range of candidate start positions. This avoids the costlier full match on non-matching regions.
- **Full match with dissection** — once `find()` identifies a candidate range, `pg_regexec()` runs the full NFA. If the pattern contains back-references, `pg_regexec()` uses `cfind()` instead. `cfind()` calls the subexpression dissection routines (`cdissect`, `ccondissect`, `citerdissect`, etc.) to locate the unique leftmost-longest (or shortest, for lazy quantifiers) decomposition that satisfies all back-reference constraints.

For patterns with no back-references and no subexpression capture (`REG_NOSUB`), execution reduces to a two-DFA pass: search NFA to find the window, then main NFA to pin the endpoints. The `MATCHALL` shortcut in the compacted NFA allows patterns like `.*` to bypass DFA simulation completely.

## Wide-Character Encoding

PostgreSQL converts all pattern and input strings from the database encoding to `pg_wchar` (UTF-32 internally) before any matching. `RE_execute()` (regexp.c) performs `pg_mb2wchar_with_len()` on the data string. `RE_compile_and_cache()` does the same for the pattern before calling `pg_regcomp()`. Match positions returned in `regmatch_t` are in character (not byte) offsets. Functions that return substrings — `regexp_match()`, `regexp_matches()`, `regexp_split_to_table()` — convert back to bytes through the original text pointer stored in `regexp_matches_ctx.orig_str`. The engine pre-sizes a conversion buffer (`conv_buf`) to the widest single match, to avoid repeated allocation during the SRF loop.

## Global Matching and the regexp_matches_ctx

The `regexp_matches()` set-returning function and related functions (`regexp_count()`, `regexp_instr()`, `regexp_split_to_table()`) collect *all* matches in a single upfront scan via `setup_regexp_matches()` (regexp.c). This allocates a `regexp_matches_ctx` holding all match start/end character offsets packed into a flat `match_locs[]` array. The layout is `nmatches * npatterns * 2` integers: for each match, for each capturing subpattern, the inclusive start index followed by the exclusive end index. Setting `use_subpatterns = false` collapses npatterns to 1. `setup_regexp_matches()` suppresses zero-length matches adjacent to a prior match end when `ignore_degenerate` is set (used by regexp_split). The match array grows with a doubling strategy (`2^n-1` sentinel sizes).

## Key Data Structures

| Structure | Location | Purpose |
|---|---|---|
| `cached_re_str` | regexp.c | One slot in the compiled-pattern cache; holds pattern bytes, flags, collation, and `regex_t` |
| `regexp_matches_ctx` | regexp.c | Cross-call state for SRF functions; stores all match offsets |
| `struct guts` | regguts.h | The internals of a compiled `regex_t`; owns colormap, subre tree, lacons, search NFA |
| `struct subre` | regguts.h | Subexpression tree node; carries its own `cnfa` and greedy/lazy preference flags |
| `struct cnfa` | regguts.h | Compacted NFA: flat arc arrays per state, MATCHALL shortcut, LACON support |
| `struct nfa` | regguts.h | Working NFA during compilation; states and arcs allocated in slabs |
| `struct colormap` | regguts.h | Maps `pg_wchar` → color integer; direct array for BMP, 2-D table for high code points |
| `struct dfa` | regexec.c | Lazy DFA execution context; caches state sets to avoid recomputation |
| `struct sset` | regexec.c | One entry in the DFA state-set cache; bitvector over NFA states |
| `pg_ctype_cache` | regc_pg_locale.c | Per-(probefunc, collation) cache of all matching code points as a `cvec` |

## Compile Flags Reference

| Flag | Effect |
|---|---|
| `REG_ADVANCED` | Full AREs (Henry Spencer advanced REs): lookaround, `\d`, `(?:...)`, etc. |
| `REG_EXTENDED` | POSIX ERE; no advanced features |
| `REG_ICASE` | Case-insensitive; uses collation-aware `pg_wc_tolower` |
| `REG_NOSUB` | Skip subexpression capture; allows a faster single-DFA execution path |
| `REG_NEWLINE` (`REG_NLSTOP | REG_NLANCH`) | `\n` affects `.`, `[^`, `^`, `$` |
| `REG_EXPANDED` | Extended syntax: whitespace and `#` comments ignored in pattern |
| `REG_QUOTE` | Treat entire pattern as literal; incompatible with other mode flags |

## Character Color Maps

The automaton never treats a character in a pattern as a raw code point. Instead, at the start of compilation, the engine partitions all code points into *colors* — integer equivalence classes such that any two characters with the same color are interchangeable for the purposes of the pattern being compiled. The engine labels NFA and DFA arcs with colors, not code points, so the number of arc labels stays proportional to the number of distinct character classes mentioned in the pattern, rather than to the full Unicode code space. A `RAINBOW` arc is a special label meaning "any non-pseudo color", used by operators like `.` that match all characters. Emitting a single `RAINBOW` arc avoids generating one arc per color.

The `struct colormap` (regguts.h) maintains this mapping in two tiers. For code points up to `MAX_SIMPLE_CHR` (the BMP range that covers virtually all common text), a flat `locolormap` array provides O(1) lookup: `GETCOLOR(cm, c)` expands to a direct array index for these characters. For supplementary-plane code points above `MAX_SIMPLE_CHR`, the `struct colormap` uses a two-dimensional `hicolormap` array instead. Its rows correspond to code-point ranges that have been explicitly mentioned in the pattern (tracked in a sorted `cmranges` array, with row zero representing all unmentioned code points). Its columns correspond to distinct locale character-class memberships, such as `isalpha` or `isdigit`. The `classbits[]` array maps each character-class code to a column-selection bitmask. `pg_reg_getcolor()` finds the row with a binary search over `cmranges`, finds the column with a bitwise OR of applicable `classbits` entries, then indexes `hicolormap[row * hiarraycols + col]`. Both `hiarrayrows` and `hiarraycols` grow by doubling as new ranges and character classes are encountered.

Color creation and splitting happen incrementally during pattern parsing (regc_color.c, which is `#include`d into regcomp.c). Initially all characters share a single color, `WHITE` (value 0). Each time a new literal character or character-class bracket expression is parsed, the engine calls `subcolor()` (for low-range characters) or `subcolorhi()` (for the high colormap). These functions check whether the target character already has its own singleton color. If not, they invoke `newsub()` to allocate a *subcolor*. A color with a pending subcolor records it in `colordesc.sub`. The optimization in `newsub()` avoids splitting a color that is only referenced by a single character (the `nschrs + nuchrs == 1` check). The engine completes the split lazily — it calls `okcolors()` at the end of bracket-expression processing to promote subcolors to full colors. When the parent color still has remaining members, `okcolors()` duplicates its existing NFA arcs onto the subcolor, giving both colors the same transitions. When the parent is left empty, though, `okcolors()` simply relabels the arcs. This arc-update strategy keeps the NFA consistent without a full re-traversal.

The `colordesc` for each color chains all NFA arcs carrying that color through a doubly-linked `colorchain`/`colorchainRev` list. This makes arc relabeling during splits O(arcs for that color) rather than O(all arcs). A `colordesc` can also carry the `PSEUDO` flag for synthetic colors such as the beginning-of-string and end-of-string sentinels, which have no real character membership. `newcolor()` links free color slots through their `sub` field and recycles them.

## DFA Execution

Compilation never pre-converts the compacted NFA into a DFA. Instead, rege_dfa.c builds DFA states on demand as it scans the input. Each DFA state is conceptually a *frozenset* of NFA states reachable simultaneously at some input position — the standard subset-construction insight, applied lazily. A `struct sset` (regexec.c) represents one such state set: it holds a bitvector over NFA states (`states[]`, sized in unsigned-word units by `wordsper`), a hash of that bitvector, and per-color out-arc pointers to successor `sset` entries. The out-arc array `outs[co]` is NULL until that transition has been computed. The hot path in `longest()` and `shortest()` is simply `css->outs[co]`, a single pointer dereference.

The DFA state cache is a flat array of `nssets` entries inside `struct dfa`, sized to `cnfa->nstates * 2` for normal patterns or a smaller constant when the `REG_SMALL` flag is set. For small NFAs (at most `FEWSTATES` states and `FEWCOLORS` colors), the entire `struct dfa` fits inside a `struct smalldfa` allocated on the stack. This eliminates malloc overhead for simple patterns. The cache has no explicit eviction policy based on frequency — instead `pickss()` evicts whichever entry was least recently *seen* in the scan, using each entry's `lastseen` pointer (the input position at which it was last the current state). Entries whose `lastseen` is older than `cp - nssets * 2/3` are considered expendable. `LOCKED` entries (the initial start state, marked `STARTER`) are never evicted. When `pickss()` reclaims a cache entry, it surgically removes the entry's forward and backward arc links, to keep the remaining entries consistent.

Cache miss handling in `miss()` computes the successor NFA-state set for a given current set and input color. It iterates over all NFA states in the current set's bitvector and follows every `PLAIN` arc with a matching color (or any `RAINBOW` arc, unless the color is `PSEUDO`). `miss()` hashes the resulting bitvector and scans it against existing cache entries via `HIT()`. If found, `miss()` returns the cached entry immediately. If not, `getvacant()` evicts a slot, and `miss()` installs the new state set. `miss()` links the new slot back into the cache's arc-chain structures, so future lookups for the same (css, co) pair will be a hit. When the NFA has LACON arcs (`HASLACONS` flag), a second inner loop extends the state set by following any lookaround arcs whose constraints are satisfied at the current input position. Because lookaround satisfaction depends on context, though, the engine deliberately does not cache transitions involving LACONs in the `outs[]` array. Every such transition forces a re-evaluation via `miss()`.

`longest()` implements leftmost-longest matching. The DFA scans the input forward, updating `lastseen` on every state set it visits. After the scan ends, the engine checks whether it visited any state set with the `POSTSTATE` flag (indicating it contains the NFA post state, i.e. a complete match). If so, it returns the latest `lastseen` among all such sets. This satisfies the POSIX requirement that the overall match extend as far right as possible. The parallel `shortest()` function breaks out of the scan as soon as the scan encounters a POSTSTATE past the minimum endpoint. The engine uses `shortest()` for the preliminary search pass and for lazy quantifiers. The `matchuntil()` variant supports incremental lookbehind evaluation: it preserves the DFA's current `sset` pointer and input position across calls, so that repeatedly checking a lookbehind constraint against a growing prefix costs O(N) total rather than O(N²).

The character-consuming scan loop does not handle zero-width assertions — `^`, `$`, and word boundaries — directly. During compilation, the engine assigns them pseudo-colors stored in `cnfa.bos[]` and `cnfa.eos[]`. The `HASLACONS` mechanism covers word-boundary and lookaround constraints. In `longest()` and `shortest()`, the engine injects the BOS pseudo-color before scanning the first real character (or after the startup transition if the match does not begin at the string start). It injects the EOS pseudo-color after the final real character when the match endpoint coincides with the string end. These synthetic transitions let the NFA's constraint arcs fire at the right moments without requiring the main scan loop to treat those positions specially.

## See also

- [[subsystems/types/regex-engine|Henry Spencer ARE Regex Engine]] — compilation internals: the lexer/parser/optimise/compact pipeline, locale-dispatch strategies, character-class caching, NFA export, and fixed-prefix extraction
- [[subsystems/memory/contexts|memory context]] — each cached compiled regexp lives in its own child context under `RegexpCacheMemoryContext`

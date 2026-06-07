---
title: "Henry Spencer ARE Regex Engine"
aliases:
  - regex engine
  - ARE engine
  - Spencer regex engine
  - pg_regex_t
  - advanced regular expressions
source_files:
  - src/backend/regex/regc_cvec.c
  - src/backend/regex/regc_lex.c
  - src/backend/regex/regc_locale.c
  - src/backend/regex/regerror.c
  - src/backend/regex/regexport.c
  - src/backend/regex/regfree.c
  - src/backend/regex/regprefix.c
symbols:
  - pg_regprefix
  - pg_regfree
  - pg_regerror
  - pg_reg_getnumstates
  - pg_reg_getinitialstate
  - pg_reg_getfinalstate
  - pg_reg_getnumoutarcs
  - pg_reg_getoutarcs
  - pg_reg_getnumcolors
  - pg_reg_getnumcharacters
  - pg_reg_getcharacters
  - pg_reg_colorisbegin
  - pg_reg_colorisend
  - findprefix
  - struct cvec
  - newcvec
  - clearcvec
  - addchr
  - addrange
  - cclasscvec
  - allcases
  - eclass
  - range
  - REG_PREFIX
  - REG_EXACT
  - REG_NOMATCH
  - REG_BADPAT
  - pg_regex_t
  - regex_arc_t
---

PostgreSQL embeds Henry Spencer's Advanced Regular Expression (ARE) library directly into the backend rather than delegating to the system's `regcomp`/`regexec`. This gives the engine a fixed, well-understood feature set across all platforms: look-ahead and look-behind assertions, backreferences, non-greedy quantifiers, named character-class escapes like `\d` and `\w`, and the full POSIX bracket-expression syntax — richer than most SQL engines allow. The compiled form, `pg_regex_t` (aliased to `regex_t` inside the engine, `src/include/regex/regex.h`), is an opaque handle whose internals the regex subsystem manages exclusively.

## Pattern Flavours

Three compile-time flavours coexist. `REG_ADVANCED` (the default for `~`, `~*`, and all `regexp_*` SQL functions) enables the full ARE superset: Perl-like lookaround, non-capturing groups `(?:...)`, embedded option flags `(?i)`, and `\d`/`\w`/`\s` shorthands. `REG_EXTENDED` gives plain POSIX ERE without those extensions. `REG_QUOTE` treats the entire pattern as a literal string. PostgreSQL handles the SQL `SIMILAR TO` form by rewriting the pattern into a POSIX regexp before compilation, rather than through a dedicated flavour.

The SQL surface — `~` (case-sensitive), `~*` (case-insensitive), `!~` and `!~*` (negated forms), `regexp_match()`, `regexp_matches()`, `regexp_replace()`, `regexp_split_to_table()`, `regexp_like()` — all funnel through a per-session compiled-pattern cache in `src/backend/utils/adt/regexp.c`. The cache avoids recompilation when the same pattern appears in successive rows of a query.

## Compilation Pipeline

Compilation proceeds through four stages driven by `pg_regcomp()` in `regcomp.c`.

The **lexer** (`regc_lex.c`, `#include`d into `regcomp.c`) tokenises the raw pattern string into atoms and operators. It handles ARE-specific syntax — embedded-option clusters `(?flags:...)`, named character-class escapes, Unicode code-point literals `\uhhhh` and `\Uhhhhhhhh`, and back-reference syntax — returning typed tokens that the parser consumes directly. Because the file is `#include`d rather than separately compiled, all internal state is private to the one compilation unit.

The **parser and NFA builder** (`regcomp.c`) constructs an NFA of `struct state` and `struct arc` nodes while consuming the token stream. Alongside the NFA graph it builds a `struct subre` tree that records the subexpression hierarchy (captures, alternations, concatenations, iterations) needed later for subexpression dissection.

**Character sets** throughout NFA construction take the form of `struct cvec` values (`regc_cvec.c`). A `cvec` holds two sorted arrays in a single allocation: isolated code points (`chrs[]`) and inclusive ranges (`ranges[]`). `newcvec()` sizes the allocation at creation time, packing the struct header and both arrays into one `MALLOC` call. `addchr()` appends a single code point. `addrange()` appends a range. `clearcvec()` resets the counts without freeing the allocation. This allows reuse across successive bracket expressions. Because NFA coloring consumes cvecs immediately after construction, most are transient. Only character-class cvecs produced by `pg_ctype_get_cache()` are long-lived.

The **optimise and compact** phases (`regcomp.c`, `regc_nfa.c`) collapse epsilon arcs, push constraint arcs (`^`, `$`, word-boundary) to their true firing positions, and eliminate dead states. They then convert the working NFA into a `struct cnfa` — a flat array-of-`carc`-arrays representation where each state's out-arcs are sorted and terminated by a `COLORLESS` sentinel. A second, stripped `cnfa` stored as `guts.search` serves as a fast preliminary scan NFA that locates candidate match windows without tracking subexpressions.

## Locale-Sensitive Character Classes

`[[:alpha:]]`, `[[:digit:]]`, and similar POSIX bracket expressions expand differently under different collations. The engine deliberately defers all such decisions to the collation layer. Before compilation begins, `pg_set_regex_collation()` (in `regc_pg_locale.c`, `#include`d into `regcomp.c`) stores the active collation OID and selects one of six dispatch strategies.

| Strategy | Condition | Classification API |
|---|---|---|
| `PG_REGEX_LOCALE_C` | C or POSIX collation | Hard-wired `pg_char_properties[128]` bitmask |
| `PG_REGEX_LOCALE_WIDE` | Default collation, UTF-8 database | `<wctype.h>` `iswdigit`, `iswalpha`, … |
| `PG_REGEX_LOCALE_1BYTE` | Default collation, non-UTF-8 database | `<ctype.h>` for code points ≤ 255 |
| `PG_REGEX_LOCALE_WIDE_L` | Named collation, UTF-8 database | `iswdigit_l`, `iswalpha_l`, … with `locale_t` |
| `PG_REGEX_LOCALE_1BYTE_L` | Named collation, non-UTF-8 database | `isdigit_l`, `isalpha_l`, … with `locale_t` |
| `PG_REGEX_LOCALE_ICU` | ICU collation provider | ICU `u_isdigit`, `u_isalpha`, … |

`cclasscvec()` (`regc_locale.c`) builds a cvec for each POSIX class by calling `pg_ctype_get_cache()` with the appropriate probe function and collation OID. `pg_ctype_get_cache()` maintains a process-global linked list of cached cvecs, each keyed by `(probefunc, collation OID)`. On the first call it scans code points from `CHR_MIN` up to `MAX_SIMPLE_CHR`, recording matching runs as ranges and isolated points as individual chars. The cache allocates entries with `malloc` (not `palloc`), and they survive for the life of the process. This makes character-class expansion cheap for repeated compilations under the same collation. A `cclasscode` of `-1` on a cached cvec signals that the scan was exhaustive — no run-time locale check is needed for supplementary code points in that class.

The performance gap between the C locale and a Unicode collation is a direct consequence of this design: under C locale the bitmask path touches only 128 entries, while under a Unicode ICU collation `pg_ctype_get_cache()` must probe every code point up to `MAX_SIMPLE_CHR` on the first compilation. Subsequent compilations under the same collation hit the cache and are equally fast.

The `allcases()` function (`regc_locale.c`) expands a single character into the full set of its case equivalents by calling `pg_wc_toupper()` and `pg_wc_tolower()` and collecting the results into a cvec. PostgreSQL invokes it whenever a literal character appears in a pattern compiled with `REG_ICASE`, and `eclass()` also invokes it when handling POSIX equivalence classes (`[=a=]`). PostgreSQL's equivalence-class implementation treats each character as its own class, delegating only to `allcases()` when case-insensitivity is active.

## Fixed-Prefix Extraction

`pg_regprefix()` (`regprefix.c`) analyses the compiled NFA to extract a mandatory leading string. It operates on the topmost `subre`'s `cnfa` rather than the search NFA, walking successive states of the compacted NFA from the `pre` state forward. At each step it checks that all out-arcs carry exactly one color and that the color has exactly one member character (a singleton color, `nschrs == 1`, `nuchrs == 0`). If those conditions hold it appends the character to the output buffer and advances. The walk stops as soon as a state has multiple out-arcs with different colors, a multi-member color, a `RAINBOW` arc, or a LACON (lookaround constraint) arc.

The return value conveys precision. `REG_PREFIX` means the walk found a mandatory prefix, but more characters may follow. `REG_EXACT` means every string satisfying the regex must equal exactly the extracted string (the walk ended at a state whose only out-arcs lead to `post` via EOS/EOL). `REG_NOMATCH` means the pattern is not left-anchored, or has no useful prefix. The caller — `regexp_fixed_prefix()` in `regexp.c`, invoked by `regex_fixed_prefix()` in `like_support.c` — converts the result to a palloc'd text for use by the planner's selectivity functions. This enables index scans on `~` and `~*` operators.

## NFA Export for Trigram Index Support

`regexport.c` exposes the compiled regex's internal NFA structure to external code without revealing the `regguts.h` data structures. The exported API operates on the search NFA (`guts.search`) and provides:

- `pg_reg_getnumstates()` / `pg_reg_getinitialstate()` / `pg_reg_getfinalstate()` — state count and pre/post state identifiers.
- `pg_reg_getnumoutarcs()` / `pg_reg_getoutarcs()` — out-arcs of a state, returned as `regex_arc_t` records each holding a color number and destination state. The API silently traverses LACON arcs and replaces them with the normal arcs reachable through them.
- `pg_reg_getnumcolors()` / `pg_reg_getnumcharacters()` / `pg_reg_getcharacters()` — color count and the code points belonging to a color. `pg_reg_getnumcharacters()` returns -1 for colors with any high-colormap entries (`nuchrs != 0`), since enumerating supplementary-plane members is not supported.
- `pg_reg_colorisbegin()` / `pg_reg_colorisend()` — classify a color as a BOS/BOL or EOS/EOL pseudo-color.

The `pg_trgm` extension (`contrib/pg_trgm/trgm_regexp.c`) is the primary consumer. It walks the exported NFA state graph to enumerate all trigrams that a pattern matching string must contain. This builds a GiST/GIN index condition that filters candidate rows before the full regex is applied.

## Lifecycle: Compilation, Caching, and Freeing

`pg_regcomp()` allocates a compiled `pg_regex_t`, and `pg_regfree()` (`regfree.c`) releases it. `pg_regfree()` is a thin wrapper that dispatches to the character-size-specific free routine stored in `re_fns`: it is safe to call with a `NULL` pointer. The engine compiles separately for each character size (currently only `pg_wchar`-width characters). `pg_regfree()` deliberately lives in its own translation unit, so it can be linked without pulling in all of `regcomp.c`.

In practice, callers rarely invoke `pg_regfree()` directly. The per-session regex cache in `regexp.c` wraps each compiled regex in its own [[subsystems/memory/contexts|memory context]] parented under `RegexpCacheMemoryContext`. Evicting an entry deletes its context, which releases all associated memory including the `guts` allocation. The cache holds up to `MAX_CACHED_RES` entries (default 32) in most-recently-used order.

## Error Reporting

`pg_regerror()` (`regerror.c`) translates integer error codes to human-readable strings. It consults a static `rerrs[]` table built from `regex/regerrs.h`, which maps each `REG_*` code to both a symbolic name string and an explanation. Two special pseudo-codes extend the interface: `REG_ATOI` looks up a symbolic name and returns its numeric value. `REG_ITOA` does the reverse. The backend's `regexp.c` wraps `pg_regerror()` output in `ereport()` calls so that invalid patterns produce standard PostgreSQL error messages.

The error codes most visible to users are `REG_NOMATCH` (match failed, not an error per se), `REG_BADPAT` (syntactically invalid pattern), `REG_ECTYPE` (unknown character class name), `REG_EBRACK` (unbalanced bracket), and `REG_ESIZE` (compiled regex too large). The engine also defines the non-error sentinels `REG_PREFIX` and `REG_EXACT` used exclusively by `pg_regprefix()`.

## See also

- [[subsystems/types/regex-matching|Regular Expression Matching]] — SQL surface, compilation cache, execution strategy, and DFA simulation
- [[subsystems/types/like-ilike|LIKE and ILIKE]] — simpler pattern matching that shares the prefix-extraction path via `like_support.c`
- [[subsystems/types/locale|Locale and Collation Support]] — collation infrastructure that `pg_set_regex_collation()` plugs into
- [[subsystems/memory/contexts|Memory Contexts]] — lifetime management for cached compiled regexes

---
title: "Snowball Stemmers"
aliases:
  - snowball stemmer
  - snowball dictionary
  - stemming
  - dsnowball
source_files:
  - src/backend/snowball/libstemmer/utilities.c
  - src/backend/snowball/libstemmer/api.c
  - src/backend/snowball/dict_snowball.c
  - src/backend/snowball/libstemmer/stem_UTF_8_english.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_english.c
  - src/include/snowball/libstemmer/api.h
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_basque.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_catalan.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_danish.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_dutch.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_finnish.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_french.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_german.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_indonesian.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_irish.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_italian.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_norwegian.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_porter.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_portuguese.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_spanish.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_1_swedish.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_2_hungarian.c
  - src/backend/snowball/libstemmer/stem_ISO_8859_2_romanian.c
  - src/backend/snowball/libstemmer/stem_KOI8_R_russian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_arabic.c
  - src/backend/snowball/libstemmer/stem_UTF_8_armenian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_basque.c
  - src/backend/snowball/libstemmer/stem_UTF_8_catalan.c
  - src/backend/snowball/libstemmer/stem_UTF_8_danish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_dutch.c
  - src/backend/snowball/libstemmer/stem_UTF_8_finnish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_french.c
  - src/backend/snowball/libstemmer/stem_UTF_8_german.c
  - src/backend/snowball/libstemmer/stem_UTF_8_estonian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_greek.c
  - src/backend/snowball/libstemmer/stem_UTF_8_hindi.c
  - src/backend/snowball/libstemmer/stem_UTF_8_hungarian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_indonesian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_irish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_italian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_lithuanian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_nepali.c
  - src/backend/snowball/libstemmer/stem_UTF_8_norwegian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_porter.c
  - src/backend/snowball/libstemmer/stem_UTF_8_portuguese.c
  - src/backend/snowball/libstemmer/stem_UTF_8_romanian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_russian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_serbian.c
  - src/backend/snowball/libstemmer/stem_UTF_8_spanish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_swedish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_tamil.c
  - src/backend/snowball/libstemmer/stem_UTF_8_turkish.c
  - src/backend/snowball/libstemmer/stem_UTF_8_yiddish.c
  - src/include/snowball/header.h
symbols:
  - SN_env
  - DictSnowball
  - stemmer_module
  - dsnowball_init
  - dsnowball_lexize
  - locate_stem_module
  - SN_create_env
  - SN_close_env
  - SN_set_current
  - find_among
  - find_among_b
  - replace_s
  - slice_from_s
  - slice_del
  - in_grouping
  - in_grouping_U
  - skip_utf8
---

PostgreSQL ships a built-in stemming subsystem based on Snowball — a small language for expressing word-suffix reduction rules — that reduces inflected words to their base form before indexing or querying. When full-text search normalizes "running" to "run" or "libraries" to "librari", a Snowball stemmer is doing the work. PostgreSQL includes pre-compiled stemmers for over 25 languages, including Arabic, Armenian, Basque, Catalan, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hindi, Hungarian, Indonesian, Irish, Italian, Lithuanian, Nepali, Norwegian, Portuguese, Romanian, Russian, Serbian, Spanish, Swedish, Tamil, Turkish, and Yiddish. None of those files are hand-authored C. The Snowball compiler generates them all from Snowball algorithm descriptions, and PostgreSQL commits them as read-only source.

## Snowball as a Language

Snowball is a domain-specific language for writing stemming algorithms. A Snowball algorithm description is a sequence of string-matching and slicing rules. The Snowball compiler translates those descriptions into C. The result is a collection of `stem_<encoding>_<language>.c` files in `src/backend/snowball/libstemmer/`. Each file is structurally identical: it defines static data tables (sorted suffix arrays and character groupings), a set of internal rule functions, and three public symbols — `<lang>_<enc>_create_env`, `<lang>_<enc>_close_env`, and `<lang>_<enc>_stem`. The algorithm logic is entirely inside the `stem` function, which returns 1 on success and updates the environment in place.

Because all stemmers share the same generated structure, adding a new language to PostgreSQL is purely mechanical: run the Snowball compiler on the algorithm file, drop the generated C into `libstemmer/`, add a corresponding `STEMMER_MODULE` entry in `dict_snowball.c`, and provide a header.

## The SN_env Structure

Every stemmer function receives a single argument: a pointer to `struct SN_env` (`src/include/snowball/libstemmer/api.h`). This structure is the complete mutable state for one stemming operation.

```c
struct SN_env {
    symbol * p;          /* the string buffer (the word being stemmed) */
    int c;               /* current cursor position */
    int l;               /* length of the current string */
    int lb;              /* lower bound for backward scans */
    int bra;             /* left bracket: start of the marked region */
    int ket;             /* right bracket: end of the marked region */
    symbol ** S;         /* string registers (language-specific count) */
    int * I;             /* integer registers (language-specific count) */
};
```

The fields `p`, `c`, and `l` together define a string buffer and a scan position within it. The cursor `c` advances forward for left-to-right rules or retreats for right-to-left rules. `bra` and `ket` delimit a "slice" — the region that a replacement operation (`slice_from_s`, `slice_del`) will overwrite. The `S` and `I` arrays hold language-specific working registers. The English stemmer uses three integer registers (`SN_create_env(0, 3)`), while stemmers for other languages vary.

Crucially, PostgreSQL allocates `SN_env` once per dictionary instance and reuses it across calls. The stemmer does not allocate fresh state for each word. It loads a new word into the existing buffer with `SN_set_current`, which calls `replace_s` to overwrite `p` and resets `c` to zero. This means the character buffer may grow as needed (`increase_size` doubles-plus-headroom in `utilities.c`), but the stemmer never frees it between calls — a deliberate design that trades a small persistent allocation for avoiding per-word `malloc` overhead.

## The Runtime in utilities.c

`utilities.c` contains the primitive operations that every generated stemmer calls. These primitives read or mutate `SN_env` rather than operating on plain strings, which keeps the generated code free of raw pointer arithmetic.

The **string and slice primitives** cover two directions of traversal. `eq_s` and `eq_s_b` test whether the string at the forward or backward cursor matches a given constant. They advance or retreat `c` on success. `find_among` and `find_among_b` are binary searches over sorted `struct among` arrays — the lookup tables that the Snowball compiler pre-sorts at compile time. A match can have an associated callback (`w->function`) that runs additional contextual tests. If those tests fail, the search backtracks to the next-longest matching prefix via the `substring_i` chain.

The **replacement primitives** operate on the bra/ket slice. `replace_s` is the workhorse. It shifts the tail of the buffer right or left to accommodate the replacement string. It updates `l` and `c` accordingly, and writes the new bytes in place. `slice_from_s` wraps it for the common case of replacing whatever is between `bra` and `ket`. `slice_del` is just `slice_from_s` with a zero-length replacement.

The **UTF-8 primitives** — `skip_utf8`, `skip_b_utf8`, `in_grouping_U`, `out_grouping_U`, and their backward variants — handle multi-byte character boundaries. They walk the buffer one Unicode codepoint at a time, interpreting the leading-byte patterns of UTF-8 to find character boundaries. A separate set of single-byte variants (`in_grouping`, `out_grouping`) handles ISO-8859-x and KOI8-R stemmers where every byte is one character.

## Memory Ownership

The upstream Snowball library uses standard `malloc`/`free`. PostgreSQL redirects those calls to its own allocator by overriding them with macros in `src/include/snowball/header.h`:

```c
#define malloc(a)    palloc(a)
#define realloc(a,b) repalloc(a,b)
#define free(a)      pfree(a)
```

This override applies only to the generated stemmer files, not to the rest of the backend. The effect is that all of the buffer memory controlled by `SN_env` — the `p` buffer, the `S` string registers, the `I` integer array — lives in whatever [[subsystems/memory/contexts|memory context]] is current when `SN_create_env` is called. `dict_snowball.c` arranges for that context to be the long-lived dictionary context (`d->dictCtx`), so the allocations persist for the lifetime of the dictionary instance.

## The PostgreSQL Dictionary Wrapper

PostgreSQL integrates Snowball into [[subsystems/full-text-search|full-text search]] through a text search dictionary template whose init and lexize functions live in `src/backend/snowball/dict_snowball.c`. PostgreSQL registers the template as `snowball`. It exposes two parameters: `language` (required) and `stopwords` (optional).

### The DictSnowball Structure

```c
typedef struct DictSnowball {
    struct SN_env *z;         /* persistent stemmer state */
    StopList       stoplist;  /* loaded stop words */
    bool           needrecode; /* true if stemmer encoding != server encoding */
    int          (*stem)(struct SN_env *z); /* function pointer to the stem() */
    MemoryContext  dictCtx;   /* context where z was allocated */
} DictSnowball;
```

`dsnowball_init` resolves the function pointer `stem` at init time to the specific language stemmer (e.g. `english_ISO_8859_1_stem`), so that `dsnowball_lexize` pays only a single indirect call per word with no language dispatch overhead.

### Initialization

`dsnowball_init` (called once when `CREATE TEXT SEARCH DICTIONARY` runs or when a cached plan first executes the dictionary) walks the `stemmer_modules` table to find a stemmer matching both language name and database encoding. The selection logic in `locate_stem_module` has two tiers. It prefers an exact encoding match. It falls back to a UTF-8 stemmer with `needrecode = true` if the language is available only in UTF-8. This fallback allows, for example, a Latin-1 database to use the UTF-8 Arabic stemmer by transcoding around each call.

After matching, `locate_stem_module` calls the stemmer's `create_env` function to allocate `SN_env` in the current (long-lived) memory context. It then stores both the environment and the `stem` function pointer in `DictSnowball`.

`readstoplist` loads stop words at init time into a sorted `StopList`. The stop word files live in `src/backend/snowball/stopwords/` as plain text files, one word per line. `searchstoplist` does a binary search on each call.

### Lexize

The dictionary pipeline calls `dsnowball_lexize` once per token. Its logic has three branches:

- Tokens longer than 1000 bytes bypass the stemmer entirely. `dsnowball_lexize` returns them lowercased but otherwise unmodified. The comment in the source explains both the efficiency rationale and a concrete correctness concern: the Turkish stemmer has a recursive structure that can crash on arbitrarily long inputs.
- Empty strings and stop words return a null `TSLexeme` array — the [[subsystems/types/text-search-dictionaries|text search dictionary]] pipeline treats this as "discard the token."
- For all other tokens, `dsnowball_lexize` lowercases the token, optionally transcodes it to UTF-8, and loads it into `SN_env` via `SN_set_current`. It passes the token through `d->stem(d->z)`, then reads the result back from `d->z->p` up to `d->z->l` bytes.

`dsnowball_lexize` wraps the stemmer call in `MemoryContextSwitchTo(d->dictCtx)`, to ensure that any reallocations of the `p` buffer land in the dictionary's long-lived context rather than the query's short-lived per-call context. Without this, the query's per-call context would free the buffer at the end of each query cycle. The persisted `SN_env` would then hold a dangling pointer.

## Encoding Handling

Each `stem_*.c` file is encoded for one character set. The same language typically appears twice in `stemmer_modules`: once as ISO-8859-x for databases using that encoding natively, and once as UTF-8. The `needrecode` flag in `DictSnowball` records which variant `locate_stem_module` selected. When `needrecode` is true, `dsnowball_lexize` calls `pg_server_to_any(txt, len, PG_UTF8)` before stemming and `pg_any_to_server` afterwards, converting between server encoding and the UTF-8 stemmer's expected input.

One language — English — additionally appears as `PG_SQL_ASCII`, which PostgreSQL treats as compatible with any server encoding. This guarantees that an English stemmer is always available regardless of database locale.

## Dictionary Registration

A Snowball dictionary is registered with:

```sql
CREATE TEXT SEARCH DICTIONARY english_stem (
    TEMPLATE = snowball,
    LANGUAGE = english,
    StopWords = english
);
```

The `TEMPLATE = snowball` clause wires `dsnowball_init` and `dsnowball_lexize` as the init and lexize callbacks. `LANGUAGE` selects the entry from `stemmer_modules`. `StopWords` names a file under `$SHAREDIR/tsearch_data/`. The `snowball.sql.in` and `snowball_create.pl` scripts in `src/backend/snowball/` generate the SQL that installs the built-in configurations during `initdb`.

## Related Topics

- [[subsystems/full-text-search|Full-text search]]
- [[subsystems/types/text-search-dictionaries|Text search dictionaries]]
- [[subsystems/types/text-search-parser|Text search parser]]

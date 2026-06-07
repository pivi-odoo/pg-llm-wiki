---
title: "Text Search Dictionaries"
aliases:
  - ts dictionary
  - text search dictionary pipeline
  - lexeme normalization
source_files:
  - src/backend/tsearch/dict.c
  - src/backend/tsearch/dict_simple.c
  - src/backend/tsearch/dict_synonym.c
  - src/backend/tsearch/dict_thesaurus.c
  - src/backend/tsearch/dict_ispell.c
  - src/backend/tsearch/spell.c
  - src/include/tsearch/ts_public.h
symbols:
  - ts_lexize
  - dsimple_init
  - dsimple_lexize
  - dsynonym_init
  - dsynonym_lexize
  - thesaurus_init
  - thesaurus_lexize
  - dispell_init
  - dispell_lexize
  - NINormalizeWord
  - NIImportDictionary
  - NIImportAffixes
  - NISortDictionary
  - NISortAffixes
  - TSLexeme
  - DictSubState
---

Full-text search in PostgreSQL converts raw document text into a `tsvector` — a sorted set of normalized lexemes — through a two-stage pipeline. The [[subsystems/types/text-search-parser|text search parser]] splits the raw text into typed tokens. The dictionary pipeline then normalizes each token into a canonical form, discards noise words, and optionally expands or contracts multi-word phrases. The output of that normalization is a `TSLexeme` structure carrying the normalized string plus positional and variant metadata. The configuration object binding token types to dictionaries is the text search configuration, stored in `pg_ts_config` and `pg_ts_config_map`.

## Text Search Configurations

A text search configuration (`pg_ts_config`) is essentially a routing table: it maps each token type emitted by a parser to an ordered list of dictionaries. Every (configuration, token_type) pair can carry a different chain. When `to_tsvector('english', text)` is called, the engine iterates over tokens from the parser and, for each token, walks the dictionary list for that token type in the `english` configuration.

Users create configurations with `CREATE TEXT SEARCH CONFIGURATION` and populate them with `ALTER TEXT SEARCH CONFIGURATION ... ADD MAPPING FOR <token_type> WITH <dict1>, <dict2>, ...`. PostgreSQL stores the built-in configurations under `$sharedir/tsearch_data/` and loads them on demand into the dictionary cache (`TSDictionaryCacheEntry`). Each cache entry holds the opaque `dictData` pointer produced by the dictionary's init function, plus a `FmgrInfo` for its `lexize` function. PostgreSQL initializes dictionaries once per session on first use, and never reloads them unless the cache entry is invalidated.

## The Dictionary Chain and Lexeme Resolution

For each token, the engine passes the raw token text to the first dictionary in the chain. The dictionary's `lexize` function returns one of three outcomes:

- A non-empty `TSLexeme` array: the dictionary recognized the token and produced one or more normalized forms. Processing stops. The engine adds these lexemes to the `tsvector`.
- An empty `TSLexeme` array (an array whose first element has `lexeme == NULL`): the dictionary recognized the token as a stop word. The engine discards the token. The chain terminates.
- `NULL`: the dictionary did not recognize the token. The engine moves to the next dictionary in the chain.

If all dictionaries in the chain return `NULL`, the engine keeps the original token text verbatim. This fallback preserves tokens that no configured dictionary understands — useful for technical identifiers or domain-specific terms that should be indexed as-is.

The `TSLexeme` struct (`ts_public.h`) also carries a `nvariant` field for split-word variants (for example, a Norwegian compound can decompose in multiple ways, each tagged with a different `nvariant`), and a `flags` field with bits `TSL_ADDPOS`, `TSL_PREFIX`, and `TSL_FILTER`.

The thesaurus dictionary is the only built-in type that breaks this single-call contract. Because it needs to match multi-token phrases, it uses a stateful protocol via the `DictSubState` struct. The engine calls `thesaurus_lexize` repeatedly, setting `dstate->getnext = true` between calls to signal that the dictionary wants to see the next token before committing to a result. The `isend` flag marks the end of the token stream so the dictionary can finalize any partial match.

## The Simple Dictionary

`dict_simple` (`dict_simple.c`) is the cheapest dictionary: it lowercases the input with `lowerstr_with_len()` and optionally checks the result against a stop-word list loaded from a `.stop` file at init time. If the word is a stop word, it returns an empty array (discard). If not, it returns the lowercased word as the sole lexeme.

The `Accept` option (default `true`) controls what happens when no stop-word list is configured or the word is not a stop word. With `Accept = false`, the dictionary returns `NULL` instead of the lowercased word, passing control to the next dictionary in the chain. This allows the engine to use `simple` as a stop-word filter ahead of a more expensive stemmer: it discards words that match the stop list immediately, while unrecognized words fall through.

## The Synonym Dictionary

`dict_synonym` (`dict_synonym.c`) performs exact one-to-one word substitution from a flat file with two-column lines (`input_word  output_word`). At init time, `dsynonym_init()` reads the `.syn` file. It optionally lowercases both columns (controlled by `CaseSensitive`), and sorts the resulting `Syn` array by the input word. Lookup is a binary search.

When the dictionary finds a match, it returns the output word as the lexeme. The output can carry the `TSL_PREFIX` flag if the output word in the file ends with `*`, enabling prefix matching in queries. Synonym dictionaries are useful for technical abbreviations, brand names, or domain-specific aliases where a simple string substitution suffices. Because they operate on single tokens only, they cannot expand or collapse multi-word phrases.

## The Thesaurus Dictionary

`dict_thesaurus` (`dict_thesaurus.c`) extends synonym replacement to multi-word input and multi-word output. The `.ths` file format is:

```
sample phrase : substitute phrase
```

Both sides are sequences of words separated by spaces. The `:` separates input from output. At init time, the thesaurus compiles its word list by running each sample word through a subdictionary (specified by the `Dictionary` option). This means that the pattern matching during document indexing operates on normalized lexemes, not raw words — the thesaurus effectively searches for patterns in the already-normalized token stream. A `?` in a sample phrase matches any stop word of the subdictionary.

Because the thesaurus must accumulate multiple tokens before it can decide whether a phrase matches, its `lexize` function is stateful (`DictSubState`). The engine calls it once per incoming token. The dictionary sets `dstate->getnext = true` to signal that it needs the next token. When the accumulated state matches a complete pattern, the dictionary returns the substitute lexemes. An output word prefixed with `*` in the `.ths` file bypasses subdictionary normalization for that word (`DT_USEASIS` flag). This allows the output phrase to contain terms that the subdictionary would otherwise reject.

Thesaurus dictionaries are appropriate for domain vocabularies where two-word phrases have a single canonical concept (for example, "New York" → "new_york") or where abbreviations are better treated as multi-word expansions.

## The Ispell Dictionary

`dict_ispell` (`dict_ispell.c`, `spell.c`) provides full morphological analysis using Hunspell/Ispell affix rules. It accepts two files: a `.dict` word list mapping base forms to affix flags, and an `.affix` file defining stripping and replacement rules. Compilation happens at init time in `dispell_init()` through a four-step process:

1. `NIImportDictionary()` reads all base forms from the `.dict` file into a temporary `Spell` array.
2. `NIImportAffixes()` reads the `.affix` file, distinguishing Ispell format from Hunspell format (detected by the `SET` directive).
3. `NISortDictionary()` builds a trie from the base-form array and frees the temporary `Spell` storage.
4. `NISortAffixes()` builds separate prefix and suffix tries from the compiled affix rules.

After `NIFinishBuild()`, `dispell_init()` deletes the intermediate build context, so only the compiled trie structures remain. This is important because the word list and affix data can consume hundreds of megabytes during compilation. The compiled form is substantially smaller.

At query time, `dispell_lexize()` lowercases the input token and calls `NINormalizeWord()`. `NINormalizeWord()` walks the affix tries to strip prefixes and suffixes, checks whether the resulting stem exists in the word trie, and returns all valid base forms as separate `TSLexeme` entries with distinct `nvariant` values. A separate stop-word list (`StopWords` option) can then filter lexemes that `NINormalizeWord` recognized but that should be discarded.

The strength of ispell is linguistic accuracy: it respects the actual morphology of the language and can produce multiple valid decompositions for compound words. The weakness is load time and memory. An English Hunspell dictionary with a comprehensive word list can take several seconds and tens of megabytes to initialize. Because PostgreSQL caches dictionaries per backend, the first query after a server restart pays that cost.

## The Snowball Stemmer

PostgreSQL bundles the Snowball stemmer library (`src/backend/snowball/`) as a distinct dictionary type (`pg_ts_template` entry `snowball_stem`). Snowball applies algorithmic suffix-stripping rules — no word list, no affix file — to reduce a word to an approximate stem. Supported languages include English, German, French, Spanish, Dutch, Russian, Portuguese, Italian, Swedish, Norwegian, Danish, and Finnish, among others.

Compared with ispell, Snowball is fast to initialize (no files to load), language-agnostic in implementation, and accurate enough for most full-text ranking use cases. Its stems are not real words — "running" becomes "run", but "flies" and "fly" both become "fli" in English. This is acceptable for similarity matching, but would be wrong if the output needed to be readable. The English built-in configuration chains ispell ahead of Snowball: ispell handles common words where the dictionary knows the right stem. Snowball catches unknown words that fall through.

## Stop Words

Stop words are tokens that carry no retrieval value — articles, prepositions, conjunctions. All built-in dictionaries support a stop-word list via the `StopWords` option, which names a `.stop` file under `$sharedir/tsearch_data/`. The dictionary sorts the stop list at load time and searches it with binary search. A matched token returns an empty `TSLexeme` array, which causes the engine to discard the token and not pass it to subsequent dictionaries.

Discarding stop words has one non-obvious consequence: phrase search using the `<->` phrase operator depends on lexeme positions. Because the engine drops stop words from the `tsvector`, a phrase like "the cat" stored with default English settings loses the "the". The position gap between "cat" and the previous non-stop word, though, is preserved numerically. However, a query like `'cat' <-> 'sat'` against text "the cat sat" may or may not match depending on whether the stop words shift the stored positions. This is an inherent trade-off of the position-preserving representation used by `tsvector`.

## Debugging with ts_lexize

`ts_lexize(dict regdictionary, token text)` exposes the raw output of a single dictionary's `lexize` function. It bypasses the configuration chain, calling the dictionary directly and returning the resulting lexeme array or NULL. This is the primary tool for diagnosing unexpected tokenization results.

```sql
SELECT ts_lexize('english_stem', 'running');
-- {run}

SELECT ts_lexize('english_stem', 'the');
-- {}   (stop word)

SELECT ts_lexize('simple', 'PostgreSQL');
-- {postgresql}
```

`dict.c` implements the function as `ts_lexize()`. It handles the two-call thesaurus protocol internally: if `dstate.getnext` is set after the first call, it issues a second call with `dstate.isend = true` to flush any buffered state.

## Introspection and Configuration

psql meta-commands provide quick visibility into the installed objects:

| Command | Shows |
|---|---|
| `\dF` | Text search configurations |
| `\dFd` | Text search dictionaries |
| `\dFp` | Text search parsers |
| `\dFt` | Text search templates |

The system catalogs involved are `pg_ts_config`, `pg_ts_config_map`, `pg_ts_dict`, `pg_ts_parser`, and `pg_ts_template`. `pg_ts_config_map` is the key table: each row maps `(mapcfg, maptokentype, mapseqno)` to a dictionary OID, with `mapseqno` determining chain order.

To create a custom configuration for a specialized domain:

```sql
CREATE TEXT SEARCH CONFIGURATION myconfig (COPY = english);

ALTER TEXT SEARCH CONFIGURATION myconfig
  ALTER MAPPING FOR asciiword, word
  WITH mysynonyms, english_ispell, english_stem;
```

This copies the `english` configuration's parser and default mappings. It then replaces the dictionary chain for `asciiword` and `word` tokens with a custom synonym dictionary, followed by ispell and the Snowball stemmer as fallbacks.

## Related Topics

- [[subsystems/types/text-search-parser|text search parser]] — how raw document text is split into typed tokens before dictionaries process them
- [[subsystems/types/locale|locale]] — locale and LC_CTYPE interact with dictionary normalization; `lowerstr()` in `simple` and `ispell` uses the database's LC_CTYPE-derived character classification

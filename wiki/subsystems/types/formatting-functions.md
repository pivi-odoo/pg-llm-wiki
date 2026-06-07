---
title: "Formatting Functions (to_char, to_date)"
aliases:
  - to_char
  - to_date
  - to_timestamp
  - to_number
  - datetime formatting
  - number formatting
source_files:
  - src/backend/utils/adt/formatting.c
symbols:
  - DCH_to_char
  - DCH_from_char
  - do_to_timestamp
  - datetime_to_char_body
  - parse_format
  - FormatNode
  - TmToChar
  - TmFromChar
  - NUMDesc
  - NUMProc
  - DCHCacheEntry
  - NUMCacheEntry
  - index_seq_search
  - adjust_partial_year_to_2020
  - from_char_set_mode
---

PostgreSQL's `to_char`, `to_date`, `to_timestamp`, and `to_number` functions share a single format-picture engine in `formatting.c`, inspired by Oracle's equivalents. The engine parses a format string into a compact array of `FormatNode` items once. It caches that parsed representation. Then it walks the node array, either to produce output (for `to_char`) or to consume input (for `to_date` / `to_timestamp` / `to_number`). This design lets repeated calls with the same format string avoid the cost of re-parsing.

## Format Picture Parsing and the FormatNode Array

PostgreSQL turns every format string into a flat array of `FormatNode` structs (formatting.c). Each node has a type (`NODE_TYPE_ACTION`, `NODE_TYPE_CHAR`, `NODE_TYPE_SEPARATOR`, `NODE_TYPE_SPACE`, or `NODE_TYPE_END`) and, for action nodes, a pointer into either `DCH_keywords` (for date/time patterns) or `NUM_keywords` (for number patterns). Literal text between pattern keywords becomes individual `NODE_TYPE_CHAR` nodes. The parser treats text inside double-quoted sections as character literals verbatim. This lets arbitrary strings survive without being misread as pattern keywords.

Pattern lookup happens through a two-level structure (index_seq_search(), formatting.c). A 95-element index array maps each ASCII character to the first keyword whose name starts with that character. A sequential scan from that position then finds the longest match. Because the keyword arrays list longer patterns before shorter ones (e.g. `DDD` before `DD` before `D`), the first match is always the longest applicable keyword. The date/time keyword set is in `DCH_keywords` (indexed by `DCH_poz` enum values). The number keyword set is in `NUM_keywords` (indexed by `NUM_poz`).

Suffixes modify how the engine handles action nodes. It stores them as a bitmask in `FormatNode.suffix`. For date/time patterns, the recognised suffixes are `FM` (fill-mode prefix, removes padding), `TM` (locale-aware translation prefix), `TH`/`th` (ordinal suffix, e.g. `1st`), and `SP` (reserved). For number patterns, `FM` is a standalone keyword rather than a prefix/postfix suffix, because the number format grammar does not have the same positional structure.

## Date/Time Two-Path Architecture

The date/time formatting code follows two entirely separate paths: output (`DCH_to_char`) and input (`DCH_from_char`).

**Output path.** `DCH_to_char()` walks the node array. It appends to a character buffer. Literal nodes copy their character directly. Action nodes call `sprintf` or string copy to render the appropriate date or time field from a `TmToChar` struct. The `TmToChar` wraps a custom `fmt_tm` struct (not POSIX's `struct tm`). Its `tm_hour` field is 64-bit, to handle intervals that span more than 24 hours. Month and day names without the `TM` suffix use static English arrays. With `TM`, they use the locale-aware arrays populated by `cache_locale_time()`.

**Input path.** `DCH_from_char()` walks the same node array against an input string, filling a `TmFromChar` struct field-by-field. Fixed-width fields (e.g. `DD` consumes exactly 2 characters) behave differently from fill-mode fields (with the `FM` suffix, the parser reads input greedily until the next non-digit). In the default non-FX mode, the parser allows extra whitespace between fields. With `FX` (or in standard/strict mode), the input must match the format character-for-character. After `DCH_from_char` populates `TmFromChar`, `do_to_timestamp()` reconciles the individual parsed fields into a `struct pg_tm`, resolving ambiguities like partial years, day-of-year, ISO week dates, and the interaction between century (CC) and two-digit year (YY).

## Gregorian vs. ISO Week Date Convention Enforcement

Each date/time keyword carries a `FromCharDateMode` tag: `FROM_CHAR_DATE_GREGORIAN` for calendar-based fields (DD, MM, YYYY, DDD, etc.) or `FROM_CHAR_DATE_ISOWEEK` for ISO 8601 week date fields (IW, ID, IYYY, IDDD). The function `from_char_set_mode()` enforces that within a single format string, all date-contributing keywords must belong to the same convention. If a format mixes e.g. `MM` with `IW`, parsing immediately raises `ERRCODE_INVALID_DATETIME_FORMAT` with a hint to avoid mixing Gregorian and ISO week date conventions. The `date_mode` on each keyword is `FROM_CHAR_DATE_NONE` for fields that are calendar-independent (time fields, era fields, timezone fields).

## Partial Year Adjustment

When the parser reads a year field shorter than 4 digits (YY or Y, not YYYY), it applies `adjust_partial_year_to_2020()` (formatting.c). The rule anchors the sliding window around the year 2020: it maps values 0–69 to 2000–2069, values 70–99 to 1970–1999, values 100–519 to 2100–2519, and values 520–999 to 1520–1999. This is a deliberate design choice to keep ambiguous two-digit years reasonably close to the present without requiring a configurable epoch parameter.

## Format Picture Cache

Parsed `FormatNode` arrays are expensive enough to be worth caching. The engine maintains two static caches, one for date/time formats (`DCHCache`, up to 20 entries of up to ~200 format nodes each) and one for number formats (`NUMCache`, up to 20 entries of up to ~100 nodes). The engine allocates cache entries in `TopMemoryContext`, so they survive transaction boundaries. If a format string is longer than `DCH_CACHE_SIZE` or `NUM_CACHE_SIZE` characters, the engine skips caching. It parses the format fresh on every call.

Eviction uses a simple counter-based LRU approximation (DCH_cache_getnew(), formatting.c). Each entry has an `age` field updated on every access. When the cache is full, the engine replaces the entry with the smallest age. To guard against integer overflow of the counter, `DCH_prevent_counter_overflow()` halves all age values whenever the counter approaches `INT_MAX`, preserving relative ordering without resetting to zero.

## Number Formatting Architecture

Number formatting uses a different set of abstractions from date/time. The `NUMDesc` struct accumulates a complete description of the number picture at parse time. This includes digit counts before and after the decimal point (`pre`, `post`); which locale-sensitive symbols appear (`need_locale`); whether fill-mode, bracket notation, or scientific notation are active; and the position range that should be zero-filled. `NUMDesc_prepare()` validates and populates this struct as the parser encounters each `NUM_keywords` node.

At execution time, `NUMProc` holds the transient state for a single `to_char` or `to_number` call. `NUM_processor()` walks the node array once more using the populated `NUMDesc` to either format a numeric value into a string or parse a string back to a numeric value. `NUM_prepare_locale()` fetches locale-sensitive components (`L` for currency symbol, `G` for group separator, `D` for decimal point) once, at the start of `NUM_processor()`. `NUM_processor()` then references them by pointer throughout. `int_to_roman()` handles Roman numeral output (the `RN`/`rn` keywords). It builds the numeral string using three static arrays for the hundreds, tens, and ones digit positions.

## Key Data Structures

| Struct | Purpose |
|---|---|
| `FormatNode` | Single parsed element: action keyword, literal char, separator, or end marker |
| `KeyWord` | Entry in `DCH_keywords` or `NUM_keywords`; holds name, length, id enum, and date_mode |
| `KeySuffix` | Entry in `DCH_suff`; holds prefix/postfix suffix name, id bitmask, and position type |
| `TmToChar` | Holds a `fmt_tm` plus fractional seconds and timezone name for output |
| `TmFromChar` | Accumulates all parsed date/time fields for input parsing |
| `NUMDesc` | Summarises a parsed number picture (digit counts, flags, locale requirements) |
| `NUMProc` | Transient execution state for a single number format/parse operation |
| `DCHCacheEntry` | One cache slot: the format string, its parsed `FormatNode` array, and an LRU age |
| `NUMCacheEntry` | One cache slot: format string, `FormatNode` array, pre-populated `NUMDesc`, and age |

## Related Topics

- [[subsystems/types/datetime-types|Date/Time Types]] — the timestamp, date, time, and interval types that `to_char` and `to_timestamp` convert to and from
- [[subsystems/types/numeric-scalar-types|Scalar Numeric Types]] — the integer and float types accepted by `to_char(numeric, fmt)` and `to_number`

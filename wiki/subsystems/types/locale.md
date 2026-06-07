---
title: "Locale and Collation Support"
aliases:
  - pg_locale
  - collation provider
  - ICU collation
  - libc collation
source_files:
  - src/backend/utils/adt/pg_locale.c
  - src/backend/utils/adt/pg_locale_libc.c
  - src/backend/utils/adt/pg_locale_icu.c
  - src/backend/utils/adt/pg_locale_builtin.c
symbols:
  - pg_locale_struct
  - pg_locale_t
  - collation_cache_entry
  - pg_newlocale_from_collation
  - pg_strcoll
  - pg_strncoll
  - pg_strxfrm
  - pg_strnxfrm
  - pg_strxfrm_prefix
  - lc_collate_is_c
  - lc_ctype_is_c
  - PGLC_localeconv
  - cache_locale_time
  - make_icu_collator
  - get_collation_actual_version
  - icu_to_uchar
  - icu_from_uchar
  - icu_language_tag
  - icu_validate_locale
  - pg_perm_setlocale
---

Locale support in PostgreSQL bridges between the portable SQL collation model and the platform-specific locale libraries — either the C runtime's `libc` locale subsystem or the ICU (International Components for Unicode) library. It determines how strings compare and sort. It determines how character classification functions like `upper()` and `lower()` behave. It also determines how formatting functions like `to_char()` render numbers, monetary values, and date/time names. Because locale behavior affects correctness of index ordering, the design is careful about when and how locale state is changed globally in the process.

## The Six LC_ Categories and Their Lifetimes

PostgreSQL tracks six locale categories inherited from POSIX, but treats them very differently (pg_locale.c, top-of-file comment):

- **LC_COLLATE** and **LC_CTYPE** are fixed at `CREATE DATABASE` time, stored in `pg_database`, and never changed at runtime. This invariant is essential: B-tree indexes depend on a stable ordering. Any mid-session change would silently corrupt them.
- **LC_MESSAGES** is settable at runtime and takes effect immediately, controlling the language of error messages. The `assign_locale_messages()` GUC hook calls `pg_perm_setlocale()` to apply it permanently.
- **LC_MONETARY**, **LC_NUMERIC**, and **LC_TIME** are GUC-settable but are never applied permanently. Instead, when `to_char()` or the `money` type need locale-aware formatting, the code briefly switches to the configured locale, copies out the needed data, and immediately restores the previous locale. Applying these categories permanently would, for example, prevent standard floating-point literals from being parsed correctly in some locales.

The `PGLC_localeconv()` function implements the brief-switch pattern for monetary/numeric data. First it saves the current `LC_MONETARY` and `LC_NUMERIC` settings using `pstrdup()`. This step is necessary because `setlocale(NULL)` may return a pointer to static storage that the next `setlocale()` call overwrites. It then switches to the configured locales, copies all `struct lconv` fields with `strdup()`, and restores the originals. Finally it converts the copied strings from the locale's encoding to the database encoding. Failure to restore is fatal. The result is cached in a static `CurrentLocaleConv` struct. The `assign_locale_monetary()` and `assign_locale_numeric()` GUC hooks invalidate the cache (pg_locale.c).

`cache_locale_time()` applies the same save-switch-copy-restore pattern for LC_TIME, using `strftime()` with format codes `%a`, `%A`, `%b`, `%B` against synthetic `struct tm` values to populate the `localized_abbrev_days`, `localized_full_days`, `localized_abbrev_months`, and `localized_full_months` arrays stored in `TopMemoryContext`. On Windows, the code uses `wcsftime()` instead. It returns UTF-16, which the code then transcodes to UTF-8.

## Collation Providers: libc vs. ICU

Per-column and per-expression collation is modeled through `pg_collation`. Each collation row carries a `collprovider` field — `COLLPROVIDER_LIBC` or `COLLPROVIDER_ICU` — which selects the runtime library. The abstract handle passed between callers is `pg_locale_t`, a pointer to `pg_locale_struct`:

```c
struct pg_locale_struct {
    char    provider;       /* COLLPROVIDER_LIBC or COLLPROVIDER_ICU */
    bool    deterministic;  /* false for nondeterministic ICU collations */
    union {
        locale_t  lt;       /* libc: per-locale object from newlocale() */
        struct {
            const char *locale;
            UCollator  *ucol;  /* ICU collator handle */
        } icu;
    } info;
};
```

A `NULL` `pg_locale_t` means "use the database's default collation" — callers fall back to global libc functions (`strcoll()`, `strxfrm()`) rather than the per-locale variants. This is a performance-oriented shortcut: the default collation's locale_t is not materialised unless it is an ICU collation.

`pg_newlocale_from_collation()` retrieves or creates the locale handle for a given collation OID. Results are cached in a per-backend hash table keyed by collation OID (`collation_cache_entry`), allocated in `TopMemoryContext`, and never freed. The cache also records the boolean flags `collate_is_c` and `ctype_is_c`. These flags allow hot-path code to skip locale-aware operations entirely when the effective locale is C or POSIX. `lc_collate_is_c()` and `lc_ctype_is_c()` expose these flags, with special fast paths for the built-in C and POSIX collation OIDs and for the default collation (pg_locale.c).

For libc collations, `pg_newlocale_from_collation()` calls `newlocale()` with `LC_COLLATE_MASK | LC_CTYPE_MASK`. When `collcollate` and `collctype` differ (an unusual case), two separate `newlocale()` calls are chained: the first creates a locale object for `LC_COLLATE`, and the second builds on it with `LC_CTYPE_MASK`. Windows does not support this split and raises an error if the two differ.

For ICU collations, `make_icu_collator()` calls `pg_ucol_open()` to open a `UCollator` for the locale string. If the collation carries an `icurules` field, `make_icu_collator()` appends those rules to the default rules of the collation and calls `ucol_openRules()` instead, allowing users to overlay custom sort order adjustments on top of a named locale. The `UCollator` handle is stored in `TopMemoryContext` for the lifetime of the backend.

## Locale Provider Backends

PostgreSQL 18 split the previously monolithic `pg_locale.c` into three provider-specific files, one per collation backend. Each backend implements a `collate_methods` vtable with `strncoll`, `strnxfrm`, and optionally `strnxfrm_prefix` function pointers, plus the `strxfrm_is_safe` flag. The `pg_locale_struct` carries a pointer to the appropriate vtable in its `collate` field (NULL when `collate_is_c` is true, since the C locale uses simple byte comparison and skips locale dispatch entirely).

### libc backend (`pg_locale_libc.c`)

The libc provider wraps the POSIX `locale_t` object created by `newlocale()`. The `pg_locale_struct.info.lt` field holds this handle. String comparison dispatches to `strncoll_libc()`. This function calls `strcoll_l()` against the per-locale object. Sort-key generation calls `strxfrm_l()` via `strnxfrm_libc()`. By default, `strxfrm_is_safe` is false, because glibc's `strxfrm()` and `strcoll()` produce inconsistent orderings for many locales. Setting `TRUST_STRXFRM` at build time can override this. On Windows with UTF-8 databases, the code uses a separate `collate_methods_libc_win32_utf8` vtable instead: `strncoll_libc_win32_utf8()` converts both strings to UTF-16 via `MultiByteToWideChar()` and then calls `wcscoll_l()`, since the Windows libc cannot compare UTF-8 strings directly. Prefix sort keys (`strnxfrm_prefix`) are not supported.

Case-folding functions (`strlower_libc`, `strupper_libc`, `strtitle_libc`) dispatch internally on whether the encoding is single-byte or multi-byte. The multi-byte path converts through `wchar_t` using `char2wchar()` / `wchar2char()` and applies `towlower_l()` / `towupper_l()` per character.

Tradeoffs: locale behaviour is entirely platform-dependent — the same locale name may sort differently across glibc versions or on different operating systems. Collation versioning uses `gnu_get_libc_version()` (glibc), `querylocale()` (FreeBSD), or `GetNLSVersionEx()` (Windows) to detect when the library has changed.

### ICU backend (`pg_locale_icu.c`)

The ICU provider wraps a `UCollator*` handle (stored in `pg_locale_struct.info.icu.ucol`) opened via `ucol_open()` for the BCP 47 locale string. When the collation row carries an `icurules` field, `ucol_openRules()` overlays custom sort-order adjustments on top of the named locale's defaults instead. The collator is allocated in `TopMemoryContext` and reused for the lifetime of the backend.

Two vtables exist: `collate_methods_icu_utf8` (for UTF-8 databases) and `collate_methods_icu` (for other encodings). In the UTF-8 path, `strncoll_icu_utf8()` calls `ucol_strcollUTF8()` directly (available since ICU 53; earlier ICU 50–52 builds had bugs). In non-UTF-8 databases, `strncoll_icu()` first converts both strings from the database encoding to `UChar` (ICU's UTF-16 representation) using the session-level `icu_converter` object, then calls `ucol_strcoll()`. The converter is initialised once per session in `init_icu_converter()`.

Sort-key generation via `strnxfrm_icu()` is always safe (`strxfrm_is_safe = true`). `strnxfrm_prefix_icu()` / `strnxfrm_prefix_icu_utf8()` support prefix sort keys, using `ucol_nextSortKeyPart()` to produce a byte string comparable with `memcmp()` for index prefix-range queries.

ICU collations may be nondeterministic (`deterministic = false`): strings that compare equal under `ucol_strcoll()` may not be byte-for-byte identical. This affects equality semantics in B-tree and hash indexes.

Tradeoffs: full Unicode collation algorithm (CLDR/UCA) with predictable cross-platform behaviour. Supports non-deterministic collations for accent-insensitive or case-insensitive matching. Requires the ICU library at build time (`--with-icu`).

### Builtin backend (`pg_locale_builtin.c`)

The builtin provider supports three locale strings: `C`, `C.UTF-8`, and `PG_UNICODE_FAST`. All builtin collations set `collate_is_c = true`, meaning they bypass the `collate_methods` vtable entirely and use simple byte comparison (`memcmp`) for sorting. This makes them fully deterministic and independent of any OS locale library.

The `info.builtin.casemap_full` flag distinguishes `PG_UNICODE_FAST` (full Unicode case mapping via `unicode_strupper`/`unicode_strlower`) from `C` and `C.UTF-8` (ASCII-only case mapping). `C` sets `ctype_is_c = true` as well. The `strfold_builtin()` function (Unicode case folding) is unique to this provider and the ICU provider — libc has no equivalent.

`create_pg_locale_builtin()` reads the locale string from `pg_database.datlocale` (for the default collation OID) or from `pg_collation.colllocale`, then calls `builtin_validate_locale()` to reject unsupported strings. Collation versioning returns a static `"1"` for all supported names, since byte-comparison behaviour is not expected to change.

Tradeoffs: deterministic and portable — the same sort order on every platform. No dependency on OS locale data. Limited to two sort orders: byte order (`C`, `C.UTF-8`) or byte order for `PG_UNICODE_FAST` (same sort order, only case mapping differs). Not suitable for language-aware collation.

### Provider Comparison

| Provider | Backend object | Sorting (`strncoll`) | Sort keys (`strnxfrm`) | Prefix keys | Tradeoffs |
|---|---|---|---|---|---|
| `libc` | POSIX `locale_t` | `strcoll_l()` | `strxfrm_l()` (unsafe by default) | None | Platform-dependent; varies across OS/libc versions |
| `icu` | ICU `UCollator*` | `ucol_strcollUTF8()` / `ucol_strcoll()` | ICU sort keys (always safe) | `ucol_nextSortKeyPart()` | Full Unicode (CLDR/UCA); cross-platform consistent; requires `--with-icu` |
| `builtin` | none (memcmp) | byte comparison | byte comparison | None | Deterministic and portable; no OS dependency; no language-aware ordering |

## String Comparison and Sort Key APIs

The exported comparison API uses provider-independent wrappers:

| Function | Description |
|---|---|
| `pg_strcoll()` | Compare two nul-terminated strings |
| `pg_strncoll()` | Compare two strings by length (handles non-nul-terminated Postgres text) |
| `pg_strxfrm()` | Generate a sort key from a nul-terminated string |
| `pg_strnxfrm()` | Generate a sort key from a length-delimited string |
| `pg_strxfrm_prefix()` | Generate a prefix-comparable sort key (ICU only) |

Each function dispatches on `locale->provider`. For libc, `pg_strcoll_libc()` calls `strcoll_l()` (or `strcoll()` for the default locale). On Windows with UTF-8 databases, libc cannot handle UTF-8 directly, so the strings are converted to UTF-16 using `MultiByteToWideChar()` and compared with `wcscoll_l()`.

For ICU, `pg_strncoll_icu()` calls `ucol_strcollUTF8()` directly when the database encoding is UTF-8 (available since ICU 50, checked via `HAVE_UCOL_STRCOLLUTF8`). For other encodings, `pg_strncoll_icu_no_utf8()` first converts the strings to `UChar` (ICU's UTF-16 internal format) using the session-level `icu_converter` (`UConverter`) object, then calls `ucol_strcoll()`. The converter is initialised once per session in `init_icu_converter()`.

By default, `pg_strxfrm_enabled()` disables sort keys via `pg_strxfrm()`/`pg_strnxfrm()` for libc, returning false unless the build defines `TRUST_STRXFRM`. The reason is a known glibc bug where `strxfrm()` and `strcoll()` produce inconsistent orderings for many locales. ICU does not have this problem, so `pg_strxfrm_enabled()` always returns true for ICU locales.

The prefix variant `pg_strxfrm_prefix()` is ICU-only and uses `ucol_nextSortKeyPart()`. It generates a byte string comparable with `memcmp()`, which the index AM can use for prefix-range queries without computing a full sort key.

## Nondeterministic Collations

ICU collations can be created as nondeterministic (`collisdeterministic = false`). In a nondeterministic collation, strings that compare equal under `ucol_strcoll()` may not be byte-for-byte identical. `pg_locale_deterministic()` exposes this flag. Callers such as the equality operator and B-tree code use it to decide whether they need to fall back to a byte-level tiebreaker after a locale-aware comparison returns zero.

## Collation Versioning

Collation behaviour can change when the OS or ICU library is updated, silently invalidating existing index orderings. `get_collation_actual_version()` queries the current version string from the provider:

- For ICU, it opens a `UCollator` and calls `ucol_getVersion()`.
- For glibc, it uses `gnu_get_libc_version()` (a proxy for the locale data version).
- For FreeBSD, it calls `querylocale()` with `LC_VERSION_MASK`.
- For Windows, it calls `GetNLSVersionEx()`.

`pg_newlocale_from_collation()` compares this actual version against the `collversion` field stored in `pg_collation` at collation creation time. A mismatch emits a WARNING advising the user to rebuild affected objects and run `ALTER COLLATION ... REFRESH VERSION`.

## ICU String Conversion Utilities

Two public functions handle the boundary between PostgreSQL's database encoding and ICU's internal `UChar` representation:

- `icu_to_uchar()` converts a database-encoded string to a palloc'd `UChar` array.
- `icu_from_uchar()` converts a `UChar` array back to a palloc'd database-encoded string.

Both delegate to the session-level `icu_converter` (initialised in `init_icu_converter()`). The converter is created once from the database encoding name mapped through `get_encoding_name_for_icu()`. Using a single persistent converter avoids the overhead of opening a new one for each string operation.

`icu_language_tag()` converts a raw locale string (which may be in POSIX, .NET, or BCP 47 format) to a canonical BCP 47 language tag via `uloc_toLanguageTag()`. This canonicalisation ("level 2") ensures that locale strings with different surface representations but identical semantics are treated uniformly before being passed to `ucol_open()`. `icu_validate_locale()` performs a best-effort sanity check — extracting the language component and verifying it against `uloc_countAvailable()` — with severity controlled by the `icu_validation_level` GUC.

## Related Topics

- [[subsystems/types/encoding-utilities|Multibyte Encoding Utilities]] — database encoding conversions used when copying locale strings into the server encoding

---
title: "Date and Time Types"
aliases:
  - datetime types
  - timestamp internals
  - interval internals
source_files:
  - src/backend/utils/adt/timestamp.c
  - src/backend/utils/adt/date.c
  - src/backend/utils/adt/datetime.c
symbols:
  - Timestamp
  - TimestampTz
  - DateADT
  - TimeADT
  - TimeTzADT
  - Interval
  - timestamp2tm
  - tm2timestamp
  - date2j
  - j2date
  - ParseDateTime
  - DecodeDateTime
---

PostgreSQL's date and time family covers six SQL types — `date`, `time`, `timetz`, `timestamp`, `timestamptz`, and `interval` — plus the special values `-infinity` and `infinity`. All point-in-time types share a single epoch (2000-01-01). They store sub-second precision in integer microseconds. They delegate text parsing through a common tokeniser/decoder pipeline defined in `datetime.c`. Understanding the physical layout of each type and the invariants the parser imposes is essential for anyone extending the type system or debugging temporal edge cases.

## Physical storage layout

Each type uses a compact integer representation. None of them stores its value as a text string or a floating-point number.

| Type | C typedef | Size | Unit |
|------|-----------|------|------|
| `date` | `DateADT` (`int32`) | 4 bytes | days since 2000-01-01 |
| `time` | `TimeADT` (`int64`) | 8 bytes | microseconds since midnight |
| `timetz` | `TimeTzADT` (struct) | 12 bytes | `time` int64 + `zone` int32 (seconds west) |
| `timestamp` | `Timestamp` (`int64`) | 8 bytes | microseconds since 2000-01-01 00:00:00 |
| `timestamptz` | `TimestampTz` (`int64`) | 8 bytes | same, always UTC |
| `interval` | `Interval` (struct) | 16 bytes | see below |

The epoch choice of 2000-01-01 differs from the Unix epoch (1970-01-01). The offset between the two is `POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE` = 10957 days = 946,684,800 seconds. PostgreSQL applies this offset whenever it converts to or from a `time_t` (timestamp.c, `time_t_to_timestamptz()`).

`Timestamp` and `TimestampTz` are the same underlying `int64`. The type system distinguishes them only by OID. `timestamptz` values are always normalised to UTC. Timezone conversion happens on input (in `timestamptz_in()`) and on output (in `timestamp2tm()` via `pg_localtime()`), never at rest.

The reserved sentinel values for infinity use the maximum and minimum `int64` values: `DT_NOEND = PG_INT64_MAX` and `DT_NOBEGIN = PG_INT64_MIN`. For `date`, the same convention uses `PG_INT32_MAX` and `PG_INT32_MIN` (date.h, `DATEVAL_NOBEGIN`/`DATEVAL_NOEND`).

## The Interval struct and its three-part invariant

`interval` does not compress its fields into a single counter. Instead, it keeps three independent components (datatype/timestamp.h):

```
typedef struct {
    TimeOffset  time;   /* int64: hours/minutes/seconds in microseconds */
    int32       day;    /* days */
    int32       month;  /* months and years */
} Interval;
```

This separation is intentional. A day is not always 86,400 seconds (DST transitions). A month is not always 30 days. Flattening to microseconds would make `interval '1 month' + date` ambiguous. PostgreSQL keeps the three fields distinct until it adds an interval to a point-in-time value. At that point, the calendar context resolves the actual offsets.

Arithmetic on the `time` field uses `int64` overflow-safe helpers (`pg_mul_s64_overflow`, `pg_add_s64_overflow`) because `tm_hour` can legally exceed 24 inside an interval. The `pg_itm` struct used for conversion uses a 64-bit `tm_hour` field to accommodate this range (datatype/timestamp.h).

The `interval` typmod packs a **range** bitmask into its high 16 bits and a **precision** into its low 16 bits (timestamp.c, comment above `intervaltypmodin()`). The range bitmask encodes which fields may be non-zero. `INTERVAL YEAR TO MONTH` zeros the `day` and `time` fields on input. Precision controls sub-second rounding down to 0–6 decimal places, exactly as for `timestamp`.

## Julian-day arithmetic and the proleptic Gregorian calendar

All calendar conversions pass through two functions: `date2j()` (calendar → Julian day number) and `j2date()` (Julian day → calendar), both in `datetime.c`. The code comment notes that PostgreSQL applies Gregorian calendar rules for all years, even before the Gregorian reform of 1582. This is the proleptic Gregorian calendar required by the SQL standard. As a consequence, PostgreSQL rejects dates such as `1500-02-29`, even though they were valid Julian-calendar dates.

The valid Julian-day range for `date` is 0 to `DATE_END_JULIAN - 1` (approximately 4714-11-24 BC to 5874897-12-31 AD). For `timestamp`, the upper bound is lower because the int64 microsecond counter overflows earlier: 294276-12-31 AD (datatype/timestamp.h, `TIMESTAMP_END_JULIAN`).

## Text parsing pipeline

All six types share the same tokeniser/decoder infrastructure. The pipeline has two stages:

1. **`ParseDateTime()`** (datetime.c) — lexes the input string into at most `MAXDATEFIELDS` (25) tokens, each classified as `DTK_NUMBER`, `DTK_STRING`, or `DTK_SPECIAL`. `ParseDateTime()` stores tokens in `field[]` (char pointers into a working buffer), with their types in `ftype[]`.

2. **`DecodeDateTime()`** or **`DecodeInterval()`** (datetime.c) — iterates the token array, using a bitmask `fmask` to track which fields have been filled so far. Keyword lookup uses a binary-search over the static `datetktbl[]` table. `DecodeDateTime()` looks up timezone abbreviations separately in `zoneabbrevtbl`. It loads this table from the `timezone_abbreviations` configuration file.

The decoder recognises a rich vocabulary of special tokens: `epoch`, `infinity`, `-infinity`, `now`, `today`, `yesterday`, `tomorrow`, and `zulu`. The words `now`, `today`, `yesterday`, and `tomorrow` resolve to the current transaction start time or derived calendar values at parse time, so they yield stable results within a transaction.

For timezone resolution, `DecodeTimezoneNameToTz()` distinguishes three kinds of timezone specifications:
- **Fixed-offset abbreviations** (`UTC`, `EST`) — stored as `TZNAME_FIXED_OFFSET`; apply a constant offset.
- **Dynamic abbreviations** (`CET`, `CEST`) — stored as `TZNAME_DYNTZ`. The offset depends on whether DST is in effect, determined by `DetermineTimeZoneAbbrevOffset()`.
- **Full zone names** (`America/New_York`) — resolved by `DetermineTimeZoneOffset()` using the IANA timezone database.

The `DateStyle` and `IntervalStyle` GUCs control both input disambiguation and output format. `DateStyle = MDY` vs `DMY` vs `ISO` affects how the parser interprets ambiguous numeric-only inputs like `01/02/03`.

## The now() / CURRENT_TIMESTAMP distinction

`now()` and `CURRENT_TIMESTAMP` both call `GetCurrentTransactionStartTimestamp()`, which returns the timestamp captured at the start of the current transaction. PostgreSQL freezes this value for the duration of the transaction. This makes it stable and replayable.

`clock_timestamp()` calls `GetCurrentTimestamp()`. `GetCurrentTimestamp()` invokes `gettimeofday()` on every call, returning the wall-clock time at the moment of the call. `statement_timestamp()` returns the timestamp captured at the start of the current SQL statement (timestamp.c).

This three-level hierarchy — transaction start, statement start, and wall clock — is an intentional design. The stable transaction timestamp supports correctness in triggers and default expressions. The wall clock is available when real-time monitoring is needed.

## Typmod (precision) enforcement

`timestamp(n)` and `time(n)` accept a precision from 0 to 6. `anytimestamp_typmod_check()` and `anytime_typmod_check()` validate the precision on input. They cap the value at `MAX_TIMESTAMP_PRECISION` (6) with a warning rather than an error, if the user specifies something higher. PostgreSQL applies the precision by rounding the microsecond component to the specified number of decimal places, using precomputed scale tables (`IntervalScales[]`, `IntervalOffsets[]`). PostgreSQL performs rounding with round-half-up semantics, implemented as divide-then-multiply after adding half the scale value.

## Cross-type casting and comparison

The type hierarchy for implicit casts is `date` < `timestamp` < `timestamptz`. Casting a `date` to `timestamptz` sets the time to midnight. It applies the session timezone, not UTC (date.c, `date2timestamptz_opt_overflow()`). This means that `date '2024-01-01' = timestamptz '2024-01-01 00:00:00 UTC'` can be false in non-UTC sessions. PostgreSQL implements cross-type comparisons between `date` and `timestamp`/`timestamptz` directly, without going through the cast system, to avoid the precision loss from converting to `double` in intermediate steps (date.c, `date_cmp_timestamp_internal()`).

## Related Topics

- [[subsystems/types/base-types|Base Types (CREATE TYPE)]] — how custom base types are registered and their I/O functions structured
- [[subsystems/storage/toast|TOAST]] — not directly used by fixed-width datetime types, but relevant if you store them inside arrays or composite types that may be TOASTed

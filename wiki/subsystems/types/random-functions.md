---
title: "Random and Pseudorandom Functions"
aliases:
  - random()
  - setseed()
  - drandom
  - pseudorandom functions
  - random_normal
  - pg_prng
source_files:
  - src/backend/utils/adt/pseudorandomfuncs.c
  - src/common/pg_prng.c
  - src/include/common/pg_prng.h
symbols:
  - prng_state
  - initialize_prng
  - setseed
  - drandom
  - drandom_normal
  - int4random
  - int8random
  - numeric_random
---

PostgreSQL's random number functions are backed by a per-backend pseudorandom number generator (PRNG) seeded from high-quality OS entropy. The functions in `pseudorandomfuncs.c` expose this PRNG to SQL and maintain a single shared generator state within each backend. As a result, all random calls within one session draw from the same sequence. The underlying generator is `pg_prng`, a Xorshift-based algorithm in `src/common/pg_prng.c`.

PostgreSQL 17 extended the SQL surface significantly. `random()` and `setseed()` existed in earlier versions, but `random_normal()`, `random(min, max)` for integers, and `random(min, max)` for numeric values are new in PG17.

## Seeding

The generator is initialized lazily on first use by `initialize_prng()`. `pg_prng_strong_seed()` succeeds on any platform that provides `/dev/urandom` or an equivalent. When it succeeds, it seeds from OS entropy and produces a different sequence every time. If OS entropy is unavailable, the fallback mixes the current `TimestampTz` with the backend PID to produce a weaker but distinct seed per process.

`setseed(double)` accepts a value in `[-1.0, 1.0]` and deterministically seeds the generator via `pg_prng_fseed()`. This is useful for reproducible query results in tests. Calling `setseed()` resets the sequence. Subsequent calls to `random()` then produce the same values as any other session that used the same seed.

## Functions

| SQL function | Underlying call | Description |
|---|---|---|
| `random()` | `pg_prng_double()` | Uniform float8 in [0.0, 1.0) |
| `random_normal(mean, stddev)` | `pg_prng_double_normal()` | Float8 from a normal distribution |
| `random(min, max)` for int4 | `pg_prng_int64_range()` | Uniform int4 in [min, max] |
| `random(min, max)` for int8 | `pg_prng_int64_range()` | Uniform int8 in [min, max] |
| `random(min, max)` for numeric | `random_numeric()` | Uniform numeric in [min, max] |
| `setseed(double)` | `pg_prng_fseed()` | Set PRNG seed |

`drandom_normal()` uses the Box-Muller transform (implemented in `pg_prng_double_normal()`) to convert uniform samples into normally distributed values, then scales and shifts the result to the requested mean and standard deviation.

For integer ranges, `pg_prng_int64_range()` produces an unbiased result even when the range is not a power of two, avoiding the modulo bias present in naive implementations.

## Per-Backend State

The PRNG state (`prng_state`) is a process-local variable. It is not shared between backends and is not affected by transactions. Parallel workers get their own independent PRNG state, initialized independently. There is no mechanism to share or synchronize random sequences across parallel workers or sessions.

Because the state is local, calling `random()` from multiple concurrent sessions produces independent sequences, even when the sessions share the same seed via `setseed()`. The sessions are separate processes with separate state.

## Related Topics

- [[subsystems/types/crypto-hash-functions]] — cryptographic hash functions available in SQL
- [[subsystems/types/numeric-scalar-types]] — the numeric type used by the range-based random function

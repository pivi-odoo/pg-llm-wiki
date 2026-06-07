---
title: "Datum Utilities"
aliases:
  - datumCopy
  - datum copy
  - datum utilities
  - ascii conversion
  - pg_to_ascii
  - version function
  - geometric selectivity stubs
source_files:
  - src/backend/utils/adt/datum.c
  - src/backend/utils/adt/ascii.c
  - src/backend/utils/adt/version.c
  - src/backend/utils/adt/geo_selfuncs.c
symbols:
  - datumCopy
  - datumGetSize
  - datumFree
  - datumIsEqual
  - datumTransfer
  - datum_image_eq
  - datum_image_hash
  - datumSerialize
  - datumRestore
  - datumEstimateSpace
  - btequalimage
  - pg_to_ascii
  - ascii_safe_strlcpy
  - pgsql_version
  - areasel
  - areajoinsel
  - positionsel
  - contsel
---

Several small files in `src/backend/utils/adt/` implement infrastructure that does not belong to any particular type: generic operations over the `Datum` abstraction, a lossy ASCII downgrade for multibyte strings, the `version()` SQL function, and placeholder selectivity estimators for geometric operators. These utilities are foundational in the sense that they work uniformly across the type system rather than being specific to any one data type.

## Type-agnostic Datum operations

PostgreSQL's `Datum` type is an opaque word-sized integer. Whether it holds an actual value (pass-by-value) or a pointer to heap-allocated data (pass-by-reference) depends entirely on the type's `typbyval` flag and `typlen` attribute from `pg_type`. Any code that stores or compares a `Datum` without knowing the concrete type must carry these two pieces of metadata alongside the value.

`datum.c` provides the canonical set of operations for this pattern. `datumGetSize()` decodes the size of a datum by switching on the `typLen` encoding:

- positive `typLen`: the datum is exactly that many bytes (by-value or fixed-length by-ref)
- `typLen == -1`: the datum points to a [[subsystems/storage/toast|varlena]] whose actual length is read from its header via `VARSIZE_ANY()`
- `typLen == -2`: the datum points to a null-terminated C string; size is `strlen() + 1`

`datumCopy()` builds on this to produce a palloc'd copy suitable for use in [[subsystems/memory/contexts|memory contexts]] that outlive the current expression evaluation. For by-value types, the function simply returns the `Datum` unchanged — the value is self-contained. For varlena types it copies the raw bytes as-is, preserving any TOAST compression or pointer indirection. So the copy remains a compressed datum, not a decompressed one. The one exception is [[subsystems/types/expanded-datum|expanded objects]]: `datumCopy()` flattens them into a contiguous palloc'd buffer via `EOH_flatten_into()`. It does this because expanded objects live in a child [[subsystems/memory/contexts|memory context]] that is typically about to be destroyed.

`datumTransfer()` is a lighter-weight variant intended for the case where the caller wants to move a datum into the current memory context without necessarily flattening it. For read-write expanded objects it calls `TransferExpandedObject()` to reparent the object rather than copying. For everything else it falls back to `datumCopy()`.

`datumIsEqual()` provides generic equality. For by-value types it compares the `Datum` words directly with `==`. For by-reference types it compares byte-for-byte with `memcmp()`. The function explicitly does not de-toast varlena values. The source comment notes that some callers may be running in the context of an aborted transaction, where detoasting is unsafe. So the comparison is over whatever representation the datum currently has. This means two logically identical varlena values in different toast representations may not compare as equal.

`datum_image_eq()` and `datum_image_hash()` are stricter variants that cast by-value datums to a specific integer width before comparing or hashing. This prevents false negatives caused by garbage in the high bits of a word when the value is narrower than `sizeof(Datum)`. This is a real hazard when one code path forms a datum and another compares it. The B-Tree deduplication infrastructure and hash aggregation both use these functions.

`datumSerialize()` and `datumRestore()` implement a compact wire format for passing datums between processes (for example, across parallel query workers). The format prepends a 4-byte header that encodes nullness, by-value status, or byte length. This allows the receiver to reconstruct the datum with a single `palloc()` and `memcpy()`.

These functions appear throughout the executor. The executor calls `datumCopy()` when caching aggregate transition values between input rows, materialising tuple slots into a longer-lived context, and building GROUP BY hash keys from expression results.

## Lossy ASCII downgrade

`ascii.c` implements `pg_to_ascii()`, which converts a string from a multibyte encoding to ASCII. It replaces code points above 127 with their closest printable ASCII approximation from a lookup table, or with a space when no approximation is defined. The supported source encodings are ISO-8859-1, ISO-8859-2, ISO-8859-15, and Windows CP1250. Any other encoding raises an error.

The conversion is intentional lossy: it differs from the general [[subsystems/types/encoding-utilities|encoding conversion]] infrastructure, which raises an error for unmappable code points. The SQL `to_ascii()` function is for applications that need a degraded representation for systems that cannot handle non-ASCII characters. It is not for round-trippable transcoding.

`ascii_safe_strlcpy()` is a related helper that replaces any non-printable-ASCII byte (including high bytes) with `'?'`. Unlike `pg_to_ascii()`, it is safe to call during postmaster startup, before the error reporting infrastructure is available. This is because it never calls `ereport()`.

## The version() function

`version.c` is a single function, `pgsql_version()`, that returns the compile-time constant `PG_VERSION_STR` as a SQL text value. The version string encodes the PostgreSQL release, build platform, compiler, and compile-time options. Applications that need to detect the server version programmatically at runtime can call `SELECT version()` rather than parsing `server_version_num`. This is useful when the full platform string matters, for example when debugging crashes on a specific OS or compiler.

## Geometric operator selectivity stubs

`geo_selfuncs.c` registers the `oprrest` and `oprjoin` selectivity functions that the catalog records for geometric operators on `point`, `box`, `polygon`, and `circle`. The file's own header comment says these are "totally bogus". The implementations return a fixed constant regardless of the operator, the column statistics, or the operand values:

- `areasel` / `areajoinsel` (overlap-style operators): 0.005
- `positionsel` / `positionjoinsel` (strict left/right/above/below): 0.1
- `contsel` / `contjoinsel` (containment): 0.001

The values are deliberately small to bias the planner toward using a GiST index on a geometric column when one exists. GiST index scans for geometric operators must traverse multiple subtrees to guarantee completeness. Because of this, the planner's cost model would underestimate their cost even with accurate selectivity numbers. So the approximation causes little additional harm in practice.

Extensions such as PostGIS provide accurate selectivity estimation for geometry in production deployments. They supply custom statistics collection and operator estimator functions that replace these stubs for PostGIS geometry columns.

## See also

- [[subsystems/types/variable-length-types|Variable-length types and varlena]]
- [[subsystems/types/expanded-datum|Expanded datum and read-write objects]]
- [[subsystems/storage/toast|TOAST storage]]
- [[subsystems/memory/contexts|Memory contexts]]
- [[subsystems/types/encoding-utilities|Encoding utilities]]
- [[subsystems/types/geometric-types|Geometric types]]

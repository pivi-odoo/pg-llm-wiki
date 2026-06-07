---
title: "Expanded Records"
aliases:
  - "Expanded Datum"
  - "ExpandedRecordHeader"
  - "ExpandedObjectHeader"
tags:
  - theme/storage-format
source_files:
  - src/backend/utils/adt/expandedrecord.c
  - src/include/utils/expandedrecord.h
  - src/include/utils/expandeddatum.h
symbols:
  - ExpandedObjectHeader
  - ExpandedRecordHeader
  - make_expanded_record_from_typeid
  - make_expanded_record_from_datum
  - deconstruct_expanded_record
  - expanded_record_get_field
  - expanded_record_set_field_internal
  - ER_flatten_into
  - ER_get_flat_size
  - EOH_init_header
  - EOHPGetRWDatum
  - EOHPGetRODatum
---

Expanded records are an in-memory representation of [[subsystems/types/composite-types|composite types]] designed for efficient field access in procedural languages such as PL/pgSQL. Rather than re-parsing a packed heap tuple every time a field is read, an expanded record deconstructs the tuple once. It caches the individual field `Datum` values, turning repeated field reads into direct array indexing.

## The flat-vs-expanded duality

PostgreSQL's varlena machinery supports two representations of any value that opts in. A *flat* form is a contiguous byte blob, usable on disk or in a heap tuple. An *expanded* form lives only in memory and trades space for computational convenience. The two forms can coexist inside a single `ExpandedRecordHeader` at the same time. The flag bits `ER_FLAG_FVALUE_VALID` and `ER_FLAG_DVALUES_VALID` track which representations are currently valid.

A flat composite value is just a `HeapTuple`-formatted blob: a header with a null bitmap, followed by attribute data packed according to alignment rules. Reading field `n` from it requires calling `heap_deform_tuple()`. This function scans every preceding attribute. This is acceptable for a single read. But a PL/pgSQL function that reads and rewrites multiple fields of a row variable would pay that cost on every access. An expanded record avoids this by deconstructing the flat tuple once into a `dvalues[]` / `dnulls[]` array pair. It then serves subsequent reads from that array.

References to expanded objects use a special class of TOAST pointer. Because all varlena values must be representable as a contiguous blob when stored, every expanded object must also be able to produce a flat form on demand.

## ExpandedObjectHeader: the protocol all expanded objects share

`ExpandedObjectHeader` (defined in `expandeddatum.h`) is the common prefix that any expanded object must embed as its first member. It contains:

- `eoh_methods` — a pointer to an `ExpandedObjectMethods` struct with two function pointers. `get_flat_size` returns the byte count of the flat representation. `flatten_into` writes the flat form into caller-allocated space.
- `eoh_context` — the [[subsystems/memory/contexts|memory context]] that owns the header and all subsidiary data. Freeing the object is as simple as deleting this context.
- `eoh_rw_ptr` / `eoh_ro_ptr` — pre-built read-write and read-only TOAST pointers to the object, stored inside the header to avoid extra allocations when returning pointers.

The phony `vl_len_` field at the start of the struct always holds `EOH_HEADER_MAGIC` (-1), a value that is impossible in a real 4-byte-header varlena datum. Code that might receive either a flat or an expanded datum can inspect this field with `VARATT_IS_EXPANDED_HEADER()` to distinguish the two cases.

`EOH_init_header()` initializes these fields. It also plants both TOAST pointers so they refer back to the header. All generic operations — `EOH_get_flat_size()`, `EOH_flatten_into()`, `TransferExpandedObject()`, `DeleteExpandedObject()` — go through `eoh_methods` and `eoh_context`, knowing nothing about the concrete type.

## ExpandedRecordHeader: fields and flags

`ExpandedRecordHeader` embeds `ExpandedObjectHeader hdr` as its first field, then extends it with record-specific state:

**Type identity**: `er_decltypeid` holds the declared type, which may be a domain. `er_typeid` / `er_typmod` identify the underlying composite type. They always match `er_tupdesc->tdtypeid` / `tdtypmod`. `er_tupdesc_id` is a process-unique integer that changes whenever the `TupleDesc` is replaced. This lets callers detect schema changes cheaply.

**Flat representation**: `fvalue` points to a `HeapTuple` if one is available. `fstartptr` / `fendptr` bracket the data area of that tuple. These boundaries distinguish Datum pointers that point into the flat tuple from those that point to separately palloc'd copies. This distinction matters when freeing old field values during a write.

**Deformed representation**: `dvalues` and `dnulls` are arrays of length `nfields` allocated inside `eoh_context`. For pass-by-reference fields, entries in `dvalues` may point either into the flat tuple's data area (when the tuple is still valid and unmodified) or to separately palloc'd chunks inside `eoh_context` (after a field has been written).

**Flag bits** control which representations are valid and what cleanup is needed:

| Flag | Meaning |
|------|---------|
| `ER_FLAG_FVALUE_VALID` | `fvalue` is current |
| `ER_FLAG_FVALUE_ALLOCED` | `fvalue` is private storage, must be freed |
| `ER_FLAG_DVALUES_VALID` | `dvalues`/`dnulls` are current |
| `ER_FLAG_DVALUES_ALLOCED` | some pass-by-ref `dvalues` entries are private copies |
| `ER_FLAG_HAVE_EXTERNAL` | some field values are out-of-line TOAST pointers |
| `ER_FLAG_TUPDESC_ALLOCED` | `er_tupdesc` is a private copy, not just a refcount bump |
| `ER_FLAG_IS_DOMAIN` | `er_decltypeid` is a domain; constraints must be checked on write |

## The deformed cache

When a field read is requested and `ER_FLAG_DVALUES_VALID` is not set, PostgreSQL calls `deconstruct_expanded_record()`. It fetches the `TupleDesc`, calls `heap_deform_tuple()` on `fvalue`, and sets `ER_FLAG_DVALUES_VALID`. From that point on, `expanded_record_get_field()` — inlined in the header — can return a field with two array reads:

```c
*isnull = erh->dnulls[fnumber - 1];
return erh->dvalues[fnumber - 1];
```

Only assigning a whole new tuple via `expanded_record_set_tuple()` invalidates the cache (`ER_FLAG_DVALUES_VALID` cleared as `ER_FLAG_FVALUE_VALID` becomes stale). When an individual field is written via `expanded_record_set_field_internal()`, the opposite happens. `expanded_record_set_field_internal()` clears `ER_FLAG_FVALUE_VALID`, because the flat form is now stale. `ER_FLAG_DVALUES_VALID` remains set, so subsequent field reads still hit the array.

Both forms can be valid simultaneously. This happens after deconstruction, when no fields have been written yet. The flat tuple is still accurate. The `dvalues` array holds pointers into it. In this state, callers can still use `fvalue` to fetch system-column values, which are only accessible from the flat form. They read user fields from `dvalues` instead.

## Read-only vs read-write pointers

The expanded-object protocol defines two kinds of TOAST pointer: read-only (RO) and read-write (RW). The distinction is a contract about ownership and mutation rights.

`EOHPGetRODatum()` returns a pointer that the caller must not write through. Any callee receiving an RO pointer must treat the value as immutable. If it needs to modify the record, it must first copy it.

`EOHPGetRWDatum()` returns a pointer that authorizes in-place modification. A callee receiving an RW pointer may call `expanded_record_set_field_internal()` directly on the header, rather than copying the entire record. This is how PL/pgSQL assignment to a row variable works efficiently: the variable holds an RW pointer. Field assignments therefore update the existing expanded record in place, rather than copying it.

The `MakeExpandedObjectReadOnly()` macro downgrades an RW pointer to RO by checking the TOAST pointer tag. `DatumIsReadWriteExpandedObject()` tests whether a datum already carries the RW tag. Functions that only read a composite argument should always work from an RO reference. This avoids accidentally triggering the copy-avoidance path in a caller that expected to retain exclusive ownership.

## Memory lifecycle

Each expanded record lives in a dedicated `AllocSetContext` created as a child of the `parentcontext` passed to the construction functions. This context holds all subsidiary storage — the `dvalues`/`dnulls` arrays, separately palloc'd field copies, the flat `HeapTuple`, and the short-lived context used for detoasting.

Destroying the record requires only deleting `eoh_context`. PL/pgSQL does this when a row variable goes out of scope or is overwritten. The executor calls `DeleteExpandedObject()` (or simply resets the variable's owning context). This deletes `eoh_context` and everything inside it.

`TransferExpandedObject()` (or the `TransferExpandedRecord` macro) reparents `eoh_context` to a different parent context. This is how PL/pgSQL moves a row variable from a short-lived per-statement context into a longer-lived function context, when needed.

`TupleDesc` lifetime is managed separately. When `er_tupdesc` comes from the type cache's refcounted copy, a `MemoryContextCallback` registered on `eoh_context` decrements the refcount. This callback fires when the context is destroyed. This avoids depending on `CurrentResourceOwner`, which may have a shorter or longer lifetime than the expanded record.

## Converting between flat and expanded forms

**Expanded from flat**: `make_expanded_record_from_datum()` takes an existing composite `Datum`, copies it as a `HeapTuple` into a fresh context, and stores it as the flat form. It deliberately defers `TupleDesc` lookup and array allocation until the first field access, since callers may never need them. `make_expanded_record_from_typeid()` and `make_expanded_record_from_tupdesc()` create an initially empty record (logically equivalent to a NULL composite), pre-allocating the `dvalues`/`dnulls` arrays alongside the header in a single allocation to save a palloc round-trip.

**Flat from expanded**: The `ER_get_flat_size()` / `ER_flatten_into()` method pair implement the `ExpandedObjectMethods` interface. If the flat form is still valid and contains no out-of-line (external TOAST) fields, `ER_flatten_into()` simply `memcpy`s the stored `HeapTuple` data — a near-zero-cost path. If the flat form is stale, `ER_get_flat_size()` triggers deconstruction. It resolves any external TOAST values via `expanded_record_set_field_internal()`, then computes the packed size using `heap_compute_data_size()`, and caches the result in `erh->flat_size`. `ER_flatten_into()` then writes the header fields and calls `heap_fill_tuple()` to pack the `dvalues`/`dnulls` arrays into the output buffer. The cached `flat_size` means that the two-call pattern required by the protocol (size, then write) does not recompute the layout.

## See also

- [[subsystems/types/composite-types|composite types]] — the catalog representation and anonymous record types that expanded records operate on
- [[subsystems/memory/contexts|memory context]] — allocation and lifetime management underlying the expanded object design
- [[subsystems/storage/toast|TOAST]] — the out-of-line storage mechanism that expanded records must handle when flattening

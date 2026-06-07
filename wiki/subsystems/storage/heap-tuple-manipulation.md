---
title: "Heap Tuple Construction and Deforming"
aliases:
  - "heap_form_tuple"
  - "heap_deform_tuple"
  - "heaptuple.c"
  - "TupleDesc"
  - "tuple descriptor"
tags:
  - theme/storage-format
source_files:
  - src/backend/access/common/heaptuple.c
  - src/backend/access/common/tupdesc.c
  - src/include/access/tupdesc.h
symbols:
  - heap_form_tuple
  - heap_deform_tuple
  - heap_modify_tuple
  - heap_copytuple
  - heap_freetuple
  - heap_getattr
  - fastgetattr
  - nocachegetattr
  - TupleDescData
  - TupleDescAttr
  - CreateTemplateTupleDesc
  - TupleDescInitEntry
  - getmissingattr
  - heap_fill_tuple
  - heap_compute_data_size
---

The in-memory API in `heaptuple.c` and `tupdesc.c` sits one level above the on-disk format described in [[subsystems/storage/heap|heap.md]]: it provides the tools for packing a Datum array into a `HeapTupleData`, navigating individual attributes within that structure, and producing modified copies. Every executor node, catalog access routine, and trigger function that reads or writes tuple columns goes through this layer. The schema descriptor (`TupleDesc`) is the glue that makes all of this possible — it carries the per-column type metadata that tells the deforming code where each attribute begins and how wide it is.

## TupleDesc: the schema descriptor

A `TupleDescData` struct (`tupdesc.h`) is a flat allocation that combines a fixed header with an inline array of `FormData_pg_attribute` entries — one per user column. Each `Form_pg_attribute` is effectively a row from `pg_attribute`, carrying the fields that matter for data layout: `atttypid`, `attlen`, `attbyval`, `attalign`, `attstorage`, and `attcacheoff`. The `TupleDescAttr(tupdesc, i)` macro resolves to a pointer into this inline array. This avoids any indirection through a pointer-of-pointers.

Two fields on `TupleDescData` govern the tuple's type identity. `tdtypeid` is the composite type OID (or `RECORDOID` for anonymous row types). `tdtypmod` is the typmod; it allows the type cache to distinguish different anonymous record shapes. When a tupdesc corresponds to a named rowtype such as a table, `tdtypeid` holds that type's OID and `tdtypmod` is `-1`.

Constraints are optional. PostgreSQL segregates them into a `TupleConstr *constr` pointer so that the majority of tupdescs — those built transiently by the executor — can leave it NULL without wasting space. When present, `TupleConstr` holds default expressions (`AttrDefault`), check constraints (`ConstrCheck`), and missing-value entries (`AttrMissing`). Missing values are the runtime representation of `ALTER TABLE ADD COLUMN ... DEFAULT` on pre-existing rows: instead of rewriting every tuple, the catalog records the default. The deforming code synthesizes it when it encounters a tuple that has fewer stored attributes than the current descriptor.

### Reference counting and lifetime

Tupdescs that live in caches — the relcache and typcache — are reference-counted via `tdrefcount`. Callers that hold a reference obtained through `IncrTupleDescRefCount()` / `PinTupleDesc()` must release it with `DecrTupleDescRefCount()` / `ReleaseTupleDesc()`. When the count reaches zero, `FreeTupleDesc()` releases the allocation and all associated constraint data. Tupdescs created transiently by the executor (for join result types, function return types, etc.) are not reference-counted: `tdrefcount` is set to `-1`. They are freed implicitly when the [[subsystems/memory/contexts|memory context]] they were palloc'd in is destroyed. The `PinTupleDesc` and `ReleaseTupleDesc` macros check `tdrefcount >= 0` before touching the counter, so callers can treat both kinds uniformly.

The [[subsystems/memory/resource-owner|ResourceOwner]] mechanism tracks pinned tupdescs and will release them during error recovery, preventing descriptor leaks when an error unwinds through code that holds cache references.

### attcacheoff: the offset cache

The most performance-sensitive field on `Form_pg_attribute` is `attcacheoff`. It starts at `-1` (uncached). `nocachegetattr()` and `heap_deform_tuple()` fill it in lazily as those routines walk through attributes for the first time. Once set, it holds the byte offset of that attribute from the start of the tuple's data region (`GETSTRUCT(tup)`). This lets subsequent accesses jump directly to the attribute without scanning preceding columns.

Caching is conservative: the deforming code only stores an offset when it can guarantee the offset will be valid for any future tuple with this descriptor. The conditions are strict — no preceding nulls, no preceding variable-width attributes. Both nulls and short varlenas make the offset unpredictable across tuples: nulls have no storage, and short varlenas may or may not have alignment padding, depending on their actual content. Once the deform loop encounters any variable-width or nullable attribute, it sets the `slow` flag. No further caching happens for that pass, though earlier entries already written remain valid.

Attcacheoff values stored in a tupdesc are shared across all tuples using that descriptor. Filling them in once therefore amortises their cost across many tuple reads. However, because `attcacheoff` is mutable state on a shared descriptor, code that copies a tupdesc and re-uses it in a different column arrangement must reset these fields — `TupleDescCopyEntry()` does this explicitly.

## Forming tuples from Datum arrays

`heap_form_tuple()` (`heaptuple.c`) takes a `TupleDesc` and parallel `Datum[]` / `bool[]` arrays and produces a single palloc'd block containing both the `HeapTupleData` wrapper and the `HeapTupleHeaderData` plus attribute data immediately following. `heap_form_tuple()` sets the `HeapTupleData.t_data` pointer to point into this same block, so the tuple is fully self-contained and can be freed with a single `pfree()` via `heap_freetuple()`.

The construction proceeds in two measurement-then-fill phases. First, `heap_compute_data_size()` iterates over all non-null attributes to sum their storage sizes, accounting for alignment padding and the possibility of converting a 4-byte-header varlena to a 1-byte short form. Then a single `palloc0` allocates the total. The code writes the null bitmap if any column is null. `heap_fill_tuple()` writes each attribute value in order. It handles four storage classes: pass-by-value scalars (stored directly), varlena with a 1-byte header (no alignment, packed), varlena with a 4-byte header (aligned), and fixed-length pass-by-reference (aligned copy). `heap_fill_tuple()` converts short varlenas whose `attstorage` is not `TYPSTORAGE_PLAIN` to the 1-byte-header form at pack time (`ATT_IS_PACKABLE`, `heaptuple.c`). This shrinks in-memory tuples and reduces write amplification.

`heap_fill_tuple()` simultaneously builds the null bitmap and sets the relevant `t_infomask` bits: `HEAP_HASNULL` if any column is null, `HEAP_HASVARWIDTH` if any column is variable-length, and `HEAP_HASEXTERNAL` if any column is an out-of-line [[subsystems/storage/toast|TOAST]] pointer. `heap_form_tuple()` flattens expanded object datums (arrays or other expanded representations held in an expanded-object memory context) into the tuple at form time. This makes the resulting tuple a pure byte-array snapshot, independent of the expanded object's lifetime.

The result is a palloc'd tuple with `t_self` set to `InvalidItemPointer` and `t_tableOid` to `InvalidOid`. It is not yet a disk tuple: transaction visibility fields (`t_xmin`, `t_xmax`, `t_cid`) are zeroed. The datum-format header fields (`tdtypeid`, `tdtypmod`) are filled in, however, so that the tuple can also function as a composite-type datum without further conversion.

## Deforming tuples into Datum arrays

`heap_deform_tuple()` is the inverse of `heap_form_tuple()`. It walks the tuple's attribute data from left to right. It extracts each attribute into a caller-provided `Datum[]` and `bool[]`. For attributes that are not null and have a cached `attcacheoff`, the walk short-circuits to a direct fetch. For pass-by-reference types, the returned Datum is a pointer into the tuple's data region; the caller is responsible for ensuring the tuple (and, if it is a buffer-backed tuple, the buffer pin) remains valid for the lifetime of those pointers.

When the tuple has fewer stored attributes than the descriptor — which happens when a column was added via `ALTER TABLE ADD COLUMN` after the row was written — `heap_deform_tuple()` fills the trailing entries from the descriptor's missing-value table via `getmissingattr()`. If there is no missing value, `heap_deform_tuple()` returns the column as NULL. This makes column addition with a default effectively O(1) at the storage level: existing rows do not need to be rewritten. PostgreSQL pays the cost at deform time rather than at DDL time.

The `slow` flag within the deform loop serves the same caching purpose as in `nocachegetattr()`. When all attributes so far are non-null and fixed-width, the loop can safely write `attcacheoff` values into the descriptor. This makes subsequent deforms of other tuples with the same schema faster. As soon as the loop encounters a null or variable-width attribute, it sets `slow`. The offset-writing stops. The loop then computes alignment from the actual byte content of the tuple.

## Single-attribute access

When only one or a few columns are needed, deforming the whole tuple is wasteful. `heap_getattr()` (`htup_details.h`) handles this. It dispatches system attributes to `heap_getsysattr()`, which reads from the `HeapTupleData` envelope and header fields without touching attribute data. Otherwise, it checks the null bitmap for quick null detection and falls through to `fastgetattr()`.

`fastgetattr()` is an inline function that provides the fast path: if the target attribute's `attcacheoff` is already populated and there are no nulls in the tuple, `fastgetattr()` can read the attribute with a single pointer arithmetic step and `fetchatt()`. When this fast path is not available — because `attcacheoff` is uncached, or because nulls make it unreliable — `fastgetattr()` calls `nocachegetattr()`. This function walks the attribute list from position 0. It populates `attcacheoff` entries for fixed-width leading columns as it goes.

The consequence of this design is that repeated single-attribute access in a loop over many tuples is O(1) per access for the leading fixed-width columns once the cache is warm, but O(N) per access for variable-width or post-null columns. Code that needs most of a tuple's columns should prefer `heap_deform_tuple()`, which pays the O(N) traversal once and returns all values, rather than calling `heap_getattr()` N times and paying O(N²).

## Copying and modifying tuples

`heap_copytuple()` produces an independent copy of a tuple as a single palloc block, identical in layout to what `heap_form_tuple()` would produce. This is the safe way to materialise a buffer-backed tuple — one where `t_data` points into a shared buffer page — before releasing the buffer pin. `heap_copytuple()` transfers all fields, including `t_self` and `t_tableOid`, from the original to the returned copy. Callers can therefore use the copy as a proxy for the original row identity even when the buffer is no longer pinned.

`heap_freetuple()` is simply `pfree()` on the `HeapTuple` pointer. This works because both `heap_copytuple()` and `heap_form_tuple()` allocate the wrapper and data in one block.

`heap_modify_tuple()` creates a new tuple that is a copy of an existing one but with selected columns replaced. It deforms the source tuple into a temporary `Datum[]` / `bool[]`, overlays the replacement values for columns where `doReplace` is true, and calls `heap_form_tuple()` on the result. `heap_modify_tuple()` copies the identification fields (`t_ctid`, `t_self`, `t_tableOid`) from the original to the new tuple, so that callers that use the new tuple for an update operation have the correct row identity. `heap_modify_tuple_by_cols()` is a variant that takes an array of target column numbers instead of a boolean replacement map. This is more convenient when a small fixed set of columns changes.

## Buffer-backed vs. palloc'd tuples

A critical distinction runs through all of this: a tuple's `t_data` pointer may point either into a shared buffer page or into a private palloc'd block. The two cases are functionally identical for read access, but have different lifetime and mutability rules.

The buffer manager owns a buffer-backed tuple. The caller must hold a pin on the buffer for as long as it holds a pointer into the page. It must not modify the tuple data without holding an exclusive content lock on the buffer. The `HeapTupleData` wrapper for a buffer-backed tuple is typically stack-allocated or allocated separately from the data it points to.

The caller's [[subsystems/memory/contexts|memory context]] owns a palloc'd tuple produced by `heap_form_tuple()` or `heap_copytuple()`. Callers can read it, pass it around, and free it without concern for buffer pins or locks. Because the wrapper and data are a single allocation, `heap_freetuple()` frees everything in one call. Callers that need to keep a tuple after releasing a buffer lock or closing a scan must always make a palloc'd copy first.

## MinimalTuple

`MinimalTuple` is a stripped variant that omits the transaction visibility fields at the front of `HeapTupleHeaderData`. It retains only `t_infomask2` onward. It shares the same data layout from that point. The same attribute-walking logic therefore works for both types after applying `MINIMAL_TUPLE_OFFSET` to compensate for the missing prefix. `heap_form_minimal_tuple()` accepts the same `TupleDesc` / `Datum[]` / `bool[]` arguments as `heap_form_tuple()` and produces this compact form. Executor nodes such as hash join and sort use this compact form to reduce memory pressure when accumulating intermediate rows. Conversion between the two representations is explicit: `heap_tuple_from_minimal_tuple()` and `minimal_tuple_from_heap_tuple()` copy bytes and adjust the header fields.

## Related Topics

- [[subsystems/storage/heap|Heap storage and tuple format]] — on-disk layout, infomask bits, HOT, insert/delete/update paths
- [[subsystems/storage/toast|TOAST]] — out-of-line attribute storage referenced by HEAP_HASEXTERNAL tuples
- [[subsystems/memory/contexts|Memory contexts]] — palloc'd tuple lifetimes and context-based deallocation
- [[subsystems/memory/resource-owner|ResourceOwner]] — tupdesc reference tracking during error recovery
- [[subsystems/executor/jit-llvm|JIT]] — uses TupleDesc and slot deforming paths as JIT compilation targets

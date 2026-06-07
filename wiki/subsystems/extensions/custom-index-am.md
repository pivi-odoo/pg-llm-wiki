---
title: "Custom Index Access Methods"
aliases:
  - "custom index AM"
  - "IndexAmRoutine"
  - "index AM extension"
  - "CREATE ACCESS METHOD"
  - "amapi.h"
source_files:
  - src/include/access/amapi.h
  - src/backend/access/index/indexam.c
  - src/backend/commands/indexcmds.c
  - src/backend/access/index/amapi.c
symbols:
  - IndexAmRoutine
  - GetIndexAmRoutine
  - ambuild
  - aminsert
  - ambulkdelete
  - amcostestimate
  - ambeginscan
  - amgettuple
  - amgetbitmap
  - amvalidate
  - amcanreturn
  - IndexScanDescData
---

# Custom Index Access Methods

Since PostgreSQL 9.6, extensions can introduce entirely new index types — not just new operator classes for existing types like B-tree or GiST, but entirely new storage and lookup strategies. Extensions register a custom index access method (AM) in the `pg_am` catalog. Once registered, it becomes immediately indistinguishable from built-in methods: users create it with `CREATE INDEX USING mymethod`, the planner uses it when it's cheaper, and VACUUM calls it the same way it calls nbtree or GiST. The `bloom` extension in the PostgreSQL contrib tree is the canonical reference implementation. pgvector's HNSW and IVFFlat AMs are prominent production examples.

The integration surface is the `IndexAmRoutine` struct defined in `src/include/access/amapi.h`. It is a flat table of function pointers plus a set of boolean capability flags. Once an AM returns this struct from its handler, the core system drives all index operations through those pointers — there are no special cases for custom versus built-in AMs.

## Registering an Access Method

Registration happens at the SQL level, typically inside the extension's install script:

```sql
CREATE FUNCTION mymethod_handler(internal) RETURNS index_am_handler
    AS 'MODULE_PATHNAME', 'mymethod_handler'
    LANGUAGE C STRICT;

CREATE ACCESS METHOD mymethod TYPE INDEX HANDLER mymethod_handler;
```

`mymethod_handler` is a C function that takes a single `internal` argument and returns `index_am_handler`. Its job is to palloc an `IndexAmRoutine`, fill every field, and return it. The handler follows the standard `PG_FUNCTION_INFO_V1` convention.

Users create indexes with the new method exactly as they would with a built-in one:

```sql
CREATE INDEX ON mytable USING mymethod (col1, col2);
```

The planner finds the AM's OID via `pg_am` and looks up `amhandler`. It calls `amhandler` to obtain the `IndexAmRoutine` and inspects the capability flags. `indexcmds.c` calls `GetIndexAmRoutine(accessMethodForm->amhandler)` during both `CREATE INDEX` validation and index compatibility checks during `ALTER TABLE`.

```c
PG_FUNCTION_INFO_V1(mymethod_handler);

Datum
mymethod_handler(PG_FUNCTION_ARGS)
{
    IndexAmRoutine *amroutine = makeNode(IndexAmRoutine);

    amroutine->amstrategies   = MY_NSTRATEGIES;
    amroutine->amsupport      = MY_NSUPPORT;
    amroutine->amcanorder     = false;
    amroutine->amcanunique    = false;
    amroutine->amcanmulticol  = true;
    /* ... more flags ... */

    amroutine->ambuild        = mymethod_build;
    amroutine->ambuildempty   = mymethod_buildempty;
    amroutine->aminsert       = mymethod_insert;
    /* ... more callbacks ... */

    PG_RETURN_POINTER(amroutine);
}
```

The core fetches this routine via `GetIndexAmRoutine(accessMethodForm->amhandler)` in `indexcmds.c` whenever `CREATE INDEX` needs to validate capabilities or plan execution. PostgreSQL stores the AM's OID in `pg_am`. The boolean capability flags from the routine (`amcanunique`, `amcanmulticol`, `amcanorder`, etc.) flow directly into DDL validation and planner decisions.

## The IndexAmRoutine Callback Table

The table below lists every callback in `IndexAmRoutine` (amapi.h). Callbacks marked **required** must be non-NULL; the others may be NULL if the feature is not supported.

| Callback | Required | Purpose |
|---|---|---|
| `ambuild` | Yes | Build the index from scratch by scanning the heap; called by `CREATE INDEX`. Returns an `IndexBuildResult` with tuple counts. |
| `ambuildempty` | Yes | Write an empty index into the relation's init fork; used by `CREATE INDEX CONCURRENTLY` before the heap scan phase. |
| `aminsert` | Yes | Insert one index entry for a heap tuple. Called on every `INSERT` and `UPDATE` that produces a new heap tuple. |
| `ambulkdelete` | Yes | Bulk-delete index entries for dead TIDs; called by VACUUM with a callback that answers whether each TID is dead. |
| `amvacuumcleanup` | Yes | Post-VACUUM cleanup: reclaim pages, update index statistics, reset bloom filters, etc. Called after `ambulkdelete`. |
| `amcostestimate` | Yes | Provide selectivity, startup cost, total cost, and correlation estimates to the planner for a proposed index scan. |
| `amoptions` | Yes | Parse and validate per-index `WITH (...)` storage options; returns a palloc'd `bytea` encoding the options. |
| `amvalidate` | Yes | Validate that an operator class is complete and correct for this AM; called by `CREATE OPERATOR CLASS`. |
| `ambeginscan` | Yes | Allocate and return an `IndexScanDesc` for a new scan; called before `amrescan`. |
| `amrescan` | Yes | (Re)initialize an existing scan with new scan keys and order-by keys; called at scan start and on rescan. |
| `amendscan` | Yes | Release all resources held by a scan; called when the scan is done. |
| `amgettuple` | If ordered scans supported | Fetch the next matching TID (and optionally column values) from an active scan, one tuple at a time. NULL if the AM only supports bitmap scans. |
| `amgetbitmap` | If bitmap scans supported | Fetch all matching TIDs into a `TIDBitmap` in one call; used by bitmap index scan nodes. NULL if not supported. |
| `amcanreturn` | No | Per-column check: can the AM return the stored column value without a heap fetch (index-only scan)? NULL means no. |
| `amproperty` | No | Report AM, index, or column properties to `pg_index_column_has_property()` and related functions. |
| `ambuildphasename` | No | Return a human-readable phase name for progress reporting during `ambuild`. |
| `amadjustmembers` | No | Adjust dependency types for operators and support functions being added to an opclass or opfamily. |
| `ammarkpos` | No | Mark the current scan position so it can be restored later. Required only if the executor needs mark/restore. |
| `amrestrpos` | No | Restore a previously marked scan position. |
| `amestimateparallelscan` | No | Return the size of the DSM area needed to coordinate a parallel scan. |
| `aminitparallelscan` | No | Initialize the DSM area for a parallel scan before workers start. |
| `amparallelrescan` | No | Reset the parallel scan state so workers can restart. |

At a minimum an AM needs `ambuild`, `ambuildempty`, `aminsert`, `ambulkdelete`, `amvacuumcleanup`, `amcostestimate`, `amoptions`, `amvalidate`, `ambeginscan`, `amrescan`, `amendscan`, and at least one of `amgettuple` or `amgetbitmap`.

## Capability Flags

The boolean fields in `IndexAmRoutine` (all set in the handler function) govern what the planner and DDL commands allow:

| Flag | Meaning |
|---|---|
| `amcanorder` | AM can return tuples in index key order (supports `ORDER BY` using the index) |
| `amcanorderbyop` | AM can order by the result of an operator (e.g., `ORDER BY col <-> point` for kNN) |
| `amcanbackward` | AM can scan backwards through `amgettuple` |
| `amcanunique` | AM can enforce uniqueness constraints |
| `amcanmulticol` | AM supports multi-column indexes |
| `amoptionalkey` | Scans do not require a constraint on the first index column |
| `amsearcharray` | AM handles `ScalarArrayOpExpr` quals natively (e.g., `col = ANY(array)`) |
| `amsearchnulls` | AM handles `IS NULL` / `IS NOT NULL` quals |
| `amstorage` | The stored key type can differ from the indexed column type |
| `amclusterable` | `CLUSTER` can reorder the heap using this index |
| `ampredlocks` | AM participates in predicate locking for serializable transactions |
| `amcanparallel` | AM supports parallel index scans |
| `amcaninclude` | AM supports `INCLUDE` columns (non-key columns stored in the index) |
| `amsummarizing` | AM stores data at block granularity rather than per-tuple (like BRIN) |

## Scan State Management

An `IndexScanDescData` (defined in `access/relscan.h`) represents each active index scan. The AM allocates and returns this struct from `ambeginscan`. It then stores its private scan state in `scan->opaque` — a `void *` field reserved for AM use. A typical pattern:

```c
IndexScanDesc
mymethod_beginscan(Relation rel, int nkeys, int norderbys)
{
    IndexScanDesc scan = RelationGetIndexScan(rel, nkeys, norderbys);
    MyMethodScanOpaque so = palloc(sizeof(MyMethodScanOpaqueData));
    /* initialise so fields */
    scan->opaque = so;
    return scan;
}
```

`amrescan` receives the scan keys and sets up (or resets) the traversal state stored in `scan->opaque`. `amgettuple` or `amgetbitmap` reads from `scan->opaque` on each call. `amendscan` frees everything in `scan->opaque` before the core releases the `IndexScanDescData` itself.

The dispatch layer in `indexam.c` routes all `index_beginscan`, `index_rescan`, `index_getnext_tid`, and `index_endscan` calls through `rel->rd_indam`, which is the cached `IndexAmRoutine *` for the relation's AM.

## Cost Estimation

The planner calls `amcostestimate` — or a generic wrapper — to decide whether using the index is cheaper than a sequential scan. The function receives the `PlannerInfo`, the `IndexPath` (including which quals are index conditions), and a loop count. It must fill in:

- `*indexStartupCost` — overhead before the first tuple arrives
- `*indexTotalCost` — total cost including all tuples
- `*indexSelectivity` — fraction of heap rows the index scan is expected to return
- `*indexCorrelation` — correlation between index order and heap physical order (−1 to 1)
- `*indexPages` — estimated number of index pages touched

A poorly calibrated `amcostestimate` has direct consequences. Overestimating cost causes the planner to ignore the index even when it would be faster. Underestimating cost causes the planner to prefer the index when a sequential scan would win. Custom AMs should look at the actual operator strategies in the scan quals and use `pg_statistic` data (via `estimate_num_groups`, `clause_selectivity`, and similar planner utilities) to produce realistic numbers. Generic fallbacks tend to produce poor plans for non-standard search patterns such as kNN or approximate matching.

## Index-Only Scans

If an AM stores the original indexed column values — not a hash, signature, or summary — it can avoid heap fetches entirely for qualifying tuples. Setting `amcanreturn` to a non-NULL function pointer signals this capability. The executor then calls `amcanreturn(rel, attno)` per column to confirm that column `attno` is returnable.

When executing an index-only scan the executor checks the [[subsystems/storage/visibility-map|visibility map]] for the heap page. If the page is marked all-visible, it takes the column values from the index tuple directly and skips the heap fetch. If the page is not all-visible it falls back to a heap fetch to verify visibility. The AM is responsible for storing the unmodified column value in its index tuples. An AM that stores only a derived key (a bloom filter bit pattern, a vector quantization code) cannot support index-only scans and should leave `amcanreturn` as NULL.

## Parallel Index Scans

An AM that sets `amcanparallel = true` can run its scans across multiple parallel workers. The parallel scan protocol uses dynamic shared memory (DSM) to coordinate workers:

1. The leader calls `amestimateparallelscan()` to learn how many bytes of DSM the AM needs to coordinate the scan.
2. The leader allocates a DSM segment and calls `aminitparallelscan(target)` to initialise the coordination area.
3. Each worker joins via `index_beginscan_parallel`, which maps the existing segment. Workers call `amgettuple` independently. The AM's coordination state in DSM partitions the work.
4. `amparallelrescan` resets the coordination state if the parallel scan must restart.

The AM is entirely responsible for the partitioning logic. A common approach is a shared atomic counter over page ranges, with each worker claiming the next unscanned chunk.

## Operator Classes and the AM Contract

A custom AM does not stand alone: users must also define operator classes that bind the AM's strategy numbers to concrete operators. The strategy numbers are AM-defined. Bloom uses strategy 1 for equality. A kNN AM might use strategy 1 for `<->` (distance) instead. `CREATE OPERATOR CLASS` wires up those operators and any required support functions. It stores the associations in `pg_amop` and `pg_amproc`.

`CREATE OPERATOR CLASS` calls the AM's `amvalidate(opclassoid)` callback to confirm the opclass is complete. A minimal validation checks that all required support functions are present and that the operator's input types match what the AM expects. An AM that skips a thorough `amvalidate` risks silent corruption later when a scan tries to call a missing support function.

`amadjustmembers`, if provided, allows the AM to adjust the dependency types (`ref_is_hard`) of operators and support functions being added. Hard dependencies prevent `ALTER OPERATOR FAMILY DROP` and force `CASCADE` on drop — appropriate for support functions that are essential to the opclass's correctness. Soft (auto) dependencies are appropriate for convenience operators that can be removed without breaking the AM.

## Real-World Examples

**bloom** (`contrib/bloom`) is the canonical custom AM in the PostgreSQL source tree. It implements a Bloom filter index: each index page stores a fixed-width bit array per indexed tuple. Lookups check whether the filter matches, but may produce false positives. As a result, the AM always sets `amgetbitmap` (not `amgettuple`). The executor re-checks every candidate against the heap. Because bloom stores derived bit patterns rather than column values, `amcanreturn` is NULL. Its `amoptions` callback parses `WITH (length=..., col1=...)` to set the filter width and per-column bit counts.

**pgvector** implements two custom AMs: IVFFlat and HNSW (Hierarchical Navigable Small World). Both support kNN queries via `amgettuple` driven by an `ORDER BY col <-> query_vector` clause. They set `amcanorderbyop = true` so the planner knows the AM can produce tuples in distance order. HNSW builds a multi-layer proximity graph during `ambuild`. It then navigates the graph during `amgettuple` using a greedy beam search. Because the graph stores quantized or full-precision vectors, it can support index-only scan for vector retrieval depending on build options.

## See also

- [[subsystems/extensions/overview|Extension System]] — `PG_FUNCTION_INFO_V1`, `PG_MODULE_MAGIC`, and the overall extension architecture
- [[subsystems/indexes/btree|B-tree Index Internals]] — the reference AM implementation; a useful comparison point for understanding scan state and vacuum callbacks
- [[subsystems/catalog/core-catalogs|Core Catalogs]] — `pg_am` and `pg_opclass` schema

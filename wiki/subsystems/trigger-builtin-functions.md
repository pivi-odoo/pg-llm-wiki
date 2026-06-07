---
title: "Built-in Trigger Functions"
aliases:
  - suppress_redundant_updates_trigger
  - tsvector_update_trigger
  - built-in trigger functions
source_files:
  - src/backend/utils/adt/trigfuncs.c
  - src/backend/utils/adt/tsvector_op.c
symbols:
  - suppress_redundant_updates_trigger
  - tsvector_update_trigger_byid
  - tsvector_update_trigger_bycolumn
  - tsvector_update_trigger
---

PostgreSQL ships several trigger functions implemented in C that cover two common automation patterns: suppressing no-op updates to reduce storage and [[subsystems/wal/overview|WAL]] write amplification, and automatically maintaining a `tsvector` column for [[subsystems/full-text-search|full-text search]]. Unlike user-defined trigger functions written in PL/pgSQL or another procedural language, these are compiled into the backend itself and invoked through the standard [[subsystems/triggers|trigger]] dispatch path.

## Suppressing Redundant Updates

`suppress_redundant_updates_trigger` is a BEFORE UPDATE trigger function that cancels an update when the proposed new row is byte-for-byte identical to the existing row. It does this by returning `NULL` from a BEFORE row-level trigger, which instructs the executor to skip the update entirely. As a result, the executor writes no heap tuple, generates no WAL record, and fires no subsequent AFTER triggers.

The comparison operates directly on the on-disk tuple representation. It checks four conditions in sequence: total tuple length, header offset, attribute count, and the `t_infomask` flags (masking out transaction-status bits that vary independently of user data). Only if all four match does it fall through to a `memcmp` of the entire tuple payload past the fixed header. This approach is fast for narrow rows, but its cost scales with row width. For tables with very wide rows or workloads where updates almost always change at least one column, the overhead of the comparison may outweigh the savings from the occasional suppressed write.

An important behavioral caveat: when this function returns `NULL`, PostgreSQL also skips *all* subsequent BEFORE UPDATE triggers on the same row. If another BEFORE trigger performs side effects — auditing, cascaded changes, external notifications — it will not fire for suppressed updates. This ordering dependency makes `suppress_redundant_updates_trigger` most reliable when it is the sole BEFORE UPDATE trigger on the table, or when it is listed first and downstream triggers are genuinely idempotent with respect to unchanged rows.

Attach it with a statement-level `CREATE TRIGGER`:

```sql
CREATE TRIGGER suppress_redundant
BEFORE UPDATE ON my_table
FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger();
```

## Maintaining a tsvector Column Automatically

Two built-in functions automate the task of keeping a `tsvector` column current as text source columns change: `tsvector_update_trigger` and `tsvector_update_trigger_column`. Both are BEFORE INSERT OR UPDATE trigger functions. On INSERT they always compute the vector. On UPDATE they recompute it only when at least one source column is marked as modified in `tg_updatedcols`. If no source column changed, the trigger preserves the existing tsvector value and returns the tuple unmodified.

The trigger argument list has a fixed structure: the first argument names the destination `tsvector` column, the second argument specifies the text search configuration, and all remaining arguments name the source text columns. The parser concatenates source columns in order, so their sequence affects tokenisation at boundaries.

### Fixed Configuration vs. Per-Row Configuration

`tsvector_update_trigger` resolves the text search configuration at trigger-creation time from the second argument, which must be a schema-qualified configuration name (e.g., `pg_catalog.english`). The schema qualification is mandatory to ensure the resolution is path-independent and stable across `search_path` changes.

`tsvector_update_trigger_column` instead reads the configuration OID from a column in each row. The second argument names a column of type `regconfig`. This is the right choice when different rows must use different language configurations. For example, a multilingual document table might have a `lang_config` column that stores `pg_catalog.english` for English documents and `pg_catalog.french` for French ones.

Example using the fixed-configuration variant:

```sql
CREATE TRIGGER tsvupdate
BEFORE INSERT OR UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION
  tsvector_update_trigger(tsv, 'pg_catalog.english', title, body);
```

Example using the per-row configuration variant:

```sql
CREATE TRIGGER tsvupdate_col
BEFORE INSERT OR UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION
  tsvector_update_trigger_column(tsv, lang_config, title, body);
```

In both cases, the function modifies the tuple in-flight before it reaches the heap. This means no additional application-level code is needed to keep the `tsvector` column current. A [[subsystems/indexes/gin|GIN]] index on the maintained column then supports efficient full-text queries.

## Relationship to User-Defined Trigger Functions

These built-in functions follow the same protocol as any user-defined trigger function: they receive a `TriggerData` pointer through `fcinfo->context`, inspect `tg_event` flags, and return a `HeapTuple` (or `NULL` to cancel). The distinction is purely implementation depth — they are written in C and linked directly into the backend, while user-defined trigger functions go through a procedural language handler. When the built-in behavior is insufficient (custom comparison logic, conditional suppression, multi-table coordination), the [[subsystems/triggers|triggers]] infrastructure supports writing equivalent logic in any supported language.

## Related Topics

- [[subsystems/triggers|Triggers]] — trigger dispatch, timing, and the full row-level trigger protocol
- [[subsystems/full-text-search|Full-text search]] — tsvector/tsquery types, configurations, and ranking
- [[subsystems/indexes/gin|GIN]] — the index type used for tsvector columns
- [[subsystems/wal/overview|WAL]] — why suppressing a write also eliminates its WAL record

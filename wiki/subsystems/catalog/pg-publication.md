---
title: "Publication Catalog Management"
aliases:
  - pg_publication
  - publication catalog
source_files:
  - src/backend/catalog/pg_publication.c
  - src/include/catalog/pg_publication.h
  - src/include/catalog/pg_publication_rel.h
  - src/include/catalog/pg_publication_namespace.h
symbols:
  - Publication
  - PublicationRelInfo
  - PublicationDesc
  - PublicationActions
  - PublicationPartOpt
  - GetPublication
  - GetPublicationRelations
  - GetAllTablesPublicationRelations
  - GetSchemaPublicationRelations
  - publication_add_relation
  - publication_add_schema
  - is_publishable_relation
  - pub_collist_to_bitmapset
  - GetTopMostAncestorInPublication
---

The publication catalog layer manages the metadata that defines what a logical replication publisher will send. It spans three system catalogs — `pg_publication`, `pg_publication_rel`, and `pg_publication_namespace` — and provides the lookup functions that the pgoutput plugin calls at runtime to decide whether a given table change should be replicated. Getting these catalog operations right is central to [[subsystems/replication/logical|logical replication]] correctness: stale membership decisions mean either missed changes or phantom rows on the subscriber.

## The Three Catalog Tables

`pg_publication` (`PublicationRelationId`) holds one row per publication. Its `FormData_pg_publication` struct records the publication name, owner, and five boolean flags:

| Column | Meaning |
|---|---|
| `puballtables` | Publication covers every publishable table in the database |
| `pubinsert` | INSERT events are replicated |
| `pubupdate` | UPDATE events are replicated |
| `pubdelete` | DELETE events are replicated |
| `pubtruncate` | TRUNCATE events are replicated |
| `pubviaroot` | Partition changes are reported under the root partitioned table's identity |

`pg_publication_rel` (`PublicationRelRelationId`) maps specific tables to publications. Beyond the `prpubid`/`prrelid` foreign keys, it carries two variable-length fields: `prqual` (a serialised `pg_node_tree` holding the row filter expression) and `prattrs` (an `int2vector` of sorted attribute numbers for the column list). Both are nullable — a NULL `prqual` means no row filter; a NULL `prattrs` means all columns are replicated.

`pg_publication_namespace` (`PublicationNamespaceRelationId`) maps schemas to publications for `FOR TABLES IN SCHEMA` publications. It is structurally simpler — just `pnpubid` and `pnnspid` with no per-mapping qualifications.

## The In-Memory Publication Struct

Callers that need publication metadata work through the `Publication` struct rather than touching catalog tuples directly. `GetPublication()` fetches a `pg_publication` row by OID via the `PUBLICATIONOID` syscache and copies the relevant fields into a palloc'd `Publication`:

```c
typedef struct Publication {
    Oid    oid;
    char  *name;
    bool   alltables;
    bool   pubviaroot;
    PublicationActions pubactions;  /* insert/update/delete/truncate bools */
} Publication;
```

The companion `PublicationDesc` struct, used by the output plugin, extends this with four validity flags (`rf_valid_for_update`, `rf_valid_for_delete`, `cols_valid_for_update`, `cols_valid_for_delete`) that record whether the row filter and column list are compatible with the table's replica identity for UPDATE and DELETE operations.

`PublicationRelInfo` is the transient struct used during `ALTER PUBLICATION ... ADD TABLE` processing. It packages the open `Relation`, the optional `whereClause` parse tree, and the optional column name `List` that `publication_add_relation()` needs to validate and persist.

## Publishability Rules

Not every relation is eligible for publication. `is_publishable_class()` and its open-relation variant `is_publishable_relation()` enforce four hard invariants (pg_publication.c):

1. The relation must be a plain table (`RELKIND_RELATION`) or a partitioned table (`RELKIND_PARTITIONED_TABLE`).
2. The relation must not be a catalog relation (`IsCatalogRelationOid()` check) and must have an OID at or above `FirstNormalObjectId` — this excludes `information_schema` tables and everything created during initdb.
3. The relation must be permanently stored (`relpersistence == RELPERSISTENCE_PERMANENT`); unlogged and temporary tables are ineligible.
4. System schemas (`pg_catalog`, `pg_toast`) and temporary namespaces are rejected for schema-level publications (`check_publication_add_schema()`).

The code comment in `is_publishable_class()` acknowledges that the `FirstNormalObjectId` threshold is an imperfect proxy for "not a system relation" and suggests that a future `relispublishable` column on `pg_class` would be cleaner.

## Adding Relations and Schemas

`publication_add_relation()` is the catalog entry point for `ALTER PUBLICATION ... ADD TABLE`. It:

1. Checks for an existing `pg_publication_rel` row via the `PUBLICATIONRELMAP` syscache (the unique index on `(prrelid, prpubid)` provides the real duplicate guard; the cache lookup is just for a friendlier error message).
2. Calls `check_publication_add_relation()` to enforce the publishability rules.
3. Calls `publication_translate_columns()` to convert the column name list to a sorted `AttrNumber` array, rejecting system columns, generated columns, and duplicates.
4. Writes a new `pg_publication_rel` tuple via `CatalogTupleInsert()`.
5. Records `DEPENDENCY_AUTO` links from the new mapping row to both the publication and the relation, and `DEPENDENCY_NORMAL` links to any columns and expression objects referenced by the row filter.
6. Invalidates the relcache entries for the affected relation and all its partitions so that the cached publication membership is rebuilt.

`publication_add_schema()` follows the same pattern for `pg_publication_namespace`, except there are no per-mapping qualifications or column lists to handle.

The relcache invalidation step (`InvalidatePublicationRels()`) is broader than it might seem: when a partitioned table is added to a publication, the invalidation covers every partition in the tree. This is necessary because partition leaf tables inherit publication membership implicitly. The invalidation step must clear any cached negative answer on a partition.

## Partition Handling and `publish_via_partition_root`

Partition support introduces a three-way `PublicationPartOpt` enum:

| Value | Meaning |
|---|---|
| `PUBLICATION_PART_ROOT` | Only the explicitly named partitioned table |
| `PUBLICATION_PART_LEAF` | Only leaf partitions |
| `PUBLICATION_PART_ALL` | The named table plus all partitions at every level |

`GetPublicationRelations()` accepts this option and routes through `GetPubPartitionOptionRelations()` to expand each explicitly listed table OID into the appropriate set. The `pubviaroot` flag on the publication drives the choice: when it is true, the output plugin reports changes using the root table's identity and schema, so only root tables (or standalone tables) are needed in the relation set.

`pg_get_publication_tables()`, the set-returning function backing the `pg_publication_tables` view, must handle a subtlety when multiple publications are mixed. If any publication in the input array has `pubviaroot = true`, `filter_partitions()` walks the result list and removes any partition whose ancestor also appears — preventing the same physical row change from being reported twice under both the child and root identity.

`GetTopMostAncestorInPublication()` solves the related problem for the output plugin: given a partition that just changed, find the highest ancestor in the publication's membership. It walks the ancestor list (ordered from immediate parent to root) and checks each ancestor against both `pg_publication_rel` (via `GetRelationPublications()`) and `pg_publication_namespace` (via `GetSchemaPublications()`). The returned OID tells pgoutput which table identity to use in the replication stream.

## Column Lists

Column lists are stored in `pg_publication_rel.prattrs` as a sorted `int2vector` of user-column attribute numbers (no system columns, no generated columns, no duplicates). `publication_translate_columns()` enforces the sort via `qsort` before it inserts the tuple, so the catalog representation is always canonical.

At runtime the output plugin needs a `Bitmapset` for fast membership tests. `pub_collist_to_bitmapset()` converts the stored `int2vector` Datum to a `Bitmapset`, optionally building into a specified `MemoryContext` and optionally merging into an existing set. Attribute numbers in the bitmapset are not offset by `FirstLowInvalidHeapAttributeNumber` because system columns are forbidden; this simplifies the arithmetic on the output plugin side.

When `pg_get_publication_tables()` encounters a row with a NULL column list (FOR ALL TABLES or schema publications), it synthesises the full column list by scanning `pg_attribute` and including every non-dropped, non-generated column. This ensures callers always get a concrete `int2vector` regardless of how the publication was defined.

## Catalog Lookup Patterns

The code uses two distinct lookup patterns depending on the access pattern:

- **Syscache** (`SearchSysCache1`, `SearchSysCacheList1`): Used for `PUBLICATIONOID`, `PUBLICATIONRELMAP`, and `PUBLICATIONNAMESPACEMAP` lookups. Appropriate when the OID is known or when scanning all mappings for a single key (e.g., all publications for a relation).
- **Systable scan** (`systable_beginscan`): Used when scanning by the other direction — all relations for a publication — or when no suitable syscache covers the access pattern. `GetPublicationRelations()` and `GetPublicationSchemas()` both use this path.

`GetAllTablesPublicationRelations()` is the most expensive lookup: it does a full `pg_class` scan filtered by `relkind` and `is_publishable_class()`. The system calls it only when a `FOR ALL TABLES` publication exists, which is the unusual case; the path is not on the hot path for ordinary per-table publications.

## Related Topics

- [[subsystems/replication/logical|Logical Replication]] — the decoding pipeline, pgoutput plugin, and apply worker that consume publication metadata
- [[subsystems/catalog/pg-depend|Object Dependencies]] — the dependency tracking that ensures publication mappings are dropped when referenced objects are removed

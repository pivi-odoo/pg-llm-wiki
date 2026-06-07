---
title: "Statistics Shared Memory"
aliases:
  - pgstat shared memory
  - PgStatShared
  - PgStat_EntryRef
  - statistics collector redesign
  - cumulative statistics
source_files:
  - src/backend/utils/activity/pgstat_shmem.c
  - src/include/utils/pgstat_internal.h
  - src/include/pgstat.h
symbols:
  - PgStat_ShmemControl
  - PgStatShared_HashEntry
  - PgStatShared_Common
  - PgStat_EntryRef
  - PgStat_KindInfo
  - PgStat_HashKey
  - PgStat_Kind
  - StatsShmemSize
  - StatsShmemInit
  - pgstat_get_entry_ref
  - pgstat_lock_entry
  - pgstat_drop_entry
  - pgstat_gc_entry_refs
---

Prior to PostgreSQL 15, a dedicated `stats collector` background process maintained cumulative statistics (table access counts, function call counts, etc.). It received UDP messages from backends and wrote stats files to disk. PostgreSQL 15 replaced this with a shared-memory model: each backend now stores and updates stats directly in a dynamic shared hash table. This eliminates the collector process, the UDP socket, and the filesystem intermediary. The shared memory infrastructure for this system lives in `pgstat_shmem.c`.

## The shared stats hash table

PostgreSQL stores all per-object statistics — per-relation, per-database, per-function, per-replication-slot, per-subscription — in a `dshash_table` (a dynamic shared hash; see `lib/dshash.c`) allocated in dynamic shared memory attached to the main shared memory segment. `PgStat_HashKey` keys the hash:

```c
typedef struct {
    PgStat_Kind kind;   /* type of statistics object */
    Oid         dboid;  /* database OID (InvalidOid for shared objects) */
    Oid         objoid; /* table/function/slot OID */
} PgStat_HashKey;
```

Each entry in the hash is a `PgStatShared_HashEntry`. It contains the key, an [[subsystems/locking/lwlocks|LWLock]] for entry-level locking, a generation counter (used to detect dropped entries), and a flexible body whose layout depends on `kind` (e.g. `PgStatShared_Relation` for tables, `PgStatShared_Function` for functions). The body always begins with a `PgStatShared_Common` header containing a spinlock and the last-reset timestamp.

**Fixed-size entries** (archiver, [[subsystems/background/bgwriter|bgwriter]], checkpointer, I/O, SLRU, WAL) skip the dynamic hash. PostgreSQL preallocates them in the static `PgStat_ShmemControl` struct at startup and accesses them via direct pointers. These are the "singleton" stats that do not need per-object keying.

## Backend-local entry references

Acquiring a lock on the shared hash and dereferencing a `dshash` entry on every stats update would be prohibitively expensive. Instead, each backend maintains a process-local hash table (`pgStatEntryRefHash`) that maps `PgStat_HashKey` to a `PgStat_EntryRef` — a cached pointer to the shared entry's body.

```
PgStat_EntryRef
├── shared_entry  → PgStatShared_HashEntry (in DSA)
├── shared_stats  → PgStatShared_Common (body start, in DSA)
└── pending       → backend-local pending struct (palloc'd)
```

The `pending` field is a kind-specific accumulator. When a backend increments a counter (e.g. a tuple read), it writes to `pending`, not to `shared_stats`. This avoids shared-memory writes on every tuple. PostgreSQL flushes the pending buffer to shared memory (under the entry's LWLock) at transaction commit, at certain checkpoints in the backend's activity loop, and when the backend exits.

The flush is kind-specific: each `PgStat_KindInfo` descriptor registers a `flush_pending_cb` that performs the merge. For relation stats, this adds the pending deltas to the shared counters atomically. For WAL stats, it accumulates bytes written and WAL records generated.

## Entry lifecycle and garbage collection

When an object is dropped (a table is dropped, a function is removed), `pgstat_drop_entry` removes the `PgStatShared_HashEntry` from the dshash. It also increments `PgStat_ShmemControl.gc_request_count`. Each backend compares this count against a local `pgStatSharedRefAge`. When they diverge, the backend calls `pgstat_gc_entry_refs`. This function scans `pgStatEntryRefHash` and releases any `PgStat_EntryRef` whose `generation` no longer matches the shared entry. This lazy invalidation avoids broadcasting signals to all backends on every drop.

Database-level drops (`pgstat_drop_database_and_contents`) iterate the dshash and drop all entries whose `dboid` matches, then drop the database entry itself. A dshash sequential scan handles this, avoiding repeated lock acquisitions.

## Snapshot model for queries

When a backend queries `pg_stat_user_tables` or similar views, the stats must reflect a consistent point in time. A live hash that other backends are concurrently modifying would not provide that consistency. The stats system provides a snapshot mechanism:

- `pgstat_fetch_entry` fetches **per-object stats** (`PGSTAT_KIND_RELATION`, etc.) on demand. The first call for a given key within a snapshot context locks the shared entry. It then copies the data into backend-local snapshot memory and returns a pointer to the copy.
- `pgstat_snapshot_fixed` snapshots **fixed-size stats** in bulk. It copies the preallocated structs from `PgStat_ShmemControl` under their respective spinlocks.
- PostgreSQL invalidates snapshots at the next transaction start. This ensures that a single query sees internally consistent data.

## Kinds and the KindInfo registry

`PgStat_Kind` is an enum with one value per statistics type. PostgreSQL registers the characteristics of each kind — struct size, pending struct size, flush/delete/reset callbacks, snapshot behaviour — in a `PgStat_KindInfo` array indexed by `kind`. This registry is what allows `pgstat_shmem.c` to manage all stats kinds generically without switching on kind in the core entry-management functions.

| Kind constant | Covers | Shared struct |
|---|---|---|
| `PGSTAT_KIND_DATABASE` | Per-database counters | `PgStatShared_Database` |
| `PGSTAT_KIND_RELATION` | Per-table, per-index access | `PgStatShared_Relation` |
| `PGSTAT_KIND_FUNCTION` | Per-function call/time | `PgStatShared_Function` |
| `PGSTAT_KIND_REPLSLOT` | Per-replication-slot | `PgStatShared_ReplSlot` |
| `PGSTAT_KIND_SUBSCRIPTION` | Per-subscription | `PgStatShared_Subscription` |
| `PGSTAT_KIND_IO` | I/O by backend type and context | `PgStatShared_IO` |
| `PGSTAT_KIND_WAL` | WAL generation metrics | `PgStatShared_Wal` |
| `PGSTAT_KIND_ARCHIVER` | WAL archiver activity | `PgStatShared_Archiver` |
| `PGSTAT_KIND_BGWRITER` | Background writer activity | `PgStatShared_BgWriter` |
| `PGSTAT_KIND_CHECKPOINTER` | Checkpointer activity | `PgStatShared_Checkpointer` |
| `PGSTAT_KIND_SLRU` | SLRU cache hits/misses | `PgStatShared_SLRU` |

## Related Topics

- [[subsystems/observability/overview|Observability Overview]]
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]]
- [[subsystems/observability/pg-stat-io|pg_stat_io]]
- [[subsystems/memory/dsa|Dynamic Shared Area (DSA)]]
- [[subsystems/background/stats-collector|Stats Collector (historical)]]

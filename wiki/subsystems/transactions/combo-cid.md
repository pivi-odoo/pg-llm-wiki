---
title: Combo Command IDs
aliases:
  - combo CID
  - combocid
  - ComboCidKey
  - intra-transaction command visibility
tags:
  - theme/concurrency-control
source_files:
  - src/backend/utils/time/combocid.c
  - src/include/access/htup_details.h
symbols:
  - ComboCidKeyData
  - ComboCidEntryData
  - GetComboCommandId
  - HeapTupleHeaderAdjustCmax
  - HeapTupleHeaderGetCmin
  - HeapTupleHeaderGetCmax
  - AtEOXact_ComboCid
  - HEAP_COMBOCID
---

Heap tuple headers store only a single 32-bit command ID field (`t_cid`) to save space. Yet [[subsystems/transactions/mvcc|MVCC]] visibility checks sometimes need both `cmin` (the command that inserted the tuple) and `cmax` (the command that deleted it) independently. When a transaction inserts and then deletes the same tuple — an operation that is uncommon but valid — PostgreSQL encodes both values as a *combo CID*: an index into a backend-local lookup table that maps that index back to the original `(cmin, cmax)` pair.

## The Header Space Constraint

Before PostgreSQL 8.3, `HeapTupleHeaderData` held separate fields for `cmin` and `cmax`. The 8.3 redesign overlaid them into a single `t_cid` field in the `HeapTupleFields` union, saving four bytes per tuple on disk. This works cleanly in the normal case, because `cmin` and `cmax` serve different phases of a tuple's lifetime. `cmin` is only interesting while the inserting transaction is still in progress. `cmax` is only interesting while the deleting transaction is still in progress. Once a transaction commits or aborts, both values become irrelevant. Other backends only care about the transaction IDs `xmin` and `xmax`, not the sub-transaction command counters.

The overlap fails only when a single transaction both inserts and deletes the same tuple. In that situation, intra-transaction visibility — answering "was this row visible before the current command?" — requires both values simultaneously. The `t_cid` field cannot hold both, so PostgreSQL stores a combo CID token there instead and sets the `HEAP_COMBOCID` flag bit (`0x0020`) in `t_infomask` to signal that the raw field value is not a plain command ID.

## Backend-Local Storage

The combo CID mechanism is entirely backend-private. It consists of two data structures allocated in `TopTransactionContext` and discarded at end of transaction via `AtEOXact_ComboCid`:

- A flat array `comboCids` of `ComboCidKeyData` structs, indexed by the combo CID value itself. Each entry holds the `(cmin, cmax)` pair. Translating a combo CID back to its component values is a direct array subscript — O(1) with no locking needed.
- A hash table `comboHash` keyed on `ComboCidKeyData` that maps `(cmin, cmax)` pairs to their already-assigned combo CID index. This allows reuse of existing entries when the same pair is encountered again, keeping the table small.

PostgreSQL initializes both structures lazily, the first time a transaction calls `GetComboCommandId`. The initial allocation is 100 entries for both the array (`CCID_ARRAY_SIZE`) and the hash table (`CCID_HASH_SIZE`). When the array fills, `repalloc` doubles it. The doubling happens before a new hash entry is inserted, so a failed allocation cannot leave the hash table pointing at an out-of-bounds array slot.

The `ComboCidKeyData` struct is a plain pair of `CommandId` fields:

```c
typedef struct
{
    CommandId   cmin;
    CommandId   cmax;
} ComboCidKeyData;
```

The corresponding hash entry wraps this key together with the assigned combo CID index:

```c
typedef struct
{
    ComboCidKeyData key;
    CommandId   combocid;
} ComboCidEntryData;
```

## Assigning and Reading Combo CIDs

When a tuple is about to be deleted and its `xmin` belongs to the current transaction, `HeapTupleHeaderAdjustCmax` intercepts the write. It reads the existing `cmin` from the tuple header and calls `GetComboCommandId(cmin, cmax)` to obtain or reuse a combo CID. It then replaces the caller's `*cmax` with that index and sets `*iscombo = true`. The caller then stores the returned value into `t_cid` and sets `HEAP_COMBOCID` in `t_infomask` via `HeapTupleHeaderSetCmax`.

On the read side, `HeapTupleHeaderGetCmin` and `HeapTupleHeaderGetCmax` each check whether `HEAP_COMBOCID` is set. If so, they call `GetRealCmin` or `GetRealCmax`, which are simple array lookups into `comboCids`. If the flag is clear, the function returns the raw `t_cid` value directly.

Both accessors assert that the caller is inside the originating transaction. This is correct by design: combo CIDs are meaningless to any other backend. A foreign backend that needs the raw stored value — for example, during recovery or logical replication state transfer — must call `HeapTupleHeaderGetRawCommandId` directly and handle the combo CID case separately. The serialization functions `SerializeComboCIDState` and `RestoreComboCIDState` exist precisely to transfer this backend-local state when a transaction's execution context is handed off, such as during parallel query.

## Scope and Limits

Because the combo CID value is a 32-bit index, the theoretical maximum number of distinct `(cmin, cmax)` pairs per transaction is 2^32. In the pathological case where each command deletes a tuple inserted by every earlier command, the number of pairs grows as N*(N+1)/2. This exhausts the 32-bit space at roughly 92,000 commands. In practice, transactions hit memory or disk space limits first.

`AtEOXact_ComboCid` unconditionally frees the entire combo CID table at end of transaction. There is no need to write combo CID data to [[subsystems/wal/overview|WAL]] or share it through shared memory; it lives only for the duration of the transaction that needs it, in the backend that owns it.

## Related Topics

- [[subsystems/transactions/mvcc|MVCC]]
- [[subsystems/transactions/snapshot|Snapshots]]
- [[subsystems/memory/contexts|Memory contexts]]
- [[subsystems/wal/overview|WAL]]

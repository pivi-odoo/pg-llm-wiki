---
title: "DSM Segment Registry"
aliases:
  - DSM registry
  - GetNamedDSMSegment
  - named DSM segment
  - dsm_registry
tags:
  - theme/extensibility
source_files:
  - src/backend/storage/ipc/dsm_registry.c
  - src/include/storage/dsm_registry.h
symbols:
  - DSMRegistryCtxStruct
  - DSMRegistryEntry
  - DSMRegistryShmemSize
  - DSMRegistryShmemInit
  - GetNamedDSMSegment
  - init_dsm_registry
---

The DSM segment registry, added in PostgreSQL 17, gives extensions and internal subsystems a way to create and share dynamic shared memory (DSM) segments by name, without needing to register a `shmem_request_hook` at startup. Before the registry existed, any code that needed shared memory had to run at postmaster startup time, before shared memory was allocated; extensions in particular had to use a hook just to reserve a fixed-size slice of the shared memory block. The registry lifts that restriction by letting any backend allocate segments lazily, on first use.

## How It Works

The registry is itself a small, fixed-size struct (`DSMRegistryCtxStruct`) in the main shared memory block, allocated at server startup. It stores handles for a DSA area and a `dshash_table`. The first backend that calls `GetNamedDSMSegment()` creates the `dshash_table`.

`init_dsm_registry()` (called lazily) creates or attaches to the dynamic hash table under `DSMRegistryLock` ([[subsystems/locking/lwlocks|LWLock]]). The table maps 63-character string names to `DSMRegistryEntry` values, each containing a `dsm_handle` and the segment size.

`GetNamedDSMSegment()` is the single public entry point. Given a name, a size, and an optional initialization callback, it:

1. Initializes the registry if needed.
2. Looks up or inserts an entry in the `dshash_table` under a per-entry lock.
3. If the entry was not found (first call for this name), it creates a new DSM segment of the requested size and calls the `init_callback` to fill it. It then pins both the segment and the DSA mapping so they survive backend disconnection, and stores the resulting `dsm_handle` in the entry.
4. If the entry was found (segment already exists), it attaches the backend to the existing segment if it is not already attached. It then pins the mapping.
5. Returns the segment's address. Sets `*found` to indicate whether this was a new or existing segment.

The `found` output lets callers initialize their own per-backend state based on whether they created the segment or joined an existing one.

## Concurrency

The `dshash_table` provides per-entry locking. Two backends requesting the same name serialize only against each other, not against all registry users. The function releases the lock before it returns. The segment address is accessible without holding any locks after the call.

The size consistency check (`entry->size != size`) catches the case where two callers register the same name with different sizes — an obvious programming error that is worth detecting explicitly.

The registry makes all allocations in `TopMemoryContext` to ensure they survive transaction boundaries. DSM handles and DSA state must persist for the lifetime of the backend.

## Usage by Extensions

An extension that needs shared memory can now do:

```c
MySharedStruct *shared;
bool found;

shared = GetNamedDSMSegment("myext-shared-state",
                             sizeof(MySharedStruct),
                             my_init_callback,
                             &found);
```

The first backend to call this will run `my_init_callback`; subsequent backends will attach to the already-initialized segment. The extension needs no `shmem_request_hook`. The segment persists until the server restarts.

## Related Topics

- [[subsystems/storage/dsm-impl]] — the underlying DSM and DSA mechanisms
- [[subsystems/storage/shared-memory]] — how the static shared memory block is managed
- [[subsystems/extensions/overview]] — extension development context

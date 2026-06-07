---
title: "Injection Points"
aliases:
  - injection points
  - InjectionPointAttach
  - InjectionPointRun
  - USE_INJECTION_POINTS
source_files:
  - src/backend/utils/misc/injection_point.c
  - src/include/utils/injection_point.h
symbols:
  - InjectionPointEntry
  - InjectionPointsCtl
  - InjectionPointCacheEntry
  - InjectionPointAttach
  - InjectionPointDetach
  - InjectionPointRun
  - InjectionPointLoad
  - InjectionPointCached
  - IsInjectionPointAttached
  - InjectionPointShmemSize
  - InjectionPointShmemInit
  - InjectionPointCacheRefresh
---

Injection points are a PostgreSQL 17 testing infrastructure that allows test code to hook into arbitrary named locations in server code and execute callbacks. Where a production code path contains `INJECTION_POINT("name")`, PostgreSQL calls an attached callback instead of (or in addition to) the normal control flow. This lets TAP tests and in-core regression tests simulate race conditions, inject waits, and override behavior — all without modifying the server's production code paths.

Injection points are only available in builds compiled with `--enable-injection-points` (the `USE_INJECTION_POINTS` preprocessor flag). In production builds the macro is a no-op and the entire subsystem compiles away.

## Shared Memory Layout

All active injection points are stored in a fixed shared memory array (`InjectionPointsCtl`) of up to 128 `InjectionPointEntry` slots. Each entry stores:

- A name (up to 63 characters), a library name, and a function name.
- A 1024-byte opaque `private_data` area for the callback to receive custom parameters.
- An atomic generation counter that serves as a lock-free validity sentinel.

The generation counter protocol allows backends to read entries without holding `InjectionPointLock` ([[subsystems/locking/lwlocks|LWLock]]):
- An **even** generation means the slot is unused.
- An **odd** generation means the slot is active.
- A backend reads the generation before and after copying the entry fields; if the generation has not changed, the copy is coherent. If it changed, the slot was concurrently recycled and the read must be retried.

Writes (attach and detach) require `InjectionPointLock` exclusively, to prevent two backends from modifying the array simultaneously. They must also use a write barrier (`pg_write_barrier()`) before incrementing the generation from even to odd.

`max_inuse` is an atomic counter tracking the highest active index plus one, avoiding a full 128-slot scan when there are few injection points.

## Attaching and Detaching

`InjectionPointAttach()` registers a new point: it scans for a free slot (even generation), writes all fields, then atomically bumps the generation to odd. It errors if the name already exists.

`InjectionPointDetach()` marks the slot unused by bumping the generation from odd to even. Backends that cached the entry will discover the mismatch on their next generation check and drop the cache entry.

## Per-Backend Cache

Loading a callback from a shared library (`load_external_function()`) is expensive relative to running it. Each backend therefore caches loaded callbacks in a local `HTAB` (`InjectionPointCache`), keyed by injection point name. Each cache entry records the slot index and the generation number at the time it was cached.

`InjectionPointCacheRefresh()` is called before every execution. It:
1. If `max_inuse` is zero, it destroys the entire cache (no injection points active).
2. If the local cache has an entry for this name, it validates the entry by re-reading the slot's generation. A matching generation means the callback is still valid. A mismatched generation (the slot was recycled or reused) evicts the stale entry.
3. It scans the shared array for the name, verifying coherence with the double-read protocol. If it finds the name, it loads the callback library.

## Running a Point

`InjectionPointRun()` calls `InjectionPointCacheRefresh()` and invokes the callback if one is found. The callback receives the injection point's name, a pointer to its `private_data`, and an opaque `arg` that the caller supplies.

**PostgreSQL 18** expanded the injection point API significantly. In PostgreSQL 17, the macro took only a name and the callback took only `(name, private_data)`. In PG18:

- `INJECTION_POINT(name, arg)` passes an extra caller-supplied argument through to the callback.
- `INJECTION_POINT_LOAD(name)` pre-warms the per-backend cache without executing the callback. This is useful in critical sections or memory-constrained contexts where the actual `load_external_function()` call inside `InjectionPointCacheRefresh()` cannot be allowed to allocate memory at run time.
- `INJECTION_POINT_CACHED(name, arg)` runs the callback directly from the local cache without re-checking shared memory. Used when the injection point is known to be loaded and freshness is not a concern.
- `IS_INJECTION_POINT_ATTACHED(name)` tests whether a callback is attached, without running it.

In a non-injection-points build all four macros compile to no-ops (or `false` for the predicate).

## Use in Testing

The in-core `injection_point` module (in `src/test/modules/`) provides SQL functions to attach, detach, and run injection points from test scripts. Tests can use these to inject a wait at a specific code location in another backend, then verify that the waiting backend becomes visible in `pg_stat_activity` with the expected wait event before releasing it. This is how PostgreSQL's own regression tests exercise concurrency without relying on timing.

## Related Topics

- [[subsystems/extensions/hooks]] — production-grade extension hooks
- [[subsystems/extensions/overview]] — broader extension system
- [[subsystems/observability/wait-events]] — wait events that injection points can trigger
- [[subsystems/storage/shared-memory]] — how InjectionPointsCtl is allocated in shared memory

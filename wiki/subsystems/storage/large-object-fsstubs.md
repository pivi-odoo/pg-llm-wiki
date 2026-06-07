---
title: "Large Object SQL Interface (be-fsstubs)"
aliases:
  - large object fsstubs
  - lo_open
  - lo_read
  - lo_write
source_files:
  - src/backend/libpq/be-fsstubs.c
  - src/include/libpq/be-fsstubs.h
symbols:
  - be_lo_open
  - be_lo_close
  - be_lo_read
  - be_lo_write
  - be_lo_lseek
  - be_lo_lseek64
  - be_lo_tell
  - be_lo_tell64
  - be_lo_truncate
  - be_lo_truncate64
  - be_lo_unlink
  - be_lo_creat
  - be_lo_create
  - be_lo_import
  - be_lo_export
  - be_lo_get
  - be_lo_get_fragment
  - be_lo_from_bytea
  - be_lo_put
  - AtEOXact_LargeObject
  - AtEOSubXact_LargeObject
  - LargeObjectDesc
  - newLOfd
  - closeLOfd
---

The file `be-fsstubs.c` implements the SQL-callable large object interface that clients interact with through functions like `lo_open`, `lo_read`, `lo_write`, `lo_lseek`, `lo_truncate`, and `lo_unlink`. It sits between the SQL function manager and the lower-level inversion API (`inv_api.c`), translating POSIX-like file descriptor semantics into operations on `pg_largeobject` catalog tuples. The name "fsstubs" reflects its historical role as a stub layer that made the inversion large object system look like a filesystem to callers.

## The File Descriptor Abstraction

SQL clients address open large objects using integer file descriptors — small non-negative integers that index into a backend-private array called `cookies`. Each slot holds a pointer to a `LargeObjectDesc`, which tracks the logical OID of the large object, an MVCC snapshot, the current seek offset, and permission flags (`IFS_RDLOCK`, `IFS_WRLOCK`).

The `cookies` array starts with 64 slots and doubles on demand, all allocated from a dedicated [[subsystems/memory/contexts|memory context]] named `"Filesystem"` (`fscxt`). Using a private context makes bulk cleanup at transaction end trivially cheap: dropping the context reclaims everything at once without iterating through individual allocations.

The integer FD is purely a session-local index. It has no meaning outside the backend process or the current transaction. Clients that attempt to cache FD values across transaction boundaries will find them invalidated at commit or rollback.

## Permission Enforcement

Access mode is established at open time in `be_lo_open`. The function calls `inv_open` with the requested mode flags. As of PostgreSQL 11, `inv_open` performs the ACL checks there, then sets `IFS_RDLOCK` and/or `IFS_WRLOCK` in the descriptor's flags field. Subsequent reads and writes in the fsstubs layer check these flags before delegating to the inversion API, producing a specific error about the open mode rather than a generic privilege denial. Consider a caller who opens a large object read-only and then attempts `lo_write`. The resulting error correctly names the FD mode issue, not a privilege issue.

Write operations — `lo_open` with `INV_WRITE`, `lo_creat`, `lo_create`, `lo_truncate`, `lo_unlink`, `lowrite`, `lo_import`, `lo_from_bytea`, `lo_put` — all call `PreventCommandIfReadOnly` to refuse execution inside a read-only transaction or standby.

## Snapshot Lifetime Management

`be_lo_open` may open a descriptor that carries an MVCC snapshot. In that case it registers the snapshot against `TopTransactionResourceOwner` rather than the current portal's resource owner. This keeps the snapshot alive for the duration of the transaction rather than just until the current query or cursor closes. Without this, a snapshot acquired inside one portal could be freed while the large object FD was still live and in use by another.

Cleanup in `closeLOfd` reverses this by unregistering the snapshot from `TopTransactionResourceOwner` before calling `inv_close`.

## Transaction and Subtransaction Cleanup

Every operation that opens or creates a large object sets a module-level flag `lo_cleanup_needed`. The transaction machinery calls `AtEOXact_LargeObject` at the end of every transaction. If `lo_cleanup_needed` is false the function returns immediately with no work done; this avoids any overhead in transactions that never touch large objects.

On commit, `AtEOXact_LargeObject` explicitly closes all open FDs to avoid resource-leak warnings from the resource owner machinery. On abort, the open FDs can be left alone because the transaction cleanup machinery tears down both the `fscxt` memory context and the snapshot registrations anyway.

Subtransaction handling in `AtEOSubXact_LargeObject` is more nuanced. Each `LargeObjectDesc` records the subtransaction ID (`subid`) that owns it. When a subtransaction commits, `AtEOSubXact_LargeObject` re-attributes its descriptors to the parent subtransaction by updating `subid`. When a subtransaction aborts, it immediately closes descriptors owned by that subtransaction, ensuring that a failed savepoint cannot leave LO FDs dangling in an inconsistent state.

## The 32-bit / 64-bit API Pairs

Several operations exist in paired forms: `be_lo_lseek` / `be_lo_lseek64`, `be_lo_tell` / `be_lo_tell64`, `be_lo_truncate` / `be_lo_truncate64`. The 32-bit variants accept or return `int32` offsets and add explicit overflow checks. They return an error if the true `int64` result cannot be represented. PostgreSQL added the 64-bit variants later to support large objects bigger than 2 GB, and they expose the full offset range. The maximum theoretical large object size is `INT_MAX * LOBLKSIZE`, where `LOBLKSIZE` is `BLCKSZ / 4`.

## Bytea-Oriented Convenience Functions

In addition to the FD-based interface, `be-fsstubs.c` provides a set of higher-level functions that operate without an explicit open/close cycle:

- `be_lo_get` reads an entire large object and returns it as a `bytea`.
- `be_lo_get_fragment` reads a byte range, computing the actual read length against the true object size to avoid over-allocation.
- `be_lo_from_bytea` creates a new large object and populates it from a `bytea` value in a single call.
- `be_lo_put` writes a `bytea` value at a specified offset within an existing large object.

These functions open a `LargeObjectDesc` directly into `CurrentMemoryContext` rather than the `fscxt` cookie array, since they do not expose an FD to the caller and manage their own open/close lifecycle within the function call.

## Import and Export

`be_lo_import` and `be_lo_export` transfer data between server-side filesystem files and large objects. Import creates a new inversion object and streams the file contents in through `inv_write` in 8 KB chunks. Export opens the large object for reading and streams it out to a file with a `022` umask (the export routine temporarily relaxes the backend's normal `077` umask to produce readable export files).

The SQL wrapper restricts both operations to superusers, since they read and write arbitrary server-side file paths. The 8 KB buffer constant (`BUFSIZE`) is intentional: it limits stack usage while keeping I/O reasonably efficient.

## Relationship to the Inversion API

`be-fsstubs.c` does no direct catalog access itself. All catalog reads and writes flow through the inversion layer (`inv_api.c`). It accesses `pg_largeobject` tuples, manages the `LOBLKSIZE`-sized page chunks, and handles the [[subsystems/storage/toast|TOAST]]-adjacent compression of individual page tuples. The fsstubs layer is concerned only with the session state: the cookie table, snapshot lifetimes, transaction cleanup, and translating SQL function arguments into inversion API calls.

## Related Topics

- [[subsystems/storage/toast|TOAST]] — tuple storage mechanism that large object pages interact with for compression
- [[subsystems/memory/contexts|Memory contexts]] — the `fscxt` allocation context used to hold open descriptors
- [[subsystems/storage/heap|Heap storage]] — underlying relation storage for `pg_largeobject`

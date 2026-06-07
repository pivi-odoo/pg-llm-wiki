---
title: "Sequence Access Method"
aliases:
  - sequence_open
  - sequence_close
  - sequence access method
source_files:
  - src/backend/access/sequence/sequence.c
  - src/include/access/sequence.h
symbols:
  - sequence_open
  - sequence_close
  - validate_relation_kind
---

`sequence.c` provides the access-method layer for sequence relations. It thinly wraps `relation_open()` and `relation_close()`. It also checks that the opened relation is actually a sequence (`RELKIND_SEQUENCE`). It is the counterpart to the heap access layer (`heap_open()` / `heap_close()`). Other relation types follow the same pattern, part of an access-method separation that PostgreSQL introduced progressively over several releases.

`sequence_open(Oid, LOCKMODE)` calls `relation_open()` and then `validate_relation_kind()`. If the relkind is not `RELKIND_SEQUENCE`, it raises an error via `errdetail_relkind_not_supported()`. This function produces a human-readable message naming the actual relkind. This prevents code that expects a sequence from accidentally operating on a table, index, or view with the same OID.

`sequence_close(Relation, LOCKMODE)` is a direct pass-through to `relation_close()`. The locking contract is the same: if `lockmode` is not `NoLock`, `sequence_close()` releases the lock; otherwise the caller is responsible for releasing it at transaction end.

The actual sequence increment logic — advancing `last_value`, caching values, WAL-logging changes — lives in `src/backend/commands/sequence.c`. This access-method file exists solely to provide the typed open/close interface.

## Related Topics

- [[code-paths/sequence]] — how `nextval()` executes, including the caching and WAL mechanisms
- [[subsystems/storage/table-am]] — the general table access method interface
- [[subsystems/storage/heap]] — the analogous layer for heap relations

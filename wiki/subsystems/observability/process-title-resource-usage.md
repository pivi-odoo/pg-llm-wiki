---
title: "Process Title and Resource Usage"
aliases:
  - process title
  - ps display
  - pg_rusage
  - set_ps_display
  - update_process_title
  - CPU timing
source_files:
  - src/backend/utils/misc/ps_status.c
  - src/backend/utils/misc/pg_rusage.c
  - src/include/utils/ps_status.h
  - src/include/utils/pg_rusage.h
symbols:
  - set_ps_display
  - set_ps_display_with_len
  - set_ps_display_suffix
  - init_ps_display
  - save_ps_display_args
  - flush_ps_display
  - pg_rusage_init
  - pg_rusage_show
  - PGRUsage
  - update_process_title
---

PostgreSQL maintains two parallel lightweight observability mechanisms, updated throughout a backend's lifecycle: the process title visible to `ps` and the operating system's process-level activity view, and resource usage snapshots. The snapshots supply CPU and elapsed-time figures to EXPLAIN ANALYZE and autovacuum logs. Both aim to impose negligible overhead on normal query processing.

## The Process Title

Every PostgreSQL backend rewrites its process title as it moves through different lifecycle states. The canonical format for a client backend is `postgres: username database host query`. When no query is active, the backend replaces `query` with a state string such as `idle`, `idle in transaction`, or `idle in transaction (aborted)`. This string is what `ps aux` and similar tools display. It is also the source of the `state` and `query` columns exposed through [[subsystems/observability/pg-stat-activity|pg_stat_activity]].

`set_ps_display()` (`ps_status.h`) performs the update. It is a thin inline that forwards to `set_ps_display_with_len()` (`ps_status.c`). The function writes the activity portion into an internal buffer after a fixed prefix. The prefix includes the cluster name (if `cluster_name` is set) and the backend type string initialised by `init_ps_display()`. PostgreSQL computes the fixed prefix once at backend startup; subsequent calls overwrite only the activity portion, keeping the write bounded and cheap.

A separate suffix mechanism (`set_ps_display_suffix()`, `set_ps_display_remove_suffix()`, `ps_status.c`) allows PostgreSQL to append and remove a transient annotation — such as a wait-event description — without disturbing the main activity string. The buffer tracks a `ps_buffer_nosuffix_len` watermark so that a new `set_ps_display()` call cleanly discards any outstanding suffix.

### Platform-Specific Write Mechanisms

The actual mechanism for making the new title visible to external tools varies by operating system. PostgreSQL selects it at compile time (`ps_status.c`):

- **Linux, AIX, Solaris, macOS, GNU Hurd** use `PS_USE_CLOBBER_ARGV`: during postmaster startup `save_ps_display_args()` measures the contiguous memory region occupied by `argv[]` and `environ[]`. It relocates the environment strings into freshly allocated memory and arranges for `argv[0]` to point at the process title buffer. Subsequently, `flush_ps_display()` writes the new title in place and zero-pads any remaining bytes from the previous, longer string. This is the most widely used path and the one that `ps` reads directly from the kernel's process metadata.
- **FreeBSD** uses `setproctitle_fast()`, a dedicated C library call optimised to minimise the cost of each update.
- **Other BSDs** use the standard `setproctitle()` library function.
- **Windows** does not expose a settable process title in the same sense; `PS_USE_WIN32` instead creates or replaces a named Windows Event object whose name encodes the status string, readable by tools like Process Explorer.

On platforms where none of these options are viable, PostgreSQL compiles out the feature entirely (`PS_USE_NONE`). `set_ps_display()` then becomes a no-op.

### The update_process_title GUC

The `update_process_title` boolean GUC (`ps_status.c`) gates all updates. It defaults to `true` on all platforms except Windows. There, the overhead of the named-event creation is measurable enough that it defaults to `false`. `update_ps_display_precheck()` performs the check early in both `set_ps_display_with_len()` and the suffix functions, so a disabled setting eliminates virtually all execution cost.

## Relation to pg_stat_activity

The same code paths that call `set_ps_display()` update the `state` and `wait_event` columns in [[subsystems/observability/pg-stat-activity|pg_stat_activity]]. The backend writes directly into its shared-memory activity entry (managed by the pgstat subsystem) at the same transition points where the process title changes — entering a query, leaving one, acquiring a lock that causes a wait. PostgreSQL keeps the two views of the backend's state in sync — the process title readable externally via `ps`, and the row in `pg_stat_activity` readable via SQL — because they are both consequences of the same backend state transitions, not because one copies from the other.

When you disable `track_activities` (the GUC that controls `pg_stat_activity` query tracking), PostgreSQL does not write the current query string into the shared-memory entry. Process title updates via `set_ps_display()` continue independently — the two GUCs are orthogonal.

## Resource Usage Snapshots

`pg_rusage.c` provides a two-call API for measuring the CPU time and wall-clock time consumed by an operation:

```c
typedef struct PGRUsage {
    struct timeval tv;   /* wall-clock time at snapshot */
    struct rusage  ru;   /* kernel resource usage at snapshot */
} PGRUsage;
```

`pg_rusage_init()` captures both a `gettimeofday()` result and a `getrusage(RUSAGE_SELF, ...)` result into a `PGRUsage` struct. `pg_rusage_show()` takes a `const PGRUsage *` representing the earlier snapshot. It calls `pg_rusage_init()` again to get the current values, computes the deltas, and formats them into a static string of the form:

```
CPU: user: 0.01 s, system: 0.00 s, elapsed: 0.05 s
```

This string appears as the `cpu: ...` line at the end of each operation in EXPLAIN ANALYZE output (managed by `explain.c` using a `PGRUsage` taken before and after each node's execution) and in [[subsystems/background/autovacuum|autovacuum]] log lines that report per-table vacuum and analyze durations.

The API intentionally relies on `getrusage()`, a single lightweight system call that returns accumulated user and system CPU times from the kernel's per-process accounting. There is no IPC, no shared memory, and no lock acquisition. The only shared state is the static result buffer inside `pg_rusage_show()`. This is safe because PostgreSQL backends are single-threaded.

The process title write is similarly inexpensive: it is a bounded `memcpy` into a buffer that already exists in the process's address space (the former `argv` region on Linux). There is no system call beyond whatever the kernel does when `ps` reads `/proc/<pid>/cmdline`; the backend itself pays no kernel-crossing cost per update. `gettimeofday()` is typically a vDSO call that does not enter the kernel at all. `getrusage()` reads kernel accounting fields that are already maintained per-process. The combined snapshot cost is therefore measured in microseconds. Neither mechanism involves communication with other backends — there is no lock contention and no shared-memory synchronisation on the write path. This is why both can be called freely at high frequency during normal query processing.

## Related Topics

- [[subsystems/observability/pg-stat-activity|pg_stat_activity]]
- [[subsystems/observability/overview|observability overview]]
- [[code-paths/explain|EXPLAIN]]

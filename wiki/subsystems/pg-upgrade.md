---
title: "pg_upgrade"
aliases:
  - "pg_upgrade"
  - "major version upgrade"
source_files:
  - src/bin/pg_upgrade/pg_upgrade.c
  - src/bin/pg_upgrade/pg_upgrade.h
  - src/bin/pg_upgrade/check.c
  - src/bin/pg_upgrade/relfilenumber.c
symbols:
  - transferMode
  - TRANSFER_MODE_LINK
  - TRANSFER_MODE_CLONE
  - transfer_all_new_tablespaces
  - transfer_relfile
  - FileNameMap
  - VISIBILITY_MAP_FROZEN_BIT_CAT_VER
  - JSONB_FORMAT_CHANGE_CAT_VER
  - check_for_data_type_usage
  - UserOpts
---

# pg_upgrade

`pg_upgrade` upgrades a PostgreSQL cluster to a new major version without a
dump/restore cycle. A traditional `pg_dump | psql` upgrade rewrites every row
into a fresh database — for large clusters this can take many hours.
`pg_upgrade` avoids this by moving the physical heap files directly from the
old cluster to the new one. It then rewrites only the system catalogs that
changed between versions. User data stays in place; only catalog rows are
touched. This is possible because PostgreSQL's on-disk heap format — page
layout, tuple header fields, MVCC visibility information — has been stable
across major versions. What does change between major versions is the *system
catalog schema*: `pg_proc` gains new columns, `pg_class` fields shift, internal
OID spaces are reorganized. `pg_upgrade` rewrites exactly those catalog rows.
It leaves user data untouched.

To keep this safe, `pg_upgrade` enforces that certain OID spaces remain
identical between the old and new clusters. It controls `pg_class.relfilenode`
so relation filenames match on disk. It controls `pg_type.oid` because type
OIDs are embedded in composite values. It controls `pg_enum.oid` because enum
OIDs are stored directly in user table rows. It controls `pg_tablespace.oid`
so tablespace directories align. These invariants are documented at the top of
`pg_upgrade.c`. They are a prerequisite for the file-transfer strategy to work
at all.

## The hard-link optimization

By default, `pg_upgrade` copies relation files from the old cluster to the new
cluster. Copying is safe: if anything goes wrong, the old cluster remains untouched
and you can roll back. However, copying time is proportional to data size,
so it is slow for large clusters.

Passing `--link` switches the transfer mode to hard links. A hard link creates
an additional directory entry pointing to the same inode, so the "copy" is
instantaneous regardless of data size. Only catalog rewrites, which touch a
small fraction of total data, consume meaningful time. The tradeoff is
significant: after a hard-linked upgrade, the old and new clusters share
inodes. Starting the old cluster after the new one has been running would cause
both to modify the same files, corrupting both.

The `transferMode` enum in `pg_upgrade.h` lists three modes:

- `TRANSFER_MODE_COPY` — byte-for-byte copy; safe to roll back
- `TRANSFER_MODE_LINK` — hard link; near-instantaneous, but no rollback
- `TRANSFER_MODE_CLONE` — copy-on-write reflink, available on some filesystems

**PostgreSQL 17:** `--copy-file-range` adds a fourth transfer mode that uses the
`copy_file_range(2)` syscall on Linux and FreeBSD. The kernel copies file data
directly between file descriptors without reading it into userspace. This makes
it more efficient than a standard read/write copy. It still produces independent
files that allow rollback.

**PostgreSQL 18:** `--swap` mode swaps the old and new cluster directories (or
hard-links the relation files in the opposite direction) rather than copying
data to the new cluster. The actual cutover — the directory rename — is nearly
instantaneous regardless of database size, significantly reducing downtime for
large clusters. Unlike `--link`, the old cluster directory remains separately
recoverable until explicitly deleted.

The actual dispatch happens in `transfer_all_new_tablespaces()` in
`relfilenumber.c`. This function selects the appropriate file operation per relation.

## The upgrade process

### Pre-upgrade checks

Both clusters must be shut down before `pg_upgrade` runs. The checks in
`check.c` verify:

- Encoding and locale compatibility between the two clusters
- Data checksum version consistency (`ControlData.data_checksum_version`)
- No prepared transactions open in the old cluster
- No user tables containing `reg*` columns (`regproc`, `regclass`,
  `regtype`, etc.) — these types store catalog OIDs as values and would become
  stale after the catalog is rebuilt
- No user-defined postfix operators, incompatible polymorphics, or tables
  with OIDs, which have been removed or incompatibly changed

Any incompatibility causes `pg_upgrade` to abort with a descriptive message
before any data has moved.

**PostgreSQL 18:** The initial database checks phase is parallelized when
`--jobs` is specified, reducing total check time on clusters with many
databases.

### Catalog initialization

The new cluster is initialized with `initdb` (or verified if the user
pre-initialized it). This populates the new cluster's `pg_catalog` with all
system objects at the new version's schema. At this point the new cluster has
correct system objects but no user databases.

### Data file transfer

`pg_upgrade` iterates over every user database. For each relation, it builds a
`FileNameMap` (defined in `pg_upgrade.h`) that records the old and new
tablespace paths and the relation's `relfilenumber`. It then calls
`transfer_relfile()` for each file segment, choosing clone, copy, or hard link
based on the selected mode.

[[subsystems/storage/visibility-map|Visibility map]] files are handled specially. If the old cluster predates the
frozen-bit format change (tracked by the constant
`VISIBILITY_MAP_FROZEN_BIT_CAT_VER 201603011` in `pg_upgrade.h`), the
visibility map must be rewritten rather than simply transferred.

[[subsystems/storage/toast|Toast]] OIDs receive special care: because toast pointers are stored inside user
table rows, toast table OIDs must be identical between old and new clusters.
`pg_upgrade` controls these assignments explicitly during the catalog
initialization step.

### Catalog upgrade scripts

The new cluster binary includes SQL scripts that transform the old catalog
layout into the new one. `pg_upgrade` starts the new cluster in a special
single-user restricted mode that processes only internal catalog commands.
It then runs these scripts. User tables are not touched during this phase.

### Configuration files

`pg_upgrade` does not copy `pg_hba.conf` or `postgresql.conf` from the old
cluster. Authentication and server parameters must be reconfigured in the new
cluster manually. This is intentional: the new version may have deprecated,
renamed, or split parameters. Blindly copying the old configuration could
cause the new server to fail to start.

## What pg_upgrade cannot handle

Some data types have changed their on-disk representation between major
versions. Historically, geometric types, `tsquery`, `tsvector`, and `jsonb`
(during the 9.4 beta period, tracked by `JSONB_FORMAT_CHANGE_CAT_VER`) have
all had format changes. `pg_upgrade` checks for columns of these types in user
tables. It fails with a clear message if any are found. The user must dump and
restore those specific tables separately.

Extensions that store data in their own tables may also need separate upgrade
steps. Extension authors are expected to provide upgrade scripts invoked by
`ALTER EXTENSION ... UPDATE`. The version-specific detection functions in
`version.c` — including `check_for_data_type_usage()` and friends — handle
incompatibility detection before any files are moved.

Large objects are handled transparently. They live in `pg_largeobject`, a
regular catalog table. `pg_upgrade` migrates it like any other user relation.

## Logical replication slots and subscriptions

Prior to PostgreSQL 17, `pg_upgrade` does not carry over logical replication
slots on the old cluster. Slots must be dropped before the upgrade and
recreated on the new cluster; subscriptions on the subscriber side likewise
require manual recreation with fresh replication origins and positions.

**PostgreSQL 17:** When both the source and target clusters are PostgreSQL 17 or
later, `pg_upgrade` preserves logical replication slots on publishers. It
transfers full subscription state — including replication origins and confirmed
flush positions — on subscribers. Upgrades from versions earlier than 17 still
require manual recreation of slots and subscriptions.

## --check mode

Running `pg_upgrade --check` executes all pre-upgrade validation without making
any changes to either cluster. The `UserOpts.check` flag in `pg_upgrade.h`
gates this code path throughout. The old cluster can remain running during
`--check`, enabling validation well before a maintenance window. This is the
recommended first step for any production upgrade: it surfaces encoding
mismatches, incompatible data types, problematic extensions, and other blockers
at zero risk.

## After the upgrade

`pg_upgrade` leaves two scripts in the current working directory:

**`analyze_new_cluster.sh`** runs `ANALYZE` on every table in the new cluster.
Without running this script, the planner falls back to default estimates. It
can then produce severely suboptimal plans on the first queries after the upgrade.
Running `ANALYZE` immediately — or enabling [[subsystems/background/autovacuum|autovacuum]] and letting it catch
up — is critical before exposing the new cluster to production traffic.

**PostgreSQL 18:** `pg_upgrade` preserves table-level optimizer statistics
(per-relation and per-attribute stats) in the new cluster during the upgrade
itself, so the new cluster does not require an immediate `ANALYZE` before
serving queries. Extended statistics objects are not preserved and must be
rebuilt separately. Pass `--no-statistics` to skip statistics transfer and
revert to the pre-18 behavior of running `analyze_new_cluster.sh` manually.

**`delete_old_cluster.sh`** removes the old cluster's data directory. This
should only be run after the new cluster has been verified as healthy. In
hard-link mode, running this script also reclaims disk space: until one side
is deleted, both clusters share the same inodes and the disk usage is not
freed.

## Downtime and zero-downtime alternatives

`pg_upgrade` requires both clusters to be offline for the duration of the
upgrade. With `--link`, the downtime is typically minutes — the time to build
hard links plus run catalog scripts. Without `--link`, it is proportional to
data size and can be hours.

**PostgreSQL 18:** `--swap` mode reduces cutover time to the near-instantaneous
cost of directory renames, making `pg_upgrade` viable for large clusters where
even `--link` would require significant preparation time.

For workloads that cannot tolerate extended downtime, the alternative is to set
up [[subsystems/replication/logical|logical replication]] from the old cluster to a new cluster running the
target major version, let replication converge, and then perform a brief
cutover. This reduces user-visible downtime to seconds but is operationally
complex: it requires managing replication slots, handling DDL changes during
replication, verifying sequence values, and carefully timing the cutover window.

The two approaches are complementary. `pg_upgrade` is simpler and appropriate
when a maintenance window of acceptable length is available. Logical replication
upgrades minimize downtime at the cost of significantly more planning and
operational overhead.

## Related Topics

- [[subsystems/storage/pg-upgrade-support|pg_upgrade Storage Support]] — storage-layer helpers (relation file mapping, OID reservation) that the upgrade binary depends on to transfer heap files safely between cluster versions.
- [[subsystems/catalog/core-catalogs|Core Catalogs]] — the system catalog tables whose schema changes between major versions are the primary target of the catalog upgrade scripts that pg_upgrade runs.
- [[subsystems/replication/logical|Logical Replication]] — the zero-downtime upgrade alternative: replicating from the old cluster to a new major-version cluster before a brief cutover.
- [[subsystems/replication/slots|Replication Slots]] — slots must be dropped before upgrading from pre-17 clusters and are preserved or recreated as part of the PostgreSQL 17+ slot-transfer feature.
- [[subsystems/storage/checksums|Page Checksums]] — pg_upgrade validates that data checksum settings match between the old and new clusters during its pre-upgrade checks.
- [[subsystems/transactions/two-phase-commit|Two-Phase Commit]] — prepared transactions in the old cluster are an explicit blocker; pg_upgrade aborts if any are found during pre-upgrade validation.
- [[code-paths/analyze|ANALYZE]] — the mandatory post-upgrade step that rebuilds planner statistics in the new cluster so query plans remain optimal from the first queries.

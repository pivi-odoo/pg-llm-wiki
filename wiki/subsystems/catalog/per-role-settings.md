---
title: "Per-Role and Per-Database GUC Settings"
aliases:
  - ALTER ROLE SET
  - ALTER DATABASE SET
  - pg_db_role_setting
  - per-role GUC
  - per-database GUC
source_files:
  - src/backend/catalog/pg_db_role_setting.c
  - src/include/catalog/pg_db_role_setting.h
  - src/backend/utils/init/postinit.c
  - src/backend/commands/user.c
  - src/backend/commands/dbcommands.c
symbols:
  - AlterSetting
  - DropSetting
  - ApplySetting
  - process_settings
  - AlterRoleSet
  - AlterDatabaseSet
---

`ALTER ROLE … SET` and `ALTER DATABASE … SET` let administrators attach persistent GUC defaults to a role, a database, or the combination of both, without touching `postgresql.conf`. The shared catalog `pg_db_role_setting` stores the settings. The backend applies them automatically to every new session that matches, before any user SQL runs. This mechanism is the standard way to give each application its own [[subsystems/catalog/schema-search-path|search_path]], enforce per-tenant timeouts, or isolate memory budgets without writing wrapper scripts.

## The pg_db_role_setting catalog

`pg_db_role_setting` (OID 2964, `DbRoleSettingRelationId`) is a shared catalog — it lives in the `global/` tablespace and is visible across all databases on the same cluster. Its schema is minimal:

| Column | Type | Meaning |
|---|---|---|
| `setdatabase` | `oid` | Database OID, or `0` for a role-only or global setting |
| `setrole` | `oid` | Role OID, or `0` for a database-only or global setting |
| `setconfig` | `text[]` | Array of `'name=value'` strings, one per GUC assignment |

A unique index on `(setdatabase, setrole)` enforces that at most one row exists per targeting combination. All GUC assignments for a given (database, role) pair are packed into the `setconfig` array of a single row. The array format matches what `ProcessGUCArray()` expects, so the same machinery used for `proconfig` (per-function GUC overrides) handles it.

Because the catalog is shared, `DROP DATABASE` and `DROP ROLE` must clean up their respective rows. `DropSetting()` (`pg_db_role_setting.c`) handles both cases. The respective drop commands call it.

## Targeting combinations

The `(setdatabase, setrole)` key supports four meaningful targeting combinations. Zero OIDs (`InvalidOid`) act as wildcards:

| `setdatabase` | `setrole` | SQL form | Applies to |
|---|---|---|---|
| database OID | role OID | `ALTER ROLE r IN DATABASE d SET …` | Sessions where both match |
| `0` | role OID | `ALTER ROLE r SET …` | Sessions for role `r` in any database |
| database OID | `0` | `ALTER DATABASE d SET …` | Any session connecting to database `d` |
| `0` | `0` | `ALTER ROLE ALL SET …` (superuser only) | Every session on the cluster |

The global (`0`, `0`) combination is rarely needed in practice. It provides a way to set a cluster-wide default that `postgresql.conf` does not carry — for example, when the config file is managed by an orchestration tool that does not expose all GUC variables.

## How settings are applied at connection time

During `InitPostgres()` (`postinit.c`), after authentication is complete and before user code runs, the backend calls `process_settings()`. That function opens `pg_db_role_setting` under an `AccessShareLock` and takes a catalog snapshot, then calls `ApplySetting()` four times, once for each targeting combination, in order from most specific to least specific:

```
database+role  (PGC_S_DATABASE_USER)
role-only      (PGC_S_USER)
database-only  (PGC_S_DATABASE)
global         (PGC_S_GLOBAL)
```

Each call to `ApplySetting()` scans for a row matching the exact `(databaseid, roleid)` pair passed to it, then feeds the `setconfig` array into `ProcessGUCArray()` at `PGC_SUSET` context. The GUC engine tracks the *source* of every variable's current value — `PGC_S_DATABASE_USER` ranks highest among the four, `PGC_S_GLOBAL` lowest. Inside `set_config_option_ext()` (`guc.c`), if the incoming source is lower priority than what is already recorded for that variable, the function silently ignores the new value:

```c
if (record->source > source)
{
    elog(DEBUG3, "\"%s\": setting ignored because previous source is higher priority", name);
    return -1;
}
```

This ordering means the most specific setting always wins. For `work_mem`, the precedence runs from most to least specific: `(role r, database d)`, then `(role r, any database)`, then a database-wide default, then a global default. A subsequent session-level `SET work_mem = …` (source `PGC_S_SESSION`) overrides all of them for the duration of that session.

The four `ApplySetting()` calls share a single catalog snapshot acquired before the loop, for efficiency.

## Precedence relative to other GUC sources

The full GUC source priority chain, from lowest to highest, is:

```
compiled-in default
  → postgresql.conf / ALTER SYSTEM (PGC_S_FILE)
    → global pg_db_role_setting (PGC_S_GLOBAL)
      → database pg_db_role_setting (PGC_S_DATABASE)
        → role pg_db_role_setting (PGC_S_USER)
          → role-in-database pg_db_role_setting (PGC_S_DATABASE_USER)
            → client startup packet options (PGC_S_CLIENT)
              → SET command / set_config() (PGC_S_SESSION)
```

The startup sequence applies `postgresql.conf` settings earlier, before `process_settings()` runs, so a per-role or per-database setting can override a value from the config file. Session-level `SET` and client startup options (the `options` field in the startup packet) override everything from `pg_db_role_setting`.

## Modifying and removing settings

`AlterSetting()` (`pg_db_role_setting.c`) is the write path for all four SQL forms. It scans the catalog for an existing row matching the `(databaseid, roleid)` pair, then:

- **SET**: calls `GUCArrayAdd()` to merge the new `name=value` pair into the array, inserting a new row if none exists.
- **RESET name**: calls `GUCArrayDelete()` to remove the named entry from the array; deletes the row entirely if the array becomes empty.
- **RESET ALL**: calls `GUCArrayReset()` which removes all entries whose `default_val` flag is not set; deletes the row if nothing remains.

`SET` and `RESET DEFAULT` are semantically equivalent — both remove the explicit override and cause the compiled-in default (or `postgresql.conf` value) to apply. Only an explicit `SET guc = value` stores a value; `RESET` or `SET guc TO DEFAULT` removes the entry from the array.

The permission model for writing settings (`AlterRoleSet()`, `user.c`):

- **Global** (`ALTER ROLE ALL SET`): requires superuser.
- **Database-only** (`ALTER DATABASE d SET`): requires ownership of the database.
- **Role-only** or **role-in-database**: requires superuser, or `CREATEROLE` plus `ADMIN OPTION` on the target role, or be the target role itself (for self-modification).

## Practical patterns

**Schema isolation per application.** The most common use of this mechanism is pinning `search_path` so different applications connecting to the same database cannot accidentally share or shadow each other's objects:

```sql
ALTER ROLE app_user SET search_path = app_schema, public;
ALTER ROLE admin_user SET search_path = admin_schema, app_schema, public;
```

The `$user` substitution in the default path achieves similar isolation automatically when each role has its own schema with the same name — see [[subsystems/catalog/schema-search-path|search_path]].

**Per-role memory limits.** [[subsystems/executor/work-mem-and-spill|work_mem]] governs how much memory a sort or hash can use before spilling to disk. A reporting role that runs large analytical queries can be given a higher budget:

```sql
ALTER ROLE reporting_user SET work_mem = '256MB';
```

This has no effect on OLTP roles that did not get an explicit setting.

**Per-database timezone and locale.** A multi-tenant cluster hosting databases for clients in different regions can set `TimeZone` at the database level without touching `postgresql.conf`:

```sql
ALTER DATABASE asia_db SET TimeZone = 'Asia/Tokyo';
ALTER DATABASE eu_db SET TimeZone = 'Europe/Berlin';
```

**Timeout enforcement.** `statement_timeout` and `lock_timeout` can be enforced per role without requiring application-level discipline:

```sql
ALTER ROLE web_app SET statement_timeout = '5s';
ALTER ROLE web_app SET lock_timeout = '2s';
```

The most specific override wins, so an administrator can set a higher (or no) timeout for a DBA role even if the database has a tight default:

```sql
ALTER DATABASE mydb SET statement_timeout = '30s';
ALTER ROLE dba_user IN DATABASE mydb SET statement_timeout = '0';  -- unlimited
```

## Interaction with connection poolers

Connection poolers in **session mode** (one backend per application session for its lifetime) are transparent to this mechanism — the backend receives the settings at connect time and retains them throughout.

**Transaction mode** poolers (such as PgBouncer in `transaction` mode) present a complication. The backend is shared across multiple application "sessions"; its initial GUC state was established when the backend first connected. If the pooler routes a client that authenticated as `role_a` onto a backend that originally connected as `role_b`, the GUC state reflects `role_b`'s defaults. PgBouncer in transaction mode does not re-run `process_settings()` between clients — it cannot, because the backend is already past `InitPostgres()`.

The practical consequence is that transaction-mode pooling does **not reliably apply** per-role GUC settings, unless the pooler is configured to issue `RESET ALL` (or a custom reset query) before handing the connection to a new client. Even then, the reset returns to `postgresql.conf` defaults, not to per-role settings. Applications that depend on per-role defaults under a transaction-mode pooler must either pin the setting in the connection string, issue a `SET` immediately after acquiring a connection, or use per-database settings (which are stable because the backend always connects to a fixed database).

## Inspecting current settings

```sql
-- View all stored settings
SELECT rolname, datname, setconfig
FROM pg_db_role_setting s
LEFT JOIN pg_roles r ON r.oid = s.setrole
LEFT JOIN pg_database d ON d.oid = s.setdatabase;

-- Show what is currently in effect for the active session
SELECT name, setting, source
FROM pg_settings
WHERE source IN ('database', 'user', 'database user');
```

The `pg_settings.source` column uses the string labels `'database'`, `'user'`, and `'database user'` for the three lower priority tiers of `pg_db_role_setting`, and `'global'` for the (`0`, `0`) row. A source of `'session'` means a `SET` command overrode the stored default.

## Related Topics

- [[subsystems/catalog/schema-search-path|search_path]] — the most commonly pinned setting via this mechanism
- [[subsystems/auth/role-management|role management]] — `pg_authid` and `CREATE/ALTER/DROP ROLE`
- [[architecture/client-connection|client connection]] — the full `InitPostgres()` sequence in which `process_settings()` runs
- [[subsystems/catalog/core-catalogs|core system catalogs]] — `pg_db_role_setting` in context of other shared catalogs

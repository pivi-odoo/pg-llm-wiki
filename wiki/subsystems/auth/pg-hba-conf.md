---
title: "pg_hba.conf: Options, Ordering, and Inspection"
aliases:
  - "pg_hba.conf deep dive"
  - "HBA options"
  - "pg_hba_file_rules"
tags:
  - symptom/auth-failure
source_files:
  - src/backend/libpq/hba.c
  - src/backend/utils/adt/hbafuncs.c
  - src/include/libpq/hba.h
  - src/backend/catalog/system_views.sql
symbols:
  - tokenize_auth_file
  - parse_hba_auth_opt
  - HbaLine
  - ClientCertMode
  - check_hba
  - load_hba
  - fill_hba_view
  - CONF_FILE_MAX_DEPTH
---

The [[subsystems/auth/overview]] page covers the connection pipeline, method names, address matching, and reload behavior. This page goes deeper into the parts that matter most in practice: the full set of per-method options, the `include` directives added in PostgreSQL 16, the system views that expose the parsed rule set, ordering strategies, and the failure modes that trip up experienced operators.

## Include Directives

PostgreSQL 16 added three record types that let you split `pg_hba.conf` across multiple files. The tokenizer in `hba.c` handles them during `tokenize_auth_file()` before any line is parsed.

| Directive | Behavior |
|---|---|
| `include <path>` | Include the file; error if it does not exist |
| `include_if_exists <path>` | Include the file; silently skip if absent |
| `include_dir <directory>` | Include all `*.conf` files in the directory, sorted by name |

PostgreSQL resolves relative paths relative to the data directory. Include files can themselves contain further `include` directives. `CONF_FILE_MAX_DEPTH` (defined in `conffiles.h`) bounds the nesting depth to prevent infinite loops. Each included file shows up as its own `file_name` in `pg_hba_file_rules`, so you can always tell which file a rule came from.

A typical use is to put emergency superuser access in `/etc/postgresql/pg_hba_emergency.conf`. Include it at the top of `pg_hba.conf` with `include`. Protect that file with narrower filesystem permissions. Application team changes to the main file cannot touch the emergency entry.

## Method-Specific Options

Options appear after the auth method as `key=value` pairs. The parser in `parse_hba_auth_opt()` validates each option against the method's allowed set; an unrecognized option name is a fatal parse error for that line.

### scram-sha-256 / md5 / password

These three password-based methods share the same option set for certificate layering (see `clientcert` below). None of them require any other options.

### peer

| Option | Effect |
|---|---|
| `map=<mapname>` | Consult `pg_ident.conf` to translate the OS username to a PostgreSQL username |

Without `map`, the OS username must equal the PostgreSQL username exactly. With a map, PostgreSQL applies the rules in `pg_ident.conf` under that map name.

### gss (Kerberos)

| Option | Default | Effect |
|---|---|---|
| `include_realm=0\|1` | `1` | Whether the Kerberos realm is included in the derived username. Defaults to `1`; setting it to `0` is dangerous in multi-realm environments because principals from different realms would be indistinguishable |
| `map=<mapname>` | — | `pg_ident.conf` mapping for the principal name |
| `krb_realm=<realm>` | — | Only accept principals from this realm; principals from other realms are rejected |
| `upn_username=0\|1` | `0` | (SSPI only) Use the User Principal Name format instead of the SAM-compatible format |

### ldap

LDAP supports two binding modes that are mutually exclusive: simple bind (uses `ldapprefix`/`ldapsuffix` to construct the bind DN directly) and search-then-bind (searches for the DN first using `ldapbasedn`).

| Option | Effect |
|---|---|
| `ldapserver=<host>` | LDAP server hostname (required unless `ldapurl` is given) |
| `ldapport=<port>` | Server port (default 389 plain, 636 with `ldaptls`) |
| `ldaptls=0\|1` | Upgrade the connection with StartTLS |
| `ldapscheme=ldap\|ldaps` | Use `ldaps://` for implicit TLS from the start |
| `ldapprefix=<string>` | Prepended to username to form the bind DN (simple bind mode) |
| `ldapsuffix=<string>` | Appended to username to form the bind DN (simple bind mode) |
| `ldapbasedn=<dn>` | Base DN for the directory search (search-then-bind mode) |
| `ldapbinddn=<dn>` | DN to use for the initial search bind (search-then-bind mode) |
| `ldapbindpasswd=<pw>` | Password for the search bind DN |
| `ldapsearchattribute=<attr>` | Attribute to match the PostgreSQL username against (default `uid`) |
| `ldapsearchfilter=<filter>` | Full LDAP filter string; cannot be combined with `ldapsearchattribute` |
| `ldapurl=<url>` | Full `ldap://` or `ldaps://` URL encoding all of the above |

`ldapprefix` and `ldapsuffix` cannot be combined with `ldapbasedn`, `ldapbinddn`, `ldapbindpasswd`, `ldapsearchattribute`, or `ldapsearchfilter`. The parser enforces this at load time.

### radius

RADIUS is a multi-server protocol. All four list options accept comma-separated values. Their list lengths must either be 1 (applied to all servers) or equal the number of servers.

| Option | Effect |
|---|---|
| `radiusservers=<host,...>` | One or more RADIUS server hostnames or IPs (required) |
| `radiussecrets=<secret,...>` | Shared secrets, one per server or one for all |
| `radiusports=<port,...>` | Server ports, default 1812 |
| `radiusidentifiers=<id,...>` | NAS-Identifier attribute sent in the request |

### cert

The `cert` method requires a `hostssl` connection. It implicitly sets `clientcert=verify-full`. The only additional option is:

| Option | Effect |
|---|---|
| `clientcertname=cn\|dn` | Whether to use the certificate CN (default) or full DN to derive the PostgreSQL username |

### clientcert: layering certificates on top of passwords

Any `hostssl` line can add certificate verification on top of a password-based method using the `clientcert` option:

| Value | Effect |
|---|---|
| `verify-ca` | Client certificate must be signed by a trusted CA; CN is not checked against username |
| `verify-full` | Certificate must be signed by a trusted CA and the CN must match the PostgreSQL username |

The `HbaLine` struct captures this as a `ClientCertMode` enum (`clientCertOff`, `clientCertCA`, `clientCertFull`) in `hba.h`. When used with `cert` auth, PostgreSQL permits only `verify-full`. The companion `clientcertname=dn` option switches the name comparison from CN to the full Distinguished Name.

Example combining SCRAM with mandatory client cert:

```
hostssl  all  all  10.0.0.0/8  scram-sha-256  clientcert=verify-full
```

This requires both a valid client certificate whose CN matches the username and the correct SCRAM password.

## Inspecting Rules with pg_hba_file_rules

`pg_hba_file_rules` is a superuser-only view that re-parses `pg_hba.conf` (and any included files) on each query. It returns one row per non-empty, non-comment line. It does not read the in-memory parsed list. Instead, it re-tokenizes from disk, so it reflects the file on disk even before a reload.

```sql
SELECT rule_number, file_name, line_number, type, database, user_name,
       address, netmask, auth_method, options, error
FROM pg_hba_file_rules
ORDER BY rule_number;
```

| Column | Type | Notes |
|---|---|---|
| `rule_number` | integer | Sequence number among successfully parsed rules; NULL for lines with errors |
| `file_name` | text | Which file the line came from (useful with `include`) |
| `line_number` | integer | Line number within that file |
| `type` | text | Connection type string (`local`, `host`, `hostssl`, etc.) |
| `database` | text[] | Database name list |
| `user_name` | text[] | Role name list |
| `address` | text | IP address or hostname |
| `netmask` | text | Subnet mask |
| `auth_method` | text | Auth method name |
| `options` | text[] | Method-specific options as `key=value` strings |
| `error` | text | Parse error message; NULL on success |

Lines with errors still appear in the view — `rule_number` is NULL and `error` is populated. This makes the view the right tool to diagnose a failed reload: after `SELECT pg_reload_conf()`, query for `error IS NOT NULL`.

```sql
-- Find all lines that failed to parse
SELECT file_name, line_number, error
FROM pg_hba_file_rules
WHERE error IS NOT NULL;
```

Note that `fill_hba_view()` in `hbafuncs.c` opens the file fresh each time, so this view always shows the on-disk state, not the state that was last successfully loaded.

## pg_ident_file_mappings

Added alongside `pg_hba_file_rules` in PostgreSQL 16, this superuser-only view exposes the parsed content of `pg_ident.conf`:

```sql
SELECT map_number, file_name, line_number,
       map_name, sys_name, pg_username, error
FROM pg_ident_file_mappings
ORDER BY map_number;
```

| Column | Notes |
|---|---|
| `map_number` | Sequence number among valid mappings; NULL on error |
| `map_name` | The map name referenced by `map=` in `pg_hba.conf` |
| `sys_name` | OS username or regex pattern |
| `pg_username` | PostgreSQL username or back-reference pattern |
| `error` | Parse error message |

Use this view to verify that a regex mapping resolves as expected before enabling a new `peer` or `gss` entry.

## Ordering: First Match Wins

`check_hba()` walks `parsed_hba_lines`. It stops at the first entry that matches the connection. There is no scoring, no specificity ranking — only position. This simple rule has several implications.

**Explicit rejects must come before permissive rules.** To block a specific host before a broader subnet rule catches it:

```
# Block a compromised host before the broader rule
host  all  all  192.168.1.99/32   reject
host  all  all  192.168.1.0/24    scram-sha-256
```

**Separate replication connections from regular ones.** The `replication` pseudo-database only matches replication protocol connections (WAL streaming, `pg_basebackup`). Put replication entries near the top so they do not interact with per-database entries lower down:

```
local  replication  replicator              peer
host   replication  replicator  10.0.0.0/8  scram-sha-256
```

Remember that `pg_basebackup` uses the replication pseudo-database, so the above entry covers both streaming replicas and base backups.

**Per-database auth methods via ordering.** Different databases can require different methods even from the same host by placing more specific entries first:

```
# Sensitive database requires certificate auth
hostssl  finance   all  10.0.0.0/8  cert
# Everything else gets SCRAM
hostssl  all       all  10.0.0.0/8  scram-sha-256
```

## Testing Changes Safely

The standard reload path is:

```sql
SELECT pg_reload_conf();
```

This sends `SIGHUP` to the postmaster, which calls `load_hba()` to re-parse the file. If parsing succeeds, new connections use the new rules; existing connections are unaffected. If parsing fails, the old rules remain active. The postmaster logs the error, but it may not be visible to the session that called `pg_reload_conf()`.

To verify the reload succeeded, check for errors immediately after:

```sql
SELECT pg_reload_conf();
SELECT file_name, line_number, error
FROM pg_hba_file_rules
WHERE error IS NOT NULL;
```

An empty result means all lines parsed cleanly. A non-empty result means the file on disk has errors. Because parsing failed, the old rules are still in effect. You can fix the file and reload again without losing access.

**Keep a superuser emergency entry near the top and never remove it:**

```
# Emergency access — never remove this line
local  all  postgres  peer
```

Place this before any other `local` entries. Because it uses `peer` (not `password`), credential rotation cannot lock it out. If a bad `pg_hba.conf` reload ever drops all authentication methods, you can still connect via `psql -U postgres` from the OS user `postgres` on the server itself. From there you can fix the file.

## Common Misconfigurations

**`host` instead of `hostssl` for password methods.** A line like:

```
host  all  all  0.0.0.0/0  password
```

sends passwords in plaintext over the network. Replace `host` with `hostssl` for any method that transmits credentials.

**`all` in the user field creating unintended superuser access.** An entry of the form `host all all 0.0.0.0/0 trust` or even `host all all 0.0.0.0/0 scram-sha-256` allows any role to connect — including superusers — from any address. Prefer explicit role lists or `+group_name` for sensitive environments, or add a preceding `reject` for superuser roles from untrusted addresses.

**DNS-based address matching with unreliable reverse DNS.** A hostname in the address field triggers a double-lookup (reverse then forward) on every connection. If the DNS response is slow, spoofed, or absent, authentication fails or becomes non-deterministic. Prefer CIDR notation for production rules.

**Missing replication entry for streaming replication.** A new standby that cannot authenticate will stall at the replication start. The required entry is in the `replication` pseudo-database, which is distinct from `all`:

```
host  replication  replicator  <standby-ip>/32  scram-sha-256
```

If this line is missing, `pg_basebackup` and `pg_receivewal` will also fail, because they connect to the `replication` pseudo-database.

**Forgetting `include_realm` for GSS.** The default `include_realm=1` means the authenticated principal name includes the realm — for example `alice@EXAMPLE.COM`. Without a `map=` entry that strips the realm, the username presented to PostgreSQL is `alice@EXAMPLE.COM`, which will not match a role named `alice`. Either set `include_realm=0` (safe only in single-realm deployments) or use a `pg_ident.conf` map to translate `alice@EXAMPLE.COM` to `alice`.

## Related Topics

- [[subsystems/auth/overview|Authentication Overview]] — covers the full connection pipeline, all method names, address matching logic, and reload behavior that this page extends.
- [[subsystems/auth/ssl-tls|SSL/TLS]] — details on configuring server and client certificates referenced by `hostssl` lines and the `clientcert` option.
- [[subsystems/auth/gssapi|GSSAPI]] — deep dive into Kerberos/GSS authentication, realm handling, and `pg_ident.conf` mapping that the `gss` method relies on.
- [[subsystems/auth/role-management|Role Management]] — how roles and groups are defined, which directly determines what goes in the user field of HBA rules.
- [[subsystems/roles-privileges|Roles and Privileges]] — covers the privilege model that HBA rules gate access to, including superuser and group membership.
- [[architecture/client-connection|Client Connection]] — describes the full connection lifecycle from the postmaster accepting a socket to a backend being spawned, placing HBA evaluation in context.
- [[troubleshooting/auth-failures|Authentication Failures]] — practical guide to diagnosing connection rejections, including how to read HBA-related error messages.

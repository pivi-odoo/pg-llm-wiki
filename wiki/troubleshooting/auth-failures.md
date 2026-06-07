---
title: "Diagnosing Authentication Failures"
aliases:
  - "Auth Failures"
  - "Authentication Troubleshooting"
tags:
  - symptom/auth-failure
source_files:
  - src/backend/libpq/auth.c
  - src/backend/libpq/hba.c
  - src/backend/libpq/be-secure.c
symbols:
  - auth_failed
  - check_hba
  - auth_peer
  - getpeereid
  - getpwuid
  - HbaLine
  - pg_hba_file_rules
  - pg_authid
  - log_connections
---

PostgreSQL authentication failures are deliberately vague on the client side. The server sends the minimum detail needed to tell the user something went wrong, because exposing the precise reason — user does not exist vs. wrong password vs. hash mismatch — would allow an attacker to enumerate valid usernames or probe the password storage format. All meaningful diagnostic information goes to the server log. To troubleshoot any auth failure, the first step is always to read the server log.

## Getting More Detail from the Server

By default a PostgreSQL server logs little about normal connection activity. Three GUCs change that:

```sql
-- in postgresql.conf or as superuser at session level
log_connections = on          -- logs each successful authentication
log_disconnections = on       -- logs session duration on disconnect
log_min_messages = debug1     -- set to debug5 for maximum verbosity
```

With `log_connections = on`, every authenticated connection produces a line like:

```
LOG:  connection received: host=192.168.1.10 port=54321
LOG:  connection authenticated: identity="alice" method=scram-sha-256 (pg_hba.conf:12)
```

With `log_min_messages = debug1`, the server emits additional lines from `hba.c` showing which `pg_hba.conf` lines it checked during matching. Reload after changing these parameters with `SELECT pg_reload_conf()` or `SIGHUP` to the postmaster; no restart is needed.

The server also logs the matched `pg_hba.conf` line and its file/line number as a `DETAIL` in the server log for every `auth_failed()` call (`src/backend/libpq/auth.c`), even though it never sends that detail to the client.

## "FATAL: password authentication failed for user"

This error covers three auth methods — `password`, `md5`, and `scram-sha-256` — and can arise from several distinct root causes.

**The password is simply wrong.** The most common case. Check with `\password` in psql or `ALTER ROLE name PASSWORD '...'`.

**The user has no password.** If `pg_authid.rolpassword IS NULL` and the auth method requires a password, the comparison always fails. A role created without a password (or with `NOPASSWORD`) cannot authenticate via any password-based method:

```sql
SELECT rolname, rolpassword IS NULL AS no_password
FROM pg_authid
WHERE rolcanlogin;
```

**Hash type mismatch between stored password and required method.** The hash stored in `pg_authid.rolpassword` must be compatible with the method in `pg_hba.conf`. A password hashed as MD5 (stored as `md5<32-hex-chars>`) cannot satisfy a `scram-sha-256` requirement, and vice versa. To inspect the stored format:

```sql
SELECT rolname,
       CASE
           WHEN rolpassword IS NULL            THEN 'no password'
           WHEN rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'scram-sha-256'
           WHEN rolpassword LIKE 'md5%'        THEN 'md5'
           ELSE 'unknown'
       END AS password_type
FROM pg_authid
WHERE rolcanlogin;
```

If the type does not match the method, reset the password after setting `password_encryption` to the desired algorithm:

```sql
SET password_encryption = 'scram-sha-256';
ALTER ROLE alice PASSWORD 'newpassword';
```

**Client or driver does not support SCRAM.** libpq before version 10 cannot perform SCRAM-SHA-256. Drivers that wrap older libpq, some JDBC versions before 42.2.0, and older ODBC drivers will fail with `SCRAM authentication requires libpq version 10 or above` or a similar message. The fix is to upgrade the driver or, as a temporary measure, change `pg_hba.conf` to use `md5` for that client and reset the stored password accordingly.

## "FATAL: no pg_hba.conf entry for host X, user Y, database Z, SSL off/on"

This error means the connection matched no line in `pg_hba.conf` — `check_hba()` exhausted all parsed `HbaLine` entries without finding a match (`src/backend/libpq/auth.c`, `uaImplicitReject`). The full message tells you the client IP, username, database name, and whether the client negotiated TLS before the error. Each of those fields must match an `pg_hba.conf` entry simultaneously.

**Connection type mismatch.** If the server log shows `SSL off` but your `pg_hba.conf` only has `hostssl` entries for that host, the server will reject the connection before auth even starts. Likewise, a `hostnossl` entry rejects a connection that negotiated TLS. The SSL state reported in the error message is accurate. TLS negotiation happens before the startup message. So by the time `check_hba()` runs, the SSL flag is already final.

**IP address not in any CIDR block.** Even a one-bit mismatch means no match. Use the `pg_hba_file_rules` view to see the parsed rules and verify the CIDR:

```sql
SELECT line_number, type, database, user_name, address, auth_method
FROM pg_hba_file_rules
ORDER BY line_number;
```

Then verify the client's actual IP with `\conninfo` in psql or `SELECT inet_client_addr()` from within a connection that does work.

**Database name mismatch.** Connection poolers often connect to a different database than the application expects. PgBouncer's `dbname` in `pgbouncer.ini` defaults to the pool name, not necessarily the PostgreSQL database name. Confirm what database the client is actually requesting by examining the server log when `log_connections = on`.

**User field mismatch.** The user field in `pg_hba.conf` is an exact match or group membership check; it is not a regex unless prefixed with `/`. A typo or case difference prevents the match.

To test quickly, connect directly with psql specifying the exact host and check whether SSL negotiated:

```bash
psql -h 10.0.0.5 -U alice -d mydb
# then inside psql:
\conninfo
```

`\conninfo` will report `SSL connection (protocol: TLSv1.3, ...)` or `You are connected to ... via socket` — the SSL state here matches what the error message would have reported.

## "FATAL: role X does not exist"

This error is distinct from an authentication failure. It occurs before any auth logic runs. This happens during the startup phase, when the backend looks up the requested username in `pg_authid`. The role simply does not exist. Authentication never started.

Check with:

```sql
SELECT rolname FROM pg_roles WHERE rolname = 'alice';
```

PostgreSQL role names are case-sensitive; the server stores them exactly as specified at creation. If the application passes `Alice` but the role exists as `alice`, the lookup fails. Quoting at creation time (`CREATE ROLE "Alice"`) creates a case-sensitive role; without quoting, PostgreSQL folds names to lowercase.

## SSL-Specific Failures

**"SSL connection has been closed unexpectedly"** — The TLS handshake or an in-progress SSL connection dropped at the TCP level. The client sees this; the server log will show an `SSL error: ...` line from `be-secure-openssl.c` with the OpenSSL error string. Common causes: mismatched TLS version requirements (`ssl_min_protocol_version`), cipher negotiation failure, or a firewall that passes port 5432 TCP but strips SSL traffic mid-stream.

**"certificate verify failed"** — In `sslmode=verify-ca` or `verify-full`, the client is checking the server's certificate against its CA bundle (controlled by `PGSSLROOTCERT` or `~/.postgresql/root.crt`). If the server certificate is self-signed and the CA is not in the client's trust store, this error occurs. For `cert` auth, the server is checking the client's certificate against `ssl_ca_file`. If the client did not present a certificate, or presented one signed by an unknown CA, the server rejects the connection before auth.

**"SCRAM authentication requires libpq version 10 or above"** — The driver's libpq is too old to speak SCRAM. Upgrade libpq or the driver, or fall back to `md5` in `pg_hba.conf` while you update the driver.

**Connection timing out at SSL negotiation** — A firewall may accept the TCP SYN to port 5432 but then not forward or drop SSL ClientHello packets, causing the client to hang waiting for the server SSL response. Test connectivity with `openssl s_client -connect host:5432 -starttls postgres` — if it hangs at `CONNECTED` without printing the server certificate, the network is blocking the SSL exchange.

## Peer and Ident Auth Failures

**"Peer authentication failed for user X"** — `auth_peer()` (`src/backend/libpq/auth.c`) calls `getpeereid()` on the Unix socket to learn the OS UID of the connecting process, then resolves it to a username with `getpwuid()`. If that OS username does not match the requested PostgreSQL username and there is no matching `pg_ident.conf` mapping, the check fails.

Diagnose by verifying the OS user of the connecting process:

```bash
id                       # shows the effective UID/username of the current shell
ps aux | grep psql       # or whatever client process
```

If the OS user is `bob` but the application requests PostgreSQL user `app_user`, you need a `pg_ident.conf` mapping:

```
# pg_ident.conf
# map-name   system-username   pg-username
mymap        bob               app_user
```

Then add `map=mymap` to the `pg_hba.conf` entry. Reload with `SELECT pg_reload_conf()`.

For `ident` (TCP), the failure means the identd daemon on the client machine returned a username that does not match and has no mapping. Ident depends on the client machine running a cooperative `identd` and is rarely trustworthy in modern environments.

## LDAP and RADIUS Failures

**"could not initialize LDAP"** — The server cannot connect to the LDAP server at all. Check `ldapserver`, `ldapport`, and network reachability. If `ldaptls` is set, check that the system CA bundle on the PostgreSQL host trusts the LDAP server's TLS certificate.

**"LDAP login failed for user X on server Y"** — The LDAP server rejected the bind attempt. In search-then-bind mode, the initial bind with `ldapbinddn` / `ldapbindpasswd` succeeded but the second bind with the user's credentials failed. In simple bind mode, the LDAP server rejected the DN constructed from `ldapprefix` + username + `ldapsuffix`. The server log includes the LDAP error string (e.g., `Invalid credentials`) and any LDAP diagnostic message from `LDAP_OPT_DIAGNOSTIC_MESSAGE`. Enable verbose LDAP logging on the directory server itself (for OpenLDAP, `loglevel 256` in `slapd.conf`) for more detail.

**RADIUS failures** — The server logs `RADIUS` errors with the server IP and error code. Common issues: wrong `radiussecrets` (shared secret mismatch), RADIUS server not reachable on `radiusports` (default 1812), or the RADIUS server returning `Access-Reject` for a valid credential (check the RADIUS server's own log).

## Emergency Access Recovery

If `pg_hba.conf` ends up misconfigured in a way that locks out all connections — or if all superusers have lost their passwords — you can restore access without a full reinstall.

**Step 1: Connect via the Unix socket as the OS postgres user.** If the `local` socket entry still allows `peer` or `trust` auth for the `postgres` OS user, log in as that OS user and connect:

```bash
sudo -u postgres psql
```

Peer auth on the local socket compares the OS UID directly and requires no password.

**Step 2: If that also fails, edit `pg_hba.conf` directly and reload.** Add a `trust` line at the top of `pg_hba.conf` for the local socket:

```
# TEMPORARY — remove after recovery
local   all   postgres   trust
```

Then send SIGHUP to the postmaster (or call `pg_ctl reload`) — no restart is needed for `pg_hba.conf` changes. Connect immediately and fix the problem (reset passwords, correct `pg_hba.conf` entries), then remove the temporary `trust` line and reload again.

**Step 3: If the postmaster cannot reload** (e.g., the file has a syntax error that prevents any config from loading), fix the file and restart the server. A completely unparseable `pg_hba.conf` causes `load_hba()` to fail. The old rules remain in effect. So a reload of a bad file will not lock you out further — the old rules stay.

## Related Topics

- [[subsystems/auth/pg-hba-conf|pg_hba.conf]] — detailed reference for the host-based authentication file whose matching rules are the source of most "no pg_hba.conf entry" failures
- [[subsystems/auth/ssl-tls|SSL/TLS]] — covers server certificate configuration, `ssl_ca_file`, and TLS version negotiation that underlies SSL-specific auth failures
- [[subsystems/auth/role-management|Role Management]] — explains how roles and passwords are stored in `pg_authid`, including password hashing formats relevant to SCRAM vs MD5 mismatches
- [[subsystems/auth/gssapi|GSSAPI]] — Kerberos-based authentication method configured in `pg_hba.conf`, a common source of auth failures in enterprise environments
- [[subsystems/roles-privileges|Roles and Privileges]] — covers role creation, login attributes, and the `NOPASSWORD` option that causes password-based auth to always fail
- [[architecture/client-connection|Client Connection]] — describes the full connection establishment sequence, showing where SSL negotiation and HBA checking occur relative to each other
- [[subsystems/wire-protocol|Wire Protocol]] — documents the startup message exchange and authentication message flow that the client and server perform before any SQL is run
- [[subsystems/auth/overview|PostgreSQL Authentication]] — the broader authentication architecture covering `pg_hba.conf` parsing, `ClientAuthentication()`, and how each auth method (password, SCRAM, peer, GSSAPI, SSL) plugs into it

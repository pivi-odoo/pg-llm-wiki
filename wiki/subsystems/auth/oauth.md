---
title: "OAuth 2.0 Bearer Authentication"
aliases:
  - OAUTHBEARER
  - OAuth authentication
  - oauth_validator_libraries
  - auth-oauth
tags:
  - theme/extensibility
source_files:
  - src/backend/libpq/auth-oauth.c
  - src/include/libpq/oauth.h
  - src/include/common/oauth-common.h
symbols:
  - pg_be_oauth_mech
  - oauth_ctx
  - OAuthValidatorCallbacks
  - ValidatorModuleState
  - ValidatorModuleResult
  - oauth_init
  - oauth_exchange
  - validate
  - load_validator_library
  - check_oauth_validator
  - oauth_validator_libraries_string
---

PostgreSQL 18 added native support for the OAUTHBEARER SASL mechanism (RFC 7628), allowing clients to authenticate with a Bearer token issued by an OAuth 2.0 authorization server. Unlike password or certificate authentication, the server never sees the user's credentials. It only receives a token, and delegates the token's validity to a pluggable validator module. The feature is configured per-connection in `pg_hba.conf` with `method oauth`. It also requires at least one validator library to be loaded via the `oauth_validator_libraries` GUC.

## The OAUTHBEARER SASL Mechanism

OAuth authentication is layered on top of the existing SASL framework (`src/backend/libpq/sasl.h`). The server-side mechanism declares three callbacks — `get_mechanisms`, `init`, and `exchange` — matching the same interface used by SCRAM. This means the wire-level framing (SASLInitialResponse, SASLResponse, AuthenticationSASLFinal) is identical to SCRAM from the client's perspective.

The OAUTHBEARER exchange always completes in one round trip. The client sends a GS2-header followed by key-value pairs (separated by `\x01` bytes as specified in RFC 7628). The only required key is `auth`, whose value is an HTTP Authorization header containing a `Bearer <token>` string. The server validates the token. If validation fails, it sends a JSON error document describing the required issuer and scopes. The client then responds with a single `\x01` to acknowledge the error, a mandatory dummy round-trip defined by the RFC.

Channel binding (`p` flag in the GS2 header) is not supported because no `OAUTHBEARER-PLUS` variant exists in the standard. The server accepts the `y` flag for future compatibility with a hypothetical extension.

## Validator Modules

The server has no built-in way to verify a Bearer token. What constitutes a valid token is entirely deployment-specific (expected issuer, audience, scopes, user claim). The server therefore delegates token validation to an external shared library loaded at authentication time. The `oauth_validator_libraries` GUC holds a comma-separated list of permitted library names. The `pg_hba.conf` line may specify which one to use via the `validator` option. If only one library is configured, the server picks it automatically.

A validator library must export `_PG_oauth_validator_module_init()`, which returns an `OAuthValidatorCallbacks` struct containing:

| Callback | Required | Purpose |
|---|---|---|
| `startup_cb` | No | Called once when the library is loaded |
| `shutdown_cb` | No | Called during [[subsystems/memory/contexts|memory context]] cleanup |
| `validate_cb` | **Yes** | Receives the token and username; returns `ValidatorModuleResult` |

`validate_cb` must populate a `ValidatorModuleResult` with `authorized` (bool) and optionally `authn_id` (the authenticated identity string, for user mapping). The validator runs synchronously within the authentication process.

## User Mapping and Authorization

After the server validates a token, it follows one of two paths depending on the `oauth_skip_usermap` option in `pg_hba.conf`:

- **`oauth_skip_usermap = off`** (default): The server checks the validator's `authn_id` against `pg_ident.conf`, using the normal user-mapping rules. This is the same mechanism used by Kerberos and SCRAM authentication.
- **`oauth_skip_usermap = on`**: The validator's `authorized` response is the final word. The server trusts the validator to decide whether the token grants access to the requested role. The server performs no identity mapping.

The second mode is appropriate when the validator itself enforces fine-grained authorization (e.g. checking token scopes against database roles).

## Error Response

When validation fails, the server sends a JSON document to the client before closing the connection:

```json
{ "status": "invalid_token",
  "openid-configuration": "https://auth.example.com/.well-known/openid-configuration",
  "scope": "openid postgres" }
```

The `openid-configuration` URL points to the OAuth discovery document for the required authorization server. The `scope` field lists the required OAuth scopes. Both come from the `oauth_issuer` and `oauth_scope` parameters in `pg_hba.conf`. A well-behaved client can use this information to initiate an OAuth device-authorization flow and retry.

## pg_hba.conf Parameters

| Parameter | Required | Description |
|---|---|---|
| `issuer` | Yes | The OAuth issuer URI, used to build the discovery document URL and included in error responses |
| `scope` | Yes | Space-separated OAuth scopes required for access |
| `validator` | Conditional | Which library from `oauth_validator_libraries` to use; required when multiple libraries are configured |
| `oauth_skip_usermap` | No | If `on`, the validator is the sole authorization authority; no identity mapping is done |

## Related Topics

- [[subsystems/auth/overview]] — authentication method overview and SASL framework
- [[architecture/backend-startup]] — where `ClientAuthentication()` is invoked
- [[subsystems/auth/ssl-tls]] — TLS, the transport layer typically combined with OAuth

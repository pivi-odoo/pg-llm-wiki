---
title: "Crypto Hash Functions"
aliases:
  - cryptohash
  - md5
  - sha256
  - sha512
  - cryptohashfuncs
source_files:
  - src/backend/utils/adt/cryptohashfuncs.c
  - src/include/common/cryptohash.h
  - src/include/common/md5.h
  - src/include/common/sha2.h
  - src/common/cryptohash.c
  - src/common/cryptohash_openssl.c
symbols:
  - pg_cryptohash_ctx
  - pg_cryptohash_type
  - pg_cryptohash_create
  - pg_cryptohash_init
  - pg_cryptohash_update
  - pg_cryptohash_final
  - pg_cryptohash_free
  - pg_cryptohash_error
  - cryptohash_internal
  - md5_text
  - md5_bytea
  - sha256_bytea
  - sha512_bytea
  - pg_md5_hash
---

PostgreSQL exposes six SQL-callable cryptographic hash functions — `md5()`, `sha224()`, `sha256()`, `sha384()`, and `sha512()`. A portable abstraction layer backs them. It transparently delegates to either OpenSSL's EVP API or a built-in software implementation, depending on how the server was compiled. The SQL entry points live in `src/backend/utils/adt/cryptohashfuncs.c`. `src/include/common/cryptohash.h` defines the abstraction layer. `src/common/cryptohash.c` (fallback) and `src/common/cryptohash_openssl.c` (OpenSSL path) implement it.

## Return type asymmetry

The six functions do not share a uniform return type. The difference is intentional rather than accidental. `md5()` accepts either `text` or `bytea`. It always returns `text` containing the 32-character lowercase hex string (for historical compatibility). The SHA-2 family — `sha224()`, `sha256()`, `sha384()`, `sha512()` — accept only `bytea`. They return raw `bytea` containing the digest bytes.

This means callers who need hex output from a SHA function must use `encode()`:

```sql
-- hex-encoded SHA-256
SELECT encode(sha256('hello'::bytea), 'hex');

-- base64-encoded SHA-512
SELECT encode(sha512('hello'::bytea), 'base64');

-- binary MD5 from its hex output
SELECT decode(md5('hello'), 'hex');
```

The PostgreSQL function reference documents the asymmetry. It will not change, because `md5()` predates the SHA-2 additions by many years.

## The cryptohash abstraction layer

The SHA-2 functions share a single internal helper (`cryptohash_internal`, `cryptohashfuncs.c`) that drives the generic `pg_cryptohash_ctx` API. The lifecycle is: `pg_cryptohash_create` → `pg_cryptohash_init` → `pg_cryptohash_update` → `pg_cryptohash_final` → `pg_cryptohash_free`. Every step returns an integer status (0 = success, −1 = error). `pg_cryptohash_error(ctx)` retrieves a human-readable string for the last failure.

The context type itself, `pg_cryptohash_ctx`, is fully opaque. Callers only see a pointer. The struct definition differs between the two backend implementations. This lets the build system swap implementations without any change to the code that uses them.

`pg_cryptohash_final` validates that the destination buffer is at least as large as the digest before writing, returning `PG_CRYPTOHASH_ERROR_DEST_LEN` if not. This prevents buffer overruns even when the caller supplies a statically sized buffer.

## MD5's separate path

MD5 does not go through the generic `pg_cryptohash_ctx` pipeline. The SQL functions `md5(text)` and `md5(bytea)` call `pg_md5_hash()` from `src/common/md5_common.c` directly, which writes the hex string into a fixed 33-byte stack buffer (`MD5_HASH_LEN + 1 = 33`). The `cryptohash_internal` helper explicitly rejects `PG_MD5` (and `PG_SHA1`) with a hard error, so there is no accidental overlap between the two paths.

MD5 has its own header (`src/include/common/md5.h`) with constants for the digest length (16 bytes binary, 32 hex chars) and the MD5 password string format (`md5` prefix + 32 hex chars = 35 characters total, used in MD5 authentication). The convenience function `pg_md5_encrypt()` in that same file handles MD5 password hashing for the `md5` authentication method. It is entirely separate from the SQL-callable `md5()` function.

## Built-in vs OpenSSL backends

The fallback implementation (`cryptohash.c`) allocates a single `pg_cryptohash_ctx` that contains a union of all six algorithm state structures:

```c
union {
    pg_md5_ctx    md5;
    pg_sha1_ctx   sha1;
    pg_sha224_ctx sha224;
    pg_sha256_ctx sha256;
    pg_sha384_ctx sha384;
    pg_sha512_ctx sha512;
} data;
```

Every context allocation is `sizeof(pg_cryptohash_ctx)` regardless of the algorithm. This means a SHA-224 context wastes the space that would otherwise hold a SHA-512 state. This is a deliberate simplicity trade-off. The wasted bytes are negligible.

The OpenSSL implementation wraps an `EVP_MD_CTX *` instead. It also integrates with the backend's [[subsystems/memory/resource-owner|ResourceOwner]] mechanism: `pg_cryptohash_create` calls `ResourceOwnerEnlargeCryptoHash` before any allocation. It then registers the new context with `ResourceOwnerRememberCryptoHash`. `pg_cryptohash_free` unregisters it. This ensures EVP contexts are released even if a transaction is aborted mid-flight, preventing memory leaks on error paths. PostgreSQL allocates the context in `TopMemoryContext` (not the current [[subsystems/memory/contexts|memory context]]), so the resource owner cleanup code can always reach it.

The OpenSSL backend also handles a FIPS mode quirk: in FIPS-enabled builds, `EVP_DigestInit_ex` can push two errors onto the OpenSSL error queue during initialisation. The code calls `ERR_clear_error()` before `EVP_DigestInit_ex` and again immediately after the call to drain both, preventing stale errors from contaminating subsequent operations.

## Security hygiene

Both implementations call `explicit_bzero(ctx, sizeof(pg_cryptohash_ctx))` before freeing the context. This prevents hash state from lingering in freed memory where it could be read by a later allocation. That hash state may have processed sensitive input such as passwords.

`src/include/catalog/pg_proc.dat` registers all six SQL functions as `proleakproof = true`. A leakproof function is one that cannot leak information about its inputs through side channels such as error messages or timing. This attribute lets PostgreSQL use the functions safely inside security-barrier views and row security policies without exposing protected data.

## SHA-1 is internal-only

The `pg_cryptohash_type` enum includes `PG_SHA1`, and the internal implementations support it. But there is no `sha1()` SQL function. PostgreSQL uses SHA-1 internally — most notably in SCRAM authentication. But it deliberately does not expose SHA-1 as a user-callable function.

## Common use cases

**Content-addressable fingerprinting.** `sha256(contents)` on a `bytea` column produces a stable 32-byte identifier that changes whenever the content changes. The pattern is common in storage systems that need deduplication or integrity verification.

**Legacy MD5 checksums.** Large bodies of existing data or external systems may already use MD5 identifiers. PostgreSQL's `md5()` is compatible with standard hex-encoded MD5 output, making interoperability straightforward. This holds even though MD5 is no longer safe for security-sensitive purposes.

**Password hashing.** The `md5` authentication method stores passwords as `md5(password || username)`. It is still available for compatibility. But `scram-sha-256` is strongly preferred for new deployments. Do not use the SQL `md5()` function to implement application-level password storage. Use a dedicated password hashing scheme (bcrypt, Argon2, etc.) instead.

**Data integrity in ETL pipelines.** Comparing `sha256()` digests before and after a transformation or load step is a lightweight way to detect corruption or truncation without storing and comparing entire row sets.

## Algorithm characteristics

| Function | Output type | Digest size | Block size |
|---|---|---|---|
| `md5(text\|bytea)` | `text` (hex) | 16 bytes / 32 hex chars | 64 bytes |
| `sha224(bytea)` | `bytea` | 28 bytes | 64 bytes |
| `sha256(bytea)` | `bytea` | 32 bytes | 64 bytes |
| `sha384(bytea)` | `bytea` | 48 bytes | 128 bytes |
| `sha512(bytea)` | `bytea` | 64 bytes | 128 bytes |

MD5 is cryptographically broken. Collision attacks are practical. Preimage resistance has been weakened. It remains in PostgreSQL for compatibility and non-security uses (checksums, legacy identifiers). But avoid it in any context where collision resistance or second-preimage resistance matters. SHA-256 or SHA-512 is appropriate for new code.

## Related Topics

- [[subsystems/auth/overview]]
- [[subsystems/wire-protocol]]
- [[subsystems/memory/resource-owner]]
- [[subsystems/memory/contexts]]
- [[subsystems/types/encoding-utilities]]

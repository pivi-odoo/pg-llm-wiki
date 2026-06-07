---
title: "Binary Encoding Functions (encode / decode)"
aliases:
  - encode
  - decode
  - binary_encode
  - binary_decode
  - base64
  - hex encode
  - escape encoding
source_files:
  - src/backend/utils/adt/encode.c
symbols:
  - binary_encode
  - binary_decode
  - hex_encode
  - hex_decode
  - hex_decode_safe
  - pg_base64_encode
  - pg_base64_decode
  - esc_encode
  - esc_decode
  - pg_find_encoding
---

PostgreSQL's `encode(data bytea, format text)` and `decode(string text, format text)` functions convert binary data to and from text representations. PostgreSQL supports three formats — `hex`, `base64`, and `escape` — each suited to different use cases. Web applications reach for these constantly: hex for checksums and UUIDs, base64 for binary blobs in JSON or HTTP payloads, and escape when debugging raw bytea values. The implementation lives in `encode.c`.

## Dispatch Architecture

A static `enclist[]` table registers all three formats. Each entry holds a `pg_encoding` struct with four function pointers: an encoder, a decoder, and a size-estimator for each direction. When `binary_encode()` or `binary_decode()` is called, `pg_find_encoding()` walks the list and matches the format name with `pg_strcasecmp()`. This means `'HEX'`, `'Hex'`, and `'hex'` are all accepted.

The two-step size estimation is a design choice for safety. The encoder first calls `encode_len()` to get a worst-case output size. It allocates exactly that much, checked against `MaxAllocSize`. Then it calls `encode()`, which returns the true length. If the true length exceeds the estimate — indicating a bug in the estimator — the backend immediately calls `elog(FATAL)` rather than continuing with corrupted memory.

## Hex Format

Hex encoding is the simplest: each input byte becomes exactly two lowercase hexadecimal characters. The encoder uses a static `hextbl[]` lookup that maps nibbles to `0-9a-f`. The decoder uses a complementary `hexlookup[128]` table that maps ASCII characters back to nibbles. Entries for invalid characters hold `-1`. The decoder silently skips whitespace (space, tab, newline, carriage return) during decoding. This makes pasted hex strings from log output or `psql` work without pre-processing.

Hex output is always lowercase. If a downstream system requires uppercase, use `upper(encode(data, 'hex'))`.

## Base64 Format

Base64 follows RFC 1421, encoding three input bytes as four characters from the alphabet `A–Z`, `a–z`, `0–9`, `+`, `/`. Padding with `=` characters brings the output to a multiple of four characters. The encoder inserts a newline every 76 output characters. This follows the PEM-style line-wrapping convention. The newlines are normal and intentional.

The decoder treats whitespace (including those newlines) as transparent, so the round-trip works cleanly. The decoder validates both the alphabet and the padding sequence strictly: an `=` in the wrong position or a truncated input raises `ERRCODE_INVALID_PARAMETER_VALUE`.

A common point of confusion: some web libraries produce URL-safe base64 using `-` and `_` instead of `+` and `/`. PostgreSQL's decoder does not accept those variants — convert them before passing to `decode()`.

## Escape Format

The escape format predates the `hex` and `base64` formats. It was historically the default text representation of `bytea`. It encodes zero bytes (`\0`) and any byte with the high bit set as a backslash followed by three octal digits (e.g. `\377`). The encoder doubles the backslash itself to `\\`. It passes ASCII bytes that are not zero and not high-bit-set through unchanged.

The decoder reverses the process: `\\` → `\`, `\NNN` → the octal value, anything else → error. The escape format is mostly a compatibility mechanism today. `hex` is the default `bytea` output mode since PostgreSQL 9.0.

## Size Limits

Both `binary_encode` and `binary_decode` check the estimated output size against `MaxAllocSize - VARHDRSZ` before allocating. For hex, this caps input at roughly 500 MB on a 32-bit build. In practice, this limit is never reached on 64-bit systems. Base64's worst-case expansion is `(n + 2) / 3 * 4 + n / 57` bytes (including newlines), which is about 37% larger than the input.

## Related Topics

- [[subsystems/types/variable-length-types|Variable-length types]] — `bytea` storage and the `VARHDRSZ` header that wraps encoded results
- [[subsystems/types/encoding-utilities|Multibyte encoding utilities]] — the separate system for character-encoding conversion (`pg_client_to_server` etc.)
- [[subsystems/storage/toast|TOAST]] — large bytea values are TOASTed before `encode()` sees them

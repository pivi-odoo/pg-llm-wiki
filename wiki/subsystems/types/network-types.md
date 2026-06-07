---
title: "Network Address Types"
aliases:
  - inet
  - cidr
  - macaddr
  - network types
tags:
  - theme/query-optimization
  - theme/storage-format
source_files:
  - src/backend/utils/adt/network.c
  - src/backend/utils/adt/mac.c
  - src/include/utils/inet.h
  - src/backend/utils/adt/network_selfuncs.c
  - src/backend/utils/adt/inet_cidr_ntop.c
  - src/backend/utils/adt/inet_net_pton.c
  - src/backend/utils/adt/mac8.c
symbols:
  - inet_struct
  - network_in
  - network_cmp_internal
  - network_abbrev_convert
  - cidr_set_masklen_internal
  - bitncmp
  - bitncommon
  - macaddr
  - macaddr8
  - inet_cidr_ntop
  - inet_net_pton
  - macaddr8_recv
---

PostgreSQL provides four built-in types for storing network addresses: `inet` and `cidr` for IP addresses (both v4 and v6), and `macaddr` and `macaddr8` for Ethernet MAC addresses. All four are useful for applications that manage network infrastructure, audit connection logs, or enforce address-level access controls. All four also support operators and index access patterns that go well beyond what a plain text column offers.

## The shared `inet` storage representation

Both `inet` and `cidr` share an identical on-disk layout. Their C representation is `inet_struct` (inet.h):

```c
typedef struct {
    unsigned char family;    /* PGSQL_AF_INET or PGSQL_AF_INET6 */
    unsigned char bits;      /* prefix length (masklen) */
    unsigned char ipaddr[16];/* address in network byte order, up to 128 bits */
} inet_struct;
```

This struct is wrapped in the standard varlena header to form the `inet` type. Because the payload is at most 18 bytes (2 header bytes + 16 address bytes), the value always fits in the 1-byte varlena header format. So `inet`/`cidr` values are never [[subsystems/storage/toast|TOAST]]ed. The `SET_INET_VARSIZE` macro sets the varlena length to `VARHDRSZ + 2 + ip_addrsize()`, where `ip_addrsize()` is 4 for IPv4 and 16 for IPv6. So IPv4 datums are physically smaller than IPv6 ones.

The `PGSQL_AF_INET` and `PGSQL_AF_INET6` constants are defined relative to the platform's `AF_INET` value (`AF_INET + 0` and `AF_INET + 1` respectively) rather than using `AF_INET6` directly. This avoids a dump/reload requirement on platforms that historically lacked `AF_INET6` support.

## `inet` vs `cidr`: the single-invariant difference

`inet` and `cidr` share the same C struct and almost all the same operator code. The only semantic difference is an invariant enforced at input time: **CIDR values must have all host bits (bits to the right of the prefix length) set to zero**. The common input function `network_in()` enforces this with `addressOK()` when parsing a CIDR value. Storing `192.168.1.5/24` succeeds with `inet` but fails with `cidr` because the `.5` sets host bits that fall outside the /24 mask.

On output, `cidr_out()` always appends `/n` even when the notation would omit it (e.g., `192.168.1.0/24` is always displayed with the prefix), while `inet_out()` omits `/n` for host addresses that have the maximum prefix length for their family (i.e., /32 for IPv4, /128 for IPv6).

`cidr_set_masklen_internal()` is the canonical function for producing a valid CIDR value: it copies only the masked bytes from the source address and zeroes the rest, so the invariant is preserved for any masklen. The planner uses this via `network_scan_first()` to derive B-tree range bounds for containment queries (see below).

## Comparison and sort ordering

`network_cmp_internal()` defines the total order used by all comparison operators, B-tree indexes, and `ORDER BY`:

1. **Address family**: IPv4 sorts before IPv6.
2. **Network bits (masked prefix)**: compare the first `min(bits_a, bits_b)` bits.
3. **Prefix length**: shorter prefixes sort first among addresses with equal network bits.
4. **Full address**: all bits compared — effectively just the host part given the above.

This means that `192.168.1.0/24` sorts before `192.168.1.0/25` (same network bits, shorter prefix). It also means that `192.168.1.128/25` sorts after `192.168.1.0/24` (different network bits in the first 25 bits). PostgreSQL designs the ordering so that all addresses within a given CIDR block appear contiguous in a B-tree scan. This is what makes the containment index optimization possible.

## Containment operators and index acceleration

The subnet containment operators (`<<`, `<<=`, `>>`, `>>=`, `&&`) are the most distinctive feature of `inet`/`cidr`. `network_sub()` implements `<<` (strict subset): it checks that the left side has a longer prefix than the right and that their shared network bits match (`bitncmp()`). All containment operators require that both operands share the same address family.

Because containment queries are common in practice (e.g., "which hosts are in the 10.0.0.0/8 block?"), PostgreSQL includes a planner support function `network_subset_support()` that converts a containment predicate with a constant right-hand side into a pair of B-tree range conditions. Given `x <<= '192.168.1.0/24'::inet`, the planner generates:

```
x >= network_scan_first('192.168.1.0/24')  -- i.e., 192.168.1.0/24 itself
AND
x <= network_scan_last('192.168.1.0/24')   -- i.e., 192.168.1.255/32
```

These bounds are correct because of the sort order described above: all addresses contained within a CIDR block occupy a contiguous range in the B-tree. This lets a standard B-tree index serve containment queries efficiently without requiring a specialized GiST index (though a GiST index with the `ip4r` extension exists for more complex cases).

## Sort abbreviation with HyperLogLog

For large sorts, `network_sortsupport()` enables abbreviated key generation (network_abbrev_convert(), network.c). The goal is to pack as much ordering information as possible into a single machine-word datum, so that an integer compare can resolve most comparisons instead of calling `network_cmp_internal()`.

For IPv4 on 64-bit platforms the layout is:

```
[ 1 bit family | 32 bits network | 6 bits masklen | 25 bits subnet ]
```

For IPv6 on 64-bit platforms, there is only room for:

```
[ 1 bit family | 63 bits network (truncated) ]
```

The family bit places all IPv4 addresses before IPv6, matching the authoritative comparator. The HyperLogLog cardinality estimator (`network_abbrev_abort()`) monitors whether abbreviation is beneficial: if it observes fewer than 1 distinct abbreviated key per 2,000 inputs, it abandons abbreviation to avoid the overhead of decompressing abbreviated keys for tie-breaking.

## MAC address types

`macaddr` stores a 6-byte EUI-48 Ethernet address (e.g., `12:34:56:78:9a:bc`). Its C representation is a simple fixed-size struct of 6 `unsigned char` fields `a` through `f` (inet.h). Being fixed-width and exactly 6 bytes, it is passed by reference but never varlena.

`macaddr8` stores an 8-byte EUI-64 address (e.g., `12:34:56:ff:fe:78:9a:bc`), using a struct with fields `a` through `h`. PostgreSQL also provides `macaddr8(macaddr)` to convert a 6-byte address to EUI-64 by inserting the `FF:FE` bytes in the middle.

The `macaddr` input function accepts several notation styles: colon-separated (`08:00:2b:01:02:03`), dash-separated (`08-00-2b-01-02-03`), and Cisco-style pairs (`0800.2b01.0203`). All representations normalize to the six-field colon notation on output. MAC addresses support bitwise `~`, `&`, and `|` operators for masking operations. They also support sort abbreviation via the same HyperLogLog mechanism as `inet`.

The `trunc(macaddr)` function zeroes out the lower 3 bytes of the OUI/NIC split, returning just the vendor OUI component — useful for grouping addresses by manufacturer.

## inet/cidr text conversion and MAC-8 address type

`inet_cidr_ntop.c` implements the reverse path from internal form to text. `network_out()` and `cidr_out()` use it to render an `inet_struct` value as a human-readable string such as `192.168.1.0/24` or `::1`. `inet_net_pton.c` handles the opposite direction: it parses a text string into the internal `inet_struct` form, and `network_in()` and `cidr_in()` call it. It validates that the supplied prefix length does not exceed the address family's maximum (32 for IPv4, 128 for IPv6), and rejects strings with set host bits when a CIDR interpretation is requested. `mac8.c` implements the EUI-64 `macaddr8` type, which is an 8-byte variant of the 6-byte `macaddr`. Like `macaddr`, it is a fixed-width, pass-by-reference type with full input/output, comparison, and hash support. The file also provides `macaddr8_set7bit()`, which flips the universal/local bit (bit 6 of the first byte) to generate a valid EUI-64 modified interface identifier from an EUI-48 MAC address — the transformation used, for example, in IPv6 SLAAC address generation. `macaddr8_recv()` provides binary receive support, reading 8 raw bytes from the wire-format message buffer.

## Key functions reference

| Function | Location | Purpose |
|---|---|---|
| `network_in()` | network.c | Shared input for `inet` and `cidr`; enforces CIDR host-bit invariant |
| `network_cmp_internal()` | network.c | Total order used by all comparison operators |
| `cidr_set_masklen_internal()` | network.c | Produces a valid CIDR value by zeroing host bits |
| `bitncmp()` | network.c | Bit-level prefix comparison (Paul Vixie, ISC) |
| `bitncommon()` | network.c | Count of common leading bits; used by `inet_merge()` |
| `network_abbrev_convert()` | network.c | Packs inet value into abbreviated sort key |
| `network_subset_support()` | network.c | Planner support: converts containment to B-tree range |
| `network_scan_first/last()` | network.c | Range bounds for containment index scans |
| `internal_inetpl()` | network.c | Adds an `int8` offset to an inet address (carries propagate) |

## Selectivity Estimation

`network_selfuncs.c` provides two planner hook functions — `networksel` (restriction selectivity) and `networkjoinsel` (join selectivity) — that the query optimizer calls whenever it encounters an inet/cidr operator in a WHERE clause or join condition. Without these hooks, the planner would fall back to a generic default fraction and routinely mis-estimate the cost of queries that filter on network prefixes.

### The operators covered

Both estimators handle the full inet operator family via an internal code number assigned by `inet_opr_codenum()`:

| Code | Operator | Meaning |
|------|----------|---------|
| -2 | `>>` | strict supernet |
| -1 | `>>=` | supernet or equal |
| 0 | `&&` | overlap |
| 1 | `<<=` | subnet or equal |
| 2 | `<<` | strict subnet |

Negating a code yields the commutator. This lets the estimators reuse the same logic regardless of which side holds the variable.

### How restriction selectivity works (`networksel`)

IP address columns have a hierarchical prefix structure that breaks the assumptions behind standard equality-based selectivity. A /8 block contains roughly 16 million distinct /32 addresses. A /24 contains 256. If the MCV list for a sessions table contains the ten most frequent client IPs — all /32 host addresses — naively matching them against a /24 query constant will produce a near-zero estimate. This happens even when the /24 encompasses thousands of rows. The estimators in this file account for prefix relationships explicitly so that containment queries get realistic cardinality estimates.

`networksel` follows the standard restriction estimator pattern: separate the population into the MCV fraction and the non-MCV (histogram) fraction, estimate each independently, then combine.

For the MCV portion it calls the generic `mcv_selectivity()` helper, which evaluates the actual inet operator against every MCV entry and sums the matching frequencies. This is exact for MCV rows.

For the histogram portion it calls `inet_hist_value_sel()`, which walks the btree histogram buckets and classifies each bucket in one of three ways against the constant:

- **Full match**: both bucket endpoints satisfy the operator — the entire bucket contributes 1.0.
- **Partial match**: the endpoints straddle the constant — the bucket contributes `1 / 2^d`, where `inet_hist_match_divider()` computes `d` (the *match divider*) as the number of non-common decisive prefix bits between the bucket boundary and the query. A bucket whose boundary shares many prefix bits with the query constant contributes more than one whose boundary diverges early.
- **No match**: neither endpoint satisfies the operator — the bucket contributes 0.

The divider formula reflects the natural binary scaling of IP address space: each extra divergent bit roughly halves the fraction of addresses that fall inside a given prefix. When both bucket boundaries yield a valid divider, the estimator uses the larger one to reduce over-estimation for buckets with disparate masklens.

The final selectivity is:

```
selec = mcv_selec + (1.0 - nullfrac - sumcommon) * non_mcv_selec
```

If no statistics tuple exists at all, the estimator returns the fallback constants `DEFAULT_INCLUSION_SEL` (0.005) and `DEFAULT_OVERLAP_SEL` (0.01).

### How join selectivity works (`networkjoinsel`)

Join selectivity is structurally similar to `eqjoinsel()` but cannot reduce the problem to one-to-one matching: containment operators match many-to-many, so the estimator must in principle check every pair of values from the two sides. The estimator decomposes the join population into up to four cross-products:

1. **MCV × MCV** (`inet_mcv_join_sel`): evaluate the operator for every pair; sum `freq1 * freq2` for matching pairs. This is O(N²) in MCV list length, so the estimator caps both lists at `MAX_CONSIDERED_ELEMS` (1024) entries.
2. **MCV × histogram** and **histogram × MCV** (`inet_mcv_hist_sel`): for each MCV entry use `inet_hist_value_sel` to estimate the fraction of the other side's histogram population it matches, weighted by the MCV's frequency.
3. **Histogram × histogram** (`inet_hist_inclusion_join_sel`): treat interior elements of the second histogram as a uniform sample of the non-MCV population and apply `inet_hist_value_sel` against the first histogram for each sample element.

For semi-joins and anti-joins, `networkjoinsel_semi` uses a different logic: for each LHS value it asks "is there at least one matching row on the RHS?" If the value matches any RHS MCV, the answer is certainly yes (return 1.0). Otherwise, `networkjoinsel_semi` clamps the estimated count from the RHS histogram to [0, 1] and uses it as a probability.

To prevent O(N²) runtime at high statistics targets, both histogram loops decimate: they sample only every k-th element, choosing k so that they consider no more than 1024 elements.

### Practical impact

A query like `SELECT * FROM sessions WHERE client_addr <<= '203.0.113.0/24'` touches `networksel` at planning time. A good estimate here determines whether the planner chooses a sequential scan, a B-tree index scan (enabled by `network_subset_support()`), or a nested loop join order. A factor-of-100 mis-estimate on a sessions table with millions of rows can flip the join strategy and turn a millisecond lookup into a full table scan.

## See also

- [[subsystems/types/base-types|Base Types (CREATE TYPE)]] — how fixed-length and varlena types are defined in general
- [[subsystems/storage/toast|TOAST]] — varlena storage mechanism (inet/cidr values never require it)
- [[subsystems/executor/jit-llvm|JIT]] — JIT-compiled expressions involving inet operators

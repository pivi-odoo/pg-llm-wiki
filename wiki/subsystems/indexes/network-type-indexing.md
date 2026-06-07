---
title: "GiST and SP-GiST Indexes for Network Address Types"
aliases:
  - inet index
  - cidr index
  - network address index
  - inet_ops
source_files:
  - src/backend/utils/adt/network_gist.c
  - src/backend/utils/adt/network_spgist.c
symbols:
  - GistInetKey
  - inet_gist_consistent
  - inet_gist_union
  - inet_gist_penalty
  - inet_gist_picksplit
  - inet_spg_config
  - inet_spg_choose
  - inet_spg_picksplit
  - inet_spg_inner_consistent
  - inet_spg_leaf_consistent
  - inet_spg_node_number
---

PostgreSQL ships two specialized index operator classes for `inet` and `cidr` columns: a [[subsystems/indexes/gist|GiST]] class that represents keys as IP address ranges derived from CIDR prefixes, and an [[subsystems/indexes/spgist|SP-GiST]] class that builds a prefix trie directly from the binary structure of IP addresses. Both support the full set of containment operators (`<<`, `<<=`, `>>`, `>>=`, `&&`) that a standard [[subsystems/indexes/btree|B-tree]] cannot accelerate with a single index scan. A B-tree is built on a total order. PostgreSQL's sort order for `inet` values (address family, then masked prefix bits, then prefix length, then full address) makes addresses *within* a given CIDR block sort contiguously. The planner exploits this via `network_subset_support()` to rewrite a simple containment predicate into a pair of B-tree range bounds. This approach covers only the case of finding rows *contained within* a constant CIDR block. The inverse question — which CIDR blocks in this table *contain* a given host address? — depends on both prefix length and address bits together. The `>>`, `>>=`, and `&&` operators have no equivalent range bounds for this question. GiST and SP-GiST solve this by encoding structural information about IP prefix hierarchy into the index keys themselves. Index traversal can then skip entire subtrees that provably cannot match the query. This makes them the right tool for any schema that needs to efficiently answer questions like "which CIDR blocks contain this client address?" or "which firewall rules overlap with this prefix?"

## GiST: union keys that capture prefix range

The GiST operator class stores each index page's key as a `GistInetKey` — a compact struct that summarizes all the `inet`/`cidr` values below that node using three fields beyond the address bytes:

- **`family`**: the IP address family (IPv4, IPv6, or zero if mixed).
- **`minbits`**: the smallest prefix length (`ip_bits`) among all values below this node. This lets the consistency check immediately discard a node when the query's prefix length constraint cannot be satisfied by any child.
- **`commonbits`**: the number of leading address bits shared by every value below this node. This is the depth at which the subtree's addresses start to diverge.

At a leaf node `commonbits` equals the full address width, so the leaf key is essentially the original value. At an internal node both fields independently track their respective minima: `minbits` might be smaller than `commonbits`, or vice versa. Keeping them separate, rather than collapsing to one field, measurably improves search speed. The source comments report roughly a twofold improvement compared to a simpler single-field design.

The consistency check (`inet_gist_consistent`) applies up to five checks in order: family mismatch (can prune whole subtrees when address families differ), prefix length check using `minbits` (prunes for sub/supernet operators), prefix bit comparison using `commonbits` (prunes when common address bits don't match the query), full netmask comparison (at leaves only), and finally full address comparison (at leaves only, for equality-based strategies). Because each check is purely arithmetic on small fixed-width fields, the prune rate is high even for non-containment operators like `<` and `>`.

### Penalty and split decisions

When inserting a new value, the penalty function measures how much inserting into a given subtree would degrade that subtree's key. A family mismatch costs 4. Extending into a shorter-prefix subtree (widening `minbits`) costs 3. Reducing `commonbits` costs an amount inversely proportional to the new common prefix length (a shorter common prefix means a higher penalty). This gradient guides insertion toward subtrees whose existing key already contains the new value's prefix, keeping similar prefixes physically close.

When a GiST page overflows, `inet_gist_picksplit` partitions the entries in two. It first checks whether multiple address families are present. If so, it splits IPv4 from IPv6, because addresses from different families can never match containment operators against each other. Within a single family it finds the first bit position after the shared common prefix where the entries diverge. Entries with bit 0 at that position go left. Entries with bit 1 go right. If no single bit produces a non-trivial split (all entries share the full address), it falls back to a 50/50 arbitrary split.

## SP-GiST: a prefix trie over IP address bits

The SP-GiST operator class builds a radix trie where each internal node stores a CIDR value as its prefix and fans out into exactly four children. The node layout encodes two independent decisions made at the split point:

- **Bits 0/1** (even/odd node): determined by the next address bit after the common prefix. Addresses whose next bit is 0 go to even nodes; addresses whose next bit is 1 go to odd nodes.
- **Bits 0/2** (low/high node): determined by whether the value's prefix length exceeds the common prefix. Values with the same prefix length as the current node's prefix go to low nodes (0 or 1). Values with a longer prefix go to high nodes (2 or 3).

This four-way fan-out means that a single internal node simultaneously partitions by the next address bit *and* by whether the entry's masklen is still at the current trie depth or extends deeper. At the top of the tree, entries from different address families are separated by a two-node split (no prefix, node 0 = IPv4, node 1 = IPv6) before the single-family four-way fanout begins.

The node-number assignment (`inet_spg_node_number`) is the core of the design: given a value and the current `commonbits` depth, it inspects bit `commonbits` of the address and the relationship between `ip_bits(val)` and `commonbits` to produce a node number 0–3. This assignment is consistent between insertion and search. As a result, traversal never needs to visit all four children, unless the query could match values in all four structural categories.

### Traversal and consistency

The inner consistency function (`inet_spg_inner_consistent`) uses a bitmap of which child nodes to visit. The function starts with all four bits set. It applies up to six checks in sequence, clearing bits as it proves that certain branches cannot match. The checks mirror the GiST logic: family mismatch, masklen constraints, common prefix bit comparison, next network bit, masklen comparison after prefix match, and next host bit. However, the SP-GiST trie records a single unambiguous prefix rather than a union range, so the checks tend to prune more aggressively at each node. There is no GiST-style penalty needed: SP-GiST insertion follows the trie deterministically, splitting nodes when a new value's prefix diverges from the current node's prefix.

## Choosing between GiST and SP-GiST

SP-GiST is the better default choice for `inet` columns. IP address space decomposes cleanly into non-overlapping prefixes, exactly the property that makes a trie efficient. As a result, SP-GiST subtrees never overlap. Traversal can descend exactly one path per query, with only occasional multi-branch exploration. GiST internal keys can overlap: a parent's union range includes its children's union ranges. This forces the consistency check to explore multiple subtrees more often.

SP-GiST also stores less per-node metadata: its prefix is a plain CIDR value, while GiST must track both `minbits` and `commonbits` separately. For a column with many distinct CIDR prefixes — typical of a firewall rule table or an IP geolocation dataset — the SP-GiST index tends to be smaller and to answer containment queries with fewer page reads.

GiST has one advantage: it handles mixed-structure data more gracefully. If a table holds both host addresses (e.g., `/32` entries) and wide CIDR blocks (e.g., `/8` entries) in large numbers, GiST's penalty-guided insertion can sometimes produce a more balanced tree than the deterministic SP-GiST trie. GiST is also the only option when combining network-type indexing with other GiST-compatible column types in a single multi-column index.

Both operator classes cover the same operator set: `<<`, `<<=`, `>>`, `>>=`, `&&`, `=`, `<>`, `<`, `<=`, `>`, `>=`. Neither requires `recheck` for any operator. All results returned from the index are exact.

## Creating and using these indexes

```sql
-- SP-GiST (recommended for inet/cidr columns)
CREATE INDEX ON access_rules USING spgist (network inet_ops);

-- GiST alternative
CREATE INDEX ON access_rules USING gist (network inet_ops);

-- Queries that use the index
-- "Does this client IP fall inside any stored network block?"
SELECT rule_name FROM access_rules WHERE network >>= '203.0.113.42';

-- "Which stored prefixes overlap with this new allocation?"
SELECT prefix FROM allocated_ranges WHERE prefix && '10.20.0.0/14';

-- "All rules that are subnets of the /16 datacenter block"
SELECT * FROM firewall_rules WHERE network <<= '10.128.0.0/16';

-- Equality and ordering also use the index
SELECT * FROM access_rules WHERE network = '192.168.1.0/24';
```

The `inet_ops` operator class name is explicit, but it is also the default for both `gist` and `spgist` on `inet` and `cidr` columns. As a result, the `USING spgist (network)` form without `inet_ops` also works.

## Application patterns

**Access control lists and allow/block lists.** A table of CIDR-range rules with a GiST or SP-GiST index supports the query "does this incoming request IP match any rule?" as a single index scan using `>>=` or `>>`. Without an index this query requires a sequential scan and a per-row prefix check.

**IP geolocation by range prefix.** Geolocation databases are often stored as CIDR blocks tagged with a region or ASN. Finding the most-specific matching block for a given IP (the block with the longest prefix that contains the address) can be expressed as:

```sql
SELECT region, masklen(network) AS len
FROM geo_blocks
WHERE network >>= $1::inet
ORDER BY len DESC
LIMIT 1;
```

The SP-GiST index makes the `>>=` filter efficient. The final sort and limit are over a small result set.

**Firewall rule overlap detection.** When adding a new firewall rule, checking for conflicts with existing rules uses the `&&` (overlaps) operator. An SP-GiST index on the existing rules table makes this check fast even with millions of stored prefixes.

**Multi-tenant isolation.** Applications that assign IP address ranges to tenants can store each tenant's allowed CIDR blocks in an indexed table. A containment query then validates that an incoming connection's source IP belongs to the expected tenant, enforcing network-level access control in the database layer rather than in application code.

## Related Topics

- [[subsystems/indexes/gist|GiST index access method]]
- [[subsystems/indexes/spgist|SP-GiST index access method]]
- [[subsystems/indexes/btree|B-tree indexes]]
- [[subsystems/types/network-types|Network address types (inet, cidr)]]
- [[subsystems/indexes/index-am|Index access method interface]]

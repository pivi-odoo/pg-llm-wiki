---
title: "GiST Index Support for tsvector (tsgistidx)"
aliases:
  - tsvector GiST
  - tsvector_ops GiST
  - gtsvector
source_files:
  - src/backend/utils/adt/tsgistidx.c
symbols:
  - SignTSVector
  - GistTsVectorOptions
  - gtsvector_compress
  - gtsvector_decompress
  - gtsvector_consistent
  - gtsvector_union
  - gtsvector_penalty
  - gtsvector_picksplit
  - gtsvector_same
  - gtsvector_options
  - checkcondition_arr
  - checkcondition_bit
  - makesign
  - hemdist
  - hemdistsign
  - unionkey
---

The `tsvector_ops` GiST operator class lets a [[subsystems/indexes/gist|GiST]] index accelerate full-text `@@` queries over [[subsystems/full-text-search|tsvector]] columns. Unlike the GIN-based approach, which stores one posting list per lexeme, the GiST opclass stores one compact key per indexed row: a bit-signature derived from the row's lexeme set. It uses this key to prune irrelevant subtrees during a top-down tree search. The tradeoff is that GiST keys are inherently lossy, so every match that survives the index scan must be rechecked against the heap.

## The SignTSVector Key Format

The central design choice in this opclass is the `SignTSVector` type, the internal key stored in index pages. It has three modes controlled by a `flag` field:

| Flag | Mode | What the data region contains |
|------|------|-------------------------------|
| `ARRKEY` | Array key | A sorted, deduplicated array of `int32` CRC values, one per lexeme |
| `SIGNKEY` | Signature key | A fixed-length bit vector (bloom filter) |
| `SIGNKEY | ALLISTRUE` | Saturated signature | No data — every bit is implicitly set |

Leaf pages in a fresh tree start with array keys. `gtsvector_compress()` builds an array key by computing a CRC-32 over each lexeme string, sorting the resulting integer array, and deduplicating it. This exact-lexeme representation is precise: given a query lexeme's own CRC, a binary search over the sorted array answers "is this lexeme present?" in O(log n) time.

Array keys exist only on leaf pages. Inner pages always hold signature keys. Compression from array to signature occurs in two situations: when `gtsvector_union()` aggregates keys from multiple rows during an inner-page update, and when an individual array key grows large enough to exceed `TOAST_INDEX_TARGET`. At that point, `gtsvector_compress()` converts it to a bloom filter in-place.

The bloom filter is a plain byte array of configurable length (`siglen`). Each lexeme's CRC is mapped to a bit position with `HASHVAL(val, siglen) = crc % (siglen * 8)`. That bit is then set. The default `siglen` is 124 bytes (31 × 4), giving 992 bits. It can be tuned with the `siglen` storage option up to `GISTMaxIndexKeySize`. When all bits in a signature are already set — meaning the filter can no longer distinguish anything — the key is promoted to the `ALLISTRUE` state, eliminating the need to store the bit array at all.

## Compress and Decompress

`gtsvector_compress()` runs at two distinct moments. For a leaf entry (the raw `tsvector` coming from the heap), it extracts lexeme CRCs and builds an `ARRKEY` key. It then upgrades to a `SIGNKEY` if the array would be oversized. For a non-leaf entry already holding a `SIGNKEY`, it only checks whether the filter has become fully saturated. If so, it replaces the key with the cheaper `ALLISTRUE` form.

`gtsvector_decompress()` is minimal — it only detoasts the stored value if it was compressed to a [[subsystems/storage/toast|TOAST]] pointer. No type transformation happens in the reverse direction. The `SignTSVector` format is directly usable by all other support functions.

## Consistent: Querying Against Lossy Keys

`gtsvector_consistent()` is the search predicate. It always sets `*recheck = true` before doing anything else, reflecting the fact that every key format in use here — whether array or signature — can produce false positives. No result is accepted without a heap recheck.

The function then dispatches on the key type:

- **`ALLISTRUE` signature**: returns `true` immediately. An all-set filter offers no discrimination, so the subtree must always be explored.
- **`SIGNKEY` signature** (inner pages): calls `TS_execute()` with the `checkcondition_bit` callback. For each query operand, the callback hashes the operand's precomputed `valcrc` to a bit position and tests that bit. A clear bit guarantees the lexeme is absent from the subtree (`TS_NO`). A set bit is inconclusive (`TS_MAYBE`), because bloom filters have false positives. Prefix queries (`val->prefix`) always return `TS_MAYBE` since a hash cannot represent prefix relationships.
- **`ARRKEY` key** (leaf pages): calls `TS_execute()` with the `checkcondition_arr` callback. The callback binary-searches the sorted CRC array for the query operand's `valcrc`. An exact match returns `TS_MAYBE` (a CRC collision could still fool us). Absence returns `TS_NO`. Prefix queries again return `TS_MAYBE`.

`TS_execute()` evaluates the full boolean tree of the `tsquery` using these ternary callbacks. A subtree where every operand returns `TS_NO` produces an overall `false`, pruning the index branch. Anything involving `TS_MAYBE` produces `true` (with recheck).

## Union: Building Inner-Node Keys

`gtsvector_union()` computes the union key that will be stored in an inner node to represent the contents of a child subtree. It always produces a `SIGNKEY` output, regardless of whether the inputs are array keys or signature keys.

The `unionkey()` helper handles the two input cases. For an `ARRKEY` input, it hashes each CRC in the array into the output bit vector. For a `SIGNKEY` input, it ORs its bits directly into the output. If any input is `ALLISTRUE`, the function immediately short-circuits and sets `ALLISTRUE` on the result. This is because an all-set filter unioned with anything is still all-set.

The output is always a fixed-length bloom filter. This means that union discards the precision of individual array keys. Higher up the tree, more lexemes from more rows are folded into the same bit vector. This increases the false-positive rate. Deep, wide trees with large `siglen` values retain more discrimination than shallow trees or small signatures.

## Penalty: Choosing the Insertion Subtree

`gtsvector_penalty()` quantifies the cost of inserting a new key into an existing subtree. Inner-page keys are always signatures, so the penalty measures how many bits the new key would add to the existing union key — a *Hamming distance* between the two bit vectors.

`hemdist()` computes this distance:

- Two non-saturated signatures: count the number of bits set in `(a XOR b)` — bits present in one but not the other.
- One saturated (`ALLISTRUE`) and one regular signature: the distance is the number of zero bits in the regular signature, because the all-true side contributes nothing new.
- Both saturated: distance is 0.

For an `ARRKEY` new entry, `gtsvector_penalty()` first converts it to a temporary bloom filter with `makesign()`. It then computes the Hamming distance against the existing signature. A penalty of 0 means the existing subtree's union key already covers every lexeme in the new document. Inserting there causes no increase in false-positive rate. A higher penalty means more bits would be set, widening the filter and increasing the chance of false positives on future queries.

## Picksplit: Dividing a Full Page

`gtsvector_picksplit()` uses a two-phase strategy when a page fills up.

**Phase 1 — seed selection**: All entries have their signatures cached in a `CACHESIGN` array. The algorithm then evaluates all pairs. It picks the pair with the maximum Hamming distance (`hemdistcache()`) as the two seed entries `seed_1` and `seed_2`. Maximally distant signatures represent the most "opposite" subsets of lexemes. They make the best roots for the two groups, because subsequent entries will clearly prefer one side over the other.

**Phase 2 — assignment**: The algorithm sorts remaining entries by the absolute difference between their distance to `seed_1` and their distance to `seed_2`. Entries with a large difference have a strong preference for one side. Entries with a small difference are roughly equidistant. Sorting by this "decisiveness" score and assigning the most decided entries first reduces the number of entries that end up on the wrong side for balance reasons. The algorithm then assigns each entry to whichever seed it is closer to in Hamming distance, using a small balancing factor (`WISH_F`) that slightly penalises the larger group to keep the two pages roughly equal in size.

As the algorithm assigns entries, it ORs their bits into the accumulating union signature for their respective group. The final `spl_ldatum` and `spl_rdatum` are the two union signatures that will be written into the parent inner page.

## Configurable Signature Length

The `siglen` storage option, parsed by `gtsvector_options()`, controls the bit-vector size. The default is 124 bytes (992 bits). A longer signature reduces the bloom filter's false-positive rate — fewer bits overlap by chance, so more subtrees can be pruned during a search — at the cost of larger index entries and more I/O per page. A shorter signature produces a more compact index but causes more heap rechecks.

The tradeoff is particularly important for collections where individual documents have many distinct lexemes. With only 992 bits and a typical document containing several hundred unique lexemes after normalisation, the filter fill rate can approach 50–70%. This gives modest discrimination. Increasing `siglen` to 256 or 512 bytes markedly reduces false-positive rates for lexeme-rich corpora.

## GiST versus GIN for Full-Text Search

The GiST opclass creates one index entry per document row. [[subsystems/indexes/gin|GIN]] creates one posting-list entry per distinct lexeme. For full-text search, GIN is almost always faster at query time because it directly intersects posting lists without traversing a tree with lossy keys. GiST is preferable in workloads dominated by writes. GIN's pending-list mechanism limits write amplification, but it can incur significant overhead during cleanup. GiST inserts, by contrast, proceed as standard balanced-tree operations with predictable cost.

GiST also supports the `<->` phrase operator at the index level (with recheck), whereas GIN's phrase support similarly requires position-level rechecks against the heap. Neither index format eliminates heap access for phrase queries. Both prune candidates before the recheck.

## Related Topics

- [[subsystems/indexes/gist|GiST Index]] — the generic access method framework this opclass plugs into
- [[subsystems/full-text-search|Full-Text Search Internals]] — tsvector and tsquery structure, the GIN-based index, ranking, and the @@ operator
- [[subsystems/indexes/gin|GIN Index]] — the alternative index access method for tsvector, typically preferred for read-heavy workloads

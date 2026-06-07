---
title: "Catalog Lookup Shortcuts (lsyscache)"
aliases:
  - lsyscache
  - catalog lookup helpers
  - get_typlen
  - get_opcode
tags:
  - theme/caching
source_files:
  - src/backend/utils/cache/lsyscache.c
symbols:
  - get_typlen
  - get_typbyval
  - get_typlenbyval
  - get_typlenbyvalalign
  - get_type_io_data
  - get_attname
  - get_attnum
  - get_atttype
  - get_atttypetypmodcoll
  - get_attavgwidth
  - get_attstatsslot
  - free_attstatsslot
  - get_opcode
  - get_commutator
  - get_negator
  - op_mergejoinable
  - op_hashjoinable
  - get_opfamily_member
  - get_opfamily_proc
  - get_func_rettype
  - get_func_signature
  - func_strict
  - func_volatile
  - func_parallel
  - get_rel_name
  - get_rel_relkind
  - get_rel_persistence
  - get_relname_relid
  - getBaseType
  - getBaseTypeAndTypmod
  - get_mergejoin_opfamilies
  - get_ordering_op_properties
  - get_equality_op_for_ordering_op
  - get_index_isvalid
  - get_index_column_opclass
  - AttStatsSlot
---

`lsyscache.c` is a collection of thin wrapper functions that expose specific fields from system catalog tuples through the [[subsystems/catalog/syscache|catalog cache]] without requiring callers to manage cache entries directly. The parser, planner, and executor each call these helpers dozens of times per query to resolve types, operators, functions, and relation metadata. They eliminate both the boilerplate of `SearchSysCache`/`ReleaseSysCache` pairs and the temptation to open the catalog tuple and hold it longer than necessary.

## Role Within the Cache Layer

The [[subsystems/catalog/syscache|syscache]] provides the raw machinery: hash tables of catalog tuples indexed by OID or name, negative-entry caching, and reference-counted lifetime management. `lsyscache.c` sits on top of that layer and does nothing else — every function follows the same pattern: call `SearchSysCache`, cast `GETSTRUCT(tp)` to the appropriate `Form_pg_*` struct, copy out the needed scalar fields, call `ReleaseSysCache`, and return. The caller never holds a live cache reference after the function returns.

This strict discipline matters because catcache entries are reference-counted: a held entry cannot be freed even after invalidation. Functions in this file are short-lived by construction. This keeps the reference count of each entry near zero during normal operation and allows invalidated entries to be reclaimed promptly.

The companion header `src/include/utils/lsyscache.h` declares every function. Code that needs a catalog field should look there first rather than calling `SearchSysCache` directly. Calling the wrapper is safer because the wrapper handles the miss case consistently and documents the semantics of each field.

## Type Information

The type-lookup functions are the most heavily used group. The planner calls them on every expression node that carries a type OID.

**Scalar property accessors** — `get_typlen()`, `get_typbyval()`, and `get_typtype()` each fetch one field from `pg_type` via the `TYPEOID` cache. The three-in-one variants `get_typlenbyval()` and `get_typlenbyvalalign()` batch multiple fields into a single lookup. This matters in tight loops such as array deconstructing, where saving even one cache probe per element is measurable.

**Six-field batch** — `get_type_io_data()` fetches `typlen`, `typbyval`, `typalign`, `typdelim`, the I/O parameter OID, and the selected I/O function (input, output, receive, or send) in a single cache hit, guided by an `IOFuncSelector` argument. It also handles the bootstrap edge case: during `initdb`, the syscache is not yet operational, so the function delegates to `boot_get_type_io_data()` instead (`lsyscache.c`).

**Domain unwrapping** — `getBaseType()` and `getBaseTypeAndTypmod()` iterate up a domain stack, calling `SearchSysCache1(TYPEOID, ...)` at each level, until they reach a non-domain base type. The loop terminates because `pg_type.typbasetype` is acyclic by construction. Callers that need both the base OID and the effective typmod use `getBaseTypeAndTypmod()` to avoid a second traversal.

**Array/element navigation** — `get_element_type()` returns `typelem` only for types that use `array_subscript_handler` (true arrays); it returns `InvalidOid` for other types that happen to have a `typelem` set. `get_array_type()` goes in the other direction, returning `typarray`. `get_base_element_type()` combines domain unwrapping with element-type retrieval in a single pass.

**Type output** — `getTypeOutputInfo()` and `getTypeInputInfo()` additionally validate that the type is fully defined (not a shell type) and that the requested I/O function actually exists, raising errors if not. They return the function OID alongside a boolean indicating whether the type is varlena.

## Attribute Information

The attribute group wraps the `ATTNUM` and `ATTNAME` syscaches (which index `pg_attribute`).

`get_attname()` and `get_attnum()` convert between an `(relid, attnum)` pair and an `(relid, attname)` pair. Both support a `missing_ok` parameter: when true, a missing attribute returns `NULL` or `InvalidAttrNumber`; when false, the functions raise an error. This reflects two call patterns. The parser expects attributes to exist and wants an error on miss. The planner sometimes probes opportunistically and wants `NULL`.

`get_atttypetypmodcoll()` is a three-fer that returns `atttypid`, `atttypmod`, and `attcollation` from a single cache lookup (`lsyscache.c`). Callers that need all three for expression type-checking should use this rather than three separate calls.

`get_attoptions()` uses `SysCacheGetAttr()` instead of `GETSTRUCT()` because `pg_attribute.attoptions` is a varlena column that can be null; the fixed-size fields in `Form_pg_attribute` do not cover it. This pattern — `SearchSysCache` plus `SysCacheGetAttr` for nullable varlena columns — appears throughout `lsyscache.c` wherever catalog columns are not in the fixed `Form_pg_*` struct.

**Statistics access** — `get_attavgwidth()` consults `pg_statistic` via the `STATRELATTINH` cache to return the average stored width of a column. It checks a plugin hook (`get_attavgwidth_hook`) first, enabling statistics extensions to override the estimate. `get_attstatsslot()` is the more complex companion: given an already-fetched `pg_statistic` tuple, it extracts one of the up-to-five statistics slots by kind and operator OID, deconstructs the `stavalues` array, and returns the results in an `AttStatsSlot` struct. Callers must call `free_attstatsslot()` to release the palloc'd arrays when done. Unlike other functions in this file, `get_attstatsslot()` takes a pre-fetched tuple rather than performing its own cache lookup. Callers typically extract multiple slots from the same tuple, and holding the entry across all extractions is more efficient.

## Operator Information

`get_opcode()` returns the `oprcode` field — the OID of the underlying C function — given an operator OID. `get_commutator()` and `get_negator()` return the commutator and negator operator OIDs respectively, both returning `InvalidOid` if none exists. The planner uses these pervasively during predicate simplification and constraint proofs.

`op_mergejoinable()` and `op_hashjoinable()` consult `pg_operator.oprcanmerge` and `pg_operator.oprcanhash` for the common case. They special-case `ARRAY_EQ_OP` and `RECORD_EQ_OP` by delegating to the type cache (`lookup_type_cache()`), because those operators' join eligibility depends on whether the element or field types support the corresponding operation (`lsyscache.c`).

`get_oprrest()` and `get_oprjoin()` return the selectivity estimator function OIDs (`oprrest` and `oprjoin`), used by the planner to estimate predicate selectivity.

## Operator Family and Access Method Support

The operator family functions bridge operators and access methods. `get_opfamily_member()` looks up the `pg_amop` entry matching a given opfamily, left type, right type, and strategy number, returning the operator OID. The inverse — given an operator OID, find which strategy it implements in which families — requires a partial-key list search (`SearchSysCacheList1(AMOPOPID, ...)`), since one operator can appear in multiple opfamilies.

`get_mergejoin_opfamilies()` uses exactly this list-search pattern to return all btree opfamilies in which a given operator represents equality, producing the list the planner needs to check merge join compatibility. `get_ordering_op_properties()` similarly iterates a list to find a btree opfamily where the given operator implements `<` or `>`, returning the family OID, input type, and strategy number. `get_equality_op_for_ordering_op()` chains these two: find the opfamily for a `<` operator, then find the `=` operator in that family.

`get_opfamily_proc()` looks up `pg_amproc` via `AMPROCNUM` to return the OID of an access-method support procedure given opfamily, left type, right type, and procedure number.

`equality_ops_are_compatible()` and `comparison_ops_are_compatible()` answer whether two operator OIDs share a btree or hash opfamily. The planner uses this to determine whether pathkeys derived from different predicates are interchangeable.

## Function Information

The function group wraps `pg_proc` via the `PROCOID` cache. The available accessors cover the properties the planner and executor need most:

| Function | Returns |
|---|---|
| `get_func_name()` | `proname` as a palloc'd string |
| `get_func_rettype()` | `prorettype` OID |
| `get_func_nargs()` | `pronargs` count |
| `get_func_signature()` | return type OID plus a palloc'd array of argument OIDs |
| `get_func_variadictype()` | `provariadic` OID |
| `get_func_retset()` | `proretset` boolean |
| `func_strict()` | `proisstrict` boolean |
| `func_volatile()` | `provolatile` character |
| `func_parallel()` | `proparallel` character |
| `get_func_prokind()` | `prokind` character (function, procedure, aggregate, window) |
| `get_func_leakproof()` | `proleakproof` boolean |
| `get_func_support()` | `prosupport` OID of the planner support function |

`op_strict()` and `op_volatile()` call `func_strict()` and `func_volatile()`. Both first call `get_opcode()` to resolve the operator to its underlying function before delegating.

## Relation Information

The relation group wraps `pg_class` via `RELOID` and `RELNAMENSP`. `get_relname_relid()` resolves a name and namespace OID to a relation OID using `GetSysCacheOid2()` — a helper that performs the lookup and returns the OID stored in a specified attribute column, all without exposing the raw tuple to the caller. The reverse, `get_rel_name()`, returns a palloc'd copy of the relation name. A comment warns that relation names are not unique across namespaces, so the result should not be used for anything beyond error messages.

The scalar field accessors — `get_rel_relkind()`, `get_rel_persistence()`, `get_rel_namespace()`, `get_rel_tablespace()`, `get_rel_relispartition()`, `get_rel_type_id()` — each make a single `RELOID` lookup. These are distinct from [[subsystems/catalog/relcache|relcache]] lookups: they go through the catcache, not the fully-assembled relation descriptor, so they are cheaper when only one `pg_class` field is needed and no open-relation handle exists.

## Index Information

A small set of functions wraps `pg_index` via `INDEXRELID`. `get_index_isvalid()` and `get_index_isclustered()` return single boolean fields. `get_index_column_opclass()` is more involved: it fetches the `pg_index` tuple, reads the `indclass` OID vector using `SysCacheGetAttrNotNull()` (because `indclass` is a variable-length field outside the fixed-size struct), then indexes into the vector by column number to return the opclass OID.

## Design Conventions

Three conventions appear consistently across the file and are worth noting for callers and contributors.

**Return value on miss.** Functions that return an OID return `InvalidOid` on a cache miss unless they are documented to error. Functions that return a scalar (char, int16, bool) return a neutral default (`'\0'`, `0`, `false`). Functions named with a `missing_ok` parameter make the behavior explicit.

**Varlena columns require SysCacheGetAttr.** The `Form_pg_*` structs cover only the fixed-size (non-null, non-varlena) columns of each catalog. Any column that is nullable or varlena — such as `pg_attribute.attoptions`, `pg_type.typdefaultbin`, or `pg_statistic.stavalues1` — must be extracted via `SysCacheGetAttr()` after obtaining the tuple through the normal lookup path.

**Multi-field lookups consolidate round trips.** The `get_typlenbyval()`, `get_typlenbyvalalign()`, `get_atttypetypmodcoll()`, and `get_type_io_data()` functions exist specifically to collapse what would otherwise be two or more consecutive lookups on the same cache key into one. This matters most in the executor's tight loops over columns and in the planner's type-checking passes.

## Related Topics

- [[subsystems/catalog/syscache|Catalog Caches]] — the underlying catcache and syscache infrastructure that all lsyscache functions sit on top of
- [[subsystems/catalog/relcache|Relation Cache (relcache)]] — the higher-level cache for fully assembled relation descriptors; use when you need more than a single `pg_class` field

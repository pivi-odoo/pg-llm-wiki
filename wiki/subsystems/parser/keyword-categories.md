---
title: "Keyword Categories"
aliases:
  - "SQL Keywords"
  - "Reserved Words"
  - "Keyword Recognition"
source_files:
  - src/include/parser/kwlist.h
  - src/backend/parser/scan.l
  - src/backend/parser/gram.y
  - src/common/kwlookup.c
  - src/pl/plpgsql/src/pl_reserved_kwlist.h
  - src/pl/plpgsql/src/pl_unreserved_kwlist.h
symbols:
  - ScanKeywordLookup
  - PG_KEYWORD
  - unreserved_keyword
  - col_name_keyword
  - type_func_name_keyword
  - reserved_keyword
  - ColLabel
  - ColId
  - BareColLabel
  - BARE_LABEL
---

# Keyword Categories

PostgreSQL recognises around 470 keywords — words that carry special meaning in SQL syntax. Not all of them are equal: some cannot appear in object names without double-quoting, while others can stand freely as identifiers. This distinction, managed through a four-tier classification system, is what determines whether `SELECT timestamp AS timestamp` is legal SQL.

## How the Scanner Identifies Keywords

The scanner first matches every identifier-shaped token — a sequence of letters, digits, and underscores — against the keyword table before the parser sees it. The scanner (`scan.l`) calls `ScanKeywordLookup` the moment it finishes reading an identifier. That function, implemented in `src/common/kwlookup.c`, uses a compile-time perfect hash to locate the candidate entry in constant time. It then does a character-by-character ASCII-only case-insensitive comparison to confirm the match. Crucially, the comparison uses a simple A–Z to a–z mapping rather than locale-aware `tolower()`, ensuring that keywords like `SELECT` are matched consistently regardless of database locale settings (this also avoids the Turkish-I problem).

The keyword table itself lives in `src/include/parser/kwlist.h`. It is a flat list of `PG_KEYWORD` macro invocations, each supplying the keyword's lowercase text, its token value, its category, and whether it may serve as a bare column label (without an explicit `AS`). A build-time script (`gen_keywordlist.pl`) processes this list. It emits the perfect-hash machinery. The entries must appear in ASCII order for the script to work.

When the scanner finds a match it returns the keyword's token value along with the canonical lowercase spelling. When it does not find a match, it returns `IDENT` — a plain identifier. The parser then treats the token as a name with no special meaning. The category annotation in `kwlist.h` is therefore metadata for humans and for grammar rules, not a run-time dispatch mechanism. The scanner does not consult the category at match time. Every keyword, regardless of category, flows into the parser as a distinct token type. The grammar productions determine which contexts accept each token as a name.

## The Four Categories

The grammar (`gram.y`) defines four non-terminal rules — `unreserved_keyword`, `col_name_keyword`, `type_func_name_keyword`, and `reserved_keyword` — each listing the tokens that belong to that tier. These rules appear inside broader name-classification productions (`ColId`, `type_function_name`, `NonReservedWord`, `ColLabel`) that control exactly where keyword tokens are accepted as names.

### unreserved_keyword

A keyword in this category carries no syntactic ambiguity when used as an identifier. The parser can tell from surrounding tokens whether the word is a keyword or a name in that position, so the grammar needs no special machinery. `ColId` accepts `unreserved_keyword` tokens. It is the production used for table names, column names, schema names, and most other object identifiers.

Examples: `ABORT`, `ACCESS`, `BEGIN`, `COMMIT`, `COPY`, `CURSOR`, `INDEX`, `LANGUAGE`, `ROLLBACK`, `SEQUENCE`, `VACUUM`.

This is by far the largest category — roughly 315 of the ~470 keywords fall here — because PostgreSQL goes out of its way to keep words usable as names.

### col_name_keyword

These keywords would create parser ambiguity if allowed as general type or function names. They are safe as column names. The ambiguity typically arises because the keyword can introduce a type specification followed by `(`. This looks syntactically identical to a function call in some contexts. The comment in `gram.y` explicitly warns against mixing `col_name_keyword` tokens with the type/function name productions for this reason.

Examples: `BETWEEN`, `BIGINT`, `BOOLEAN`, `COALESCE`, `EXISTS`, `FLOAT`, `INTEGER`, `INTERVAL`, `NULLIF`, `REAL`, `TIMESTAMP`, `VARCHAR`.

### type_func_name_keyword

These keywords are usable as type names and function names but not as plain column or table names. Most are relational operators or join qualifiers (`INNER`, `LEFT`, `RIGHT`, `FULL`, `CROSS`, `JOIN`, `IS`, `LIKE`, `ILIKE`, `SIMILAR`). These would be ambiguous in identifier position. The parser cannot determine, without further lookahead, whether the word is part of an expression or the beginning of a new clause.

Examples: `AUTHORIZATION`, `BINARY`, `COLLATION`, `CONCURRENTLY`, `CROSS`, `FREEZE`, `FULL`, `INNER`, `IS`, `JOIN`, `LEFT`, `LIKE`, `NATURAL`, `OUTER`, `RIGHT`, `SIMILAR`, `VERBOSE`.

### reserved_keyword

Reserved keywords are the most constrained tier. They appear in `gram.y`'s `reserved_keyword` rule. Only the `ColLabel` production permits them as names. `ColLabel` covers the right-hand side of an `AS` clause. Anywhere else — table name, column name, function name — they require double-quoting. The comment in `gram.y` says a keyword belongs here only when it "could not be distinguished from variable, type, or function names in some contexts." It adds: "Don't put things here unless forced to."

The typical source of ambiguity for reserved keywords is that they introduce major syntactic constructs. `SELECT`, `FROM`, `WHERE`, `GROUP`, and `HAVING` are structural markers that the parser uses to delimit clause boundaries. If any of them could also be an identifier, the grammar would become ambiguous without multi-token lookahead. PostgreSQL's LR(1) parser has only one token of lookahead, so these words must be unambiguously keywords at every point in parsing.

Examples: `ALL`, `AND`, `AS`, `CASE`, `CHECK`, `COLLATE`, `COLUMN`, `CONSTRAINT`, `CREATE`, `DEFAULT`, `DISTINCT`, `ELSE`, `EXCEPT`, `FALSE`, `FOR`, `FROM`, `GRANT`, `GROUP`, `HAVING`, `IN`, `INTO`, `LIMIT`, `NOT`, `NULL`, `ON`, `OR`, `ORDER`, `SELECT`, `TABLE`, `TRUE`, `UNION`, `WHERE`, `WITH`.

## Category Summary

| Category | Count | Usable as column/table name | Usable as type/function name | Usable in AS label |
|---|---|---|---|---|
| `unreserved_keyword` | ~315 | Yes | Yes | Yes |
| `col_name_keyword` | ~55 | Yes | No | Yes |
| `type_func_name_keyword` | ~23 | No | Yes | Yes |
| `reserved_keyword` | ~78 | No | No | Yes (AS only) |

All four categories are accepted in `ColLabel`, meaning every keyword can appear after `AS` in a select list without quoting.

## Practical Implications for SQL Authors

The rule of thumb is: if a word appears in the `reserved_keyword` list you must double-quote it to use it as an identifier. Everything else can generally be used unquoted, though `type_func_name_keyword` words cannot be table or column names without quoting.

```sql
-- Works fine: "begin" is unreserved
CREATE TABLE begin (id int);

-- Works fine: "timestamp" is col_name_keyword
SELECT now() AS timestamp;

-- Fails: "select" is reserved
CREATE TABLE select (id int);  -- ERROR

-- Fixed with double-quotes
CREATE TABLE "select" (id int);

-- "inner" is type_func_name_keyword, not a valid table name unquoted
SELECT * FROM inner;           -- ERROR
SELECT * FROM "inner";         -- OK
```

The `AS` keyword in select lists is forgiving by design: `ColLabel` includes all four categories. As a result, `SELECT 1 AS from` is valid even though `FROM` is reserved. The grammar can unambiguously parse `AS from` as a label context.

## Adding New Keywords

Adding a keyword requires editing `kwlist.h` and, if it participates in grammar rules, also `gram.y`. The rule in the comment at the top of the `gram.y` keyword sections is: *put a new keyword into the first list it can go into without causing shift or reduce conflicts*. This conservative placement keeps as many words as possible available as identifiers.

PostgreSQL adds new keywords sparingly. Any word that moves from `IDENT` territory into a reserved or semi-reserved category becomes a backward-compatibility break. Any existing schema object named with that word will require quoting after the upgrade. The PostgreSQL project treats this as a significant cost. It evaluates new keywords carefully during feature development. This is why even many SQL standard keywords remain unreserved in PostgreSQL. For example, `INDEX`, `SEQUENCE`, `VACUUM`, and `TRIGGER` are all unreserved despite being central PostgreSQL concepts. No grammatical ambiguity forces them into the reserved tier.

The `kwlist.h` entries must stay in ASCII order. The `gen_keywordlist.pl` script enforces this at build time. It also generates both the perfect-hash function and the `keyword_tokens` array that maps keyword numbers to Bison token codes. When the corresponding grammar rule in `gram.y` is updated, developers must keep the category field in `kwlist.h` in sync manually. The comment in `gram.y` notes that there is currently no single source of truth that generates both.

## PL/pgSQL Keyword Overlap

PL/pgSQL has its own scanner and its own two-tier keyword system, defined in `src/pl/plpgsql/src/pl_reserved_kwlist.h` and `src/pl/plpgsql/src/pl_unreserved_kwlist.h`. The PL/pgSQL reserved list contains about 25 keywords. The unreserved list holds about 84.

This set partially overlaps with the core SQL keyword list. Words like `BEGIN`, `DECLARE`, `IF`, `LOOP`, `RETURN`, and `WHILE` that are SQL-level unreserved keywords become PL/pgSQL-reserved keywords inside a function body. PL/pgSQL runs its own lexer pass first. It can therefore apply tighter reservations within the procedural language without affecting SQL statement parsing.

The consequence is that a PL/pgSQL variable named `loop` or `return` will conflict with the language's reserved keywords. However, those words are not reserved in plain SQL. This is distinct from the four-category system described above. It applies only inside `DO` blocks and stored function bodies.

PL/pgSQL also has an `unreserved_keyword` production in `pl_gram.y`, analogous to the one in `gram.y`, listing words that the procedural language parser can accept as identifiers. A word may be PL/pgSQL-unreserved even while being SQL-reserved. For instance, `COLLATE` appears in `pl_unreserved_kwlist.h`. It can be a variable name in PL/pgSQL code, even though it is a SQL reserved keyword.

## The bare-label dimension

A fifth attribute — `BARE_LABEL` vs `AS_LABEL` — sits orthogonally to the four categories. It controls whether a keyword can follow a select-list expression as a column alias without writing `AS`. Most keywords carry `BARE_LABEL`, but a handful (`ARRAY`, `AS`, `CHAR`, `CHARACTER`) are marked `AS_LABEL`. This means the syntax requires an explicit `AS` keyword before them, to avoid ambiguity. `kwlist.h`'s fourth column tracks this. The `BareColLabel` grammar production enforces it.

See [[subsystems/parser/overview]] for how the scanner and parser fit into the broader parse pipeline.

The bare-label classification also applies to non-keyword tokens: plain `IDENT` tokens always qualify as bare labels, so user-defined identifiers can always serve as unquoted `AS` aliases. The restriction only bites for the small set of keywords where the parser cannot rule out an alternative interpretation without seeing the `AS` keyword first.

## Related Topics

- [[subsystems/parser/overview|Parser Overview]] — describes how the scanner and parser stages fit together, providing context for where keyword classification is applied.
- [[subsystems/parser/scanner-support|Scanner Support]] — covers the lexical scanning infrastructure in `scan.l` that calls `ScanKeywordLookup` and drives keyword recognition.
- [[subsystems/parser/parse-tree-nodes|Parse Tree Nodes]] — explains the node types that keyword-driven grammar productions produce when the parser reduces rules.
- [[subsystems/parser/semantic-analysis|Semantic Analysis]] — shows what happens after parsing, where keyword-resolved tokens are interpreted against the catalog.
- [[subsystems/plpgsql/overview|PL/pgSQL Overview]] — covers the PL/pgSQL procedural language, which has its own two-tier keyword system that partially overlaps with the SQL keyword list.
- [[subsystems/catalog/schema-search-path|Schema Search Path]] — relevant because reserved words that cannot be used as unquoted schema or object names interact directly with how the search path resolves identifiers.
- [[subsystems/parser/operator-resolution|Operator Resolution]] — operator tokens and keyword tokens follow adjacent lookup paths in the scanner and share grammar-level ambiguity concerns.

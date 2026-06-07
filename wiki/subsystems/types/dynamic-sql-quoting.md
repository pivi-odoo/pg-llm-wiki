---
title: "Dynamic SQL Quoting and Type-Name Formatting"
aliases:
  - quote_ident
  - quote_literal
  - quote_nullable
  - format() dynamic SQL
  - format_type
  - dynamic SQL injection
source_files:
  - src/backend/utils/adt/quote.c
  - src/backend/utils/adt/format_type.c
  - src/backend/utils/adt/varlena.c
  - src/backend/utils/adt/ruleutils.c
symbols:
  - quote_ident
  - quote_literal
  - quote_nullable
  - text_format
  - format_type
  - format_type_extended
  - quote_identifier
  - quote_qualified_identifier
  - printTypmod
---

PostgreSQL provides a small family of SQL-callable functions — `quote_ident`, `quote_literal`, `quote_nullable`, `format`, and `format_type` — that together cover the entire surface of safe dynamic SQL construction: quoting identifiers, escaping string literals, handling NULL, composing complete statements, and looking up canonical type names for catalog introspection. Getting any one of these wrong in a PL/pgSQL `EXECUTE` statement is the database equivalent of string-concatenated HTML — a direct path to SQL injection. Using them correctly is both safer and more readable than manual escaping.

## Identifier Quoting with quote_ident

PostgreSQL identifiers are case-folded to lowercase unless they are double-quoted. `quote_ident(text)` adds double quotes around an identifier only when they are necessary to preserve its meaning or prevent a parse conflict. The decision rule implemented in `quote_identifier` (in `ruleutils.c`, called by the SQL-callable `quote_ident`) is precise:

- An identifier is safe without quotes if it starts with a lowercase ASCII letter or underscore, contains only lowercase ASCII letters, digits, and underscores, and is not a SQL keyword (reserved or column-name category — unreserved keywords like `name` are allowed unquoted).
- Any character outside that set — uppercase letters, spaces, punctuation, non-ASCII — forces quoting.
- The GUC `quote_all_identifiers` overrides the safe check, forcing quotes on everything. `pg_dump --quote-all-identifiers` uses this override.

When `quote_ident` adds quotes, it doubles any embedded double-quote character (`"` → `""`). The function returns the original pointer unchanged if no quoting is needed. It allocates a new palloc'd string only when it must add delimiters.

Practical consequences: `quote_ident('mycolumn')` returns `mycolumn` unchanged. `quote_ident('MyColumn')` returns `"MyColumn"`. `quote_ident('select')` returns `"select"` because `select` is a reserved keyword. `quote_ident('user')` returns `"user"` for the same reason. An empty string `''` returns `""` — a valid but unusual quoted identifier.

## Literal Quoting with quote_literal and quote_nullable

Where `quote_ident` wraps identifiers for use as object names in SQL, `quote_literal(text)` wraps string values for use as literal constants. The output is always safe regardless of the `standard_conforming_strings` setting. The implementation in `quote_literal_internal` (in `quote.c`) detects any backslash character in the input. If it finds one, it prepends the `E` escape-string prefix before the opening single quote. `quote_literal_internal` also doubles single-quote characters in the value. The result can be pasted into any SQL string context without further escaping.

`quote_nullable(text)` extends this with NULL handling. When its argument is SQL NULL, it returns the four-character string `NULL` (without quotes). This is the correct SQL representation of a null value in an `EXECUTE` statement. When the argument is non-null it delegates directly to `quote_literal`. This matters because naively concatenating a null text variable into a dynamic SQL string produces nothing. In SQL, the concatenation of any value with NULL is NULL. This silently corrupts the statement. `quote_nullable` makes the NULL explicit and safe.

The asymmetry between identifiers and literals is intentional. A null identifier has no meaningful SQL representation. There is no concept of "a column whose name is NULL". Because of this, `format()` raises an error when `%I` receives a NULL argument. `%L` produces `NULL` instead, following the same convention as `quote_nullable`.

## The format() Builder

`format(formatstr, ...)` (implemented as `text_format` in `varlena.c`) is the preferred way to assemble complete dynamic SQL statements. It provides three conversion specifiers:

- `%I` — passes its argument through `quote_identifier`, safe for table names, column names, schema names, and any other SQL identifier.
- `%L` — passes its argument through `quote_literal_cstr`, safe for string values, parameters, and data being embedded as literals.
- `%s` — raw substitution with no escaping, appropriate only for trusted content such as a hardcoded SQL fragment, a type name already validated by other means, or the output of another quoting function.

A fourth pseudo-specifier `%%` produces a literal percent sign.

By default, `format()` consumes arguments from left to right. Positional references like `%1$I` allow reuse of a single argument in multiple places. This is useful when generating SQL that references the same table name in different roles — for example, both as the source table and in a `FROM` clause join.

NULL behavior per specifier matches the rules described above: `%s` renders NULL as an empty string, `%L` renders it as `NULL`, and `%I` raises an error.

The practical pattern for safe dynamic SQL in PL/pgSQL looks like:

```sql
EXECUTE format(
    'INSERT INTO %I.%I (%I) VALUES (%L)',
    schema_name, table_name, column_name, user_supplied_value
);
```

This is safe regardless of what `schema_name`, `table_name`, `column_name`, or `user_supplied_value` contain. The equivalent constructed by string concatenation — `'INSERT INTO ' || schema_name || '.' || table_name || ...` — is vulnerable to injection via any of those variables. It also breaks on identifiers with spaces or reserved-word names.

## When String Concatenation Becomes Injection

The injection risk is worth understanding concretely. If `table_name` is supplied by an application user and contains `users; DROP TABLE users--`, the concatenated `'SELECT * FROM ' || table_name` becomes `SELECT * FROM users; DROP TABLE users--`. This executes two statements. With `format('SELECT * FROM %I', table_name)`, the same input becomes `SELECT * FROM "users; DROP TABLE users--"`. This fails with a benign "relation not found" error, because no table with that name exists.

Less obviously, forgetting `%I` for column names is also exploitable: a column name of `col1, pg_sleep(10)--` in a `SELECT %s FROM ...` construction causes unexpected side effects. The rule is: any value that originates outside the current trusted code path — a function argument, a catalog lookup result, a configuration value — must go through `%I` or `%L` before entering a dynamic SQL string.

## Reserved-Word Table Names

A common source of confusion is identifiers that look safe but are SQL keywords. A table named `order`, `user`, `table`, or `select` will cause a parse error if used unquoted in dynamic SQL. `quote_ident` and `%I` handle this correctly: `quote_ident('order')` returns `"order"` because `order` is a reserved keyword. The safe-identifier test in `quote_identifier` explicitly calls `ScanKeywordLookup` against the full keyword table, treating anything above `UNRESERVED_KEYWORD` category as unsafe.

## format_type: Canonical Type Names for Catalog Queries

`format_type(type_oid, typemod)` translates a type OID plus a type modifier integer (as stored in `pg_attribute.atttypmod`) into the canonical SQL type name for that combination. `pg_dump`, `\d` output from psql, and catalog introspection queries all use it heavily.

The implementation in `format_type_extended` (in `format_type.c`) has two modes of operation. For a set of well-known built-in types it returns their SQL-standard names rather than the internal `pg_type.typname` values: OID `23` (int4) becomes `integer`, OID `701` (float8) becomes `double precision`, OID `1114` (timestamptz) becomes `timestamp with time zone`. This mapping exists because the SQL standard names are special productions in the grammar. They parse differently from ordinary identifiers. The internal name would need quoting instead, and the grammar would reject that quoting.

For all other types, the function falls back to the catalog name from `pg_type.typname`. It applies `quote_identifier` if the name contains special characters or matches a keyword. If the type is not in the current search path, it qualifies the name with the schema.

The `typemod` argument controls precision and length suffixes. Passing `-1` with `FORMAT_TYPE_TYPEMOD_GIVEN` set produces the unadorned base name. Passing an actual modifier value produces output like `character varying(255)` or `numeric(10,2)`. Passing SQL NULL for the second argument activates a slightly prettier representation, for display contexts where the parser's exact interpretation does not matter — `character` rather than `bpchar`, for instance. This NULL value maps to the `typemod = -1` code path, but without the `FORMAT_TYPE_TYPEMOD_GIVEN` flag.

For array types, `format_type` looks through to the element type. It then appends `[]` to form the array notation. For example, the OID of `integer[]` produces `integer[]`.

The `FORMAT_TYPE_ALLOW_INVALID` flag (set by the SQL-callable wrapper) makes the function return `???` for unknown OIDs instead of raising an error. This behavior is appropriate for catalog queries, where stale OIDs might appear.

A typical catalog introspection query using `format_type`:

```sql
SELECT attname, format_type(atttypid, atttypmod) AS type
FROM pg_attribute
WHERE attrelid = 'mytable'::regclass AND attnum > 0 AND NOT attisdropped;
```

## Related Topics

- [[subsystems/planner/overview|planner]] — uses `format_type` for type display in EXPLAIN output
- [[subsystems/storage/toast|TOAST]] — storage strategy interacts with `type_maximum_size`, which shares typemod encoding knowledge with `format_type`
- [[subsystems/triggers|triggers]] — `ruleutils.c` uses `quote_identifier` extensively when decompiling trigger definitions
- [[subsystems/partitioning/overview|partitioning]] — partition constraint expressions are decompiled using the same quoting infrastructure

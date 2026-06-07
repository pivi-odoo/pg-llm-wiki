---
title: "XML Type Internals"
aliases:
  - xml type
  - xmltype
  - XML storage
source_files:
  - src/backend/utils/adt/xml.c
  - src/include/utils/xml.h
symbols:
  - xml_parse
  - pg_xml_init
  - pg_xml_done
  - PgXmlErrorContext
  - XmlTableBuilderData
  - XmlTableRoutine
  - xmloption
  - xmlbinary
  - xml_in
  - xml_out
  - xpath_internal
---

The `xml` type stores well-formed XML documents or XML content fragments as variable-length text. Its implementation is almost entirely conditional on the server being compiled with libxml2. Without that library, the type still exists in the catalog and its I/O functions work, but nearly every operation raises an "unsupported XML feature" error. This split allows databases containing XML data to be dumped and restored even on servers built without libxml2 support.

## Storage representation

`xmltype` is a typedef for `struct varlena`, meaning it is stored identically to `text` (xml.h). The raw bytes are the UTF-8 encoded XML text, potentially preceded by an XML declaration (`<?xml version="1.0" encoding="..."?>`). PostgreSQL does not convert the stored text to any internal DOM representation; it remains as character data on disk and in memory, subject to [[subsystems/storage/toast|TOAST]] compression and out-of-line storage like any other varlena type. The binary I/O path (`xml_recv()`, `xml_send()`) encodes values in the database server encoding, not necessarily UTF-8, and re-encodes on receipt.

Because the on-disk format is plain text, casting between `xml` and `text` is binary-compatible (the cast function `xmltotext` simply returns the input pointer recast). The distinction between the types is purely at the validation layer.

## Parsing and validation on input

When a value is accepted via `xml_in()`, PostgreSQL parses it through libxml2 to confirm it is well-formed. It then discards the resulting DOM tree and stores only the original text. This means the stored bytes are always the user-supplied text, not a re-serialized form. The type acts as a validated text store, not a normalizing one.

The central parsing function `xml_parse()` (xml.c) handles the DOCUMENT/CONTENT distinction controlled by the `xmloption` GUC:

- **`XMLOPTION_DOCUMENT`**: Input must be a single-rooted XML document. `xml_parse()` uses libxml2's `xmlCtxtReadDoc()` with `XML_PARSE_NOENT | XML_PARSE_DTDATTR`. These flags expand entity references and apply DTD-defined attribute defaults per the SQL/XML:2008 specification.
- **`XMLOPTION_CONTENT`**: Input is the production `XMLDecl? content` — an optional declaration followed by any sequence of nodes. libxml2 does not natively support this grammar, so `xml_parse()` first calls `parse_xml_decl()` to strip the declaration, then passes the remainder to `xmlParseBalancedChunkMemory()`. A special case: if the parser detects a `<!DOCTYPE` inside what was presented as CONTENT, it promotes the parse to DOCUMENT mode automatically. This provides SQL/XML:2006-compatible behavior, where any valid document is also valid content.

PostgreSQL code (`parse_xml_decl()`, xml.c) handles the XML declaration entirely; libxml2 does not, because it can only parse the content node portion. The custom parser walks the `<?xml ... ?>` prologue byte-by-byte, extracting version, encoding, and standalone attributes.

## Error handling architecture

libxml2 uses global error callbacks. This creates a problem in PostgreSQL: libxml2 must not call `longjmp` or `ereport` directly, since that would bypass libxml2's own cleanup. The solution is a deferred error pattern built around `PgXmlErrorContext`:

```c
struct PgXmlErrorContext {
    int                   magic;          /* ERRCXT_MAGIC for validity check */
    PgXmlStrictness       strictness;
    bool                  err_occurred;   /* deferred error flag */
    StringInfoData        err_buf;        /* accumulated error text */
    xmlStructuredErrorFunc saved_errfunc; /* libxml2 state to restore */
    void                 *saved_errcxt;
    xmlExternalEntityLoader saved_entityfunc;
};
```

`pg_xml_init()` installs `xml_errorHandler` as libxml2's structured error callback and replaces the external entity loader with `xmlPgEntityLoader`. `xml_errorHandler` never calls `ereport` directly for errors; instead it sets `errcxt->err_occurred = true` and accumulates the message text. After any libxml2 API call that could fail, the caller checks `xmlerrcxt->err_occurred` and then calls `xml_ereport()` to convert the accumulated error into a PostgreSQL `ereport()`. PostgreSQL emits warnings and notices from libxml2 immediately, since they do not cause a `longjmp`.

Every caller of `pg_xml_init()` must call `pg_xml_done()` in both normal and error paths, typically via a `PG_TRY`/`PG_CATCH` block. `pg_xml_done()` restores the previously saved libxml2 handlers and frees the context. An assert verifies that no pending error was silently dropped on a clean exit path.

The external entity loader `xmlPgEntityLoader` silently replaces any external entity URL with an empty string. This prevents libxml2 from fetching arbitrary files or URLs during XML parsing, a security measure noted explicitly in the source.

## Strictness levels

`PgXmlStrictness` controls which libxml2 messages are promoted to PostgreSQL errors:

| Level | Behavior |
|---|---|
| `PG_XML_STRICTNESS_LEGACY` | All messages accumulated in `err_buf`; `err_occurred` never set. Used by the deprecated `xml2` contrib module. |
| `PG_XML_STRICTNESS_WELLFORMED` | Only parser-domain errors are reported. Namespace and other non-parser messages are suppressed. Used during `xml_in()` validation. |
| `PG_XML_STRICTNESS_ALL` | All errors, warnings, and notices are reported. Used by `xpath()`, `XMLTABLE`, and XML construction functions. |

## GUC parameters

Two GUCs affect XML behavior:

- **`xmloption`** (`XMLOPTION_DOCUMENT` or `XMLOPTION_CONTENT`): Controls whether implicit casts from `text` to `xml` require a full document or allow a content fragment. Defaults to `CONTENT`.
- **`xmlbinary`** (`XMLBINARY_BASE64` or `XMLBINARY_HEX`): Controls the encoding used when serializing `bytea` values inside XML via `map_sql_value_to_xml_value()`. Defaults to `BASE64`.

## XML construction functions

The executor evaluates SQL/XML constructor expressions (`XMLELEMENT`, `XMLFOREST`, `XMLCONCAT`, `XMLPI`, `XMLROOT`, `XMLCOMMENT`) and routes them to C functions in xml.c. `xmlelement()` and similar functions use libxml2's `xmlTextWriter` API to build the XML string into an `xmlBuffer`, then convert the result with `xmlBuffer_to_xmltype()`. The `xmlconcat()` function merges XML declarations from multiple inputs. It picks a common version if all inputs agree. It promotes `standalone` to `yes` only if all inputs declare it as such.

`xml_out_internal()` intentionally strips the `encoding` attribute from any XML declaration before output. This is because PostgreSQL converts character encodings during transmission to the client. Preserving a declaration that claims a specific encoding would therefore be incorrect.

## XPath and XMLTABLE

`xpath()` and `xmlexists()` share the internal function `xpath_internal()` (xml.c). This function parses the XML document value into a libxml2 DOM and compiles the XPath expression with `xmlXPathCtxtCompile()` (preferred over `xmlXPathCompile()` to avoid a stack-overflow bug in older libxml2 versions). It then evaluates the expression and converts the resulting `xmlXPathObject` to a PostgreSQL array. `xml_xmlnodetoxmltype()` serializes node results back to XML text. It copies the node with `xmlCopyNode()` to preserve namespace declarations from ancestor nodes before calling `xmlNodeDump()`. `map_sql_value_to_xml_value()` converts scalar results (boolean, number, string).

The generic `TableFuncScan` executor node implements `XMLTABLE`. PostgreSQL's table function API defines a `TableFuncRoutine` callback struct; `XmlTableRoutine` (xml.c) provides the XML-specific implementations. The state for a single `XMLTABLE` scan lives in `XmlTableBuilderData`:

```c
typedef struct XmlTableBuilderData {
    int                  magic;        /* XMLTABLE_CONTEXT_MAGIC */
    int                  natts;
    long int             row_count;
    PgXmlErrorContext   *xmlerrcxt;
    xmlParserCtxtPtr     ctxt;
    xmlDocPtr            doc;
    xmlXPathContextPtr   xpathcxt;
    xmlXPathCompExprPtr  xpathcomp;    /* row filter */
    xmlXPathCompExprPtr *xpathscomp;   /* per-column filters */
} XmlTableBuilderData;
```

`XmlTableInitOpaque` calls `pg_xml_init()`. The matching `XmlTableDestroyOpaque` calls `pg_xml_done()`. Because the libxml2 error handler state is global, the source comments note a critical constraint: no other executor node may run libxml2 between initialization and destruction of an `XMLTABLE` node. The node must be driven to completion in a single pass, typically by materializing its output into a tuplestore.

## SQL-to-XML mapping

A separate set of functions (`table_to_xml`, `schema_to_xml`, `database_to_xml`, and their `_and_xmlschema` variants) implements the SQL/XML mapping of relational data to XML documents. These work by issuing SQL queries through SPI, then rendering rows using `SPI_sql_row_to_xmlelement()`. `map_sql_value_to_xml_value()` converts column values to their XSD string representations. It handles type-specific formatting: dates and timestamps use ISO 8601 format (XSD), booleans use `true`/`false`, and bytea uses base64 or hex according to the `xmlbinary` GUC. `map_sql_identifier_to_xml_name()` maps identifiers to valid XML names, using the SQL/XML:2008 section 9.1 escaping scheme (`_xNNNN_` for characters that are not valid XML name characters).

## Related Topics

- [[subsystems/types/base-types|Base Types]] — how varlena types like xml are registered and their I/O interface
- [[subsystems/storage/toast|TOAST]] — out-of-line storage for large XML values

---
title: "Articles"
aliases:
  - "References"
  - "Articles"
---

# Articles

Secondary sources about PostgreSQL internals. Treat these as interpretation and context, not as implementation truth — the source code is authoritative.

## Official documentation

- [PostgreSQL Documentation — Frontend/Backend Protocol](https://www.postgresql.org/docs/current/protocol.html) — the specification of the wire protocol; authoritative for message formats.
- [PostgreSQL Documentation — System Catalogs](https://www.postgresql.org/docs/current/catalogs.html) — schema of all system tables.
- [PostgreSQL Documentation — Executor](https://www.postgresql.org/docs/current/executor.html) — high-level overview in the developer docs.

## Internals guides

- [The Internals of PostgreSQL](https://www.interdb.jp/pg/) (Hironobu Suzuki) — chapter-by-chapter walkthrough of heap storage, MVCC, query processing, and WAL. Reliable but written against older versions; check source for current details.
- [PostgreSQL 14 Internals](https://postgrespro.com/community/books/internals) (Egor Rogov, Postgres Professional) — free ebook covering architecture, isolation, WAL, and replication in depth.

## Papers

- Stonebraker & Rowe, *The Design of POSTGRES* (1986) — original design paper. Explains the rule system, type extensibility, and the design decisions behind the storage manager that still shape the codebase.
- Hellerstein, Haas & Wang, *Online Aggregation* (SIGMOD 1997) — background on sampling-based query execution; relevant context for understanding why the Volcano model suits interactive queries.

## Blog posts

Add entries here as relevant posts are identified. Prefer posts that cite specific locations in source files or commit hashes over posts that describe behaviour without grounding it in code.

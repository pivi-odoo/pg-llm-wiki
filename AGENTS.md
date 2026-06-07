# Agent Instructions

This repository is an LLM-assisted wiki for PostgreSQL internals.

## Directory rules

- `sources/` contains raw evidence.
- `sources/postgresql/upstream.git/` is the PostgreSQL mirror clone.
- `sources/postgresql/worktrees/` contains read-only PostgreSQL worktrees.
- Never edit files under `sources/postgresql/upstream.git/`.
- Never edit files under `sources/postgresql/worktrees/`.
- `source-index/` contains generated indexes. Do not hand-edit unless explicitly asked.
- `wiki/` contains durable Obsidian-browsable explanations.
- `questions/` contains messy investigations.
- `reports/` contains generated maintenance reports.
- `tools/` contains scripts.

## Wiki directory layout

The vault root is `wiki/`. Top-level directories:

| Directory | Contents |
|---|---|
| `architecture/` | High-level system overviews (process model, shared memory, startup sequence) |
| `code-paths/` | Per-command execution walkthroughs (one page per SQL command or operation) |
| `sql-features/` | SQL syntax, behavioral semantics, and usage patterns for specific features |
| `subsystems/` | Deep dives into internal subsystems, grouped by area (see below) |
| `troubleshooting/` | Diagnostic guides for specific failure modes |
| `references/` | External article links and bibliography |

`subsystems/` subdirectories: `auth/`, `background/`, `catalog/`, `executor/`, `extensions/`, `indexes/`, `locking/`, `memory/`, `observability/`, `parser/`, `partitioning/`, `planner/`, `plpgsql/`, `replication/`, `rewriter/`, `storage/`, `transactions/`, `types/`, `wal/`.

`wiki/index.md` is the home page. It lists every page organised by section.

## sql-features/ vs subsystems/ charter

These two sections cover the same features from different angles. The rule is strict:

- **`subsystems/`** covers how PostgreSQL implements the feature internally: data structures, algorithms, executor nodes, planner decisions, source file layout. A `subsystems/` article assumes the reader wants to understand the implementation.

- **`sql-features/`** covers how to use the feature from SQL: syntax, behavioral rules, usage patterns, gotchas, and trade-offs visible from the SQL layer. A `sql-features/` article assumes the reader already knows what the feature is (linking to the paired `subsystems/` article for internals) and wants to understand how to write correct, performant SQL using it.

Enforce this split when writing or reviewing articles:
- A `sql-features/` article must **not** re-explain how PostgreSQL implements the feature internally. Its opening paragraph should state the SQL-facing purpose and link to the paired `subsystems/` article.
- A `subsystems/` article must **not** drift into SQL tutorial territory or duplicate the usage examples in the paired `sql-features/` article.

## Naming

- Use lowercase kebab-case filenames and directories.
- Do not use spaces in filenames.
- Put pretty display names in YAML frontmatter using `title`.
- Add useful Obsidian aliases using `aliases`.

## Frontmatter

Every wiki page must have YAML frontmatter. Required fields:

```yaml
---
title: "Pretty Title Here"
aliases:
  - alternative name
  - another alias
source_files:
  - src/backend/path/to/file.c
  - src/include/path/to/header.h
symbols:
  - RelevantFunctionName
  - RelevantStructName
---
```

- `title`: human-readable display name used by Obsidian.
- `aliases`: alternative names a reader might search for.
- `source_files`: the primary `.c` and `.h` files the page draws from.
- `symbols`: the key functions, structs, and macros discussed on the page.

Optional field, add when applicable (see Tags below):

```yaml
tags:
  - theme/concurrency-control
  - symptom/lock-wait
```

Field order: `title`, `aliases`, `tags`, `source_files`, `symbols`.

## Tags

Tags give Obsidian's tag pane a browsing path that cuts across the directory tree, for readers who have a problem in hand rather than a subsystem name. Only use tags from this fixed taxonomy — do not invent new ones. Most pages get 0–3 tags; it's fine for a page to have none.

`symptom/*` — the page is directly useful when diagnosing this problem (add sparingly; mainly `troubleshooting/` pages and the specific deep-dive pages they point to):

| Tag | Use for |
|---|---|
| `symptom/slow-query` | Query latency investigation |
| `symptom/high-cpu` | CPU-bound backend/planner/executor behavior |
| `symptom/high-io` | I/O-bound behavior, checkpoint/vacuum I/O spikes |
| `symptom/lock-wait` | Lock contention, waiting backends |
| `symptom/deadlock` | Deadlock detection and causes |
| `symptom/replication-lag` | Standby/subscriber falling behind |
| `symptom/bloat` | Table/index bloat |
| `symptom/connection-exhaustion` | Running out of connections/backends |
| `symptom/out-of-memory` | OOM kills, memory exhaustion |
| `symptom/disk-full` | pg_wal or data directory filling up |
| `symptom/xid-wraparound` | Transaction ID / MultiXact wraparound risk |
| `symptom/auth-failure` | Authentication rejections |
| `symptom/corruption` | Data or index corruption |
| `symptom/failover` | Failover/switchover behavior and issues |

`theme/*` — the page's subject is part of this cross-cutting concept, even though it lives in a subsystem-specific directory (add one where clearly applicable):

| Tag | Spans |
|---|---|
| `theme/concurrency-control` | MVCC, locking, isolation, snapshots |
| `theme/durability` | WAL, checkpoints, crash recovery, fsync |
| `theme/caching` | syscache, relcache, buffer manager, memoize |
| `theme/parallelism` | Parallel query, parallel apply, parallel vacuum/workers |
| `theme/query-optimization` | Planner costing, statistics, plan shape |
| `theme/vacuum-and-maintenance` | Vacuum, autovacuum, freezing, bloat internals |
| `theme/observability` | Stats views, wait events, logging |
| `theme/extensibility` | Hooks, custom access methods/FDWs/operators |
| `theme/wire-protocol` | Client-server protocol, libpq, connection handling |
| `theme/storage-format` | Page/tuple layout, TOAST, compression |

Do not tag with the directory/subsystem name itself (e.g. no `#storage`, `#executor`) — the directory tree already provides that navigation; tags should add an axis it can't.

## Wiki rules

- Write generic articles that apply across versions. Do not pin articles to a specific version in frontmatter or prose.
- Add inline version notes only when behaviour actually differs between versions.
- Create version-specific pages only when version caveats dominate the page.
- Source-level claims should cite the source file. Omit the version unless the detail is version-specific.
- Do not claim certainty when unsure.

## Article structure

Every page follows this structure:

1. **Opening paragraph** (no header): introduce what the mechanism does and why it matters in 2–4 sentences. This is the page's "lead" — it should make sense out of context and give a reader enough to decide whether to read further.
2. **Concept sections**: organise around design decisions and invariants, not around the call graph. Use `##` headers for major concepts, `###` for sub-concepts.
3. **Related Topics** (last section): a short `## Related Topics` or `## See also` bullet list of `[[wikilinks]]` to closely related pages.

Do not add an `## Introduction` or `## Overview` header — the opening paragraph is the introduction. Do not add a `## Conclusion` or `## Summary` header.

## Cross-linking

- Link the **first occurrence** of a concept per page when a dedicated wiki page exists for it. One link per target per page is enough; do not repeat it.
- Use `[[path/to/page|display text]]` format. The path is relative to the vault root (`wiki/`) with no `.md` extension.
- Do not add links inside code blocks, frontmatter, or inside the label of an existing link.
- Key concepts to link on first mention (when not on the concept's own page):

| Term | Target |
|---|---|
| autovacuum | `subsystems/background/autovacuum` |
| bgwriter | `subsystems/background/bgwriter` |
| CLOG | `subsystems/storage/clog` |
| fillfactor | `subsystems/storage/fillfactor` |
| free space map / FSM | `subsystems/storage/fsm` |
| hint bits | `subsystems/transactions/hint-bits` |
| JIT | `subsystems/executor/jit-llvm` |
| LWLock / lightweight lock | `subsystems/locking/lwlocks` |
| memory context | `subsystems/memory/contexts` |
| pg_stat_statements | `subsystems/observability/pg-stat-statements` |
| ResourceOwner | `subsystems/memory/resource-owner` |
| TOAST / TOASTed | `subsystems/storage/toast` |
| visibility map | `subsystems/storage/visibility-map` |
| work_mem | `subsystems/executor/work-mem-and-spill` |
| XID wraparound | `subsystems/transactions/xid-wraparound` |

## Writing style

- Write like a good technical wiki, not like a response to a prompt. Explanations should read as background the article assumes you should know, not as answers to explicit "why?" questions.
- Weave context and motivation into the description naturally. Do not use "Why X?" or "Goal:" headers to label explanations — just explain.
- Teach concepts, not code. Source references are evidence; they are not the content. A reader should understand the design after reading the page, not just know which function to grep for.
- Keep the tone direct and confident. Avoid filler phrases like "it is worth noting" or "as mentioned above".

### Sentence-level clarity

Borrowed from ASD-STE100 (Simplified Technical English) — the sentence-shape discipline, not the restricted vocabulary. Domain terminology stays unrestricted; these rules are about how sentences are built.

- One idea per sentence. Split sentences joined by "and"/"which" when they describe two separate facts or steps.
- Prefer active voice and a stated subject: "The planner chooses a sequential scan" over "A sequential scan is chosen".
- Use the same term for the same concept throughout a page. Do not vary vocabulary for the sake of variety (e.g. don't alternate "backend", "process", and "connection" for the same thing).
- Avoid stacking more than two nouns as a modifier chain. "The vacuum freeze threshold" is fine; reword longer chains ("the threshold that triggers freeze during vacuum").
- Prefer one main clause plus at most one subordinate clause. Break up sentences with multiple subordinate/relative clauses.

### Anti-patterns

These patterns make an article read like a code walkthrough rather than a wiki. Avoid them.

- **Numbered section headers.** `## 1. Why Checkpoints Exist`, `## 5. CreateCheckPoint() Step by Step`. If ordering matters, use a Mermaid diagram or a numbered list inside a prose section — not numbered `##` headers.
- **Function-name section headers.** `## ExecModifyTable`, `## HeapTupleSatisfiesMVCC`, `## LockAcquire() Step by Step`. Function names belong inline in prose as parenthetical references — `(ExecModifyTable(), nodeModifyTable.c)` — not as section titles.
- **Step-by-step source traces.** Prose or numbered lists that mirror source lines ("Step 1: calls Y at line 42. Step 2: then calls Z at line 67.") is not wiki content. Explain the design intent, the invariants, and the constraints; cite source locations as supporting evidence.
- **Function bodies as primary content.** Pasting a complete function body as the main content of a section just restates what a reader can already find with grep. Code blocks are appropriate only when the exact syntax is the point — a key struct layout, a short decision condition. When the block just restates the surrounding prose, remove it.
- **"Why X?" and "Motivation" headers.** `## Why Checkpoints Exist`, `## Motivation`, `## Goal`. The motivation belongs in the opening sentences of the concept being introduced, not in a labelled section of its own.

### Positive patterns

- Lead with what a mechanism *does* and *why it matters*, then bring in implementation details as supporting evidence.
- Name a mechanism by its concept before naming the function that implements it. "The checkpointer spreads writes over time to avoid I/O spikes (`CheckpointWriteDelay()`, `checkpointer.c`)" is better than "`CheckpointWriteDelay()` sleeps when progress exceeds the elapsed-time fraction".
- Organise sections around concepts and design decisions, not around call graphs. A section that maps one-to-one with a single function is a smell.
- Reference tables (struct fields, flag constants, state enums) are reference material, not code traces. Keep them.

## Source truth

- PostgreSQL source code is the authority for implementation details.
- `source-index/` is only a navigation aid.
- Articles/postmortems are secondary sources and should be treated as interpretation or evidence, not as implementation truth.

## Obsidian

- The Obsidian vault root is `wiki/`.
- Use Obsidian links for wiki pages.
- Use inline code for source paths, for example `src/backend/executor/execMain.c`.

## Diagrams

- Wiki articles may use Mermaid diagrams inside fenced code blocks tagged ` ```mermaid `.
- Obsidian renders Mermaid natively; no plugin is required.
- Use diagrams where a visual adds clarity that prose or ASCII cannot match easily:
  - `flowchart LR` for data-structure transformation pipelines (input → function → output).
  - `flowchart TD` for call hierarchies or stage sequences.
  - `sequenceDiagram` for protocol exchanges or time-ordered interactions.
  - `stateDiagram-v2` for state machines (e.g. portal states, lock modes).
- Do not add a diagram just to restate prose. Each diagram should show structure or flow that is harder to read in text.

### Mermaid authoring checklist (apply every time, no exceptions)

Run this check before finalising every Mermaid block:

1. **Open every Mermaid block with ` ```mermaid `, never with bare ` ``` `.** A bare fence renders as a plain code block and the diagram is not drawn.
2. **No `\n` and no literal newlines inside node label strings.** Neither renders as a line break — `\n` appears literally, and a real newline can silently break the parser. Use `<br/>` and keep every label on one source line.
   - Wrong: `A["foo()\nbar.c:42"]` · wrong: `A["foo()\n    bar.c:42"]` (literal newline)
   - Right:  `A["foo()<br/>bar.c:42"]`
3. **No `\n` in edge labels** — `-->|"text\nmore text"|`. Reword or use a space.
4. **Quoted node labels for any label containing special characters** (parentheses, slashes, angle brackets, colons). Always wrap in double quotes: `A["func()"]`.
5. **Keep node IDs short and ASCII-only** (letters and digits). The display text goes in the label string.
6. **Keep diagrams narrow: use `flowchart TD` by default.** `flowchart LR` is acceptable only when the chain has 2–3 nodes and a left-to-right reading order is meaningful (e.g. a 3-step pipeline). Longer chains, call hierarchies, taxonomies, and state machines always use `flowchart TD`. If a TD layout would be too tall, split it into two separate diagrams.
7. After writing a diagram, scan every `"..."` string for `\n` and check that no label spans multiple source lines.

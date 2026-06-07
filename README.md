# PostgreSQL LLM Wiki

An Obsidian-browsable LLM-assisted wiki for PostgreSQL internals.

## Layout

- `sources/`: raw evidence, including PostgreSQL source checkouts and articles.
- `source-index/`: generated indexes over sources.
- `wiki/`: Obsidian vault with durable explanations.
- `questions/`: messy investigations and open questions.
- `reports/`: lint, stale-page, and coverage reports.
- `tools/`: scripts used by the wiki.
- `justfile`: common commands.

## Principles

- Do not edit PostgreSQL source checkouts.
- Use lowercase kebab-case filenames.
- Store pretty display names in YAML frontmatter.
- Use shared wiki pages by default.
- Add version notes only when behavior differs.
- Split version-specific pages only when divergence becomes large.

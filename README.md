# PostgreSQL LLM Wiki

An LLM-assisted, source-backed wiki for PostgreSQL internals. The articles form an
[Obsidian](https://obsidian.md/)-browsable vault, but they are also ordinary Markdown and can be
read directly on GitHub.

The wiki covers PostgreSQL architecture, SQL execution paths, SQL features, internal subsystems,
and troubleshooting. PostgreSQL source code is the authority for implementation details.

## Reading the wiki

Start at [`wiki/index.md`](wiki/index.md), or open the `wiki/` directory as an Obsidian vault.

Timestamped ZIP archives are available from the repository's GitHub Releases page. Every push to
`master` builds an archive from the current vault and publishes it as a new release.

## Layout

- `wiki/`: the Markdown and Obsidian vault with durable explanations.
- `sources/`: raw evidence, including local PostgreSQL mirrors, worktrees, articles, papers, and
  talks.
- `source-index/`: generated navigation indexes over PostgreSQL source trees.
- `config/`: source-version and indexing configuration.
- `reports/`: generated coverage and maintenance reports.
- `tools/`: Python scripts for indexing, linting, and reporting.
- `.github/workflows/`: automated wiki release publication.

PostgreSQL mirrors and worktrees under `sources/postgresql/` are local, read-only evidence. They
are intentionally excluded from Git.

## Local setup

The maintenance tools require Python 3.12 or newer, [uv](https://docs.astral.sh/uv/),
[just](https://just.systems/), Git, and `zip`.

```bash
just sync
```

Common commands:

| Command | Purpose |
|---|---|
| `just lint` | Check Python code and wiki metadata and links. |
| `just fmt` | Format the Python tools. |
| `just check` | Run the basic checks and build a wiki archive. |
| `just export` | Create `exports/pg-llm-wiki-YYYYMMDD-HHMMSS.zip`. |
| `just fetch` | Fetch refs and tags in the local PostgreSQL mirror. |
| `just index VERSION` | Generate the source index for a configured PostgreSQL version. |

To create the local PostgreSQL mirror and add a versioned worktree:

```bash
just clone-postgres
just add-version PG_18_4 REL_18_4
just index PG_18_4
```

See [`config/versions.toml`](config/versions.toml) for the configured versions and subsystem
classification rules.

## Authoring principles

- Do not edit PostgreSQL source checkouts.
- Use lowercase kebab-case filenames.
- Store pretty display names in YAML frontmatter.
- Write generic articles and add version notes only when behavior differs.
- Treat source indexes as navigation aids, not implementation authority.
- Keep SQL usage in `wiki/sql-features/` and implementation details in `wiki/subsystems/`.

The complete article structure, frontmatter, linking, tagging, and style rules are in
[`AGENTS.md`](AGENTS.md).

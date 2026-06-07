# Install Python dependencies
sync:
    uv sync --dev

# Fetch PostgreSQL refs and tags
fetch:
    git -C sources/postgresql/upstream.git fetch --all --tags

# Clone PostgreSQL as a bare mirror
clone-postgres:
    git clone --mirror https://git.postgresql.org/git/postgresql.git sources/postgresql/upstream.git

# Add a PostgreSQL worktree for a version
add-version VERSION REF:
    git -C sources/postgresql/upstream.git worktree add ../worktrees/{{VERSION}} {{REF}}

# Index one PostgreSQL version
index VERSION:
    uv run python tools/index_postgres.py {{VERSION}}

# Lint Python and wiki metadata/links
lint:
    uv run ruff check tools
    uv run python tools/lint_wiki.py

# Format Python tools
fmt:
    uv run ruff format tools

# Export compact LLM context
export:
    uv run python tools/export_obsidian.py

# Run basic checks
check:
    just lint
    just export

# Build the agent container
docker-build:
    docker build -t pg-llm-wiki-agent .

_ensure-local-dirs:
    mkdir -p .local/agent-home sources

# Open a shell in the agent container.
docker-shell: _ensure-local-dirs
    docker run --rm -it \
        --user "$(id -u):$(id -g)" \
        --env-file .env \
        -e HOME=/agent-home \
        -e GIT_OPTIONAL_LOCKS=0 \
        -e TERM="${TERM}" \
        -e COLORTERM="${COLORTERM}" \
        -v "$PWD:/workspace" \
        -v "$PWD/sources:/workspace/sources:ro" \
        -v "$PWD/.git:/workspace/.git:ro" \
        -v "$PWD/.local/agent-home:/agent-home" \
        -w /workspace \
        pg-llm-wiki-agent

# Run Claude Code in the agent container.
claude: _ensure-local-dirs
    docker run --rm -it \
        --user "$(id -u):$(id -g)" \
        --env-file .env \
        -e HOME=/agent-home \
        -e GIT_OPTIONAL_LOCKS=0 \
        -e TERM="${TERM}" \
        -e COLORTERM="${COLORTERM}" \
        -v "$PWD:/workspace" \
        -v "$PWD/sources:/workspace/sources:ro" \
        -v "$PWD/.git:/workspace/.git:ro" \
        -v "$PWD/.local/agent-home:/agent-home" \
        -w /workspace \
        pg-llm-wiki-agent claude --dangerously-skip-permissions

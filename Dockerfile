FROM ubuntu:24.04

# This image is an agent/dev workstation for the PostgreSQL LLM wiki.
#
# Important design choice:
# - Tools are installed as root at image build time.
# - The container is run as the host user at runtime via:
#     docker run --user "$(id -u):$(id -g)"
#
# This avoids root-owned files in the bind-mounted repo while keeping the
# Dockerfile simple.

ENV DEBIAN_FRONTEND=noninteractive

# Python behavior for scripts/tools.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Claude Code behavior inside containers.
#
# DISABLE_AUTOUPDATER:
#   The image should control the installed Claude Code version.
#
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:
#   Reduces non-essential telemetry/network traffic.
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Install base development tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fd-find \
        git \
        git-lfs \
        gh \
        gnupg \
        jq \
        less \
        nano \
        ncurses-bin \
        ncurses-term \
        nodejs \
        npm \
        openssh-client \
        procps \
        ripgrep \
        universal-ctags \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu installs fd as "fdfind"; create the expected "fd" alias.
RUN ln -s /usr/bin/fdfind /usr/local/bin/fd || true

# Install just, used as the project command runner.
RUN curl --proto '=https' --tlsv1.2 -fsSL https://just.systems/install.sh \
    | bash -s -- --to /usr/local/bin

# Install uv and uvx.
#
# uv manages Python environments/dependencies.
# uvx runs Python tools without manually managing virtualenvs.
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# Install Claude Code.
#
# For more reproducible builds later, pin the version:
#   npm install -g @anthropic-ai/claude-code@X.Y.Z
RUN npm install -g @anthropic-ai/claude-code

# The repo is mounted here at runtime:
#   -v "$PWD:/workspace"
WORKDIR /workspace

CMD ["/bin/bash"]

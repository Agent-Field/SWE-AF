# ---------------------------------------------------------------------------
# Stage 1 — aforge: fetch the released AForge CLI from the public download host
# and verify it against the release checksums before it ever enters the image.
#
# The release publishes GZIPPED binaries plus a checksums.txt whose hashes are
# of the DECOMPRESSED binaries, so this stage gunzips first and then rewrites
# the matching checksum line to the local file name before `sha256sum -c`.
#
# Both ARGs are overridable so a mirror / air-gapped registry can be used:
#   docker build --build-arg AFORGE_BASE_URL=... --build-arg AFORGE_VERSION=...
# Changing AFORGE_VERSION also busts this layer's cache (per the docker cache
# rule) — a floating URL alone would keep restoring a stale binary.
# ---------------------------------------------------------------------------
ARG AFORGE_BASE_URL=https://agentfield.ai/downloads/aforge
ARG AFORGE_VERSION=v0.1.0

FROM debian:bookworm-slim AS aforge
ARG AFORGE_BASE_URL
ARG AFORGE_VERSION
# TARGETARCH is populated by BuildKit; the fallback keeps the classic builder
# working on the only architecture this image is published for.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /out
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    curl -fsSL "${AFORGE_BASE_URL}/${AFORGE_VERSION}/aforge-linux-${arch}.gz" -o aforge.gz; \
    gunzip -c aforge.gz > aforge; \
    curl -fsSL "${AFORGE_BASE_URL}/${AFORGE_VERSION}/checksums.txt" -o checksums.txt; \
    grep " aforge-linux-${arch}$" checksums.txt | sed 's/  aforge-linux-.*/  aforge/' > aforge.sha256; \
    test -s aforge.sha256; \
    sha256sum -c aforge.sha256; \
    chmod +x aforge; \
    ./aforge --help > /dev/null; \
    rm -f aforge.gz checksums.txt aforge.sha256


# ---------------------------------------------------------------------------
# Stage 2 — runtime: the SWE-AF Python node.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: git (worktrees, branches), curl (healthcheck), jq (agent bash),
# openssh-client (optional SSH git), gh CLI (draft PRs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates openssh-client jq nodejs npm && \
    # Install GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    # Install OpenCode CLI v1.2+ for opencode provider (with run --model support)
    curl -fsSL https://opencode.ai/install | bash && \
    # Install Codex CLI for codex runtime provider
    npm install -g @openai/codex && \
    codex_path="$(command -v codex)" && \
    mv "${codex_path}" /usr/local/bin/codex-real && \
    printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    '' \
    'auth_mode="${SWE_CODEX_AUTH_MODE:-auto}"' \
    '' \
    'case "${auth_mode}" in' \
    '  chatgpt)' \
    '    unset OPENAI_API_KEY' \
    '    ;;' \
    '  api_key)' \
    '    if [ -z "${OPENAI_API_KEY:-}" ]; then' \
    '      echo "SWE_CODEX_AUTH_MODE=api_key requires OPENAI_API_KEY to be set" >&2' \
    '      exit 2' \
    '    fi' \
    '    ;;' \
    '  auto)' \
    '    ;;' \
    '  *)' \
    '    echo "Invalid SWE_CODEX_AUTH_MODE: ${auth_mode}. Expected one of: auto, chatgpt, api_key" >&2' \
    '    exit 2' \
    '    ;;' \
    'esac' \
    '' \
    'exec /usr/local/bin/codex-real "$@"' \
    > /usr/local/bin/codex && \
    chmod +x /usr/local/bin/codex && \
    rm -rf /var/lib/apt/lists/*

# Add OpenCode to PATH for non-interactive shells
ENV PATH="/root/.opencode/bin:${PATH}"
# AForge CLI (aforge runtime), fetched + checksum-verified in stage 1.
COPY --from=aforge /out/aforge /usr/local/bin/aforge

# Tell OpenCode to read its model AND small_model from the deployer's
# HARNESS_MODEL env var via {env:...} interpolation. Without this config,
# OpenCode auto-selects a small_model from whatever providers it finds
# keys for — landing on DeepSeek V3.1 in our environment, bypassing every
# env var the deployer set. Per-call -m on `opencode run` pins the main
# model regardless; small_model is what falls through to config, so it
# has to honor the same env var the rest of the stack uses.
#
# Default HARNESS_MODEL inside the image so a fresh container with no
# env override has *some* value to interpolate. Railway / docker-compose
# overrides win because their env injects after the image's ENV.
ENV HARNESS_MODEL=openrouter/moonshotai/kimi-k2.6
RUN mkdir -p /root/.config/opencode
COPY opencode.json /root/.config/opencode/opencode.json

# Git identity — env vars take highest precedence and are inherited by all
# subprocesses including Claude Code agent instances spawned by the SDK
ENV GIT_AUTHOR_NAME="SWE-AF" \
    GIT_AUTHOR_EMAIL="eng@agentfield.ai" \
    GIT_COMMITTER_NAME="SWE-AF" \
    GIT_COMMITTER_EMAIL="eng@agentfield.ai"

# Configure git identity and use gh CLI as credential helper so all git
# HTTPS operations (clone, push, fetch) authenticate via GH_TOKEN at runtime.
RUN git config --global user.name "SWE-AF" && \
    git config --global user.email "eng@agentfield.ai" && \
    gh auth setup-git --hostname github.com --force

# Install uv for fast package installation
RUN pip install --no-cache-dir uv

# Install project dependencies
COPY requirements-docker.txt /app/requirements.txt
RUN uv pip install --system -r /app/requirements.txt

# Copy application code
COPY . /app/

# Pre-create /workspaces so named-volume mounts inherit correct permissions
# (without this, Docker creates it as root read-only on fresh deployments)
RUN mkdir -p /workspaces && chmod 777 /workspaces

EXPOSE 8003

ENV PORT=8003 \
    AGENTFIELD_SERVER=http://control-plane:8080 \
    NODE_ID=swe-planner \
    AGENTFIELD_AFORGE_COMMAND=exec

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "-m", "swe_af"]

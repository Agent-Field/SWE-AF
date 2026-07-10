# SWE-AF Go node — multi-stage build.
#
# Build from the SWE-AF repo root so the whole go/ module is in context:
#     docker build -f go/Dockerfile .
#
# The Go module depends on the AgentField Go SDK via a `replace` directive
# (go/go.mod: replace github.com/Agent-Field/agentfield/sdk/go => ../../agentfield/sdk/go).
# The SDK lives in a *sibling* repo that is NOT in this build context, so the
# builder stage clones it at a pinned ref and lays it out so the replace path
# (../../agentfield/sdk/go, relative to /src/SWE-AF/go) resolves to /src/agentfield/sdk/go.
#
# Cache-busting (per the docker cache rule): the SDK checkout is keyed on the
# AGENTFIELD_SDK_REF build arg. Bump the ref (or pass --build-arg) to force a
# re-clone; an unchanged ref restores the cached layer.

# ---------------------------------------------------------------------------
# Stage 1 — builder: clone the SDK at a pinned ref, build both static binaries.
# ---------------------------------------------------------------------------
# golang 1.23 satisfies go.mod's `go 1.21` directive and matches the AgentField
# control-plane Go prerequisite (Go 1.23+).
FROM golang:1.23-bookworm AS builder

# Pinned AgentField SDK ref. Default = agentfield origin/main HEAD at port time
# (v0.1.107-rc.1). Changing this string invalidates the clone layer below.
ARG AGENTFIELD_SDK_REF=55ad55f3159234bcc354a4bc37abeaf616b252ca
ARG AGENTFIELD_REPO=https://github.com/Agent-Field/agentfield.git

WORKDIR /src

# Clone ONLY sdk/go from the agentfield repo at the pinned SHA (single-commit
# depth-1 fetch + cone sparse-checkout keeps the layer small). Placed as a
# sibling of SWE-AF/ so the go.mod replace path resolves. This layer's cache
# key includes AGENTFIELD_SDK_REF, so it re-runs iff the ref changes.
RUN git init -q /src/agentfield && \
    cd /src/agentfield && \
    git remote add origin "${AGENTFIELD_REPO}" && \
    git sparse-checkout init --cone && \
    git sparse-checkout set sdk/go && \
    git fetch --depth 1 origin "${AGENTFIELD_SDK_REF}" && \
    git checkout -q FETCH_HEAD && \
    test -f /src/agentfield/sdk/go/go.mod

# Prime the module cache from go.mod/go.sum before copying sources so dependency
# downloads cache independently of source edits. GOWORK=off: no workspace in the
# image, resolution goes through the replace directive.
ENV GOWORK=off CGO_ENABLED=0 GOOS=linux
COPY go/go.mod go/go.sum /src/SWE-AF/go/
WORKDIR /src/SWE-AF/go
RUN go mod download

# Copy the rest of the module and build both static binaries.
COPY go/ /src/SWE-AF/go/
RUN go build -trimpath -ldflags="-s -w" -o /out/swe-planner ./cmd/swe-planner && \
    go build -trimpath -ldflags="-s -w" -o /out/swe-fast ./cmd/swe-fast

# ---------------------------------------------------------------------------
# Stage 2 — runtime: slim Debian + the same external CLI surface the agents
# shell out to, mirroring the Python image (git, curl, jq, openssh, gh, opencode,
# codex) plus the Claude Code CLI (the Go harness spawns `claude` for the
# claude_code runtime; Python got it via the claude-agent-sdk pip package).
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# System deps: git (worktrees, branches), curl (healthcheck + installers),
# ca-certificates (HTTPS for gh/opencode/npm), jq (agent bash), openssh-client
# (optional SSH git), nodejs+npm (codex + claude-code CLIs), gh CLI (draft PRs),
# OpenCode CLI (open_code runtime), Codex CLI (codex runtime), Claude Code CLI
# (claude_code runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates openssh-client jq nodejs npm && \
    # GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    # OpenCode CLI v1.2+ for the open_code runtime (with run --model support)
    curl -fsSL https://opencode.ai/install | bash && \
    # Claude Code CLI for the claude_code runtime (Go harness spawns `claude`)
    npm install -g @anthropic-ai/claude-code && \
    # Codex CLI for the codex runtime, wrapped to honor SWE_CODEX_AUTH_MODE
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

# Tell OpenCode to read its model AND small_model from the deployer's
# HARNESS_MODEL env var via {env:...} interpolation. Without this config,
# OpenCode auto-selects a small_model from whatever providers it finds keys
# for, bypassing every env var the deployer set. Per-call -m on `opencode run`
# pins the main model regardless; small_model is what falls through to config,
# so it must honor the same env var the rest of the stack uses. Default the
# value inside the image so a fresh container has *some* value to interpolate;
# Railway / docker-compose overrides win because their env injects afterward.
ENV HARNESS_MODEL=openrouter/moonshotai/kimi-k2.6
RUN mkdir -p /root/.config/opencode && \
    echo '{"$schema":"https://opencode.ai/config.json","model":"{env:HARNESS_MODEL}","small_model":"{env:HARNESS_MODEL}","provider":{"openrouter":{"options":{"apiKey":"{env:OPENROUTER_API_KEY}"}}}}' \
    > /root/.config/opencode/opencode.json

# Git identity — env vars take highest precedence and are inherited by all
# subprocesses (including the CLIs the harness spawns).
ENV GIT_AUTHOR_NAME="SWE-AF" \
    GIT_AUTHOR_EMAIL="eng@agentfield.ai" \
    GIT_COMMITTER_NAME="SWE-AF" \
    GIT_COMMITTER_EMAIL="eng@agentfield.ai"

# Configure git identity and use gh CLI as credential helper so all git HTTPS
# operations (clone, push, fetch) authenticate via GH_TOKEN at runtime.
RUN git config --global user.name "SWE-AF" && \
    git config --global user.email "eng@agentfield.ai" && \
    gh auth setup-git --hostname github.com --force

# Application binaries (static, from the builder stage).
COPY --from=builder /out/swe-planner /usr/local/bin/swe-planner
COPY --from=builder /out/swe-fast /usr/local/bin/swe-fast

# Pre-create /workspaces so named-volume mounts inherit correct permissions
# (without this, Docker creates it as root read-only on fresh deployments).
RUN mkdir -p /workspaces && chmod 777 /workspaces

EXPOSE 8005

# Defaults register the Go planner as "swe-planner-go" on :8005 — a distinct
# identity from the Python swe-planner node (:8003) so both stacks can run
# against one control plane. The swe-fast service overrides NODE_ID/PORT to
# swe-fast-go/:8006 via compose.
ENV PORT=8005 \
    AGENTFIELD_SERVER=http://control-plane:8080 \
    NODE_ID=swe-planner-go

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Default entrypoint = full-pipeline node. The swe-fast service overrides this
# with the swe-fast binary (see docker-compose.go.yml).
CMD ["/usr/local/bin/swe-planner"]

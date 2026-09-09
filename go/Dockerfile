# SWE-AF Go node — multi-stage build.
#
# Build from the SWE-AF repo root so the whole go/ module is in context:
#     docker build -f go/Dockerfile .
#
# The Go module requires the AgentField Go SDK by version (go/go.mod:
# github.com/Agent-Field/agentfield/sdk/go v0.1.130) and the builder resolves it
# through the module proxy — there is no `replace` directive, so nothing here
# depends on a sibling checkout. The builder still sparse-clones the SDK at
# AGENTFIELD_SDK_REF: it records the exact commit behind that version in the
# image and fails the build early if the ref ever disappears.
#
# Cache-busting (per the docker cache rule): the SDK checkout is keyed on the
# AGENTFIELD_SDK_REF build arg. Bump the ref (or pass --build-arg) to force a
# re-clone; an unchanged ref restores the cached layer. Keep it on the same
# release as go/go.mod's require.

# ---------------------------------------------------------------------------
# Stage 0 — aforge: fetch the released AForge CLI from the public download host
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
# Stage 1 — builder: clone the SDK at a pinned ref, build both static binaries.
# ---------------------------------------------------------------------------
# golang 1.23 satisfies go.mod's `go 1.21` directive and matches the AgentField
# control-plane Go prerequisite (Go 1.23+).
FROM golang:1.23-bookworm AS builder

# Pinned AgentField SDK ref. Default = the sdk/go/v0.1.130 tag commit, the same
# release go/go.mod requires. Changing this string invalidates the clone layer
# below.
ARG AGENTFIELD_SDK_REF=aba20a9b248d9ee6c74f4e7e688ef0740c542dc9
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
# image, resolution goes through go.mod's versioned require.
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
# (claude_code runtime). The AForge CLI (aforge runtime) is copied in from
# stage 0 rather than installed here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates openssh-client jq nodejs npm \
    # Python test tooling: repos under build are frequently Python, and both
    # the verifier role and evidence-based audits need a runnable pytest —
    # without it, Python-repo verification is impossible in this image.
    python3 python3-pytest && \
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
#
# The value MUST match openRouterAutoDefaultModel (internal/config) — it is
# what an OpenRouter-only deploy actually runs, since the model-resolution
# cascade reads HARNESS_MODEL for the open_code runtime (and ONLY for
# open_code: claude_code / codex deployments resolve their own runtime
# defaults and never see this variable).
ENV HARNESS_MODEL=openrouter/deepseek/deepseek-v4-flash-0731
RUN mkdir -p /root/.config/opencode
COPY opencode.json /root/.config/opencode/opencode.json

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

# AForge CLI (aforge runtime), fetched + checksum-verified in stage 0.
COPY --from=aforge /out/aforge /usr/local/bin/aforge

# Application binaries (static, from the builder stage).
COPY --from=builder /out/swe-planner /usr/local/bin/swe-planner
COPY --from=builder /out/swe-fast /usr/local/bin/swe-fast

# Prebuilt pro-engine binary (static, vendored at go/bin). go/bin carries one
# build per supported platform; this image is linux/amd64 only, so the amd64
# one is copied to the unsuffixed path the supervisor's default SWE_PRO_BIN
# already points at. Inert unless the node is started with SWE_PRO_ENGINE=1
# (see go/docs/pro-engine.md).
COPY go/bin/swe-pro-linux-amd64 /usr/local/bin/swe-pro

# Pre-create /workspaces so named-volume mounts inherit correct permissions
# (without this, Docker creates it as root read-only on fresh deployments).
RUN mkdir -p /workspaces && chmod 777 /workspaces

EXPOSE 8005

# Defaults register this as "swe-planner" on :8005 — the product's node id, so
# callers' triggers are the same whichever implementation is serving them. The
# compose files override NODE_ID where two stacks share one control plane and
# need distinct identities, and the swe-fast service overrides NODE_ID/PORT.
ENV PORT=8005 \
    AGENTFIELD_SERVER=http://control-plane:8080 \
    NODE_ID=swe-planner \
    AGENTFIELD_AFORGE_COMMAND=exec

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Default entrypoint = full-pipeline node. The swe-fast service overrides this
# with the swe-fast binary (see docker-compose.go.yml).
CMD ["/usr/local/bin/swe-planner"]

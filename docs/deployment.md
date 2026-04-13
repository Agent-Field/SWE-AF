# Deployment Guide

This guide covers deploying SWE-AF on a new server, including prerequisites, known issues, and quick-start instructions.

## Prerequisites

### Software

| Requirement | Minimum Version | Notes |
|---|---|---|
| Docker | 20.10+ | With BuildKit support |
| Docker Compose | 2.0+ | V2 plugin (`docker compose`, not `docker-compose`) |
| Git | 2.30+ | For cloning the repository |

### Environment Variables

Copy `.env.example` to `.env` and configure at least one authentication method:

```bash
cp .env.example .env
```

**Required (one of):**

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude models |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code subscription token (uses Pro/Max credits) |

**For open-source models (alternative to Claude):**

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key (200+ models) |
| `OPENAI_API_KEY` | OpenAI API key |
| `GOOGLE_API_KEY` | Google Gemini API key |

**Optional:**

| Variable | Purpose | Default |
|---|---|---|
| `GH_TOKEN` | GitHub PAT with `repo` scope for draft PRs | *(none)* |
| `AGENTFIELD_SERVER` | Control plane URL | `http://control-plane:8080` (Docker) |
| `NODE_ID` | Agent node identifier | `swe-planner` |
| `PORT` | Agent listen port | `8003` |

### Package Versions

| Package | Minimum Version | Notes |
|---|---|---|
| `agentfield` | 0.1.67+ | Python SDK (includes opencode v1.4+ fix) |
| `claude-agent-sdk` | 0.1.20+ | Claude runtime |
| opencode CLI | 1.4+ | Only if using `open_code` runtime (see Known Issues) |

## Quick Start

### Full Stack (control plane + agent)

```bash
git clone https://github.com/Agent-Field/SWE-AF
cd SWE-AF
cp .env.example .env   # fill in API keys
docker compose up -d
```

This starts:
- **control-plane** on `:8080` — AgentField orchestration server
- **swe-agent** on `:8003` — SWE-AF full pipeline (`swe-planner` node)
- **swe-fast** on `:8004` — SWE-AF fast mode (`swe-fast` node)

### Agent Only (connect to existing control plane)

If you already have an AgentField control plane running:

```bash
git clone https://github.com/Agent-Field/SWE-AF
cd SWE-AF
cp .env.example .env   # fill in API keys

# Set AGENTFIELD_SERVER in .env to your control plane URL
docker compose -f docker-compose.local.yml up -d
```

### Verify Deployment

```bash
# Check agent health
curl http://localhost:8003/health

# Check control plane (full stack only)
curl http://localhost:8080/api/v1/health
```

## Known Issues and Fixes

### `/workspaces` read-only filesystem error

**Symptom:**
```
[Errno 30] Read-only file system: '/workspaces'
```

**Root cause:** The `/workspaces` directory was not pre-created in the Docker image. When Docker mounts a named volume, it creates the directory as root with restrictive permissions.

**Fix:** This is fixed in the current Dockerfile. If you're using an older image, rebuild:
```bash
docker compose build --no-cache
```

The fix adds `RUN mkdir -p /workspaces && chmod 777 /workspaces` to the Dockerfile before the volume mount point.

**Ref:** [#46](https://github.com/Agent-Field/SWE-AF/issues/46)

### `Product manager failed to produce a valid PRD` with `open_code` runtime

**Symptom:** Builds using the `open_code` runtime fail at the Product Manager step with a generic error. The agent completes in a few seconds (too fast for real work).

**Root cause:** opencode CLI v1.4+ changed its CLI interface:
- `-p` (prompt) flag was removed — prompt is now a positional arg to the `run` subcommand
- `-c` now means `--continue` (resume session), not project directory

**Fix:** Upgrade the `agentfield` Python SDK to a version that includes the opencode v1.4+ compatibility fix:
```bash
pip install --upgrade agentfield
```

**Ref:** [#45](https://github.com/Agent-Field/SWE-AF/issues/45)

### Fatal API errors silently retry

**Symptom:** Build with exhausted credits or invalid API key retries multiple times before failing with a misleading error (e.g., "Product manager failed to produce a valid PRD").

**Root cause:** Non-retryable API errors (credit exhaustion, invalid key) were not distinguished from transient errors, causing all retry layers to fire.

**Fix:** This is fixed in the current version. Upgrade to get `FatalHarnessError` detection that immediately aborts on:
- Credit balance too low
- Invalid API key
- Authentication failed
- Account disabled
- Quota exceeded

**Ref:** [#49](https://github.com/Agent-Field/SWE-AF/issues/49)

### Parallel builds cross-contamination

**Symptom:** Running two builds simultaneously for the same repository causes agents to receive input from the wrong build.

**Root cause:** Both builds cloned to the same workspace path (`/workspaces/<repo-name>`), sharing git state and artifacts.

**Fix:** This is fixed in the current version. Each build now gets an isolated workspace: `/workspaces/<repo-name>-<build_id>`.

**Ref:** [#43](https://github.com/Agent-Field/SWE-AF/issues/43)

## Scaling

### Multiple concurrent builds

Each build automatically gets an isolated workspace. To run multiple builds concurrently:

```bash
# Scale the agent service
docker compose up --scale swe-agent=3 -d
```

### Resource considerations

Each build clones the target repository and runs multiple LLM calls. Plan for:
- **Disk:** ~500MB per concurrent build (repo clone + artifacts)
- **Memory:** ~512MB per agent container
- **Network:** LLM API calls are the bottleneck, not compute

## Troubleshooting

| Symptom | Check |
|---|---|
| Agent not registering with control plane | Verify `AGENTFIELD_SERVER` is reachable from the container |
| Builds timing out | Check API key validity and credit balance |
| `git clone` failures | Verify `GH_TOKEN` has `repo` scope for private repositories |
| Health check failing | Check container logs: `docker compose logs swe-agent` |

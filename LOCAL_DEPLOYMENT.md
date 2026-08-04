# SWE-AF local deployment

- Host: `10.20.10.103`, Ubuntu 24.04, x86_64
- Install: `/opt/swe-af`; data: `/opt/swe-af-data`; backups: `/opt/backups/swe-af`
- Upstream commit: `7a8dba030da89dbe786e9b80eb1f6154a304bbda`
- Domain: `swe.go7s.net`; NPM terminates HTTPS and forwards to `10.20.10.103:5080`.

## Services

| Service | Local bind | Container | Exposure |
|---|---:|---:|---|
| control-plane | `10.20.10.103:5080` | 8080 | LAN/NPM only |
| swe-agent | `127.0.0.1:5803` | 8003 | loopback; `swe-af-swe-agent-1` |
| swe-fast | `127.0.0.1:5804` | 8004 | loopback; `swe-af-swe-fast-1` |
| build-db | none | 5432 | Compose network only |

## 9Router + OpenCode Setup (Active Stack)

The deployment uses **9Router** (`http://10.20.10.133:20128/v1`) as the LLM gateway with the **OpenCode** runtime (`open_code`). The Docker image (`Dockerfile`) bundles an `opencode.json` with only the `9router` provider configured, and a fail-fast entrypoint (`docker/entrypoint.sh`) validates `HARNESS_MODEL` at container startup.

**Key env vars (in `.env`):**
```bash
OMNI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx   # 9Router API key
ROUTER_API_KEY=${OMNI_API_KEY}                     # Injected by compose.override.yml
SWE_DEFAULT_RUNTIME=open_code
SWE_DEFAULT_MODEL=9router/claude-sonnet-4-6
```

**Verify 9Router model availability:**
```bash
curl -H "Authorization: Bearer $OMNI_API_KEY" http://10.20.10.133:20128/v1/models \
  | jq -r '.data[].id' | grep -E 'claude-sonnet-4-6|codex-terra|claude-opus-4-8|claude-haiku-4-5'
```

## Commands

```bash
cd /opt/swe-af
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-build --pull never
docker compose stop
docker compose restart
./scripts/status.sh
./scripts/logs.sh control-plane
./scripts/run-build.sh --repo https://github.com/OWNER/REPO --goal-file /path/goal.md --profile configs/opencode-balanced.json
./scripts/run-fast.sh --repo-path /opt/swe-af-data/test-repos/swe-af-smoke-test --goal 'Add a /health endpoint' --profile configs/opencode-balanced.json
./scripts/backup.sh
./scripts/update.sh
./scripts/restore.sh --confirm /opt/backups/swe-af/YYYYMMDD-HHMMSS
```

The control-plane, planner and fast nodes run from Compose with `restart=unless-stopped`. Planner and fast mount the host OpenCode 1.18.11 binary read-only and use the dedicated 9Router provider configuration. Query execution status with the returned execution/build ID through the control plane API. The upstream routes include planner, fast, plan, execute and resume_build.

## Runtime and limits

Profiles live in `configs/` and contain no credentials. OpenCode uses only the dedicated `/opt/swe-af-data/config/opencode.json`; the host home config is not mounted. 9Router at `http://10.20.10.133:20128/v1` is selected as `9router/<model>` and is the default provider. Omni is local to the host and is not the active provider. Gemini is represented through `runtime=open_code`, never a native `gemini` runtime. Upstream selects one runtime per build; runtime-per-role is not enabled.

Codex was verified with ChatGPT authentication in a dedicated node and a real issue execution. The passing evidence is execution `exec_20260803_050953_xvi8vhg1`, commit `20faad9f7a39957ef15069d267dd13bdbbe1c67d`, and 3 passing pytest tests. For issue-level Codex executions that must create commits, use `permission_mode: danger-full-access` only with trusted repositories in an isolated runtime; the default `workspace-write` sandbox cannot mutate the parent Git worktree metadata inside this Docker layout.

Claude via 9Router: direct CLI and Anthropic tool-use probes pass; direct coding produced commit `7340d9f` with 3 passing tests. The upstream SWE-AF `implement_issue` harness still fails before writing `.agentfield_output.json` (`ClaudeCodeProvider` exit code 1), so Claude is not marked as a passing SWE-AF pipeline runtime yet. The deployment includes `scripts/claude-cli-wrapper.sh` for the current bundled CLI argument compatibility issue.

Claude container uses the existing 9Router credential through the read-only settings mount; Claude subscription OAuth is not used. Control-plane access is restricted to the LAN bind and NPM; configure API authentication before broader exposure. NPM should enforce an Access List while AgentField API authentication is not enabled. Monitor Docker/BuildKit free-space policy before rebuilding images or running concurrent builds.

## Docker Hardening (2026-08-04)

The `swe-fast` image now includes a fail-fast entrypoint that validates `HARNESS_MODEL`'s provider prefix against `opencode.json` at container startup:

```bash
# Inside docker/entrypoint.sh
if [[ -n "${HARNESS_MODEL:-}" && "${HARNESS_MODEL}" == */* ]]; then
    provider="${HARNESS_MODEL%%/*}"
    config="/root/.config/opencode/opencode.json"
    if [[ -f "${config}" ]] && ! grep -q "\"${provider}\"" "${config}"; then
        echo "ERROR: HARNESS_MODEL='${HARNESS_MODEL}' references provider '${provider}'"
        echo "       which is not defined in ${config}."
        echo "       Set HARNESS_MODEL to a provider/model defined there, e.g."
        echo "       9router/claude-sonnet-4-6  (9Router backend)"
        echo "       openrouter/<model>          (OpenRouter)"
        exit 2
    fi
fi
```

This prevents the 300s silent hang when `HARNESS_MODEL` points at an unconfigured provider (the original incident: `HARNESS_MODEL=omni/codex-terra` while only `9router` was defined in opencode.json). The image default `HARNESS_MODEL` was changed from `openrouter/moonshotai/kimi-k2.6` to `9router/claude-sonnet-4-6` to match the bundled provider.

**Verification:** After the fix, `run_git_init` completes in ~100–260s with `success=true`, and the full pipeline runs to completion (exec_20260804_021328_x3i9n1y1: 3 task commits, 6/6 tests passed).

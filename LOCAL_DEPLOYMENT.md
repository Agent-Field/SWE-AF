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

## Operational status (2026-08-05)

| Capability | Runtime | Stack | State |
|---|---|---|---|
| **Single-repo fast build** (local path) | `open_code` | 9Router + OpenCode | **Production-ready**. `run-fast.sh` with `--repo-path` runs plan → coder → verifier → finalize → done. `verify-install.sh` 5/5 passes. |
| Single-repo fast build (remote URL) | `open_code` | 9Router + OpenCode | Known issue: `swe-fast` calls `os.makedirs(repo_path)` but never clones when given `repo_url` (issue #52). Use `repo_path` (clone locally first) or the planner `swe-planner.build` instead. |
| Single-repo full planner build (local path) | `open_code` | 9Router + OpenCode | Functional via `swe-planner.build`. Not actively exercised on this deployment. |
| Single-repo full planner build (remote URL + PR) | `open_code` | 9Router + OpenCode | Functional via `swe-planner.build` + `enable_github_pr=true`. Requires `GH_TOKEN` with repo scope. |
| Multi-repo DAG build | `open_code` | 9Router + OpenCode | Functional in `swe-planner.build` with `repos[]`. Not actively exercised on this deployment. |
| **Codex** (`codex` runtime, ChatGPT auth) | `codex` | Codex CLI | Configured (`SWE_CODEX_AUTH_MODE=chatgpt`). Smoke evidence: `exec_20260803_050953_xvi8vhg1` (commit `20faad9f`). Requires `permission_mode=danger-full-access` for git operations inside Docker (workspaces need Git worktree metadata). |
| Claude Code (CLI) | `claude_code` | Anthropic / 9Router | CLI passes for tool use, but `ClaudeCodeProvider` in SWE-AF `implement_issue` exits before `.agentfield_output.json` is written. Not a passing SWE-AF runtime yet. |
| QA synthesizer | `open_code` | 9Router + OpenCode | **Fixed (issue #113, 2026-08-05)**. `router.ai()` now returns schema directly; `getattr(result, "parsed", result)` handles both old and new AgentField SDK versions. |

### What's actually usable today

1. **Local-repo fast builds via `run-fast.sh`** — fully working. End-to-end pipeline (plan → coder → verifier → finalize) takes 15–35 minutes for a 3–6 task goal on 9Router `claude-sonnet-4-6`. Build timeout default is now 1800 s (raised from 600 s, commit `2755a1e`).
2. **`verify-install.sh`** — automated health check across 9Router reachability, container status, env consistency, provider config, and control-plane/agent health endpoints. Returns exit 0 only if all five checks pass.
3. **`run-build.sh`** — full-feature build via `swe-planner.build`. Supports remote repos with PR workflow when `GH_TOKEN` is set.
4. **Issue-level executions** via `swe-af-issue-1` / `implement_issue` — usable but each runtime has caveats (see table).

### What's known broken

- **Issue #52** (`swe-fast.build` does not clone `repo_url`) — workaround: pre-clone locally, pass `repo_path`. Tracker: <https://github.com/Agent-Field/SWE-AF/issues/52>.
- **Claude Code `implement_issue`** — `ClaudeCodeProvider` exits before `.agentfield_output.json`; use `claude-cli-wrapper.sh` for direct CLI work, or pick `open_code`/`codex` for the SWE-AF pipeline.

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

## Changelog (2026-08-05)

- **Build timeout raised** — `FastBuildConfig.build_timeout_seconds` default 600 → 1800 s (commit `2755a1e`). 3–6 task fast builds on 9Router take 15–35 min; 600 s was too tight.
- **QA synthesizer fix (issue #113)** — `router.ai(..., schema=...)` now returns the parsed Pydantic model directly. `run_qa_synthesizer` uses `getattr(result, "parsed", result)` so both old and new AgentField SDK versions work (commit `5a32796`).
- **Docker hardening** — fail-fast `entrypoint.sh` validates `HARNESS_MODEL`'s provider prefix against `opencode.json` at container startup; default model is now `9router/claude-sonnet-4-6` (commit `a6bff87`).

## Notes on day-to-day operation

- Rebuilding or restarting `swe-fast` mid-build **cancels in-flight executions** (`cancelled_by_control_plane`). Schedule rebuilds between builds, or expect a cancel.
- Execution state is held in the control plane (Postgres `build-db`), not the node container. A full stack restart does not lose history, but an in-flight build's child executions die with the node.
- Expected stage times (9Router, `claude-sonnet-4-6`, `open_code`):
  - `run_git_init`: ~2 min
  - plan (3–6 tasks): ~5–10 min
  - coder tasks (per task): ~1–3 min
  - verify: ~2–12 min
  - finalize: ~1–2 min

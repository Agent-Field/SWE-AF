# SWE-AF operations

Use `./scripts/status.sh`, `./scripts/logs.sh swe-agent --tail 200`, and `docker compose ps --all` for routine checks. Inspect log rotation with `docker inspect swe-af-control-plane-1 --format '{{json .HostConfig.LogConfig}}'`.

For recovery, restart `swe-agent`, verify `http://127.0.0.1:5803/health`, restart control-plane, verify `http://10.20.10.103:5080/api/v1/health`, then inspect node registration and execution records. Persistent data is under `/opt/swe-af-data/agentfield`; workspaces are under `/opt/swe-af-data/workspaces`.

**Key operational parameters (9Router + OpenCode):**
- **Fast build default timeout:** 1800 s (default in `schemas.py`, raised from 600 s to support multi-task pipelines).
- **Startup validation:** `docker/entrypoint.sh` fails fast if `HARNESS_MODEL` provider is missing from `opencode.json`.
- **Health verification:** `./scripts/verify-install.sh` (runs 5 automated checks: 9Router models, running containers, HARNESS_MODEL env, opencode provider config, and health endpoints).

Run backup before updates. Do not use `docker system prune -a`.

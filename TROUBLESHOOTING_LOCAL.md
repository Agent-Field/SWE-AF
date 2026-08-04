# Local troubleshooting

For port conflicts, inspect `ss -ltnp` and `docker compose config --quiet`; expected host ports are only 5080, 5803 and 5804, with agent APIs loopback-bound. For node errors, read `docker compose logs swe-agent` and verify internal server and callback URLs. For OpenCode, validate the dedicated provider config, selected `9router/<model>`, the read-only OpenCode binary mount, endpoint reachability from the container and auth without printing the key. For Codex, verify `codex login status` and keep `OPENAI_API_KEY` unset in ChatGPT mode. For disk pressure, use `df -h /` and `docker system df`; never delete unrelated volumes or run system prune.

## `run_git_init` hangs ~300s and returns `success=false`

**Symptom:** The SWE-AF fast pipeline stalls at the `run_git_init` stage. Logs show the stage was invoked but never completes; the build returns `success=false` after the runner timeout.

**Root cause:** `HARNESS_MODEL` references a provider that is NOT defined in `/root/.config/opencode/opencode.json` (e.g., `HARNESS_MODEL=omni/codex-terra` while only `9router` and `openrouter` providers exist). The OpenCode subprocess waits out the full 300s runner timeout per call before failing.

**Diagnostic commands:**
```bash
# Check the resolved HARNESS_MODEL inside the running container
docker exec swe-af-swe-fast-1 env | grep HARNESS_MODEL

# Check what providers opencode.json defines
docker exec swe-af-swe-fast-1 cat /root/.config/opencode/opencode.json | python3 -c "import json,sys; print('Providers:', list(json.load(sys.stdin).get('provider', {}).keys()))"

# View the fail-fast entrypoint output (after Aug 2026 hardening)
docker logs swe-af-swe-fast-1 --tail 20
```

**Fix:**
1. Set `HARNESS_MODEL` to a provider/model that exists in `opencode.json`. For the bundled 9Router setup: `HARNESS_MODEL=9router/claude-sonnet-4-6`.
2. Ensure `.env` contains `OMNI_API_KEY=sk-...` (the 9Router API key).
3. Restart the container: `docker compose -f docker-compose.yml -f docker-compose.override.yml restart swe-fast`.
4. Rebuild if the image was changed: `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build swe-fast`.

**Verification:** `run_git_init` completes in ~100–260s with `success=true`, and the full pipeline progresses to plan → coder → verifier → finalize.

**Aug 2026 hardening:** The Docker image now includes `docker/entrypoint.sh` that validates `HARNESS_MODEL` at container startup. If the provider prefix isn't defined in `opencode.json`, the container exits immediately with exit code 2 and a clear error message, instead of silently hanging. See `Dockerfile` lines 110-112 and `.debug-journal.md` for the original incident (2026-08-04).

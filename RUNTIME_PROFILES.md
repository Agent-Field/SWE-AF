# Runtime profiles

| Profile | Runtime | Model source | State |
|---|---|---|---|
| `claude-balanced.json` | `claude_code` | 9Router `claude-sonnet-4-6` | CLI/tool smoke PASS; SWE-AF issue harness blocked |
| `codex-balanced.json` | `codex` | `gpt-5.5` ChatGPT auth | CONFIGURED; smoke build passed |
| `opencode-balanced.json` | `open_code` | Verified 9Router IDs | **Default (configured & verified)** |
| `gemini-via-opencode.json` | `open_code` | Verified `Gemini-Flash` alias | 9Router-routed |
| `9router-via-opencode.json` | `open_code` | Verified 9Router IDs | Configured |
| `omni-via-opencode.json` | `open_code` | Legacy local fallback | Not default |

9Router `/v1/models` returned 53 models, chat returned HTTP 200, and tool-calling returned one function call from the fast container. The active OpenCode provider is `9router`; Omni local is retained only as a legacy fallback profile.

Codex smoke evidence: execution `exec_20260803_050953_xvi8vhg1` completed with branch `issue/a4822940-add-liveness-command`, commit `20faad9f7a39957ef15069d267dd13bdbbe1c67d`, verifier passed, and `pytest -q` reported 3 passed. The test used `permission_mode=danger-full-access` in the isolated Codex test node because Codex `workspace-write` cannot update Git worktree metadata inside Docker. Do not enable that mode for untrusted repositories without a separately isolated runtime.

Claude evidence: the 9Router Anthropic Messages and tool-use probes returned HTTP 200; direct Claude CLI coding produced commit `7340d9f` with 3 passing tests. The SWE-AF `implement_issue` path remains blocked because `ClaudeCodeProvider` exits before producing `.agentfield_output.json` under the current AgentField/Claude CLI combination.

**Docker hardening (2026-08-04):** The `swe-fast` image now bundles a fail-fast entrypoint (`docker/entrypoint.sh`) that validates `HARNESS_MODEL`'s provider prefix against `opencode.json` at container startup. This prevents the 300s silent hang when `HARNESS_MODEL` points at an unconfigured provider (the original 2026-08-04 incident: `omni/codex-terra` while only `9router` was defined). The image default `HARNESS_MODEL` was changed from `openrouter/moonshotai/kimi-k2.6` to `9router/claude-sonnet-4-6` to match the bundled provider.

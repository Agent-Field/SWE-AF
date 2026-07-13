# SWE-AF → Go Port: AgentField Go SDK Capability Map & Gap Analysis

**Scope:** `SWE-AF` (Python node, `swe-planner`) → `the sibling `agentfield/sdk/go` checkout` (latest main).

## 0. Go SDK package map

| Package | Purpose |
|---|---|
| `sdk/go/agent/` | Node runtime: `New(Config)`, `RegisterReasoner/RegisterSkill`, HTTP mux (`/reasoners/{name}`, `/execute/{name}`, `/health`, `/discover`, `/_internal/executions/{id}/cancel`), control-plane registration + lease loop, async execution + status callbacks, `Call`/`CallLocal`, `Note`, `Memory`, `Harness()`, execution logs, cancel |
| `sdk/go/harness/` | CLI coding-agent harness: providers `claude-code`, `codex`, `opencode`, `gemini`; `Runner.Run` with schema-file structured output + retry |
| `sdk/go/ai/` | Direct LLM API client (OpenRouter-style chat completions, tool-calling loop, multimodal) — alternative to harness, not what SWE-AF's pattern needs |
| `sdk/go/client/` | Control-plane client: execute, DID auth, approval (poll-based) |
| `sdk/go/inputs/` | Manual typed extraction from `map[string]any` (`RequiredString`, `Int`, `Object`…) |
| `sdk/go/types/` | Status enums, discovery, registration types |

## 1. Agent/node registration & reasoners

Python: `Agent` subclasses FastAPI (`sdk/python/agentfield/agent.py:503`); `@app.reasoner()` (`agent.py:1838-1848`) infers input/output schemas from type hints (`agent.py:1892-1916`), binds JSON body to typed/pydantic kwargs via `convert_function_args` (`agent.py:2301-2321`), serializes with `jsonable_encoder` (`agent.py:2513`), reads `AGENTFIELD_SERVER` env (`agent.py:639-643`). SWE-AF: `app = Agent(node_id=..., agentfield_server=...)` (`SWE-AF/swe_af/app.py:53-61`), `@app.reasoner()` `build(goal: str, ..., config: dict|None) -> dict` (`app.py:490-502`), `@router.reasoner()` throughout `swe_af/reasoners/pipeline.py` (e.g. :158).

Go: imperative, untyped.

```go
// sdk/go/agent/agent.go:55
type HandlerFunc func(ctx context.Context, input map[string]any) (any, error)
// sdk/go/agent/agent_register.go:9
func (a *Agent) RegisterReasoner(name string, handler HandlerFunc, opts ...ReasonerOption)
```

Construction: `agent.New(Config{NodeID, Version, AgentFieldURL, ListenAddress (default ":8001"), ...})` (`agent.go:524-605`). JSON body decoded to `map[string]any` only (`agent.go:1259-1264`); default input schema is permissive `additionalProperties:true` (`agent_register.go:17`); real schemas via manual `WithInputSchema` (`agent.go:61-67`). Router grouping: `NewRouter()` + `IncludeRouter` with dot-prefix namespacing (`router.go:57-198`). Endpoint path convention identical: `POST /reasoners/{name}` (`agent.go:821,1247`). Control-plane registration on startup: `Serve → Initialize → registerNode` posting `ReasonerDefinition{ID, InputSchema, OutputSchema, Tags, Triggers, AcceptsWebhook}` (`agent_lifecycle.go:94-217`), then lease/heartbeat loop (`agent_lifecycle.go:308-331`).

| Feature | SWE-AF Python usage | Go SDK equivalent | Status | Porting notes |
|---|---|---|---|---|
| Node construction + CP registration | `app.py:53-61`; `agent.py:548-569` | `agent.New(Config)` `agent.go:524`; `registerNode` `agent_lifecycle.go:126-217` | SUPPORTED | Go reads no `AGENTFIELD_SERVER`/`NODE_ID` env — wire env→Config yourself |
| Decorator registration | `app.py:490`; `pipeline.py:158` | `RegisterReasoner` `agent_register.go:9` | PARTIAL | Imperative, mechanical rewrite |
| Typed/pydantic input binding + validation | `agent.py:2301-2321`; typed params in `pipeline.py:159-170` | `map[string]any` + `inputs/inputs.go:16-68` | MISSING | Write a small generic `Bind[T]` helper: `json.Marshal(input)` → `json.Unmarshal(&T)`; SWE-AF already hydrates nested models manually (`PRD(**prd)` `pipeline.py:375`) so loss is small |
| Output serialization (`.model_dump()` dicts) | `pipeline.py:237` | handler returns `any`, `writeJSON` `agent.go:1364` | SUPPORTED | Return structs/maps directly |
| Router grouping (`include_router`) | `app.py:61`; `reasoners/__init__.py:6` | `router.go:57,172-198` | SUPPORTED | Dot-prefix instead of URL prefix |
| Per-reasoner tags, triggers, `accepts_webhook`, VC flags | `decorators.py:58-123` | `WithTags/WithTriggers/WithAcceptsWebhook/WithVCEnabled` `agent.go:107-302` | SUPPORTED | Auto-set `accepts_webhook="true"` when triggers present mirrors Python (`agent_register.go:29-32`) |
| `ReasonerFailed(msg, result=...)` (fail with structured result) | `app.py:1400-1403` | `ExecuteError{StatusCode, Message, ErrorDetails}` `agent.go:304-315` | PARTIAL | No "failed-but-keep-result" carrier; encode result into error payload or extend the SDK |

## 2. Reasoner-to-reasoner calls & DAG lineage — parity confirmed

SWE-AF calls sibling reasoners via `app.call("{NODE_ID}.reasoner", **kwargs)` (`app.py:260, 652`, etc.) so each renders as a DAG node.

- **Python sets:** `agent.py:4026-4030` — `headers = current_context.to_headers(); headers["X-Parent-Execution-ID"] = current_context.execution_id`; header constants in `execution_context.py:15-18` (`X-Run-ID`, `X-Execution-ID`, `X-Parent-Execution-ID`, `X-Session-ID`); client fills defaults `client.py:839-852`.
- **Go sets the identical headers:** `Agent.Call` (`agent.go:1545-1610`) POSTs `{AgentFieldURL}/api/v1/execute/{target}` with `X-Run-ID`, `X-Parent-Execution-ID` (= current `ExecutionID`), `X-Workflow-ID`, `X-Session-ID`, `X-Actor-ID`, `X-Agent-Node-DID`, `X-Caller-Agent-ID` (`agent.go:1581-1605`). Inbound context parsed from the same headers (`agent.go:1188-1192, 1268-1272`). In-process fast path `CallLocal` + `buildChildContext` (`agent.go:1795, 1837`).
- **Control plane consumes:** `control-plane/internal/handlers/execute.go:2019-2020` reads `X-Parent-Execution-ID`/`X-Session-ID`; stored as `ParentExecutionID` on the execution (`execute.go:480, 1476, 1505`) and echoed into child exec context (`execute.go:2188-2192`) and downstream dispatch headers (`execute.go:1767-1773`). CP mints fresh child execution IDs; DAG edge = `parent_execution_id` + `run_id` only.

**Verdict: SUPPORTED — DAG renders identically.** One requirement: pass the request's `ctx` (carrying the `ExecutionContext`) through your call tree — Go propagates lineage via `context.Context`, whereas Python does it implicitly via contextvars.

## 3. AI / harness

SWE-AF **does not** call `claude_agent_sdk` directly anymore — zero imports of `claude_agent_sdk`/`ClaudeAgentOptions` in `swe_af/` (grep verified). All AI execution goes through the AgentField SDK harness: `await router.harness(prompt=..., schema=PRD, provider=..., model=..., max_turns=..., tools=["Read","Write","Glob","Grep","Bash"], permission_mode=..., system_prompt=..., cwd=repo_path)` (`pipeline.py:208-218` and ~4 more sites; heavy use in `reasoners/execution_agents.py` consuming `result.parsed`, e.g. :180-186, :273, :503-508). Runtime switch: `swe_af/runtime/providers.py:5-27` — runtimes `claude_code | open_code | codex` → harness providers `claude | opencode | codex`; wired via `execution/schemas.py:559-560`.

Python harness signature: `Agent.harness(prompt, *, schema, provider, model, max_turns, max_budget_usd, tools, permission_mode, system_prompt, env, cwd, project_dir, schema_mode, **kwargs)` (`agent.py:3472-3489`).

Go harness: providers `claude-code`, `codex`, `opencode`, `gemini` (`harness/provider.go:6-13`); options struct `harness/provider.go:25-83`; `Runner.Run(ctx, prompt, schema map[string]any, dest any, overrides Options) (*Result, error)` (`harness/runner.go:39`) with retry/backoff (`runner.go:193`) and schema-validation retry (`runner.go:245`); agent integration `a.Harness(ctx, prompt, schema, &dest, opts)` (`agent/harness.go:84-85`). **Invocation is a CLI subprocess**: `exec.CommandContext` (`harness/cli.go:66`); Claude Code runs `claude --print --output-format json [--model … --max-turns … --permission-mode … --system-prompt … --resume … --max-budget-usd … --allowedTools …] <prompt>` (`harness/claudecode.go:33-68`), needs the `claude` binary (`npm install -g @anthropic-ai/claude-code`, error hint at `claudecode.go:94`). Structured output = same file-based protocol as Python: prompt suffix instructs Write of `.agentfield_output.json`, parse → cosmetic repair → text-extraction fallback (`harness/schema.go:14-36, 165-226`).

| SWE-AF harness feature | SWE-AF evidence | Go harness evidence | Status | Notes |
|---|---|---|---|---|
| `provider` claude_code / opencode / codex | `runtime/providers.py:5-27` | `provider.go:6-13`, `factory.go` | SUPPORTED | Name mapping: `claude` → `claude-code` |
| `model` per call (per-role model map) | `pipeline.py:211`; `execution/schemas.py:466+` | `Options.Model`, `claudecode.go:35-37` | SUPPORTED | |
| `max_turns` | `pipeline.py:213` | `Options.MaxTurns`, `claudecode.go:39-41` | SUPPORTED | |
| `permission_mode` ("auto"→bypassPermissions) | `pipeline.py:216` | `permissionMap` `claudecode.go:26-30, 43-49` | SUPPORTED | |
| `system_prompt` | `pipeline.py:217` | `claudecode.go:51-53` | SUPPORTED | |
| `cwd` / `project_dir` | `pipeline.py:218`; `agent.py:3510-3517` | `Options.Cwd/ProjectDir` `provider.go:45-51`, `claudecode.go:79-82` | SUPPORTED | |
| `tools` allowlist | `pipeline.py:214` | `Options.Tools` → `--allowedTools` `claudecode.go:63-65` | SUPPORTED | |
| `schema` structured output (pydantic → JSON schema, file protocol, repair, retries) | `pipeline.py:210`; `.parsed` throughout `execution_agents.py` | `schema.go` full pipeline; `runner.go:245` retry; `Result.Parsed` `result.go:40-47` | SUPPORTED | Pass `schema map[string]any` + `dest any` pointer; generate JSON schema from Go structs (e.g. invopop/jsonschema) |
| `schema_mode: "incremental"/"auto"` (field-by-field build for large schemas) | `agent.py:3519-3524`; `_schema.py:319-420` | no `incremental`/`SchemaMode` in `sdk/go/harness/` | ~~MISSING~~ RESOLVED (`feat/go-sdk-parity`) | Go only has single-shot + large-schema-file indirection (`schema.go:50-62`). SWE-AF's big schemas (PRD/Architecture) may need this robustness. **`harness.Options.SchemaMode` now exists** (`single`/`incremental`/`auto`); SWE-AF leaves it `single` to match Python's effective default (never passes `schema_mode`; SDK maps `None→single`). |
| `env` per call (scoped creds) | `app.py:80-93` | `Options.Env` (empty string unsets) `provider.go:41-43`, `claudecode.go:70-77` | SUPPORTED | Go auto-unsets `CLAUDECODE` for nested sessions (`claudecode.go:75-77`) |
| `max_budget_usd` cost cap | `agent.py:3480` | `Options.MaxBudgetUSD` → `--max-budget-usd` `claudecode.go:59-61` | SUPPORTED | |
| Cost/usage reporting (`cost_usd` on result) | Python accumulates `total_cost_usd` `harness/_runner.py:193-206, 316` | Go `Metrics{DurationMS, DurationAPIMS, NumTurns, SessionID}` `result.go:19-25` — **no cost field**; `parseJSONOutput` doesn't extract `total_cost_usd` (`claudecode.go:148-196`) | ~~MISSING~~ RESOLVED (`feat/go-sdk-parity`) | Extend `parseJSONOutput` — the field is in Claude's JSON output already. **`harness.Result.CostUSD` (`*float64`) now exists** and flows through `harnessx` untouched; SWE-AF consumes no cost, so no SWE-AF change. |
| Session resume | not used by SWE-AF | `Options.ResumeSessionID` → `--resume` `claudecode.go:55-57` | SUPPORTED | |
| Timeout + retries + backoff | Python runner | `Options.Timeout/MaxRetries/...` `provider.go:65-83`; idle-progress detection `cli.go:196` | SUPPORTED | |

## 4. Notes / observability — SUPPORTED, load-bearing

SWE-AF has **183** `.note(` call sites (`app.py` 74, `execution_agents.py` 64, `fast/` 21, `pipeline.py` 12, `hitl/` 12) — its primary observability channel. Python `Agent.note(message, tags)` (`agent.py:4385`): fire-and-forget, execution-context headers, POST `{api_base}/executions/note`. Go `Agent.Note(ctx, message, tags...)` / `Notef` (`agent/note.go:33, 48`): same fire-and-forget goroutine, same headers (`note.go:98-113`), same payload, same endpoint. Caveat: Go's `Note` uses `cfg.AgentFieldURL` verbatim without appending `/api/v1` (`note.go:55-65`) while the memory backend appends it itself — reconcile the base-URL convention when wiring config. Go also adds structured `ExecutionLogger` (`execution_logs.go:56-116`) and process-log tailing (`process_logs.go`).

## 5. Memory — SUPPORTED, but SWE-AF doesn't use it

SWE-AF has zero SDK-memory call sites; `hitl/credentials_store.py:3-9` explicitly avoids `app.memory` in favor of a process-local dict. Not a porting factor.

## 6. Async execution, status, pause/cancel

| Capability | Python (SWE-AF-relevant) | Go | Status |
|---|---|---|---|
| Async reasoner exec (202 + background + terminal callback) | `agent.py:1981-2005, 2439-2591` | `agent.go:1290-1308` (202), `executeReasonerAsync` `agent.go:1407-1488` | SUPPORTED |
| Status+result POST to `/api/v1/executions/{id}/status`, 5x retry | `agent.py:2591-2637` | `sendExecutionStatus`/`postExecutionStatus` `agent.go:1490-1543` | SUPPORTED |
| Status enum (`waiting` for approvals; no `paused` in either SDK) | `status.py:7-17` | `types/types.go:97-104`, `types/status.go` | SUPPORTED |
| Cooperative cancel (`POST /_internal/executions/{id}/cancel`) | `cancel.py:38-128` | `agent/cancel.go:17-107`; route `agent.go:826` | SUPPORTED — Go handlers must honor `ctx.Done()` |
| Execution watchdog with pause-clock discount | `agent.py:2476-2507` + `agent_pause.py:10-46` | no watchdog, no PauseClock | MISSING (see §7) |
| SWE-AF `resume_build` / checkpoints | `app.py:2082-2131`; `dag_executor.py:683-702, 1361-1436` — SWE-AF's own file-based crash recovery, not SDK pause/resume | n/a | Port as app logic |

## 7. Webhooks / HITL — the biggest functional gap

SWE-AF's HITL (`swe_af/hitl/ask_user.py`, `wrapper.py`) depends on **`await app.pause(**pause_kwargs)`** (`ask_user.py:488`, `app.py:834`, `wrapper.py:95,138`) — blocking suspend: registers a future (`_PauseManager`, `agent_pause.py:49-116`), transitions execution to `waiting` via `client.request_approval` (`agent.py:4608`), pause-clock stops the watchdog budget (`agent.py:4640`), and the CP's approval webhook hitting the agent's `/webhooks/approval` route resolves the future (`agent_server.py:326-358`), returning `ApprovalResult{decision, feedback, raw_response}` (`client.py:55`).

Go SDK:
- `accepts_webhook` registration flag: SUPPORTED (`agent.go:107-149`, `agent_register.go:29-32`).
- Client-side approval: `RequestApproval` / `GetApprovalStatus` / **poll-based** `WaitForApproval` with backoff (`client/approval.go:61-127`): SUPPORTED.
- **`Agent.Pause()`, PauseManager, agent-side `/webhooks/approval` callback route, PauseClock: all MISSING.**

**Status: PARTIAL.** Workaround: rebuild the HITL wrapper around `client.RequestApproval` + `WaitForApproval` polling. Loses webhook-instant resume (poll latency) and pause-clock budget discount — latter matters less since Go has no SDK watchdog.

> **Superseded (`feat/go-sdk-parity`):** `agent.Pause()`, `PauseManager`, and the agent-side `/webhooks/approval` route now exist in the Go SDK. SWE-AF HITL (ask-user + plan-approval gate) now calls `agent.Pause` through the `hitl.Pauser` seam — webhook-resumed, no polling, no poll-latency loss. Merge-gated on the SDK PR + `AGENTFIELD_SDK_REF` bump in `go/Dockerfile`. (PauseClock/watchdog remains N/A — Go has no SDK watchdog.)

## 8. Packaging — MISSING for Go in the control plane

SWE-AF ships `agentfield-package.yaml` (`entrypoint.start: python -m swe_af`, `node_id: swe-planner`, `default_port: 8003`) consumed by `af install`. The CP package installer is **Python-only**: `PackageMetadata` has no language selector, `DependencyConfig` only `Python []string` (`installer.go:101-146`), `StartCommand()` falls back to `python main` (`installer.go:563-574`), dep install = venv + pip (`installer.go:812-814`). No Go build support. Go examples run via `go run`/compiled binary + `agent.Run(ctx)` (`agent_lifecycle.go:80-91`); SDK provides an af-style CLI (`agent/cli.go:99-529`).

**Workarounds:** (a) Docker image / compiled binary with docker-compose or Railway, sidestepping `af install`; (b) shim manifest — untested, treat as unsupported; (c) add Go support to `installer.go` (CP feature).

## HARD GAPS

1. **`Agent.Pause()` webhook-resumed HITL** — no agent-level pause, PauseManager, approval callback route, PauseClock. SWE-AF's approval checkpoint (`app.py:834`) and ask-user loop (`hitl/ask_user.py:488`) depend on it. *Workaround:* poll-based `client.WaitForApproval` (`client/approval.go:92-127`); or contribute `Agent.Pause()` + `/webhooks/approval` route to the Go SDK. **RESOLVED (`feat/go-sdk-parity`):** the SDK now provides `agent.Pause()` + `PauseManager` + `/webhooks/approval`; SWE-AF HITL uses it via `hitl.Pauser`. Merge-gated on the SDK PR + Dockerfile ref bump.
2. **CP package/install support for Go nodes** — installer hardwired to Python (`installer.go:563-574, 812-814`). *Workaround:* Docker/binary deployment.
3. **Harness `schema_mode="incremental"`** — no Go counterpart (`_schema.py:319-420`). *Workaround:* single-shot + `SchemaMaxRetries` + `BuildFollowupPrompt` (`harness/schema.go:322`); port incremental mode if large-schema failures show up. **RESOLVED (`feat/go-sdk-parity`):** `harness.Options.SchemaMode` now exists; SWE-AF keeps `single` (Python's effective default), so no SWE-AF behavior change.
4. **Harness cost/usage reporting** — Go `Metrics` lacks `cost_usd` (`result.go:19-25`, `claudecode.go:148-196`). *Workaround:* small SDK patch. **RESOLVED (`feat/go-sdk-parity`):** `harness.Result.CostUSD` now exists and flows through `harnessx` untouched; SWE-AF consumes no cost, so no SWE-AF change.

**Soft gaps:** no decorator/typed-input binding (write generic `Bind[T]`); no env-var auto-config (read `AGENTFIELD_SERVER`/`NODE_ID` in `main()`); no `ReasonerFailed`-with-result carrier (encode into error payload); lineage travels via `context.Context` — every internal call path must thread `ctx`; `/api/v1` base-URL convention differs between `note.go` and memory backend.

**Everything else at parity:** registration + CP handshake, `/reasoners/{name}` paths, sync/async 202 + status callbacks, `Call` with identical lineage headers (DAG renders identically), notes, cancel, triggers/webhook flags, memory, CLI coding harness for all three runtimes.

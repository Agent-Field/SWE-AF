# SWE-AF Python → Go 1:1 Port — Design Document

**Authoritative inputs:** `research-python-inventory.md`, `research-go-sdk-gaps.md` (both in this scratchpad). This doc adds the concrete, verified-against-source design. Porting agents work ONLY from: this doc + `port-work-breakdown.md` + the Python source.

**Non-negotiable parity contract (from the user):**
1. Every registered Python reasoner is a separately registered Go reasoner with the **same name**, addressable at the same `node.reasoner` path, same input param names, same output JSON shape (pydantic `model_dump` snake_case keys).
2. All inter-reasoner calls go through the Go SDK `agent.Call` (control-plane routed) exactly where Python uses `app.call`, so the control-plane DAG UI renders identically (same nodes, same parent-child edges).
3. The HTTP API surface stays byte-compatible: `POST /api/v1/execute/async/swe-planner.build` etc. — same input field names, same result JSON key names/shapes.
4. Python code stays in the repo untouched; Python tests stay as-is.

---

## 0. Verified SDK facts (read from source, not assumed)

| Fact | Source | Consequence for the port |
|---|---|---|
| Go SDK module path = `github.com/Agent-Field/agentfield/sdk/go`, `go 1.21` | `sdk/go/go.mod` | Our module imports this path. |
| **No `sdk/go/vX.Y.Z` submodule tags exist** — only repo-level `v0.1.xx` tags | `git tag \| grep sdk` = empty | Cannot `go get` a published SDK version. Must use `replace` + workspace (§1.2). |
| `agent.Call(ctx, target, input map[string]any) (map[string]any, error)` returns the decoded execution envelope; sets `X-Run-ID`/`X-Parent-Execution-ID`/`X-Workflow-ID`/`X-Session-ID` from ctx | `agent/agent.go:1545-1610` | DAG lineage is automatic **iff we thread ctx**. Envelope unwrap mirrors Python `envelope.py`. |
| `agent.ExecutionContextFrom(ctx) ExecutionContext` is **public**; `ExecutionContext.ExecutionID` present | `agent/agent.go:1980, :30` | ReasonerFailed carrier can POST to `/api/v1/executions/{ExecutionID}/status` (§4.5). |
| Async status callback URL = `{base}/api/v1/executions/{id}/status`; CP status request struct binds only `status,status_reason,result,error,duration_ms,completed_at,progress`; `result` persisted **unconditionally of status** | `agent/agent.go:1495`; CP `handlers/execute.go:118-126, 843-913` | status=`failed` + non-nil `result` both persist → replicates `ReasonerFailed(result=...)`. `error_details` is dropped by CP (no struct field). |
| Harness: `a.Harness(ctx, prompt, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error)`; `Result.Parsed`=dest ptr on success, `Result.IsError`/`ErrorMessage` on failure | `agent/harness.go:84`; `harness/runner.go:39`; `harness/result.go:40-56` | Caller supplies pre-built JSON-schema map + `*T` dest. `CheckFatalHarnessError` reads `IsError`/`ErrorMessage`. |
| Harness schema map is **only** used to (a) build the prompt suffix and (b) list expected keys in failure diagnosis. **No programmatic validation** against the schema — validity == `json.Unmarshal` into `dest` succeeds. Map keys are alphabetized by `json.MarshalIndent`. | `harness/schema.go:36, 165-183, 278-319` | invopop/jsonschema output is sufficient; field ordering & `required` correctness are cosmetic. **Non-zero pydantic defaults must be handled in Go** (missing key → Go zero value, not the pydantic default). |
| Harness incremental `schema_mode` has **no Go equivalent**; Go runner only does single-shot + `SchemaMaxRetries` + `BuildFollowupPrompt` | `harness/runner.go`, no `SchemaMode` | Do not port incremental. Document as known difference (§10). **Superseded (`feat/go-sdk-parity`):** `harness.Options.SchemaMode` now exists; SWE-AF keeps the `single` default (§10). |
| Go codex provider parses `codex exec --json` JSONL natively; structured output uses the same file-write protocol (`BuildPromptSuffix`) as all providers | `harness/codex.go`, `harness/schema.go:36` | The Python `codex_harness_patch` is **not needed** in Go (§9). |
| Client approval: `RequestApproval` (POST `/api/v1/agents/{node}/executions/{id}/request-approval`, sets execution → `waiting`) + poll-based `WaitForApproval` (GET `/approval-status`). **No `Agent.Pause()`, no PauseManager, no `/webhooks/approval` route.** | `client/approval.go:61-127` | HITL is poll-based (§4.6). `waiting` status is set server-side by `request-approval`, so the UI shows waiting. **Superseded (`feat/go-sdk-parity`):** `agent.Pause()` + `PauseManager` + `/webhooks/approval` now exist; HITL uses `agent.Pause` (§4.6). |
| hax REST: `POST {HAX_SDK_URL}/api/v1/requests`, header `Authorization: Bearer {HAX_API_KEY}`, camelCase body (`type,payload,title,description,webhookUrl,expiresInSeconds,metadata,userId,publicKey`), response `{id,url,type,status,...}` | `<hax-sdk>/sdks/python/hax/client.py:325`, `http.py:52`, `models/request.py` | Go HITL calls hax HTTP directly (§4.6); no hax-Go-SDK needed. Omit `publicKey` → hax returns plaintext values (§4.6 note). |

---

## 1. Go module layout

### 1.1 Location & module path

A **new top-level `go/` directory inside the SWE-AF repo**, with its own `go.mod`. Module path: **`github.com/Agent-Field/SWE-AF/go`**.

Justification (vs. a sibling repo, vs. root-level go.mod):
- The Python package occupies the repo root (`swe_af/`, `pyproject.toml`); a root-level `go.mod` would entangle Go tooling with the Python tree and confuse `af install` (which reads `agentfield-package.yaml` at root). A `go/` subdir keeps `go build ./...` rooted cleanly and leaves the Python repo byte-identical (constraint #4).
- Same-repo (not a new repo) keeps the port reviewable in one place and lets Docker build both language artifacts from one checkout during the transition.
- The `/go` suffix in the module path is deliberate and harmless (Go allows it; it is not the `/vN` major-version suffix).

### 1.2 Depending on the Go SDK (no submodule tags exist)

Because there are **no `sdk/go/vX.Y.Z` tags**, a normal `require ... vX.Y.Z` is impossible. Decision:

- **Dev:** a **Go workspace** at `<workspace>/go.work` listing `./SWE-AF/go` and `./agentfield/sdk/go`. Zero `go.mod` churn; edits to the SDK are picked up live. `go.work` is git-ignored in each repo (it spans repos).
- **CI / Docker:** a **`replace` directive** in `SWE-AF/go/go.mod`:
  ```
  require github.com/Agent-Field/agentfield/sdk/go v0.0.0-00010101000000-000000000000
  replace github.com/Agent-Field/agentfield/sdk/go => ../agentfield/sdk/go
  ```
  CI and the Docker builder stage must place the agentfield repo as a **sibling checkout at a pinned SHA** (CI: `actions/checkout` a second repo; Docker: a `git clone --depth 1 <agentfield> && git checkout <PINNED_SHA>` in the builder, then `COPY`/`replace` to it). Pin the SHA in a build arg (`AGENTFIELD_SDK_REF`) so builds are reproducible and cache-bust deliberately (per the docker-pip-cache rule, the analogous concern here is the pinned ref string).
- **Future:** once the agentfield release process publishes `sdk/go/vX.Y.Z` submodule tags (or a Go pseudo-version is acceptable via `GOPRIVATE` + auth), drop the `replace` and switch to a real `require`. Document this as the migration target; do not block the port on it.

`agentfield` stays **read-only** — we never edit the SDK. Every gap is worked around app-side (§9).

### 1.3 Package tree & Python→Go file mapping

Package-per-concern (fine-grained) so waves build independently and file ownership is disjoint. `internal/` throughout (nothing is a public library).

```
go/
  go.mod
  Makefile
  Dockerfile                      # Go multi-stage; replaces Python image for the node
  cmd/
    swe-planner/main.go           # __main__.py  → env→Config, register all, agent.Run
    swe-fast/main.go              # fast/__main__.py
  internal/
    schemas/        # result/data models, enums, UnmarshalJSON defaults
    config/         # BuildConfig/ExecutionConfig/FastBuildConfig + validators + model/runtime resolution
    runtimex/       # runtime alias↔provider/adapter mapping
    fatal/          # FatalHarnessError + CheckFatalHarnessError
    envelope/       # UnwrapCallResult
    harnessx/       # env-injection wrapper + generic Run[T] + invopop schema cache + CheckFatal
    reasonerfail/   # ReasonerFailed carrier (failed-status-with-result poster)
    afx/            # Bind[T], note helpers, small SDK ergonomics
    prompts/        # all 26 prompt modules (one .go per module) + _utils + workspace
    hitl/           # credentials_store, ask_user, wrapper, hax REST client, scout
    tools/          # web_search guardrail
    dagutil/        # Kahn levels, file-conflict validation, sequence numbers, find_downstream, apply_replan
    cigate/         # watch_pr_checks (gh poller)
    coding/         # inner loop (coding_loop.py)
    dag/            # run_dag engine (dag_executor.py)
    roles/          # the 25 role reasoners (grouped files)
    orch/           # 5 orchestrators (build/plan/execute/resolve/resume) + ci-gate loop + approval gate
    fast/           # fast planner/executor/verifier + fast build + delegating wrappers
    node/           # shared *agent.Agent wiring, NodeID, register() functions
```

| Python file (abs under `SWE-AF/`) | LOC | Go package / file(s) |
|---|---|---|
| `swe_af/app.py` | 2149 | `orch/build.go`, `orch/plan.go`, `orch/execute.go`, `orch/resolve.go`, `orch/resume.go`, `orch/cigate_loop.go` (`_run_ci_gate`), `orch/approval_gate.go` (hax plan-approval `:355-471,:759-838`), `orch/clone_repos.go` (`_clone_repos`), plus `node/node.go` (Agent construction). |
| `swe_af/__main__.py`, `swe_af/fast/__main__.py` | 5/4 | `cmd/swe-planner/main.go`, `cmd/swe-fast/main.go` |
| `swe_af/reasoners/__init__.py` | 11 | `node/register.go` (router-equivalent: `RegisterReasoner` wiring); codex patch import → dropped (§9) |
| `swe_af/reasoners/pipeline.py` | 549 | `roles/planning.go` (run_product_manager/environment_scout/architect/tech_lead/sprint_planner); pure helpers `_compute_levels`/`_validate_file_conflicts`/`_assign_sequence_numbers`/`_ensure_paths` → `dagutil/` |
| `swe_af/reasoners/schemas.py` | 106 | `schemas/planning.go` (PRD, Architecture+, ReviewResult, IssueGuidance, PlannedIssue, PlanResult) |
| `swe_af/reasoners/execution_agents.py` | 1780 | `roles/coding.go`, `roles/git.go`, `roles/advisor.go`, `roles/ci.go`, `roles/verify.go` (grouped by role family — see §11 waves) |
| `swe_af/execution/dag_executor.py` | 1801 | `dag/executor.go`, `dag/gates.go`, `dag/worktree.go`, `dag/checkpoint.go` |
| `swe_af/execution/dag_utils.py` | 173 | `dagutil/dagutil.go` |
| `swe_af/execution/coding_loop.py` | 895 | `coding/loop.go`, `coding/memory.go`, `coding/artifacts.go` |
| `swe_af/execution/schemas.py` | 1200 | `schemas/*.go` (data/result models + enums) + `config/*.go` (BuildConfig/ExecutionConfig + resolution + validators) |
| `swe_af/execution/ci_gate.py` | 338 | `cigate/watch.go` |
| `swe_af/execution/envelope.py` | 66 | `envelope/envelope.go` |
| `swe_af/execution/fatal_error.py` | 88 | `fatal/fatal.go` |
| `swe_af/execution/_replanner_compat.py` | 93 | `dag/replanner_compat.go` |
| `swe_af/fast/*` (8 files) | ~1000 | `fast/planner.go`, `fast/executor.go`, `fast/verifier.go`, `fast/build.go`, `fast/wrappers.go`, `fast/schemas.go`(→`schemas/fast.go`), `fast/prompts.go`(→`prompts/fast.go`) |
| `swe_af/hitl/*` (6 files) | ~1050 | `hitl/credentials_store.go`, `hitl/ask_user.go`, `hitl/wrapper.go`, `hitl/hax_client.go`, `hitl/scout.go`, `hitl/services.go` |
| `swe_af/runtime/providers.py` | 37 | `runtimex/providers.go` |
| `swe_af/runtime/codex_harness_patch.py` | 301 | **dropped** (§9); optional `harnessx/codex_strict.go` if codex-strict parity later required |
| `swe_af/tools/web_search.py` | 75 | `tools/websearch.go` |
| `swe_af/prompts/*` (26 files) | ~3900 | `prompts/<module>.go` (verbatim string consts + template funcs) — 1:1 file map (§8) |

---

## 2. Schema strategy (Pydantic → Go structs)

### 2.1 Rules
- **One Go struct per Pydantic model.** Type name = PascalCase of the Pydantic class (already PascalCase). JSON tag = **exact snake_case pydantic field name** (the `model_dump()` key). Never use `omitempty` on fields Python always emits (Python `model_dump()` emits every field) — emitting `omitempty` would drop zero-valued keys and break byte-compatibility (constraint #3). Use plain `json:"field_name"`.
- **Optional-object / tri-state fields → pointers.** Pydantic `X | None = None` (e.g. `ask_user_form`, `workspace_manifest`, `git_init_result`, `guidance`, `sequence_number`, `verification`, `split_request`, `models`) → `*T`. Pydantic `bool | None = None` (`CoderResult.tests_passed`) → `*bool`.
- **Untyped `list[dict]` / `dict` fields → `[]map[string]any` / `map[string]any`.** Critical for `DAGState.all_issues` (holds enriched arbitrary issue dicts) and `*_results`, `debt_items`, `iteration_history`, `merge_results`, etc. Preserves round-trip of unknown keys (checkpoint compat, §5).
- **Enums → typed string consts.** e.g.
  ```go
  type IssueOutcome string
  const (
    IssueOutcomeCompleted        IssueOutcome = "completed"
    IssueOutcomeCompletedWithDebt IssueOutcome = "completed_with_debt"
    IssueOutcomeFailedRetryable   IssueOutcome = "failed_retryable"
    IssueOutcomeFailedUnrecoverable IssueOutcome = "failed_unrecoverable"
    IssueOutcomeFailedNeedsSplit  IssueOutcome = "failed_needs_split"
    IssueOutcomeFailedEscalated   IssueOutcome = "failed_escalated"
    IssueOutcomeSkipped           IssueOutcome = "skipped"
  )
  ```
  Enums to port (exact values in the schema inventory): `AdvisorAction`, `IssueOutcome`, `ReplanAction`, `QASynthesisAction`. Non-enum "pseudo-literal" strings (`estimated_scope`, `estimated_complexity`, `severity`, `complexity_assessment`, `FastTaskResult.outcome`, `CIWatchResult.status`, `RepoSpec.role`) stay plain `string` but the default-seeding (§2.2) applies.

### 2.2 Non-zero defaults — `Defaults()` + `UnmarshalJSON`

Go `json.Unmarshal` leaves absent keys at the Go zero value; Pydantic fills the declared default. Where the pydantic default is **non-zero**, the LLM omitting the field would silently produce the wrong value (e.g. coder omits `complete` → Go `false`, but pydantic `True`). Fix pattern — for **every struct with ≥1 non-zero-default field**, implement:

```go
func defaultCoderResult() CoderResult { return CoderResult{Complete: true} }

func (c *CoderResult) UnmarshalJSON(b []byte) error {
    *c = defaultCoderResult()          // seed pydantic defaults
    type alias CoderResult             // avoid recursion
    return json.Unmarshal(b, (*alias)(c))
}
```
Semantics match pydantic: an **absent** key keeps the seeded default; a **present** key (even `false`/`0`/`""`) overrides; JSON `null` into a value type is a no-op (keeps default) — acceptable. Also expose the `defaultXxx()` constructor for use as the **deterministic fallback struct** each role returns when `result.Parsed == nil` (the harness parse-failure path — every Python reasoner has such a fallback).

**Complete non-zero-default list** (implement `UnmarshalJSON` + `defaultXxx()` for each; values from the schema inventory):
`RepoSpec.create_pr=true`, `WorkspaceRepo.create_pr=true`, `IssueAdaptation.severity="medium"`, `IssueAdvisorDecision.{confidence=0.5,debt_severity="medium"}`, `IssueResult.attempts=1`, `DAGState.max_replans=2`, `RetryAdvice.confidence=0.5`, `CoderResult.complete=true`, `ReviewResult.complexity_assessment="appropriate"`, `IssueGuidance.{needs_new_tests=true,estimated_scope="medium"}`, `PlannedIssue.estimated_complexity="medium"`, `FastTask.estimated_minutes=5`, `AskUserForm.submit_label="Submit"`, and all `BuildConfig`/`ExecutionConfig`/`FastBuildConfig` numeric/bool/runtime defaults (§6). (ExecutionConfig.max_retries_per_issue defaults **1** vs BuildConfig **2** — keep the divergence; `to_execution_config_dict()` forwards BuildConfig's value anyway.)

### 2.3 Harness structured output schema generation

Use **`github.com/invopop/jsonschema`**. Per §0, the schema map is LLM-instruction-only (no hard validation, keys alphabetized on marshal), so byte-parity with pydantic's `model_json_schema()` is unnecessary; invopop's `$defs`/`$ref`/`anyOf`/`items`/`enum`/`required`-from-tags output is more than adequate and vastly better than the SDK's shallow `StructToJSONSchema` (which loses nested props, items, enums — do **not** use it). Centralize in `harnessx`:
```go
func schemaFor[T any]() map[string]any   // reflect once per T, cache in sync.Map; invopop → json → map[string]any
```
Configure invopop with `DoNotReference=false` (emit `$defs`) and field-name from json tags. Do **not** port incremental schema mode.

---

## 3. Concurrency mapping

| Python | Go |
|---|---|
| `asyncio.gather(*coros)` barrier per level | `golang.org/x/sync/errgroup`: `g, gctx := errgroup.WithContext(ctx)`; `g.SetLimit(maxConcurrentIssues)`; `g.Go(...)` per issue; `g.Wait()` is the barrier. `max_concurrent_issues=0` (unlimited) → don't call `SetLimit`. |
| semaphore bounding concurrent issues (`max_concurrent_issues=3`) | `g.SetLimit(3)` (errgroup limit) **or** `golang.org/x/sync/semaphore.Weighted`. errgroup limit is the clean fit since it also collects the first error. |
| `_call_with_timeout(coro, timeout=2700)` per issue | `ictx, cancel := context.WithTimeout(gctx, cfg.AgentTimeoutSeconds*time.Second)`; `defer cancel()`; thread `ictx` into the issue's `run_coding_loop` and all its `app.Call`s. On timeout → treat as the Python timeout branch (issue failure/retry-advisor path). |
| `asyncio.create_task(cleanup...)` background, awaited before `current_level += 1` | spawn `go func(){ ... }()` tracked by a per-level `sync.WaitGroup`; `wg.Wait()` before advancing the level (Python awaits cleanup at `dag_executor.py:1588` region before `current_level += 1`). |
| Cancellation via SDK cancel (cooperative) | Every reasoner handler must **honor `ctx.Done()`** (gap report §6). The SDK's `/_internal/executions/{id}/cancel` cancels the handler ctx; because we thread that ctx into every `app.Call` and `app.Harness`, cancellation propagates. Long deterministic loops (ci_gate poller, checkpoint waits) must `select { case <-ctx.Done(): ... }`. |
| Shared-memory closure `_memory_fn(action,key,value)` (in-process dict, `enable_learning`) | a `coding.Memory` struct guarding a `map[string]any` with a `sync.Mutex` (concurrent issues in a level read/write it). Passed by pointer into the coding loop. Not the SDK memory backend (SWE-AF never uses SDK memory). |
| hax `create_request` in `asyncio.to_thread` with 120s timeout | Go HTTP call already blocking; wrap in `context.WithTimeout(ctx, 120s)` on the request. |

**Errgroup error semantics vs. Python:** Python `gather` (no `return_exceptions=True` in the issue barrier) — but SWE-AF wraps each issue so exceptions become `IssueResult(FAILED_*)` rather than propagating. Mirror this: each `g.Go` closure must **recover/translate** its own error into an `IssueResult` and return `nil` to the group (so one issue's failure never cancels siblings mid-level). The barrier just waits; failure handling is per-issue post-barrier (matches `dag_executor.py` level-failure logic). Only genuinely fatal ctx-cancellation should abort the group.

---

## 4. Cross-cutting layers

### 4.1 Env-injection harness wrapper (`harnessx`)
Python monkeypatches `app.harness` (`app.py:80-93`) to merge scout-negotiated scoped credentials into every subprocess `env`, scoped creds winning over `os.environ`. Go: **no monkeypatch** — a single choke-point helper every role uses:
```go
// harnessx.Run generates the schema from T, injects run-scoped creds into opts.Env,
// calls app.Harness, runs CheckFatalHarnessError, and returns (*T, *harness.Result, error).
func Run[T any](ctx context.Context, app *agent.Agent, prompt string, opts harness.Options) (*T, *harness.Result, error)
```
Inside: `opts.Env = hitl.InjectCredentialsIntoEnv(opts.Env, runIDFrom(ctx))` — scoped creds override, mirroring Python precedence. `runID` from `agent.ExecutionContextFrom(ctx).RunID`. This is the ONLY way roles call the harness.

### 4.2 Envelope unwrap (`envelope`)
Port `envelope.py` verbatim. Go `agent.Call` returns `map[string]any` (the envelope). `UnwrapCallResult(raw map[string]any, label string) (any, error)`:
- envelope keys = `{execution_id,run_id,node_id,type,target,status,duration_ms,timestamp,result,error_message,cost}`.
- if none present → already unwrapped, return `raw`.
- `status ∈ {failed,error,cancelled,timeout}` → if `is_fatal_error(err)` return `*FatalHarnessError` else `fmt.Errorf("%s failed (status=%s): %s", label, status, err)`.
- else return `raw["result"]` (or `raw` when result nil). Every `app.Call` site wraps its return through this.

### 4.3 Fatal-error classification (`fatal`)
Port `fatal_error.py` verbatim: the 13 regexes (compile once, case-insensitive), `FatalHarnessError` (Go `error` type wrapping `OriginalMessage`), `IsFatalError(msg string) bool`, `CheckFatalHarnessError(r *harness.Result) error` reading `r.IsError`/`r.ErrorMessage`. Called inside `harnessx.Run` right after `app.Harness`, **before** the `Parsed==nil` fallback — so the real auth/billing message surfaces past all retry layers (roles must return the `*FatalHarnessError` up, not swallow it into a fallback struct).

### 4.4 `note()` usage (183 sites)
Go `app.Note(ctx, msg, tags...)` / `app.Notef(...)` — same fire-and-forget, same headers/endpoint (gap report §4). Port **every** `.note(` call 1:1 (they are the primary observability channel and some tests assert tag strings). **Reconcile the base-URL quirk:** `note.go` uses `cfg.AgentFieldURL` verbatim (no `/api/v1` append) while other paths append it — wire `Config.AgentFieldURL` to whatever the running CP expects and verify a note lands during the smoke test.

### 4.5 ReasonerFailed carrier (`reasonerfail`)
Replicates `ReasonerFailed(message, result=...)` **without modifying the SDK**. Mechanism (verified against CP persistence semantics):
```go
// PostFailedWithResult POSTs status=failed + result to the CP status endpoint,
// then the handler returns a plain error(message). The SDK's own subsequent
// failed-status POST carries NO result → CP's `if len(result)>0` guard means our
// result is NOT overwritten; the SDK write only sets error+status(failed, idempotent).
func PostFailedWithResult(ctx context.Context, app *agent.Agent, result any, message string) error
```
Implementation: `ec := agent.ExecutionContextFrom(ctx)`; POST `{AgentFieldURL}/api/v1/executions/{ec.ExecutionID}/status` with body `{"status":"failed","result":<result>,"error":message,"completed_at":<now>}` (marshal `result` to a JSON **object** — CP binds `result map[string]interface{}`), then `return errors.New(message)`. The handler (build orchestrator, empty-build branch) calls this and returns the error; the SDK posts `status=failed` again (no result) — CP keeps our result, sets error. Net record: `status=failed`, `result={BuildResult...}`, `error=message` — byte-identical to Python. **Verify during functional test** that the SDK's post-return status write is `failed` (not `succeeded`) when the handler returns an error, and that failed→failed is idempotent (CP terminal-guard allows it).

### 4.6 HITL — poll-based, hax called directly (`hitl`)
Python uses `app.pause()` (webhook-resumed). Go has no `Agent.Pause()`; use the **poll workaround** exactly:

> **Superseded (SDK parity, `feat/go-sdk-parity`):** the Go SDK now has `agent.Pause()` (webhook-resumed: `PauseManager` + `/webhooks/approval` route), matching Python's `app.pause()`. `hitl.RequestUserInputAndPause` and `orch/approval_gate.go` now call `pauser.Pause(...)` through a small `hitl.Pauser` seam (satisfied by `*agent.Agent`); steps 3–4 below collapse into a single `Pause` call, and the poll-latency / lost-webhook-resume caveat no longer applies. Merge is gated on the agentfield SDK PR landing + the `AGENTFIELD_SDK_REF` bump in `go/Dockerfile`.

**Ask-user flow (`hitl.RequestUserInputAndPause`), mirroring `ask_user.py:432`:**
1. Build the hax form payload from `AskUserForm` (port `build_form_builder` + `FormBuilder.to_payload()` — read `<hax-sdk>/sdks/python/hax/form_builder.py` to replicate the exact payload JSON: `{title,description,submitLabel,fields:[{id,type,label,description,required,placeholder,defaultValue,options,min,max,step}]}` in camelCase — confirm keys against `form_builder.py`).
2. `POST {HAX_SDK_URL}/api/v1/requests` with `Authorization: Bearer {HAX_API_KEY}`, body `{type:"form-builder", payload, title, description, expiresInSeconds, webhookUrl, userId, metadata}` — **omit `publicKey`** so hax returns plaintext values (see note). Wrap in `context.WithTimeout(ctx, 120s)` (mirrors `HAX_CREATE_REQUEST_TIMEOUT_SECONDS`). Response → `{id, url}`.
3. `client.RequestApproval(ctx, nodeID, execID, RequestApprovalRequest{ApprovalRequestID:id, ApprovalRequestURL:url, CallbackURL:webhookURL, ExpiresInHours:expires})` — this transitions the execution to **`waiting`** server-side (so the UI shows waiting, satisfying that requirement) and records the approval request. `nodeID`=`NODE_ID`, `execID`=`ExecutionContextFrom(ctx).ExecutionID`.
4. `client.WaitForApproval(ctx, nodeID, execID, &WaitForApprovalOptions{PollInterval:5s, MaxInterval:60s})` — blocks (poll+backoff) until status ≠ `pending`. Returns `ApprovalStatusResponse{Status, Response}`.
5. Map to `AskUserResponse` via the same table as `_parse_approval_result_to_response`: status/decision → submitted/cancelled/timeout/error; values from `Response["values"]` or `Response["response"]["values"]`, JSON-feedback fallback.

**Plan-approval gate** (`app.py:759-838`, engages only when `HAX_API_KEY` set) uses the same primitive; port `orch/approval_gate.go` with identical formatting/threading.

**`run_with_ask_user` wrapper** (`wrapper.py`): port to `hitl.RunWithAskUser` — invoke role → if parsed result carries non-nil `ask_user_form`, pause for input, append answers to `prior_user_responses`, re-invoke; bounded by `AskUserBudget{Remaining:2}`; strip the form from the final result. Used by PM, scout, issue_advisor, replanner.

**Losses vs. Python (documented, acceptable):** poll latency instead of instant webhook resume; no pause-clock budget discount (Go has no SDK watchdog, so moot). `webhookUrl` is still passed to hax/CP so instant resume works if the CP webhook fires, but Go resolves via polling regardless.

**`publicKey`/encryption note:** hax encrypts response values with the requester's public key when `publicKey` is sent. The Go poll path reads values from the **control plane's** `approval-status` (populated by the CP's approval-response webhook), not directly from hax, so omitting `publicKey` yields plaintext values end-to-end. Porting the hax `crypto.py` keypair is a later enhancement only if the deployment requires encrypted HITL payloads.

### 4.7 Model/runtime resolution (`config` + `runtimex`)
Port all env-driven resolution (inventory §8, schema inventory "resolution functions") **verbatim**, same env var names & precedence:
- `runtimex`: `RUNTIME_VALUES`, `normalize_runtime_provider`, `runtime_to_harness_provider` (`claude_code→"claude"`), `runtime_to_harness_adapter` (`claude_code→"claude-code"`). **Note the asymmetry** — provider vs adapter differ for claude only.
- `config`: `_default_runtime` (env `SWE_DEFAULT_RUNTIME`; OpenRouter-only auto-select `open_code`), `_openrouter_only_env`, `_codex_uses_chatgpt_auth` (`SWE_CODEX_AUTH_MODE`), `_codex_default_model`, `_default_model_from_env` (`SWE_DEFAULT_MODEL`/`AI_MODEL`/`HARNESS_MODEL`), `_default_planning_model`, `resolve_runtime_models` (precedence: runtime base → env cascade → `models.default` → `models.<role>`), `ROLE_TO_MODEL_FIELD` (17 keys), `_RUNTIME_BASE_MODELS` (claude_code all `sonnet` except `qa_synthesizer=haiku`; open_code minimax; codex dynamic), `_OPENROUTER_AUTO_DEFAULT_MODEL`. The role→model map resolution feeds `harness.Options.Model` for each role.
- When calling the harness, `provider` = `runtime_to_harness_adapter(runtime)` (`"claude-code"`/`"opencode"`/`"codex"`) → `harness.Options.Provider`.

---

## 5. Checkpoint JSON compatibility

`DAGState` Go struct **must round-trip** the Python-written `<artifacts>/execution/checkpoint.json` (= `DAGState.model_dump()`), so a build started under Python can resume under Go and vice-versa. Requirements:
- JSON tags = exact snake_case field names (schema inventory `DAGState` list). No `omitempty`.
- `all_issues: []map[string]any`, `levels: [][]string`, `merge_results`/`integration_test_results`/`accumulated_debt`/`adaptation_history`: `[]map[string]any`, `workspace_manifest: map[string]any` (nullable → pointer or `json.RawMessage`).
- Nested typed lists (`completed_issues`/`failed_issues: []IssueResult`, `replan_history: []ReplanDecision`) need their own faithful tags + default-seeding.
- `max_replans` default 2 (non-zero) via `UnmarshalJSON`.
- Save points identical: init, before each level (record `in_flight_issues`), after barrier, after split/replan, final (`dag/checkpoint.go`). `resume_build` reconstructs the minimal `plan_result` from the checkpoint exactly as `app.py:2109-2121`.
- Per-issue iteration state file `<artifacts>/execution/iterations/<build_id?>/<issue>.json` (`coding_loop.py:47-75`) — same path & shape (`coding/artifacts.go`).
- **Contract test:** a Go unit test loads a golden checkpoint.json produced by the Python code and asserts `Unmarshal → Marshal` is semantically identical (key set + values), and that a Python-written checkpoint drives a correct resume.

---

## 6. Config with validators (`config`)

`BuildConfig`, `ExecutionConfig`, `FastBuildConfig` — Go structs + a `Load*(raw map[string]any) (*T, error)` constructor per type that reproduces the pydantic validators **including exact error messages** (tests assert them; §9 of inventory). Because pydantic uses `ConfigDict(extra="forbid")`, unknown keys are hard errors — mirror by decoding with `json.Decoder.DisallowUnknownFields()` **after** the explicit legacy-key scan (so the legacy hint wins over a generic "unknown field").

**Legacy-key rejection (`_reject_legacy_config_keys`) — reproduce verbatim strings:**
- group key in `models` (`planning`/`coding`/`orchestration`/`lightweight`): `Legacy model group key '<k>' is not supported in V2. Use flat role keys: <hint>.`
- legacy `*_model`/known model key in `models`: `Legacy model key '<k>' is not supported in V2. Use '<hint>'.`
- top-level legacy keys (`ai_provider`,`preset`,`model`,`<role>_model`): `Legacy config keys are not supported in V2: '<key>' -> '<equivalent>', ...` (each hit formatted `'<key>' -> '<equivalent>'`).
- `_validate_flat_models`: non-dict → `models must be an object mapping role keys to model strings`; unknown keys → `Unknown model keys: <...>. Valid keys: <...>`.

**`_normalize_repos` (BuildConfig, after-validator):**
- `Specify either 'repo_url' (single-repo shorthand) or 'repos' (multi-repo list), not both.`
- `Exactly one RepoSpec with role='primary' is required; found <n>.`
- `Duplicate repo_url values are not allowed in 'repos'.`
- else synthesize `[RepoSpec{RepoURL:repo_url, Role:"primary"}]`; backfill `repo_url` from primary.
- `RepoSpec` field validators: role ∈ `{primary,dependency}` → `role must be 'primary' or 'dependency', got <v>`; repo_url scheme → `repo_url must be an HTTP(S) or SSH git URL, got <v>`.

**Properties/methods to port:** `ai_provider`, `primary_repo`, `resolved_models()`, `to_execution_config_dict()` (exact key subset in the inventory), `ExecutionConfig._model_for` + the 17 `*_model` accessors (compute `_resolved_models` once at load into a `map[string]string`). `FastBuildConfig`: `_default_fast_runtime` (no OpenRouter auto-detect), `fast_resolve_models` (roles `pm/coder/verifier/git`; unknown-key error string).

---

## 7. Prompts (`prompts`)

26 modules → 26 Go files, **verbatim** ported strings + template functions. Conventions (from the prompt inventory):
- Execution-role modules export a module-level `SYSTEM_PROMPT` string const + a `<role>_task_prompt(...) string` builder. Go: `const XxxSystemPrompt = ` + backtick raw string; `func XxxTaskPrompt(...) string`.
- Planning-role modules additionally export `<role>_prompts(...) (system, task)` returning a pair. Go: `func XxxPrompts(...) (system, task string)`.
- **Keyword-only Python params** (`architect_prompts`, `ci_fixer_task_prompt`, `pm_task_prompt`, `sprint_planner_*`, `tech_lead_*`, `environment_scout_task_prompt`, `github_pr_task_prompt`, `pr_resolver_task_prompt`, `fast_planner_task_prompt`, etc.) → Go **options struct** param (`func PMTaskPrompt(o PMTaskPromptOpts) string`) to preserve named-arg call sites and defaults. Positional-param builders → ordinary Go params.
- `prompts/_utils.py:workspace_context_block(manifest *WorkspaceManifest) string` — the pervasive multi-repo helper; port to `prompts/utils.go`. Returns `""` when manifest nil or ≤1 repo.
- `prompts/workspace.py` (the Workspace Setup/Cleanup **role** module, not a helper): `SETUP_SYSTEM_PROMPT`, `CLEANUP_SYSTEM_PROMPT`, `workspace_setup_task_prompt(...)`, `workspace_cleanup_task_prompt(...)`.
- `fast/prompts.py` → `prompts/fast.go`: `FAST_PLANNER_SYSTEM_PROMPT` + `FastPlannerTaskPrompt(o)`.

Backtick raw strings can't contain backticks; where a prompt contains a backtick, splice with `"` + "`" + `"` concatenation. Ported strings must be **character-identical** (the LLM behavior is the product) — a golden test diffs each Go const against the Python string.

Template interpolation: Python uses f-strings; Go uses `fmt.Sprintf` or `text/template`. Prefer `fmt.Sprintf` for simple interpolations, `text/template` for the list/loop-heavy ones (e.g. workspace block, issue lists) — but the **rendered output must match** the Python f-string output exactly (trailing newlines, bullet formatting). Golden tests compare rendered prompts for representative inputs.

---

## 8. Reasoner registration & the call graph

- `node/node.go`: `agent.New(agent.Config{NodeID: env("NODE_ID","swe-planner"), Version:"1.0.0", AgentFieldURL: env("AGENTFIELD_SERVER","http://localhost:8080"), Token: env("AGENTFIELD_API_KEY"), ListenAddress: ":"+env("PORT","8003"), HarnessConfig: ...})`. Read env → Config in `main()` (Go SDK reads no env itself — gap report). `HarnessConfig` sets provider/model/turns **per-call** anyway via `harnessx.Run` overrides, so the agent-level default is just a floor.
- `node/register.go`: imperative `app.RegisterReasoner("<name>", handler, opts...)` for **every** reasoner, with the **exact Python names**: orchestrators `build,plan,execute,resolve,resume_build`; roles `run_product_manager,run_environment_scout,run_architect,run_tech_lead,run_sprint_planner,run_issue_writer,run_git_init,run_workspace_setup,run_coder,run_qa,run_code_reviewer,run_qa_synthesizer,run_retry_advisor,run_issue_advisor,run_replanner,run_verifier,generate_fix_issues,run_merger,run_integration_tester,run_workspace_cleanup,run_repo_finalize,run_github_pr,run_ci_watcher,run_ci_fixer,run_pr_resolver`. Set `WithTags("swe-planner")` (router-equivalent). Set `WithAcceptsWebhook`/`WithTriggers` where the Python router does.
- `swe-fast` node (`cmd/swe-fast/main.go`): NODE_ID default `swe-fast`, PORT default `8004`; registers `build` (fast build), `fast_plan_tasks`, `fast_execute_tasks`, `fast_verify`, the 7 delegating wrappers (`run_git_init,run_coder,run_verifier,run_repo_finalize,run_github_pr,run_ci_watcher,run_ci_fixer`), **and** mounts the full role+orchestrator set (Python `fast/app.py:39` includes `swe_af.reasoners.router`). Tag `swe-fast`.
- **Input binding:** Go handlers receive `map[string]any`. Provide `afx.Bind[T](input) (T, error)` = `json.Marshal(input)` → `json.Unmarshal(&T)` (applies default-seeding via `UnmarshalJSON`). Each handler binds its typed input, and reads the **same param names** Python declares (so the async API body is byte-compatible). Handler return = the struct (or `map[string]any`) whose JSON keys match the pydantic `model_dump()` — SDK `writeJSON` serializes it.
- **Every cross-reasoner call** = `raw, err := app.Call(ctx, "swe-planner.run_coder", input); out, err := envelope.UnwrapCallResult(raw, "run_coder")` — threading the request `ctx` so DAG lineage (parent execution id) is set. The injected `call_fn` in coding_loop/dag_executor becomes a `func(ctx, target string, input map[string]any) (map[string]any, error)` = a thin closure over `app.Call`+unwrap.

---

## 9. Gap workarounds (decisions)

| Gap | Decision |
|---|---|
| **HITL `Agent.Pause()` missing** | ~~Poll-based (`RequestApproval` sets `waiting` + `WaitForApproval` backoff), hax called via HTTP directly (§4.6). No SDK change.~~ **Superseded (`feat/go-sdk-parity`):** `agent.Pause()` now exists; HITL uses it via the `hitl.Pauser` seam (webhook-resumed). Merge-gated on the SDK PR + Dockerfile `AGENTFIELD_SDK_REF` bump. |
| **ReasonerFailed-with-result carrier missing** | App-side POST of `status=failed`+`result` before returning the error; CP's unconditional result-persist + the resultless SDK re-post preserves it (§4.5). No SDK change. |
| **Harness cost/usage (`cost_usd`) missing on Go `Result`** | **Drop** — SWE-AF has no cost-tracking code of its own; cost lives in the SDK/CP layer and Go `Metrics` simply omits it. No behavior depends on it. (If later needed, a 1-line `parseJSONOutput` SDK patch extracts the field — recommend as a separate upstream PR, not part of this port.) **Update (`feat/go-sdk-parity`):** `harness.Result.CostUSD` now exists and flows through `harnessx` untouched; SWE-AF still consumes no cost (verified — no `cost_usd` reader in `swe_af/`), so this stays a no-op. |
| **Incremental `schema_mode`** | **Not ported.** Go single-shot + `SchemaMaxRetries` + `BuildFollowupPrompt` is the harness's only mode. Documented known difference; PRD/Architecture are the largest schemas — if their harness output proves flaky, revisit (the schema-retry loop already re-prompts). **Update (`feat/go-sdk-parity`):** `harness.Options.SchemaMode` (`single`/`incremental`/`auto`) now exists. SWE-AF leaves it unset → `single`, matching Python's *effective* default (swe_af never passes `schema_mode`; the Python SDK resolves `None → "single"`), so no behavior change was made. |
| **Codex harness patch** | **Not needed.** Go's harness applies the file-write structured-output protocol uniformly to all providers, and `codex.go` parses `codex exec --json` natively; the fatal-error regexes already cover codex auth/model mismatches. Skip `codex_harness_patch.py`. (Optional `harnessx/codex_strict.go` later only if codex structured-output quality demands strict-JSON-schema rewriting.) |
| **CP package install is Python-only (`af install`)** | **Docker/binary deployment** path (§10). `agentfield-package.yaml` `entrypoint.start` can't launch a Go binary via the Python installer; document the Docker path and keep the manifest for metadata (node_id, port, healthcheck, user_environment). Adding Go support to `installer.go` is a CP feature, out of scope. |
| **No env auto-config in Go SDK** | Read all env (`NODE_ID`,`PORT`,`AGENTFIELD_SERVER`,`AGENTFIELD_API_KEY`, all runtime/model/HITL/gh vars) in `main()` → Config (inventory §8.2). |
| **`/api/v1` base-URL note convention** | Verify a note lands in the smoke test; adjust `Config.AgentFieldURL` accordingly (§4.4). |

---

## 10. Packaging

- **`go/Dockerfile`** — multi-stage. Builder: `golang:1.23` (CP prereq is Go 1.23+), clone agentfield at pinned `AGENTFIELD_SDK_REF`, `go build ./cmd/swe-planner` + `./cmd/swe-fast`. Runtime: `debian:bookworm-slim` (or `python:3.12-slim` only if a runtime still needs it — it doesn't) with **git, curl, jq, gh CLI, and OpenCode CLI** installed (agents still shell out to `git`/`gh`, and `opencode`/`claude`/`codex` binaries per runtime). Copy the two static binaries. `mkdir -p /workspaces && chmod 777`. `ENV PORT=8003`, `EXPOSE 8003`, `CMD ["/usr/local/bin/swe-planner"]`. A second stage/image or `NODE_ID`/`PORT` override runs `swe-fast` on 8004.
  - **Claude Code binary** still required for `claude-code` runtime (`npm i -g @anthropic-ai/claude-code`); OpenCode for `open_code`; Codex for `codex`. Same external-CLI surface as the Python image — the harness is a subprocess shell-out in both languages.
- **docker-compose** — reuse `SWE-AF/docker-compose.yml` structure: `control-plane` (:8080), optional `build-db`, `swe-agent` (build `go/`, :8003, `NODE_ID=swe-planner`), `swe-fast` (:8004, `NODE_ID=swe-fast` + fast CMD). Volumes `agentfield-data`, `workspaces`. `docker-compose.local.yml` agent-only via `host.docker.internal:8080`.
- **`agentfield-package.yaml`** — keep for metadata (node_id, default_port 8003, healthcheck `/health`, `user_environment` require-one-of ANTHROPIC/OPENROUTER + required GH_TOKEN). Add a note that `entrypoint.start` is a placeholder for the Python installer; Go nodes deploy via Docker/compose/Railway, not `af install` (documented, per gap §8).
- **`go/Makefile`** targets: `build` (`go build ./...`), `vet` (`go vet ./...`), `test` (`go test ./...`), `check` (vet+test), `run-planner`, `run-fast`, `lint` (`golangci-lint run` if available). CI adds a Go job running `go build ./... && go vet ./... && go test ./...` on Go 1.23 (per full-test-suite-before-push: run the literal CI steps locally before any PR).
- **Python tests stay** — the Python CI job (`make check`) is untouched; add the Go job alongside it.

---

## 11. Testing strategy

Per the validation-contract rule: contracts (behaviors) first, tests derived from contracts, code last.

### (a) Go unit tests — derived from the behavioral contracts (inventory §9)
For each module, a **Validation Contract** (behaviors, not implementation). Examples (full per-module list lives in the breakdown tasks):
- **config/legacy-keys:** *Given a config with `models.planning`, `Load` returns an error whose message is exactly `Legacy model group key 'planning' is not supported in V2. Use flat role keys: models.pm, models.architect, models.tech_lead, models.sprint_planner.`* (derive one test per legacy path + `_normalize_repos` error + RepoSpec validators). Maps to `test_model_config.py`, `test_multi_repo_schemas.py`.
- **schemas/defaults:** *A `CoderResult` unmarshaled from `{}` has `complete==true`, `tests_passed==nil`.* *`IssueGuidance` from `{}` has `needs_new_tests==true`, `estimated_scope=="medium"`.* One test per non-zero-default field. Maps to `test_malformed_responses.py` intent.
- **envelope:** *An envelope with `status:"failed", error_message:"credit balance is too low"` → `UnwrapCallResult` returns a `*FatalHarnessError`.* *`status:"succeeded", result:{...}` → returns the inner result.* Maps to envelope contract.
- **fatal:** *Each of the 13 patterns matches; a benign message does not.* Maps to `test_fatal_error.py`.
- **dagutil:** *`recompute_levels` on a diamond DAG returns the correct level partition; a cycle returns an error.* *`find_downstream` returns transitive dependents.* *`apply_replan` removes/updates/adds and resets `current_level=0`.* Maps to `test_planner_pipeline.py` level assertions.
- **coding loop:** *`_detect_stuck_loop` fires on 3 identical-feedback iterations.* *Default path = 2 harness calls; flagged path = 4.* Maps to `test_coding_loop*.py`.
- **checkpoint:** *A golden Python-written `checkpoint.json` round-trips (§5).* Maps to `test_execute_workspace_manifest_*`, resume tests.
- **runtimex/config resolution:** *`resolve_runtime_models` precedence; `claude_code` qa_synthesizer=`haiku`; OpenRouter-only env → `open_code`.* Maps to `test_runtime_provider_routing.py`, `test_model_config.py`.
- **prompts (golden):** each Go const == Python string; rendered task prompts == Python f-string output for representative inputs. Maps to `test_workspace_context_block.py`, `test_web_search_guardrail.py`.
- **hitl:** *`_parse_approval_result` decision→status mapping; budget bounds re-invocation to 2.* Maps to `test_ask_user.py`, `test_environment_scout.py`.
- **empty-build guard:** *Build with 0/N completed & 0 merged → `PostFailedWithResult` invoked with the BuildResult and returns an error.* Maps to `test_empty_build_guard.py`.

Test mechanics: mock the `call_fn` closure and the harness (inject a `harnessRunner` interface) so role/loop/dag logic is exercised without live subprocesses — same as the Python tests mock `app.call`/`router.harness`. Table-driven Go tests.

### (b) Black-box functional test plan (docker compose, live CP + Go node)
- Bring up `control-plane` + Go `swe-agent`/`swe-fast` via compose. Assert `/health` on 8003/8004; assert the node registers (CP `/discover`).
- **API byte-compat:** `POST /api/v1/execute/async/swe-planner.build` with a fixed goal against a tiny repo; poll the execution; assert the result JSON **key set** matches Python `BuildResult.model_dump()` (snake_case: `plan_result,dag_state,verification,success,summary,pr_results,ci_gate_results,pr_url`).
- **DAG parity:** after a `plan` run, query the CP execution graph and assert the child-node set (`run_product_manager`, `run_architect`, `run_tech_lead`, `run_sprint_planner`, per-issue `run_issue_writer`) and parent-child edges match a Python reference run node-for-node (this is the acceptance test for constraint #2).
- **ReasonerFailed:** force an empty build; assert the CP execution record shows `status=failed` **and** a non-null `result` **and** an `error` message simultaneously.
- **HITL:** with `HAX_API_KEY` set, trigger a PM ask-user; assert the execution goes to `waiting`, submit via CP, assert resume.
- These are **new** tests (nothing in Python `tests/` hits live HTTP). Write them in Go (`go/test/functional/`) gated behind a `-tags functional` build tag so they don't run in the unit CI job.

### (c) Python tests stay untouched and passing
The Python `make check` job is unchanged; the port lives under `go/` and does not touch `swe_af/`. CI runs both jobs.

---

## 12. Open verification items (confirm during Wave 0/1, cheap)
1. `agent.Config` exact field names (`AgentFieldURL` vs `AgentFieldServer`, `ListenAddress`, `Token`, `HarnessConfig`) — read `sdk/go/agent/agent.go:524-605` before writing `node.go`.
2. The SDK's post-return status write when a handler returns an error is `failed` (not `error`/`succeeded`) and failed→failed is idempotent — confirm via the functional ReasonerFailed test (§4.5).
3. hax `FormBuilder.to_payload()` exact JSON keys — read `hax/form_builder.py` before writing `hitl/hax_client.go`.
4. Whether `app.Note` needs `/api/v1` appended for the running CP (§4.4) — confirm with a smoke note.
5. `agent.RegisterReasoner` option names (`WithTags`,`WithAcceptsWebhook`,`WithTriggers`,`WithInputSchema`) — read `sdk/go/agent/agent.go:107-302` + `agent_register.go`.

# SWE-AF Go Port — Parallel Work Breakdown

Companion to `design-go-port.md` (design authority) + the two research reports + Python source. Every task references the design doc by section (§) and the Python source it ports. **All Go paths are under `SWE-AF/go/`.**

## Rules for every task
- **File ownership is exclusive within a wave.** No two concurrent tasks touch the same file. Package boundaries = ownership boundaries (design §1.3 uses package-per-concern precisely so waves are disjoint).
- **Acceptance gate (every code task):** `go build ./... && go vet ./...` clean from `go/`, and the named unit tests pass. A task is not done until its own package compiles and its tests are green. (Do NOT run the whole suite if downstream packages don't exist yet — build/test only the packages the task owns; the wiring wave does the full build.)
- **Parity discipline:** port names, field tags, defaults, error strings, prompt text **verbatim** (constraints #1–#4). When in doubt, match Python output byte-for-byte over "cleaner" Go.
- **Validation contract first** (per the validation-contract rule): each task's spec lists behaviors; write tests from behaviors, not from your own code.
- Thread `ctx` through everything (DAG lineage + cancellation, design §3, §8).

---

## Wave 0 — Scaffold (1 task, blocking everything)

### T0 — Module scaffold
- **Owns:** `go/go.mod`, `go/Makefile`, `go/cmd/swe-planner/main.go` (stub), `go/cmd/swe-fast/main.go` (stub), `go/internal/afx/bind.go`, `go/internal/afx/note.go`, `go/doc.go`, `<workspace>/go.work` (dev workspace).
- **Depends:** none.
- **Spec:** Create module `github.com/Agent-Field/SWE-AF/go` (design §1.1). Add `require`+`replace` for `github.com/Agent-Field/agentfield/sdk/go` → `../agentfield/sdk/go` (§1.2); add `github.com/invopop/jsonschema` and `golang.org/x/sync`. Create `go.work` at repo-parent listing `./SWE-AF/go` and `./agentfield/sdk/go`. `main.go` stubs: read env → `agent.Config`, call `agent.New`, `agent.Run(ctx)` — verify against `sdk/go/agent/agent.go:524-605` for exact Config field names (design §12.1). `afx.Bind[T](input map[string]any) (T, error)` = marshal→unmarshal (§8). `afx` note helpers if `app.Note` needs base-URL reconciliation (§4.4). Makefile targets `build/vet/test/check/run-planner/run-fast` (§10).
- **Acceptance:** `go build ./...` compiles (empty node that registers nothing and starts). `go vet ./...` clean.
- **Size:** ~250 lines.

---

## Wave 1 — Independent foundation (parallel; each a distinct package)

### T1.1 — schemas: data & result models + enums + defaults
- **Owns:** `go/internal/schemas/` — `enums.go`, `planning.go` (reasoners/schemas.py), `execution.go` (execution/schemas.py result/data models), `fast.go` (fast/schemas.py models), `askuser.go` (hitl AskUserForm/Field/Response), `scout.go` (ScoutResult), `services.go` (ServiceCredentialSpec + KNOWN_SERVICES), `defaults.go` (all `defaultXxx()` + `UnmarshalJSON`).
- **Depends:** T0.
- **Spec:** Port EVERY Pydantic model from the schema inventory as a Go struct (design §2). JSON tags = exact snake_case, **no `omitempty`**. Enums `AdvisorAction/IssueOutcome/ReplanAction/QASynthesisAction` as typed string consts (exact values). Optional-object/tri-state → pointers (`*bool`, `*T`); untyped `list[dict]`/`dict` → `[]map[string]any`/`map[string]any`. Implement `UnmarshalJSON`+`defaultXxx()` for every struct in the design §2.2 non-zero-default list. Do NOT put config models here (that's T2.1) — only data/result models. `KNOWN_SERVICES` = the 9 entries verbatim (service_name/env_var/mint_url/signal_files). `WorkspaceManifest.primary_repo` method.
- **Validation contract:** `CoderResult{}`→`complete==true,tests_passed==nil`; `IssueGuidance{}`→`needs_new_tests==true,estimated_scope=="medium"`; `IssueResult{}`→`attempts==1`; `AskUserForm{}`→`submit_label=="Submit"`; enum round-trips to the exact string; a full `DAGState` round-trips (marshal→unmarshal→marshal identical). Tests: `schemas_defaults_test.go`, `schemas_roundtrip_test.go`.
- **Size:** ~1200 lines (largest data task; if >1500, split `execution.go` result models from data models into T1.1a/b — but keep `defaults.go` with whichever, disjoint).

### T1.2 — runtimex
- **Owns:** `go/internal/runtimex/providers.go` (+ `providers_test.go`).
- **Depends:** T0.
- **Spec:** Port `runtime/providers.py` verbatim (design §4.7): `RUNTIME_VALUES`, `NormalizeRuntimeProvider` (aliases → canonical; unsupported → error), `RuntimeToHarnessProvider` (`claude_code→"claude"`), `RuntimeToHarnessAdapter` (`claude_code→"claude-code"`). Note the provider/adapter asymmetry.
- **Validation contract:** `claude`/`claude-code`→`claude_code`; `opencode`→`open_code`; adapter vs provider strings differ for claude only; unsupported → error with the exact message.
- **Size:** ~120 lines.

### T1.3 — fatal + envelope
- **Owns:** `go/internal/fatal/fatal.go`, `go/internal/envelope/envelope.go` (+ tests).
- **Depends:** T0. (fatal is standalone; envelope imports fatal — same task, no cross-task edge.)
- **Spec:** Port `fatal_error.py` (13 regexes, `FatalHarnessError` error type wrapping `OriginalMessage`, `IsFatalError`, `CheckFatalHarnessError(*harness.Result)` reading `IsError`/`ErrorMessage`) and `envelope.py` (`UnwrapCallResult(map[string]any,label)`; envelope-key set; failed-status → `*FatalHarnessError` or `RuntimeError`-equivalent) — design §4.2, §4.3.
- **Validation contract:** each fatal pattern matches (case-insensitive), benign msg doesn't; envelope `status:"failed",error_message:"credit balance is too low"`→`*FatalHarnessError`; unwrapped dict passes through; `result` extracted when present.
- **Size:** ~200 lines.

### T1.4 — hitl credential store
- **Owns:** `go/internal/hitl/credentials_store.go` (+ test). (Other hitl files land in T2.4/T3.x — this file is disjoint.)
- **Depends:** T0.
- **Spec:** Port `credentials_store.py` (design §3, §4.1): process-local `map[string]map[string]string` keyed by run_id guarded by `sync.Mutex`; `Store/Get/ClearScopedCredentials(runID)`, `InjectCredentialsIntoEnv(base map[string]string, runID) map[string]string` (scoped creds override base). Never persisted.
- **Validation contract:** stored creds override base env on inject; clear removes them; unknown run_id → base unchanged.
- **Size:** ~120 lines.

### T1.5 — reasonerfail carrier
- **Owns:** `go/internal/reasonerfail/reasonerfail.go` (+ test with a stub HTTP server).
- **Depends:** T0.
- **Spec:** `PostFailedWithResult(ctx, app, result any, message string) error` (design §4.5): `agent.ExecutionContextFrom(ctx).ExecutionID`; POST `{AgentFieldURL}/api/v1/executions/{id}/status` body `{status:"failed",result:<obj>,error:message,completed_at:<now>}`; return `errors.New(message)`. Result must marshal to a JSON object.
- **Validation contract:** POSTs the exact body shape to the exact path; returns a non-nil error carrying `message`; if `ExecutionID` empty, still returns the error (best-effort POST skipped).
- **Size:** ~120 lines.

---

## Wave 2 — Second-order foundation (parallel; depend on Wave 1)

### T2.1 — config
- **Owns:** `go/internal/config/` — `buildconfig.go`, `executionconfig.go`, `fastconfig.go`, `resolve.go` (model/runtime resolution), `legacy.go` (rejection), `config_test.go`.
- **Depends:** T1.1 (schemas: RepoSpec), T1.2 (runtimex).
- **Spec:** Port `BuildConfig`/`ExecutionConfig`/`FastBuildConfig` + `Load*(raw map[string]any)(*T,error)` (design §6): legacy-key rejection with **verbatim error strings**, `_normalize_repos` (verbatim strings), `DisallowUnknownFields` after legacy scan (extra="forbid"). Resolution functions verbatim (design §4.7): `_default_runtime`, `_openrouter_only_env`, `_codex_*`, `_default_model_from_env`, `_default_planning_model`, `resolve_runtime_models`, `ROLE_TO_MODEL_FIELD` (17), `_RUNTIME_BASE_MODELS`. Properties: `AIProvider`, `PrimaryRepo`, `ResolvedModels()`, `ToExecutionConfigDict()` (exact key subset), ExecutionConfig 17 `*_model` accessors. FastBuildConfig `_default_fast_runtime` + `fast_resolve_models`.
- **Validation contract (maps to `test_model_config.py`, `test_multi_repo_schemas.py`):** each legacy key → its exact error string; both-`repo_url`-and-`repos` → exact error; missing/duplicate primary → exact errors; role-model resolution precedence (runtime base < env < `models.default` < `models.<role>`); `claude_code` qa_synthesizer=`haiku`; OpenRouter-only env → `open_code`; `SWE_CODEX_AUTH_MODE` paths; unknown model key error. Table-driven env-var tests (use `t.Setenv`).
- **Size:** ~900 lines.

### T2.2 — harnessx
- **Owns:** `go/internal/harnessx/` — `run.go` (generic `Run[T]`), `schema.go` (invopop cache), `harnessx_test.go`.
- **Depends:** T1.1 (schemas), T1.3 (fatal), T1.4 (credstore).
- **Spec:** `schemaFor[T]() map[string]any` via invopop (cache in `sync.Map`; §2.3). `Run[T](ctx, app, prompt, opts) (*T, *harness.Result, error)` (§4.1): inject scoped creds into `opts.Env` (via credstore + `ExecutionContextFrom(ctx).RunID`), call `app.Harness(ctx, prompt, schema, &dest, opts)`, `CheckFatalHarnessError` (return `*FatalHarnessError` up), and on `Result.Parsed==nil` signal the caller to use its fallback (return `dest` seeded by `UnmarshalJSON` defaults + the result so caller inspects `IsError`). Map role→`harness.Options{Provider: adapter, Model, MaxTurns, Tools, PermissionMode, SystemPrompt, Cwd, Env}`.
- **Validation contract:** schema for a nested struct emits `$defs`/`items`/`enum`; fatal error message propagates as `*FatalHarnessError`; scoped creds appear in `opts.Env` overriding base. (Harness itself mocked via an interface.)
- **Size:** ~350 lines.

### T2.3 — dagutil
- **Owns:** `go/internal/dagutil/dagutil.go` (+ test).
- **Depends:** T1.1 (schemas).
- **Spec:** Port `dag_utils.py` + the pure helpers from `pipeline.py` (design §1.3): `RecomputeLevels(issues) ([][]string,error)` (Kahn's; cycle→error), `FindDownstream(name, allIssues) map[string]bool` (transitive), `ApplyReplan(state *DAGState, decision ReplanDecision) *DAGState` (remove/update/add, recompute levels, reset current_level=0), `ComputeLevels`, `ValidateFileConflicts`, `AssignSequenceNumbers`, `EnsurePaths`.
- **Validation contract (maps to `test_planner_pipeline.py`):** diamond DAG → correct partition; cycle → error; `FindDownstream` transitivity; `ApplyReplan` resets level & filters completed; file-conflict detection.
- **Size:** ~350 lines.

### T2.4 — hitl core (ask_user + wrapper + hax REST + scout)
- **Owns:** `go/internal/hitl/ask_user.go`, `go/internal/hitl/wrapper.go`, `go/internal/hitl/hax_client.go`, `go/internal/hitl/scout.go`, `go/internal/hitl/services.go` (detect helpers), `hitl_test.go`. (credentials_store.go already owned by T1.4 — disjoint.)
- **Depends:** T1.1 (schemas: AskUser/Scout/Services), T1.5 pattern (approval via SDK client), T1.4 present.
- **Spec:** Port `ask_user.py` + `wrapper.py` (design §4.6). `hax_client.go`: `POST {HAX_SDK_URL}/api/v1/requests` (Bearer HAX_API_KEY, camelCase body, 120s timeout) → `{id,url}`; **read `<hax-sdk>/sdks/python/hax/form_builder.py` to replicate `to_payload()` JSON exactly** (design §12.3). `RequestUserInputAndPause` = build payload → hax create → `client.RequestApproval` (sets `waiting`) → `client.WaitForApproval` (poll) → map to `AskUserResponse` (verbatim decision→status table). `RunWithAskUser` budget-bounded (=2) re-invocation, strips form. `services.go`: `DetectServicesFromRepo`, `KnownServiceSummaryForPrompt`. `build_hax_client_from_env` (nil when HAX_API_KEY unset), `approval_webhook_url`.
  - **Superseded (`feat/go-sdk-parity`):** the `client.RequestApproval` + poll-based `client.WaitForApproval` step is replaced by a single `agent.Pause(...)` call (webhook-resumed) behind a `hitl.Pauser` seam. `RequestUserInputAndPause` = build payload → hax create → `pauser.Pause(ApprovalRequestID,ApprovalRequestURL,ExpiresInHours,ExecutionID)` → map to `AskUserResponse` (same table). Merge-gated on the SDK PR + Dockerfile `AGENTFIELD_SDK_REF` bump.
- **Validation contract (maps to `test_ask_user.py`, `test_environment_scout.py`, `test_hax_create_request_timeout.py`):** decision `approved/request_changes`→`submitted`, `rejected`→`cancelled`, `expired`→`timeout`; values extracted from `Response.values`/`response.values`/feedback-JSON; budget bounds re-invocation to 2; HAX_API_KEY unset → disabled (nil client); create-request timeout → error surfaces. Mock hax HTTP + SDK approval client.
- **Size:** ~700 lines.

### T2.5 — cigate
- **Owns:** `go/internal/cigate/watch.go` (+ test).
- **Depends:** T1.1 (schemas: CIFailedCheck/CIWatchResult).
- **Spec:** Port `ci_gate.py:watch_pr_checks` (design §1.3): deterministic `gh pr checks --json` poller, SHA anchoring (refuse verdict until a check for `head_sha`), log-excerpt fetch, terminal statuses. Honor `ctx.Done()` in the poll loop (§3). Shell out via `exec.CommandContext`.
- **Validation contract (maps to `test_ci_gate.py`):** SHA anchoring refuses stale verdict; failed checks collected with excerpts; `no_checks`/`timed_out`/`error` statuses; ctx cancellation stops polling. Mock `gh` via a fake command runner (inject an `execCommand` seam).
- **Size:** ~350 lines.

### T2.6 — tools/websearch
- **Owns:** `go/internal/tools/websearch.go` (+ test).
- **Depends:** T0.
- **Spec:** Port `tools/web_search.py:maybe_apply_coder_guardrail(systemPrompt) string` — append the Exa guardrail text only when `OPENCODE_ENABLE_EXA=1` and `EXA_API_KEY` set.
- **Validation contract (maps to `test_web_search_guardrail.py`):** guardrail appended only when both env present; unchanged otherwise. Verbatim guardrail text.
- **Size:** ~90 lines.

---

## Wave 3 — Prompts (highly parallel) + role reasoners

### Prompts (parallel; each owns distinct files under `go/internal/prompts/`)
Depends: T1.1 (schemas types used in signatures). Each task: port modules **verbatim** (design §7), options-struct for keyword-only params, golden test per module (Go const == Python string; rendered == Python f-string output).

- **T3.P1 — helpers + planning prompts:** `utils.go` (`_utils.workspace_context_block`), `product_manager.go`, `architect.go`, `tech_lead.go`, `sprint_planner.go`, `environment_scout.go`. (~700 lines)
- **T3.P2 — coding prompts:** `coder.go`, `qa.go`, `code_reviewer.go`, `qa_synthesizer.go`, `verifier.go`, `issue_writer.go`. (~700)
- **T3.P3 — git/workspace prompts:** `workspace.go` (setup+cleanup role prompts), `git_init.go`, `merger.go`, `integration_tester.go`, `repo_finalize.go`, `github_pr.go`. (~650)
- **T3.P4 — advisor/CI/resolve prompts:** `retry_advisor.go`, `issue_advisor.go`, `replanner.go`, `fix_generator.go`, `ci_fixer.go`, `pr_resolver.go`, `fast.go` (fast planner prompt). (~800)
- **Acceptance each:** package `prompts` builds with all files present — **note:** since these share package `prompts`, the package only fully compiles when all four land. Coordinate: each task compiles its own files with `go build ./internal/prompts/` tolerating the others via stubs, OR run P1–P4 as one sequential-owner wave writing to the same package. **Recommendation:** give each prompt task its own subpackage (`prompts/planning`, `prompts/coding`, `prompts/gitops`, `prompts/advisor`) to keep them independently buildable, then a tiny facade re-exports. If a flat `prompts` package is preferred, run P1–P4 sequentially (they're fast) rather than concurrently.
- **Validation contract:** golden string equality + rendered-output equality (maps to `test_workspace_context_block.py`).

### Role reasoners (parallel; each owns distinct files under `go/internal/roles/`)
Depends: T2.2 (harnessx), T2.4 (hitl), config, prompts, schemas. Each role reasoner = a method on a `roles.Node{App *agent.Agent, Cfg ...}` registered by name (design §8). Each calls `harnessx.Run[T]` (or `app.ai`-equivalent for qa_synthesizer) then returns `T.model_dump()`-equivalent map/struct; deterministic fallback on `Parsed==nil`. **Every inter-role call uses `app.Call`+`envelope.UnwrapCallResult`.**

- **T3.R1 — planning roles** (`roles/planning.go`): `run_product_manager` (HITL-wrapped, budget 2), `run_environment_scout` (HITL; excludes `scoped_credentials`, stashes to credstore), `run_architect` (feedback param), `run_tech_lead` (writes `plan/review.json`), `run_sprint_planner` (inline `SprintPlanOutput`). Ports `pipeline.py:158-549`. (~1000)
- **T3.R2 — coding roles** (`roles/coding.go`): `run_coder` (`maybe_apply_coder_guardrail` on system prompt, cwd=worktree), `run_qa`, `run_code_reviewer` (`qa_ran` param), `run_qa_synthesizer` (uses `router.ai`-equivalent = the SDK's direct-LLM path, NOT the coding harness; deterministic fallback). Ports `execution_agents.py` coding rows. (~900)
- **T3.R3 — git/workspace roles** (`roles/gitops.go`): `run_git_init` (retry-fed `previous_error`), `run_workspace_setup` (worktrees + `issue/{build_id}-{seq}-{name}` branches), `run_workspace_cleanup`, `run_merger`, `run_integration_tester`, `run_repo_finalize`, `run_github_pr`. (~1100 — if >1500 split merge/integration into `roles/gitops_merge.go`)
- **T3.R4 — advisor/verify roles** (`roles/advisor.go`): `run_retry_advisor`, `run_issue_advisor` (HITL; fallback ACCEPT_WITH_DEBT), `run_replanner` (HITL), `run_verifier`, `generate_fix_issues`, `run_issue_writer`. (~1100)
- **T3.R5 — CI/resolve roles** (`roles/ci.go`): `run_ci_watcher` (NO LLM — deterministic `cigate.WatchPRChecks`), `run_ci_fixer` (model falls back coder_model), `run_pr_resolver`. (~700)
- **Acceptance each:** the `roles` package compiles (same shared-package caveat as prompts — use subpackages `roles/planning`, `roles/coding`, etc., or sequential). Unit tests mock the harness + `app.Call`. Named registration deferred to Wave 6.
- **Validation contract (maps to `test_planner_pipeline.py`, `test_coding_loop*.py`, `test_environment_scout.py`, `test_planned_issue_target_repo.py`):** each role returns the exact output key set on success; returns its deterministic fallback (not an exception) on `Parsed==nil`; fatal harness error propagates (not swallowed); scout excludes `scoped_credentials` from its return and stores them; HITL-wrapped roles re-invoke on `ask_user_form`.

---

## Wave 4 — Execution engine (depend on roles + dagutil + schemas)

### T4.1 — coding loop
- **Owns:** `go/internal/coding/loop.go`, `coding/memory.go`, `coding/artifacts.go` (+ tests).
- **Depends:** T3.R1–R5 (calls roles via `app.Call`), T2.3 (dagutil), schemas.
- **Spec:** Port `coding_loop.py` (design §1.3, §3): `RunCodingLoop`, `_run_default_path` (coder→reviewer, 2 calls), `_run_flagged_path` (coder→QA∥reviewer→synthesizer, 4 calls; parallel via errgroup), `_detect_stuck_loop` (3-window identical feedback), memory read/write (`_write_memory_on_approve`), per-iteration artifact save (`<artifacts>/coding-loop/<iter>/{coder,qa,review,synthesis}.json`), iteration-state persistence for mid-issue resume. `call_fn` = closure over `app.Call`+unwrap. Path selection from `IssueGuidance.needs_deeper_qa`.
- **Validation contract (maps to `test_coding_loop*.py`, `_regressions`, `_repo_name`):** default=2 harness calls, flagged=4; approve writes memory + returns COMPLETED with review debt as accumulated debt; fix loops feedback; block/stuck → early exit to advisor; iteration artifacts written; `repo_name` propagated. Mock `call_fn`.
- **Size:** ~900.

### T4.2 — DAG executor
- **Owns:** `go/internal/dag/executor.go`, `dag/gates.go`, `dag/worktree.go`, `dag/checkpoint.go`, `dag/replanner_compat.go` (+ tests).
- **Depends:** T4.1 (coding loop), T3.R3 (git/workspace roles via `app.Call`), T2.3 (dagutil), schemas.
- **Spec:** Port `dag_executor.py` (design §3, §5): `RunDAG` level loop with errgroup barrier (`SetLimit(max_concurrent_issues)`), per-issue `context.WithTimeout(agent_timeout_seconds)`, `_execute_single_issue` middle loop (issue-advisor ≤2, retry-advisor ≤2), worktree setup/merge-gate/integration-test/cleanup (background goroutine + WaitGroup awaited before level++), debt gate, split gate, replan gate (ABORT/CONTINUE/MODIFY_DAG/REDUCE_SCOPE), level-failure abort threshold, checkpoints at all Python save points, `_init_all_repos` multi-repo, `_replanner_compat` shim. Honor `ctx.Done()`. Each issue closure translates its own error to `IssueResult(FAILED_*)` and returns nil to the group (design §3).
- **Validation contract (maps to `test_planner_execute.py`, `test_dag_executor_multi_repo.py`, `test_execute_workspace_manifest_*`):** level barrier waits all; concurrency bounded to 3; per-issue timeout → failure path; checkpoint written before/after each level and round-trips a Python golden (§5); split → new issues + parent removed; replan MODIFY_DAG resets level 0; level-failure threshold aborts remaining; cleanup awaited before advancing. Mock coding loop + roles.
- **Size:** ~1400 (split executor/gates if a task exceeds 1500).

---

## Wave 5 — Orchestrators (depend on execution engine + roles)

Each owns a distinct file under `go/internal/orch/`. All register by exact name on swe-planner (Wave 6 wires). All call sub-reasoners via `app.Call`+unwrap (DAG parity).

- **T5.1 — plan** (`orch/plan.go`): port `app.py:1413-1622` — PM→(scout)→architect↔tech_lead review loop→sprint_planner→issue_writers (parallel `asyncio.gather`→errgroup); compute levels; returns `PlanResult` map. HITL plan-approval gate hook (calls T5.6). (~700)
- **T5.2 — execute** (`orch/execute.go`): port `app.py:1625-1682` — thin wrapper that builds `ExecutionConfig`, injects `call_fn`, calls `dag.RunDAG`, returns `DAGState.model_dump()`. (~250)
- **T5.3 — build** (`orch/build.go` + `orch/clone_repos.go`): port `app.py:490-1410` — clone repos (`_clone_repos` multi-repo), call `plan` then `execute` via `app.Call`, verify + fix loop, finalize, CI gate (calls T5.5), assemble `BuildResult`; **empty-build guard** `_is_empty_build` → `reasonerfail.PostFailedWithResult(ctx, app, buildResult, msg)` (design §4.5); credential-store clear in a `defer` (finally). (~1200)
- **T5.4 — resolve** (`orch/resolve.go`): port `app.py:1685-1957` — `_attempt_base_merge`, `run_pr_resolver` via `app.Call`, CI gate, `_post_thread_replies_and_resolve` (`gh api` REST+GraphQL `resolveReviewThread`), committer identity env (`SWE_AF_GIT_*`). (~900)
- **T5.5 — ci-gate loop** (`orch/cigate_loop.go`): port `app.py:_run_ci_gate:224-352` — watch (`run_ci_watcher` via `app.Call`) → `run_ci_fixer` → repush → rewatch, ≤`max_ci_fix_cycles`; terminal statuses verbatim. (~400)
- **T5.6 — approval gate + resume** (`orch/approval_gate.go`, `orch/resume.go`): port `app.py:355-471,759-838` (hax plan-approval, engages only when HAX_API_KEY set) using `hitl` poll primitive; and `app.py:2081-2134` resume_build (load `<artifacts>/execution/checkpoint.json`, reconstruct minimal plan_result, call `execute(resume=true)`). (~500) **Superseded (`feat/go-sdk-parity`):** the poll primitive is replaced by `agent.Pause` via the `hitl.Pauser` seam (`orch.SetPauserProvider`, wired from `node.go`); webhook-resumed. Merge-gated on the SDK PR + Dockerfile ref bump.
- **Depends:** Wave 4 (T5.2,T5.3 need dag), T3 roles, T2.4 hitl, T1.5 reasonerfail, cigate. T5.1–T5.6 are file-disjoint → parallel (each in its own `orch/*.go`; if they must share package-level helpers, put those in `orch/common.go` owned by T5.3 and have others depend on T5.3 landing first — or make T5.3 the first orch task).
- **Validation contract (maps to `test_empty_build_guard.py`, `test_resolve.py`, `test_build_isolation.py`, `test_node_id_isolation.py`, `test_clone_repos*.py`):** empty build → failed+result via carrier; resolve merge-state transitions; build isolation (no cross-build state); node_id read from env; clone handles multi-repo + shorthand.

---

## Wave 6 — Fast mode + wiring

### T6.1 — fast mode
- **Owns:** `go/internal/fast/planner.go`, `fast/executor.go`, `fast/verifier.go`, `fast/build.go`, `fast/wrappers.go` (+ tests). (schemas/fast.go from T1.1, prompts/fast.go from T3.P4.)
- **Depends:** Wave 3 roles (wrappers delegate to them), T2.2 harnessx, config (FastBuildConfig).
- **Spec:** Port `fast/*` (inventory §6): `fast_plan_tasks` (1 LLM call → `FastPlanResult`, deterministic single-task fallback), `fast_execute_tasks` (sequential, per-task 300s timeout, phase 600s → `FastExecutionResult`), `fast_verify`; `build` orchestrator pipeline `run_git_init→fast_plan_tasks→fast_execute_tasks→fast_verify→run_repo_finalize→run_github_pr` (all via `app.Call`); the 7 delegating wrappers (`run_git_init/run_coder/run_verifier/run_repo_finalize/run_github_pr/run_ci_watcher/run_ci_fixer`) that just call the full-pipeline role reasoners.
- **Validation contract (maps to fast tests + `test_multi_repo_smoke.py`):** fast build calls the 6-stage pipeline in order via `app.Call`; task timeout marks `timeout`; planning failure → single-task fallback; wrappers forward to full roles.
- **Size:** ~1000.

### T6.2 — node wiring + main
- **Owns:** `go/internal/node/node.go`, `go/internal/node/register.go`, `go/cmd/swe-planner/main.go` (final), `go/cmd/swe-fast/main.go` (final).
- **Depends:** ALL of Waves 3–6.
- **Spec:** `node.go`: `agent.New` from env→Config (design §8, §12.1). `register.go`: `RegisterReasoner` for **every** reasoner by exact name with `WithTags` (design §8 lists all names) — swe-planner set + swe-fast set (which also mounts the full role/orchestrator set). `main()`s: build node, register, `agent.Run(ctx)` with signal handling.
- **Acceptance:** `go build ./... && go vet ./...` clean across the WHOLE module (first full-module build). `go test ./...` green. The node starts and (against a running CP) registers all reasoners with the exact names.
- **Size:** ~400.

---

## Wave 7 — Packaging + functional tests

### T7.1 — packaging
- **Owns:** `go/Dockerfile`, `SWE-AF/docker-compose.go.yml` (or edit compose to add Go services — **coordinate: do not edit the Python compose in place if Python must keep running; add a `.go.yml`**), `go/Makefile` (finalize), `SWE-AF/agentfield-package.yaml` note (append-only comment).
- **Depends:** T6.2.
- **Spec:** design §10. Multi-stage Dockerfile (Go 1.23 builder cloning agentfield at pinned `AGENTFIELD_SDK_REF`; slim runtime with git+gh+opencode/claude/codex CLIs). Compose wiring for swe-agent(:8003)/swe-fast(:8004). Follow the docker-pip-cache-busting rule analog: pin `AGENTFIELD_SDK_REF` as a build arg so the SDK layer cache-busts on ref change.
- **Acceptance:** `docker build go/` succeeds; `docker compose -f ... up` brings up CP + both nodes; `/health` on 8003/8004 returns 200.
- **Size:** ~250.

### T7.2 — functional/black-box tests
- **Owns:** `go/test/functional/*_test.go` (build-tag `functional`).
- **Depends:** T7.1.
- **Spec:** design §11(b): compose-up, `/health`, registration, API byte-compat on `swe-planner.build` result key set, DAG node/edge parity vs a Python reference run, ReasonerFailed record (status=failed + result + error together), HITL waiting→resume.
- **Acceptance:** `go test -tags functional ./test/functional/` green against a live compose stack.
- **Size:** ~600.

---

## Dependency summary (critical path)
`T0 → {T1.1..T1.5} → {T2.1 config, T2.2 harnessx, T2.3 dagutil, T2.4 hitl, T2.5 cigate, T2.6 tools} → {T3.P* prompts, T3.R* roles} → T4.1 coding → T4.2 dag → {T5.* orchestrators} → {T6.1 fast, T6.2 wiring} → {T7.1 packaging → T7.2 functional}`.

**Critical path** runs through the execution engine: `T0 → T1.1 → T2.2 → T3.R2 → T4.1 → T4.2 → T5.3 → T6.2 → T7.1 → T7.2`. Front-load T1.1 (schemas) and T2.2 (harnessx) — they unblock the widest fan-out (all roles). Prompts (T3.P*) are the widest parallel band and gate only their consuming roles; start them as soon as T1.1 lands.

**Shared-package coordination note:** where multiple parallel tasks would share one Go package (`prompts`, `roles`, `orch`), either (a) split into subpackages so each task's files compile independently (recommended), or (b) serialize those specific tasks. The wave structure above assumes subpackages for `prompts/*` and `roles/*`; `orch` shares `orch/common.go` (owned by T5.3, which lands first).

# SWE-AF (Python) → Go Port: Complete Codebase Inventory

Repo: `SWE-AF` @ `02da7d2` (main). Total `swe_af/` source: **16,346 lines** across 61 files. Package: `swe-af 0.1.0`, Python >= 3.12, deps: `agentfield>=0.1.96`, `pydantic>=2.0`, `claude-agent-sdk==0.1.20` (pinned for a streaming bug: "Unknown message type: rate_limit_event"), `hax-sdk>=0.2.4`, `python-dotenv>=1.0` (`pyproject.toml:5-17`).

---

## 1. API surface / reasoner registrations

### 1.1 Node: `swe-planner` (`swe_af/app.py`)

`app.py:51-59`:
```python
NODE_ID = os.getenv("NODE_ID", "swe-planner")
app = Agent(
    node_id=NODE_ID, version="1.0.0",
    description="Autonomous SWE planning pipeline",
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
    api_key=os.getenv("AGENTFIELD_API_KEY"),
)
app.include_router(router)   # router = AgentRouter(tags=["swe-planner"])  (reasoners/__init__.py:6)
```

Port: **not hardcoded** — `app.run(host="0.0.0.0")` (`app.py:2145`); the SDK reads `PORT` env (control plane assigns it; `af run` relies on this). Docker sets `PORT=8003`. Health endpoint `/health` comes from the AgentField SDK (declared in `agentfield-package.yaml:9` `healthcheck: /health`). All reasoners are async coroutines; sync/async at the HTTP layer is the SDK's concern (`POST /api/v1/execute/async/<node>.<reasoner>` on the control plane).

**Top-level orchestrator reasoners registered directly on `app` (`@app.reasoner()`):**

| Path | Signature (inputs → output) | Location |
|---|---|---|
| `swe-planner.build` | `(goal: str, repo_path: str = "", repo_url: str = "", artifacts_dir: str = ".artifacts", additional_context: str = "", config: dict\|None, execute_fn_target: str = "", max_turns: int = 0, permission_mode: str = "", enable_learning: bool = False) -> dict` (BuildResult.model_dump; raises `ReasonerFailed(result=BuildResult)` for "empty builds": `success=False` and 0 issues ever completed/merged — `app.py:1399-1403`) | `app.py:490-1410` |
| `swe-planner.plan` | `(goal, repo_path, artifacts_dir=".artifacts", additional_context="", max_review_iterations=2, pm_model=None, architect_model=None, tech_lead_model=None, sprint_planner_model=None, issue_writer_model=None, permission_mode="", ai_provider=None, workspace_manifest: dict\|None) -> dict` (PlanResult) | `app.py:1413-1622` |
| `swe-planner.execute` | `(plan_result: dict, repo_path: str, execute_fn_target="", config: dict\|None, git_config: dict\|None, resume: bool = False, build_id="", workspace_manifest: dict\|None) -> dict` (DAGState.model_dump) | `app.py:1625-1682` |
| `swe-planner.resolve` | `(pr_url, pr_number, repo_url, head_branch, base_branch="main", ci_failures: list[dict]\|None, review_comments: list[dict]\|None, goal="", additional_context="", config: dict\|None) -> dict` — `{pr_url, pr_number, head_branch, base_branch, merge_state: "clean"\|"merged"\|"conflict"\|"skipped", resolve_result, ci_gate, thread_replies, summary, success}` | `app.py:1685-1957` |
| `swe-planner.resume_build` | `(repo_path, artifacts_dir=".artifacts", config: dict\|None, git_config: dict\|None) -> dict` — loads `<artifacts>/execution/checkpoint.json`, re-invokes `execute(resume=True)` | `app.py:2081-2134` |

**Agent-role reasoners registered on `router` (`@router.reasoner()` in `reasoners/pipeline.py` + `reasoners/execution_agents.py`)** — full list in §3. All are addressable as `swe-planner.<name>`.

### 1.2 Node: `swe-fast` (`swe_af/fast/app.py`)

- `NODE_ID = os.getenv("NODE_ID", "swe-fast")` (`fast/app.py:24`); `app.run(port=int(os.getenv("PORT", "8004")), host="0.0.0.0")` (`fast/app.py:312`). It also mounts the **full** execution router (`fast/app.py:39` imports `swe_af.reasoners.router`) so `swe-fast` exposes both the fast and full reasoner sets.
- `swe-fast.build(goal, repo_path="", repo_url="", artifacts_dir=".artifacts", additional_context="", config: dict|None) -> dict` (`fast/app.py:58-…`) — pipeline: `run_git_init → fast_plan_tasks → fast_execute_tasks → fast_verify → run_repo_finalize → run_github_pr` (see §6).
- `fast_router = AgentRouter(tags=["swe-fast"])` (`fast/__init__.py:24`) registers: `fast_plan_tasks` (`fast/planner.py:55`), `fast_execute_tasks` (`fast/executor.py:15`), `fast_verify` (`fast/verifier.py:18`) plus **thin delegating wrappers** `run_git_init`, `run_coder`, `run_verifier`, `run_repo_finalize`, `run_github_pr`, `run_ci_watcher`, `run_ci_fixer` (`fast/__init__.py:33-…`) that just call the `execution_agents` implementations.

### 1.3 Webhooks / HITL endpoints

SWE-AF registers **no HTTP endpoints of its own** for HITL. It calls out to hax-sdk (`hax_client.create_request(...)`, synchronous client run in `asyncio.to_thread` with a 120s timeout — `app.py:408-471`) and passes the **control plane's** webhook `"{cp_base_url}/api/v1/webhooks/approval-response"` (`app.py:812`, `hitl/ask_user.py:58`), then blocks via the SDK primitive `await app.pause(approval_request_id=..., approval_request_url=..., expires_in_hours=...)` (`app.py:834-838`). The approval result object has `.approved`, `.changes_requested`, `.decision`, `.feedback`, `.approval_request_id`. Plan-approval gate engages only when `HAX_API_KEY` is set (`app.py:759-761`).

### 1.4 Cross-cutting: harness monkey-patch

`app.py:80-93` — `app.harness` is wrapped so **every** harness call merges scout-negotiated credentials into the subprocess env (`inject_credentials_into_env(base_env, run_id)`), scoped credentials winning over `os.environ`. A Go port must replicate this env-injection layer.

---

## 2. Module map

| File | LOC | Purpose / key contents | Key deps |
|---|---|---|---|
| `swe_af/app.py` | 2149 | swe-planner Agent, build/plan/execute/resolve/resume_build, CI gate loop (`_run_ci_gate` :224), hax approval formatting (:355), `_clone_repos` multi-repo (:96), `_attempt_base_merge` (:1960), `_post_thread_replies_and_resolve` via `gh api` (:2003), `_is_empty_build` (:474) | agentfield (`Agent`, `ReasonerFailed`), hax, subprocess git/gh |
| `swe_af/__main__.py` / `fast/__main__.py` | 5/4 | `python -m swe_af` → `app.main()`; `python -m swe_af.fast` → fast app | — |
| `reasoners/__init__.py` | 11 | `router = AgentRouter(tags=["swe-planner"])`; applies `apply_codex_harness_patch()`; imports execution_agents + pipeline for registration side effects | agentfield `AgentRouter` |
| `reasoners/pipeline.py` | 549 | Planning-role reasoners (PM, scout, architect, tech lead, sprint planner) + pure helpers `_compute_levels` (Kahn's, :52), `_validate_file_conflicts` (:93), `_assign_sequence_numbers` (:134), `_ensure_paths` (:35) | router.harness/router.ai, prompts, hitl |
| `reasoners/schemas.py` | 106 | Planning Pydantic schemas: `PRD`, `Architecture(+Component,+Decision)`, `ReviewResult`, `IssueGuidance`, `PlannedIssue`, `PlanResult` | pydantic, hitl.AskUserForm |
| `reasoners/execution_agents.py` | 1780 | 17 execution-role reasoners (§3) | router.harness, prompts, execution.schemas, ci_gate |
| `execution/dag_executor.py` | 1801 | `run_dag` engine: level loop, worktree setup/merge/integration-test/cleanup, checkpoints, advisor/replanner integration | dag_utils, coding_loop, schemas |
| `execution/dag_utils.py` | 173 | `recompute_levels` (:10), `find_downstream` (:62), `apply_replan` (:88) | schemas |
| `execution/coding_loop.py` | 895 | Inner loop `run_coding_loop` (:516), `_run_default_path` (:287), `_run_flagged_path` (:361), iteration state persistence (:47-76), shared-memory read/write (:93-264), `_detect_stuck_loop` (:266) | call_fn (app.call) |
| `execution/schemas.py` | 1200 | ALL config + result Pydantic models (§8), runtime/model resolution functions | pydantic |
| `execution/ci_gate.py` | 338 | `watch_pr_checks()` — deterministic `gh pr checks --json` poller, SHA anchoring, log excerpt fetch | subprocess gh |
| `execution/envelope.py` | 66 | `unwrap_call_result()` — unwraps app.call envelope `{status,result}` vs raw dict, raises RuntimeError on failed calls |
| `execution/fatal_error.py` | 88 | `FatalHarnessError`, `check_fatal_harness_error(result)` — detects non-retryable harness failures (auth/billing) and aborts instead of retrying |
| `execution/_replanner_compat.py` | 93 | Back-compat shim for old replanner decision shapes |
| `fast/` (8 files) | ~1000 | Fast mode (§6) |
| `hitl/` (6 files) | ~1050 | Hax-based ask-user forms, scoped credential store (§7) |
| `runtime/providers.py` | 37 | runtime alias normalization → harness provider/adapter strings (§5) |
| `runtime/codex_harness_patch.py` | 301 | Monkey-patch of agentfield harness for Codex structured output (§5) |
| `tools/web_search.py` | 75 | `maybe_apply_coder_guardrail(system_prompt)` — appends a websearch guardrail to the coder system prompt only when `OPENCODE_ENABLE_EXA=1` and `EXA_API_KEY` set (:53-64) |
| `prompts/` (26 files) | ~3900 | One module per role. Two conventions: planning roles export `<role>_prompts(...) -> (system_prompt, task_prompt)` **and** `<role>_task_prompt(...) -> str` (e.g. `product_manager_prompts`/`pm_task_prompt`); execution roles export a module-level `SYSTEM_PROMPT: str` constant **and** `<role>_task_prompt(...) -> str` (see imports at `execution_agents.py:40-85`). `prompts/_utils.py` (44) shared formatting; `prompts/workspace.py` (199) multi-repo workspace context block builders. |

---

## 3. The agent roles (22) — schema, model key, invocation

### 3.1 The invocation primitive (critical for DAG-UI parity)

**Every agent role is a separately registered AgentField reasoner** (`@router.reasoner()`), and every role invocation from an orchestrator goes through **`app.call(f"{NODE_ID}.run_<role>", **kwargs)`** (or the injected `call_fn`, which *is* `app.call` — `app.py:1675`). That cross-reasoner `.call()` is exactly what makes each role appear as a **separate node in the AgentField execution DAG UI**. Evidence:

- `app.py:1453`: `await app.call(f"{NODE_ID}.run_product_manager", ...)`
- `coding_loop.py:617-618`: `call_fn(f"{node_id}.run_coder", ...)`; same for `run_code_reviewer` (:311), `run_qa` (:388), `run_qa_synthesizer` (:465)
- `dag_executor.py`: `run_workspace_setup` (:82,:145), `run_merger` (:264,:340), `run_integration_tester` (:451), `run_workspace_cleanup` (:557), `run_git_init` (:638), `run_issue_advisor` (:846), `run_retry_advisor` (:1080), `run_replanner` (:1269), `run_issue_writer` (:1327)

Inside each role reasoner, the actual LLM work is one call to **`router.harness(...)`** — the AgentField SDK primitive that shells out to the Claude Code / OpenCode / Codex CLI harness (NOT direct `claude_agent_sdk.query` from SWE-AF code; the SDK owns that). Canonical pattern (`pipeline.py:208-218`):

```python
result = await router.harness(
    prompt=task_prompt,
    schema=PRD,                     # Pydantic model → structured output enforcement
    provider=provider,              # "claude-code" | "opencode" | "codex"
    model=model,
    max_turns=max_turns,            # DEFAULT_AGENT_MAX_TURNS = 150
    tools=["Read", "Write", "Glob", "Grep", "Bash"],
    permission_mode=permission_mode or None,
    system_prompt=system_prompt,
    cwd=repo_path,
)
check_fatal_harness_error(result)
return result.parsed.model_dump()   # result.parsed is the schema instance
```

**One exception:** `run_qa_synthesizer` uses **`router.ai(...)`** (plain LLM call, no tool harness): `execution_agents.py:1254-1259` — `await router.ai(task_prompt, system=..., schema=QASynthesisResult, model=model)`. And `run_ci_watcher` uses **no LLM at all** (deterministic `gh` polling). `app.note(...)`/`router.note(...)` calls are timeline annotations only, not DAG nodes.

### 3.2 Role table

All output schemas in `execution/schemas.py` unless noted (planning ones in `reasoners/schemas.py`). Default `model` param is `"sonnet"` except qa_synthesizer (`"haiku"`). `max_turns` default `DEFAULT_AGENT_MAX_TURNS = 150` (`execution/schemas.py:23`). Fallback-on-failure column = what the reasoner returns if the agent errors (never blocks the pipeline).

| # | Role / reasoner | File:line | Prompt module | Output schema (fields) | Tools | Model key | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `run_product_manager` | pipeline.py:158 | prompts/product_manager.py | `PRD(validated_description, acceptance_criteria: list[str], must_have, nice_to_have, out_of_scope, assumptions=[], risks=[], ask_user_form: AskUserForm\|None)` | R,W,Glob,Grep,Bash | `pm_model` | Wrapped in `run_with_ask_user` (HITL, budget 2) |
| 2 | `run_environment_scout` | pipeline.py:240 | prompts/environment_scout.py | `ScoutResult(detected_services: list[ServiceCredentialSpec], scoped_credentials: dict[str,str], skipped_services, summary, ask_user_form)` — **`scoped_credentials` excluded from return**, stashed in in-memory store (:339-354) | R,Glob,Grep,Bash | `pm_model` | Only when HAX_API_KEY set |
| 3 | `run_architect` | pipeline.py:357 | prompts/architect.py | `Architecture(summary, components: list[ArchitectureComponent(name, responsibility, touches_files, depends_on)], interfaces: list[str], decisions: list[ArchitectureDecision(decision, rationale)], file_changes_overview)` | R,W,Glob,Grep,Bash | `architect_model` | `feedback` param for revision loops |
| 4 | `run_tech_lead` | pipeline.py:417 | prompts/tech_lead.py | `ReviewResult(approved: bool, feedback, scope_issues=[], complexity_assessment="appropriate", summary)`; writes `plan/review.json` | R,W,Glob,Grep | `tech_lead_model` | |
| 5 | `run_sprint_planner` | pipeline.py:477 | prompts/sprint_planner.py | inline `SprintPlanOutput(issues: list[PlannedIssue], rationale: str)`; `PlannedIssue(name, title, description, acceptance_criteria, depends_on=[], provides=[], estimated_complexity="medium", files_to_create=[], files_to_modify=[], testing_strategy="", sequence_number: int\|None, guidance: IssueGuidance\|None, target_repo="")`; `IssueGuidance(needs_new_tests=True, estimated_scope="medium", touches_interfaces=False, needs_deeper_qa=False, testing_guidance="", review_focus="", risk_rationale="")` | R,W,Glob,Grep | `sprint_planner_model` | |
| 6 | `run_issue_writer` | execution_agents.py:445 | prompts/issue_writer.py | `IssueWriterOutput` (success flag + path; local class) | R,W,Glob,Grep | `issue_writer_model` | One per issue, run in parallel `asyncio.gather` (app.py:1576-1597) |
| 7 | `run_git_init` | execution_agents.py:599 | prompts/git_init.py | `GitInitResult(mode: "fresh"\|"existing", original_branch, integration_branch, initial_commit_sha, success, error_message="", remote_url="", remote_default_branch="", repo_name="")` | Bash,Write | `git_model` | Retried `git_init_max_retries=3` times by build() with `previous_error` fed back |
| 8 | `run_workspace_setup` | execution_agents.py:682 | prompts/workspace.py | `WorkspaceSetupResult` containing `WorkspaceInfo(issue_name, branch_name, worktree_path)` per issue | Bash,Write | `git_model` | Creates git worktrees + `issue/{build_id}-{seq}-{name}` branches per level |
| 9 | `run_coder` | execution_agents.py:963 | prompts/coder.py | `CoderResult(files_changed=[], summary="", complete=True, iteration_id="", tests_passed: bool\|None, test_summary="", codebase_learnings=[], agent_retro={}, repo_name="")` | R,W,Edit,Bash,Glob,Grep | `coder_model` | system prompt via `maybe_apply_coder_guardrail(CODER_SYSTEM_PROMPT)`; cwd=worktree |
| 10 | `run_qa` | execution_agents.py:1060 | prompts/qa.py | `QAResult(passed, summary="", test_failures=[], coverage_gaps=[], iteration_id="")` | R,W,Edit,Bash,Glob,Grep | `qa_model` | Flagged path only |
| 11 | `run_code_reviewer` | execution_agents.py:1134 | prompts/code_reviewer.py | `CodeReviewResult(approved, summary="", blocking=False, debt_items=[], iteration_id="")` | R,W,Glob,Grep,Bash | `code_reviewer_model` | `qa_ran: bool` param |
| 12 | `run_qa_synthesizer` | execution_agents.py:1216 | prompts/qa_synthesizer.py | `QASynthesisResult(action: QASynthesisAction("approve"\|"fix"\|"block"), summary="", stuck=False, iteration_id="")` | **none — `router.ai`** | `qa_synthesizer_model` (default haiku) | Deterministic fallback :1277-1304 |
| 13 | `run_retry_advisor` | execution_agents.py:124 | prompts/retry_advisor.py | `RetryAdvice(should_retry, diagnosis, strategy, modified_context, confidence=0.5)` | R,W,Glob,Grep,Bash | `retry_advisor_model` | Fallback: should_retry=False |
| 14 | `run_issue_advisor` | execution_agents.py:205 | prompts/issue_advisor.py | `IssueAdvisorDecision(action: AdvisorAction, failure_diagnosis, failure_category, rationale, confidence=0.5, modified_acceptance_criteria=[], dropped_criteria=[], modification_justification="", new_approach="", approach_changes=[], sub_issues: list[SplitIssueSpec], split_rationale="", missing_functionality=[], debt_severity="medium", escalation_reason="", dag_impact="", suggested_restructuring="", downstream_impact="", summary="", ask_user_form)` — AdvisorAction ∈ {retry_modified, retry_approach, split, accept_with_debt, escalate_to_replan} | R,W,Glob,Grep,Bash | `issue_advisor_model` | HITL-wrapped; fallback ACCEPT_WITH_DEBT |
| 15 | `run_replanner` | execution_agents.py:319 | prompts/replanner.py | `ReplanDecision(action: ReplanAction ∈ {CONTINUE, MODIFY_DAG, REDUCE_SCOPE, ABORT}, rationale, updated_issues=[], removed_issue_names=[], skipped_issue_names=[], new_issues=[], summary="", ask_user_form)` | R,W,Glob,Grep,Bash | `replan_model` | HITL-wrapped |
| 16 | `run_verifier` | execution_agents.py:526 | prompts/verifier.py | `VerificationResult(passed, criteria_results: list[CriterionResult(criterion, passed, evidence, issue_name="")], summary, suggested_fixes=[])` | R,W,Glob,Grep,Bash | `verifier_model` | |
| 17 | `generate_fix_issues` | execution_agents.py:1313 | prompts/fix_generator.py | `FixGeneratorOutput` (`fix_issues: list[dict]`, `debt_items: list[dict]`) | R,W,Glob,Grep,Bash | `verifier_model` (passed by build) | |
| 18 | `run_merger` | execution_agents.py:749 | prompts/merger.py | `MergeResult(success, merged_branches, failed_branches, conflict_resolutions=[], merge_commit_sha="", pre_merge_sha="", needs_integration_test: bool, integration_test_rationale="", summary, repo_name="")` | Bash,R,W,Glob,Grep | `merger_model` | |
| 19 | `run_integration_tester` | execution_agents.py:822 | prompts/integration_tester.py | `IntegrationTestResult(passed, tests_written=[], tests_run, tests_passed, tests_failed, failure_details=[], summary)` | Bash,R,W,Glob,Grep | `integration_tester_model` | |
| 20 | `run_workspace_cleanup` | execution_agents.py:897 | prompts/workspace.py | `WorkspaceCleanupResult` | Bash,Write | `git_model` | Background task per level |
| 21 | `run_repo_finalize` | execution_agents.py:1409 | prompts/repo_finalize.py | `RepoFinalizeResult(success, files_removed=[], gitignore_updated=False, summary="")` | Bash,R,W,Glob,Grep | `git_model` | |
| 22 | `run_github_pr` | execution_agents.py:1467 | prompts/github_pr.py | `GitHubPRResult(success, pr_url="", pr_number=0, error_message="")` | Bash,Write | `git_model` | |
| 23 | `run_ci_watcher` | execution_agents.py:1540 | — (no LLM) | `CIWatchResult(status: Literal["passed","failed","timed_out","no_checks","error"], pr_number, elapsed_seconds=0, failed_checks: list[CIFailedCheck(name, workflow, conclusion, details_url, logs_excerpt)], summary="")` | — | — | Pure `gh pr checks` poller via `ci_gate.watch_pr_checks` |
| 24 | `run_ci_fixer` | execution_agents.py:1592 | prompts/ci_fixer.py | `CIFixResult(fixed, files_changed=[], commit_sha="", pushed=False, summary="")` | Bash,R,Edit,W,Glob,Grep | `ci_fixer_model` (falls back to coder_model) | |
| 25 | `run_pr_resolver` | execution_agents.py:1681 | prompts/pr_resolver.py | `PRResolveResult` (fixed, pushed, commit_shas, files_changed, addressed_comments[{comment_id, thread_id, addressed, note}], summary) | Bash,R,Edit,W,Glob,Grep | ci_fixer/coder | Used by `resolve` |

(Marketing "22 roles" ≈ the 17 `ROLE_TO_MODEL_FIELD` keys + workspace/cleanup/pr/watcher/resolver variants above; port all 25 registered reasoners + the 5 orchestrators + 3 fast reasoners + 7 fast wrappers.)

---

## 4. Execution engine (`swe_af/execution/`)

### 4.1 `run_dag` (`dag_executor.py:1352-1801`) — level-scheduled barrier loop

1. **Init**: `_init_dag_state` (:709) builds `DAGState` from `plan_result` (issues, `levels` from the plan). If `resume=True`, `_load_checkpoint(artifacts_dir)` (:700) restores `DAGState` and skips completed levels (:1412-1421). Initial checkpoint saved (:1436). Multi-repo: `_init_all_repos` (:596) runs `run_git_init` per repo.
2. **Shared memory**: an in-process dict behind `_memory_fn(action, key, value)` closure (:1452-1460), enabled when `config.enable_learning`.
3. **Main loop** (`while current_level < len(levels)`, :1464):
   - Filter level to active issues (not done/skipped) (:1467-1476).
   - **Worktree setup**: `_setup_worktrees` → `run_workspace_setup` reasoner creates a git worktree + branch `issue/{build_id}-{NN}-{name}` per issue; enriched `worktree_path`/`branch_name` written back into `dag_state.all_issues` for resume safety (:1489-1501).
   - Set `in_flight_issues`, **checkpoint before execution** (:1503-1505).
   - `_execute_level` (:1121) runs all issues **concurrently** (bounded by `max_concurrent_issues=3` semaphore), each via `_execute_single_issue` (:774) → `run_coding_loop`; barrier = gather. Each issue call is wrapped in `_call_with_timeout(coro, timeout=2700)` (:33).
   - Barrier reached → clear in-flight, **checkpoint** (:1514-1517); append completed/failed/skipped.
   - **Level-failure abort**: if `failed/total >= level_failure_abort_threshold` and >1 failure, skip all remaining levels and break (:1535-1558).
   - **Merge gate**: `_merge_level_branches` (:206) → `run_merger` reasoner (retry once on failure); then `_run_integration_tests` (:392) → `run_integration_tester` if `merge_result.needs_integration_test`; cleanup (`run_workspace_cleanup`) spawned as background `asyncio.create_task` (:1588).
   - **Debt gate** (:1600-1629): `COMPLETED_WITH_DEBT` results append `accumulated_debt` + `adaptation_history`, and inject `debt_notes` into downstream issues (`find_downstream`).
   - **Split gate** (:1631-1676): `FAILED_NEEDS_SPLIT` → synthesize `ReplanDecision(MODIFY_DAG, new_issues=sub_issues, removed=[parent])`, `apply_replan`, write new issue files via `run_issue_writer`.
   - **Replan gate** (:1678-1753): failures with outcome `FAILED_UNRECOVERABLE|FAILED_ESCALATED` → if `enable_replanning` and `replan_count < max_replans`, invoke `run_replanner`. Actions: `ABORT` → break; `CONTINUE` → enrich downstream with failure notes, `_skip_downstream`; `MODIFY_DAG`/`REDUCE_SCOPE` → `apply_replan` (resets `current_level=0`), write issue files, checkpoint, `continue`. Cycle in the new DAG → ValueError → skip downstream instead.
   - Await cleanup, `current_level += 1`.
4. Final worktree sweep (:1763-1784), summary note, final checkpoint, return DAGState.

**Issue state machine** (`IssueOutcome` enum, schemas.py:149): outcomes include `COMPLETED`, `COMPLETED_WITH_DEBT`, `FAILED_RETRYABLE`, `FAILED_UNRECOVERABLE`, `FAILED_ESCALATED`, `FAILED_NEEDS_SPLIT` (plus skipped tracked separately).

**Middle loop** — `_execute_single_issue` (:774): runs the coding loop; on inner-loop exhaustion invokes `run_issue_advisor` (:846) up to `max_advisor_invocations=2`, applying retry_modified/retry_approach (mutate issue, rerun), split (→ FAILED_NEEDS_SPLIT), accept_with_debt (→ COMPLETED_WITH_DEBT), escalate_to_replan (→ FAILED_ESCALATED). Plain exceptions consult `run_retry_advisor` (:1080) up to `max_retries_per_issue=2` with `modified_context` injected.

### 4.2 `dag_utils.py`

- `recompute_levels(issues) -> list[list[str]]` (:10) — Kahn's algorithm (same as `pipeline._compute_levels`, quoted in full at `pipeline.py:52-90`: in-degree map, dependents adjacency, queue-per-level BFS; `ValueError` on cycle).
- `find_downstream(issue_name, all_issues) -> set[str]` (:62) — transitive dependents.
- `apply_replan(dag_state, decision) -> DAGState` (:88) — removes/updates/adds issues, recomputes levels, resets `current_level=0` (completed issues filtered out on level re-entry).

### 4.3 `coding_loop.py` — inner loop (`run_coding_loop` :516)

Path selection from sprint-planner guidance: `needs_deeper_qa=False` → **DEFAULT**: coder → code_reviewer (2 LLM calls, reviewer sole gatekeeper); `True` → **FLAGGED**: coder → (QA ∥ reviewer in parallel) → qa_synthesizer (4 calls). Per iteration (≤ `max_coding_iterations=5`): read memory context → `run_coder` (with accumulated `feedback`) → path branch → action ∈ approve/fix/block. Approve → write memory (`_write_memory_on_approve`: conventions/learnings), return COMPLETED (with debt items from review as accumulated debt). Fix → feedback loops into next iteration. Block or `_detect_stuck_loop` (3-iteration window of repeated identical feedback, :266) → exit early to advisor. Per-iteration artifacts saved to `<artifacts>/coding-loop/<iteration_id>/{coder,qa,review,synthesis}.json` (`_save_artifact` :76); iteration checkpoint per issue `_save_iteration_state` (:59) enables mid-issue resume (:589-600).

### 4.4 Checkpoint format

- **Path**: `<artifacts_dir>/execution/checkpoint.json` (`dag_executor.py:683-685`).
- **Content**: entire `DAGState.model_dump()` as JSON — fields at `schemas.py:276-329`: paths (repo_path, artifacts_dir, prd_path, architecture_path, issues_dir), summaries, `all_issues` (full dicts incl. enriched worktree_path/branch_name), `levels`, `completed_issues/failed_issues: list[IssueResult]`, `skipped_issues`, `in_flight_issues`, `current_level`, `replan_count/replan_history/max_replans`, git fields (`git_integration_branch`, `git_original_branch`, `git_initial_commit`, `git_mode`, `pending/merged/unmerged_branches`, `worktrees_dir`, `build_id`), `merge_results`, `integration_test_results`, `accumulated_debt`, `adaptation_history`, `workspace_manifest`.
- **Save points**: init, before each level (in-flight recorded), after barrier, after split/replan, final. `resume_build` reconstructs a minimal plan_result from the checkpoint (`app.py:2109-2121`).
- Per-issue iteration state: `<artifacts>/execution/iterations/<build_id?>/<issue>.json` (`coding_loop.py:47-75`).

### 4.5 Support files

- `ci_gate.py:watch_pr_checks(repo_path, pr_number, wait_seconds, poll_seconds, head_sha="")` — polls `gh pr checks --json`, anchors to `head_sha` if given (refuses verdict until a check for that SHA is seen), collects `CIFailedCheck` with log excerpts. The **gate loop** (watch → `run_ci_fixer` → repush → rewatch, ≤ `max_ci_fix_cycles=2`) lives in `app.py:_run_ci_gate` (:224-352); terminal statuses: `passed|no_checks|timed_out|error|failed_exhausted|fixer_gave_up|loop_exhausted`.
- `envelope.py:unwrap_call_result` — every `app.call` result goes through `_unwrap(raw, label)`; handles the SDK's result envelope and raises on failure.
- `fatal_error.py` — classify harness errors (auth, quota) as `FatalHarnessError`; every role reasoner calls `check_fatal_harness_error(result)` and re-raises it past retry logic.

---

## 5. Claude runtime integration

SWE-AF does **not** call `claude_agent_sdk` directly for agent work — the **agentfield SDK's `Agent.harness()`** does (SWE-AF depends on `claude-agent-sdk==0.1.20` transitively/for the harness). SWE-AF's contract with the harness is the kwargs shown in §3.1: `prompt`, `system_prompt`, `schema` (Pydantic → structured output enforced by the SDK, `result.parsed` returns the instance; `result.parsed is None` on parse failure), `provider`, `model`, `max_turns` (150), `tools` (Claude Code tool-name allowlist), `permission_mode` (string or None; from `BuildConfig.permission_mode`), `cwd` (repo or worktree — this is the agent-isolation mechanism), `env` (injected by the credentials wrapper, `app.py:83-93`).

**Runtime selection** (`runtime/providers.py:5-37`, `execution/schemas.py:532-664`):
- Canonical runtimes: `("claude_code", "open_code", "codex")`; aliases normalized (`claude`, `claude-code` → `claude_code`; `opencode` → `open_code`).
- `runtime_to_harness_adapter`: `claude_code→"claude-code"`, `open_code→"opencode"`, `codex→"codex"` (the string passed as `provider=` to `router.harness`). `runtime_to_harness_provider` (schemas' `_runtime_to_provider`): `claude→claude`, `open_code→opencode` — used for `ai_provider` fields ("claude"|"opencode"|"codex").
- `_default_runtime()` (schemas.py:584): env-driven — `SWE_DEFAULT_RUNTIME` wins; otherwise **OpenRouter-only environments** (`OPENROUTER_API_KEY` set, no `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, `_openrouter_only_env()` :567) auto-select `open_code`; else `claude_code`. Codex has `_codex_uses_chatgpt_auth()` (:532, `SWE_CODEX_AUTH_MODE`) and `_codex_default_model()` (:554).
- `_default_model_from_env()` (:613): `SWE_DEFAULT_MODEL`; `_default_planning_model()` (:634) resolves planning default per runtime.

**Codex patch** (`runtime/codex_harness_patch.py`, applied at import in `reasoners/__init__.py:4`): monkey-patches the agentfield harness's `build_prompt_suffix` using a `contextvars.ContextVar` (`active_provider`) so claude/opencode calls keep the SDK's "use Write tool" structured-output instruction while codex calls get a Codex-native strict-JSON-schema instruction (`_codex_strict_json_schema` rewrites schemas to strict mode). A Go port targeting only claude_code can skip this; multi-runtime parity needs the equivalent behavior.

**Session/streaming/cost**: no session persistence or explicit streaming/cost-tracking code exists in SWE-AF itself — each harness call is one-shot with `max_turns`; observability is via `note()` and the control plane. Cost/streaming live in the agentfield SDK layer.

---

## 6. Fast mode (`swe_af/fast/`)

Speed-optimized single-pass build on node `swe-fast` (port 8004). Pipeline (`fast/app.py:58` docstring): `run_git_init → fast_plan_tasks → fast_execute_tasks → fast_verify → run_repo_finalize → run_github_pr` — **no** tech-lead loop, no DAG levels, no advisor/replanner/merger/integration tester, no worktrees, no HITL.

- `fast_plan_tasks` (`fast/planner.py:55`): one LLM call → `FastPlanResult(tasks: list[FastTask], rationale="", fallback_used=False)`; `FastTask(name, title, description, acceptance_criteria, files_to_create=[], files_to_modify=[], estimated_minutes=5)`; deterministic single-task fallback if planning fails.
- `fast_execute_tasks` (`fast/executor.py:15`): runs tasks sequentially against the repo (no worktrees), each `run_coder` call bounded by `task_timeout_seconds=300`, whole phase by `build_timeout_seconds=600` → `FastExecutionResult(task_results: list[FastTaskResult(task_name, outcome: "completed"|"failed"|"timeout", files_changed, summary, error)], completed_count, failed_count, timed_out=False)`.
- `fast_verify` (`fast/verifier.py:18`) → `FastVerificationResult(passed, summary="", criteria_results=[], suggested_fixes=[])`.
- `FastBuildConfig` (`fast/schemas.py:111`): `runtime` (default `_default_fast_runtime`), `models: dict|None`, `max_tasks=10`, `task_timeout_seconds=300`, `build_timeout_seconds=600`, `enable_github_pr=True`, `github_pr_base=""`, `permission_mode=""`, `repo_url=""`, `agent_max_turns=50`. Model roles: `_FAST_ROLES = ("pm_model", "coder_model", "verifier_model", "git_model")` (`fast/schemas.py:36`), resolved by `fast_resolve_models`.
- `FastBuildResult(plan_result, execution_result, verification, success, summary, pr_url="")`.
- `prompts.py` — fast-specific compact prompts. Delegating wrappers (§1.2) reuse full-pipeline agents.

---

## 7. hitl/, runtime/, tools/

**hitl/** — human-in-the-loop on Hax:
- `ask_user.py` (511): `AskUserForm(fields: list[AskUserFormField], ...)` / `AskUserFormField` / `AskUserResponse` schemas; `build_hax_client_from_env()` (:27, returns None when `HAX_API_KEY` unset → HITL globally disabled); `approval_webhook_url(app)` (:44) → `{AGENTFIELD_SERVER}/api/v1/webhooks/approval-response`; `request_user_input_and_pause` (:432): creates a hax `form-builder` request (thread + timeout wrapper :294), then `await app.pause(...)` until user submits; `_parse_approval_result_to_response` maps the pause result to `AskUserResponse`.
- `wrapper.py`: `run_with_ask_user(reasoner_fn, reasoner_kwargs, app, hax_client, budget: AskUserBudget(remaining=2), webhook_url, note_label)` (:81) — invokes the agent; if the parsed result carries a non-null `ask_user_form`, pauses for user input, appends answers to `prior_user_responses`, re-invokes; bounded by budget; strips the form from the final result. Used by PM, scout, issue_advisor, replanner.
- `credentials_store.py`: process-local `_STORE: dict[str, dict[str, str]]` keyed by run_id; `store/get/clear_scoped_credentials`, `inject_credentials_into_env(base_env, run_id)` (merged into every harness call; cleared in build()'s `finally`, `app.py:1407-1410`). **Never persisted or returned** (scout excludes it from its response).
- `scout_schema.py`: `ScoutResult` (§3.2 row 2). `services.py`: `KNOWN_SERVICES` registry, `ServiceCredentialSpec`, `detect_services_from_repo`.

**runtime/** — §5. **tools/** — `web_search.py` only: `maybe_apply_coder_guardrail` (Exa websearch guardrail text appended to coder system prompt when `OPENCODE_ENABLE_EXA=1` + `EXA_API_KEY`).

---

## 8. Config & env

### 8.1 `BuildConfig` (`execution/schemas.py:772-…`) — all fields/defaults

```
runtime: Literal["claude_code","open_code","codex"] = _default_runtime()
models: dict[str,str] | None = None          # {"default": ..., "<role>": ...}
max_review_iterations=2; max_plan_revision_iterations=2
max_retries_per_issue=2; max_replans=2; enable_replanning=True
max_verify_fix_cycles=1
git_init_max_retries=3; git_init_retry_delay=1.0
max_integration_test_retries=1; enable_integration_testing=True
max_coding_iterations=5; agent_max_turns=150 (DEFAULT_AGENT_MAX_TURNS)
execute_fn_target=""; permission_mode=""
repo_url=""; repos: list[RepoSpec]=[]        # RepoSpec(repo_url="", repo_path="", role, branch="", sparse_paths=[], mount_point="", create_pr=True)
enable_github_pr=True; github_pr_base=""
check_ci=True; max_ci_fix_cycles=2; ci_wait_seconds=1500; ci_poll_seconds=30; ci_startup_grace_seconds=30
agent_timeout_seconds=2700
max_advisor_invocations=2; enable_issue_advisor=True
enable_learning (env-derived default)
max_concurrent_issues=3                      # 0 = unlimited
level_failure_abort_threshold (float)
approval_expires_in_hours=72
```
Validators: `_validate_v2_keys` / `_reject_legacy_config_keys` (:666) reject legacy `*_model` top-level keys with migration hints; `_normalize_repos` merges `repo_url` shorthand into `repos`. Properties: `ai_provider` ("claude"|"opencode"|"codex"), `primary_repo`, `resolved_models() -> dict[str,str]` (flat `<role>_model` map), `to_execution_config_dict()` (subset handed to `execute`, incl. `enable_learning`). `ExecutionConfig` = the execute-side mirror with the flat `*_model` fields, timeouts, and loop limits.

**Model resolution order** (`resolve_runtime_models` :715): runtime's base default → `models.default` → `models.<role>` (17 roles in `ROLE_TO_MODEL_FIELD`, schemas.py:~470: pm, architect, tech_lead, sprint_planner, coder, qa, code_reviewer, qa_synthesizer, replan, retry_advisor, issue_writer, issue_advisor, verifier, git, merger, integration_tester, ci_fixer). Env `SWE_DEFAULT_MODEL` supplies the runtime-default override.

### 8.2 Environment variables (all read anywhere)

| Var | Where / purpose |
|---|---|
| `NODE_ID` | app.py:51, fast/app.py:24 — node identity (swe-planner / swe-fast) |
| `PORT` | SDK-read for main app; fast app default 8004 |
| `AGENTFIELD_SERVER` | app.py:57 (default `http://localhost:8080`) |
| `AGENTFIELD_API_KEY` | app.py:58 |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` | claude_code runtime auth; runtime auto-selection (:567-611) |
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ZHIPU_API_KEY` | open_code runtime |
| `SWE_DEFAULT_RUNTIME`, `SWE_DEFAULT_MODEL` | runtime/model defaults |
| `SWE_CODEX_AUTH_MODE` | codex auth mode (:532) |
| `GH_TOKEN` | gh/git auth (PRs, clone, thread replies) |
| `HAX_API_KEY`, `HAX_SDK_URL` (default `http://localhost:3000`), `HAX_SENDER_KEY`, `HAX_SENDER_NAME`, `AGENTFIELD_APPROVAL_USER_ID` | HITL approval gate (app.py:759-817) |
| `OPENCODE_ENABLE_EXA`, `EXA_API_KEY` | web-search guardrail |
| `SWE_AF_GIT_EMAIL` / `SWE_AF_GIT_NAME` | committer identity in resolve (app.py:1797-1798), defaults `swe-af@users.noreply.github.com` / `SWE-AF` |
| `HARNESS_MODEL` | Dockerfile:67 opencode default (`openrouter/moonshotai/kimi-k2.6`) |
| `DATABASE_URL_TEST`, `BUILD_DB_*` | optional build-db for generated apps (.env.example) |

---

## 9. Tests

`make test` = `pytest tests/ -x -q`; `make check` = test + `python -m compileall -q swe_af/` (Makefile). CI (`.github/workflows/ci.yml`): single job, Python 3.12, install deps, `make check`. No lint step in CI.

37 test files, 12,389 lines, all under `tests/`. Categorization for a Go port:

**Pure Python-internal (would NOT port):** `test_codex_harness_patch.py` (monkey-patch), `test_main_entrypoint.py`, `test_runtime_provider_routing.py` (string mapping — trivial to rewrite), `test_hax_create_request_timeout.py` (asyncio.to_thread semantics), `test_conftest_*`/`test_mock_fixture_*` (fixture plumbing), `test_fatal_error.py`, `test_malformed_responses.py` (Pydantic parse behavior).

**Behavioral/contract tests (port the *contracts*; they exercise reasoner logic against mocked `app.call`/harness, so they don't run against a Go binary as-is, but their assertions define parity):** `test_planner_pipeline.py`, `test_planner_execute.py`, `test_coding_loop.py` (+ `_regressions`, `_repo_name`), `test_dag_executor_multi_repo.py`, `test_execute_workspace_manifest_*`, `test_clone_repos*.py`, `test_ci_gate.py`, `test_resolve.py`, `test_model_config.py`, `test_multi_repo_{schemas,prompts,execution_gaps,integration,smoke}.py`, `test_build_isolation.py`, `test_node_id_isolation.py`, `test_empty_build_guard.py`, `test_planned_issue_target_repo.py`, `test_environment_scout.py`, `test_ask_user.py`, `test_workspace_context_block.py`, `test_web_search_guardrail.py`.

**Could run against a Go implementation unchanged (artifact-level):** `test_dockerfile.py`, `test_deployment_docs.py` (validate Dockerfile/docs contents, language-agnostic).

Nothing in `tests/` hits live HTTP endpoints — true black-box API tests would have to be written fresh for the port (recommend deriving them from the §1/§3 reasoner signatures).

---

## 10. Packaging / deploy — what a Go equivalent must replicate

- **`agentfield-package.yaml`**: `config_version: v1`, `name: swe-planner`, `entrypoint.start: python -m swe_af` (→ Go binary), `entrypoint.healthcheck: /health`, `agent_node.node_id: swe-planner`, `default_port: 8003`; `user_environment`: require-one-of `ANTHROPIC_API_KEY`|`OPENROUTER_API_KEY`, required `GH_TOKEN`, optional `SWE_DEFAULT_MODEL`/`AGENTFIELD_SERVER`/`AGENTFIELD_API_KEY`/`NODE_ID`. This drives `af install`.
- **Dockerfile**: `FROM python:3.12-slim`; installs git, curl, jq, **gh CLI**, **OpenCode CLI** (`/root/.opencode/bin` on PATH, `HARNESS_MODEL` default, opencode config written), git identity `SWE-AF`, `uv pip install -r requirements.txt`, `mkdir /workspaces` (chmod 777), `EXPOSE 8003`, `ENV PORT=8003`, `CMD ["python", "-m", "swe_af"]`. Go image still needs git+gh+(opencode if multi-runtime) since agents shell out to them.
- **docker-compose.yml** (full stack): `control-plane` (agentfield/control-plane:latest, :8080), `build-db` (postgres:16, optional test DB for built apps), `swe-agent` (build ., :8003), `swe-fast` (build ., :8004, different CMD/NODE_ID); volumes `agentfield-data`, `workspaces`.
- **docker-compose.local.yml**: agent-only, control plane on host via `host.docker.internal:8080`.
- **pyproject entry points**: `swe-af = swe_af.app:main`, `swe-fast = swe_af.fast.app:main` → two Go binaries (or one with a mode flag).

### Key port-parity gotchas

1. **DAG UI parity hinges on `app.call` fan-out**: every role must remain a registered reasoner invoked via the AgentField call API (Go SDK equivalent), not an in-process function call — otherwise the execution-graph UI loses per-role nodes. `note()` tags are the timeline annotations.
2. **Structured output**: `router.harness(schema=Model)` + `result.parsed` (None on parse failure → each reasoner has an explicit deterministic fallback). Go needs JSON-schema-enforced harness output + per-role fallback structs.
3. **`ReasonerFailed(message, result=...)`** semantics (failed execution that still carries a structured result) — `app.py:26-38, 1399-1403`.
4. **Envelope unwrapping** (`_unwrap`) on every call result.
5. **Checkpoint JSON is the resume contract** — Go structs must serialize DAGState compatibly if cross-version resume matters.
6. Subprocess surface: `git` (clone/fetch/checkout/merge/worktree/rev-parse), `gh` (`pr view/edit/checks`, `api` REST + GraphQL `resolveReviewThread`).
7. The harness monkey-patches (scoped-credential env injection; codex prompt-suffix patch) are Python-side hooks that must become first-class options in the Go harness layer.

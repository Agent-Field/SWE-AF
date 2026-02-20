"""Core DAG execution loop with self-healing replanning."""

from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from typing import Callable

from swe_af.execution.dag_utils import apply_replan, find_downstream
from swe_af.execution.envelope import unwrap_call_result
from swe_af.execution.schemas import (
    AdvisorAction,
    DAGState,
    ExecutionConfig,
    IssueAdaptation,
    IssueOutcome,
    IssueResult,
    LevelResult,
    ReplanAction,
    ReplanDecision,
    WorkspaceManifest,
)

# ---------------------------------------------------------------------------
# Timeout wrapper
# ---------------------------------------------------------------------------


async def _call_with_timeout(coro, timeout: int = 2700, label: str = ""):
    """Wrap a coroutine with asyncio.wait_for timeout.

    Args:
        coro: An awaitable coroutine (already called, e.g. ``call_fn(...)``).
        timeout: Seconds before raising TimeoutError.
        label: Human-readable label for error messages.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Agent call '{label}' timed out after {timeout}s"
        )


# ---------------------------------------------------------------------------
# Git worktree helpers (all delegate to reasoners via call_fn)
# ---------------------------------------------------------------------------


async def _setup_worktrees(
    dag_state: DAGState,
    active_issues: list[dict],
    call_fn: Callable,
    node_id: str,
    config: ExecutionConfig,
    note_fn: Callable | None = None,
    build_id: str = "",
) -> list[dict]:
    """Create git worktrees for parallel issue isolation.

    Returns the active_issues list with worktree_path and branch_name injected.
    """
    if note_fn:
        names = [i.get("name", "?") for i in active_issues]
        note_fn(
            f"Setting up worktrees for {names}",
            tags=["execution", "worktree_setup", "start"],
        )

    setup = await call_fn(
        f"{node_id}.run_workspace_setup",
        repo_path=dag_state.repo_path,
        integration_branch=dag_state.git_integration_branch,
        issues=active_issues,
        worktrees_dir=dag_state.worktrees_dir,
        artifacts_dir=dag_state.artifacts_dir,
        level=dag_state.current_level,
        model=config.git_model,
        ai_provider=config.ai_provider,
        build_id=build_id,
    )

    if not setup.get("success"):
        if note_fn:
            note_fn("Worktree setup failed — issues will run without isolation",
                     tags=["execution", "worktree_setup", "error"])
        return active_issues

    # Build worktree map with canonical name as key.
    # The workspace agent may return issue_name as "value-copy-trait" (correct)
    # or "01-value-copy-trait" (with sequence prefix). Handle both.
    worktree_map: dict[str, dict] = {}
    for w in setup.get("workspaces", []):
        raw_name = w["issue_name"]
        worktree_map[raw_name] = w
        # Also strip leading NN- sequence prefix for fallback matching
        stripped = re.sub(r"^\d{2}-", "", raw_name)
        if stripped != raw_name:
            worktree_map[stripped] = w

    enriched = []
    for issue in active_issues:
        ws = worktree_map.get(issue["name"])
        if ws:
            enriched.append({
                **issue,
                "worktree_path": ws["worktree_path"],
                "branch_name": ws["branch_name"],
                "integration_branch": dag_state.git_integration_branch,
            })
        else:
            enriched.append(issue)

    if note_fn:
        note_fn(
            f"Worktree setup complete: {len(worktree_map)} worktrees",
            tags=["execution", "worktree_setup", "complete"],
        )

    return enriched


async def _merge_level_branches(
    dag_state: DAGState,
    level_result: LevelResult,
    call_fn: Callable,
    node_id: str,
    config: ExecutionConfig,
    issue_by_name: dict,
    file_conflicts: list[dict],
    note_fn: Callable | None = None,
) -> dict | None:
    """Merge completed branches into the integration branch.

    Single-repo path (workspace_manifest is None): merges all completed
    branches into dag_state.git_integration_branch as before.

    Multi-repo path (workspace_manifest is set): groups completed IssueResults
    by repo_name and dispatches one run_merger call per repo concurrently via
    asyncio.gather.

    Returns the MergeResult dict, or None if nothing to merge.
    """
    # --- Single-repo path: unchanged ---
    if dag_state.workspace_manifest is None:
        completed_branches = []
        for r in level_result.completed:
            if r.branch_name:
                issue_desc = issue_by_name.get(r.issue_name, {}).get("description", "")
                completed_branches.append({
                    "branch_name": r.branch_name,
                    "issue_name": r.issue_name,
                    "result_summary": r.result_summary,
                    "files_changed": r.files_changed,
                    "issue_description": issue_desc,
                })

        if not completed_branches:
            return None

        if note_fn:
            branch_names = [b["branch_name"] for b in completed_branches]
            note_fn(
                f"Merging {len(completed_branches)} branches: {branch_names}",
                tags=["execution", "merge", "start"],
            )

        merge_kwargs = dict(
            repo_path=dag_state.repo_path,
            integration_branch=dag_state.git_integration_branch,
            branches_to_merge=completed_branches,
            file_conflicts=file_conflicts,
            prd_summary=dag_state.prd_summary,
            architecture_summary=dag_state.architecture_summary,
            artifacts_dir=dag_state.artifacts_dir,
            level=level_result.level_index,
            model=config.merger_model,
            ai_provider=config.ai_provider,
        )

        merge_result = await call_fn(f"{node_id}.run_merger", **merge_kwargs)

        # Retry once on failure (handles transient auth errors, network blips)
        if not merge_result.get("success") and merge_result.get("failed_branches"):
            if note_fn:
                note_fn(
                    "Merge failed, retrying once...",
                    tags=["execution", "merge", "retry"],
                )
            merge_result = await call_fn(f"{node_id}.run_merger", **merge_kwargs)

        dag_state.merge_results.append(merge_result)
        for b in merge_result.get("merged_branches", []):
            if b not in dag_state.merged_branches:
                dag_state.merged_branches.append(b)

        # Record unmerged branches for visibility
        for b in merge_result.get("failed_branches", []):
            if b not in dag_state.unmerged_branches:
                dag_state.unmerged_branches.append(b)

        if note_fn:
            note_fn(
                f"Merge complete: merged={merge_result.get('merged_branches', [])}, "
                f"failed={merge_result.get('failed_branches', [])}",
                tags=["execution", "merge", "complete"],
            )

        return merge_result

    # --- Multi-repo path: group by repo_name, one merger call per repo ---
    manifest = WorkspaceManifest(**dag_state.workspace_manifest)

    # Group IssueResults by repo_name (fall back to primary if empty)
    by_repo: dict[str, list] = {}
    for r in level_result.completed:
        if r.branch_name:
            repo = r.repo_name or manifest.primary_repo_name
            by_repo.setdefault(repo, []).append(r)

    if not by_repo:
        return None

    if note_fn:
        note_fn(
            f"Multi-repo merge: dispatching to {list(by_repo.keys())}",
            tags=["execution", "merge", "start"],
        )

    async def _call_merger_for_repo(
        repo_name: str,
        issue_results: list,
    ) -> dict:
        """Invoke run_merger for a single repo."""
        ws_repo = next(
            (r for r in manifest.repos if r.repo_name == repo_name), None
        )
        if ws_repo is None or ws_repo.git_init_result is None:
            return {"success": False, "merged_branches": [], "failed_branches": []}

        git_init = ws_repo.git_init_result
        integration_branch = git_init.get("integration_branch", "")
        if not integration_branch:
            return {"success": False, "merged_branches": [], "failed_branches": []}

        branches_to_merge = [
            {
                "branch_name": r.branch_name,
                "issue_name": r.issue_name,
                "result_summary": r.result_summary,
                "files_changed": r.files_changed,
                "issue_description": issue_by_name.get(r.issue_name, {}).get("description", ""),
            }
            for r in issue_results
        ]

        result = await call_fn(
            f"{node_id}.run_merger",
            repo_path=ws_repo.absolute_path,
            integration_branch=integration_branch,
            branches_to_merge=branches_to_merge,
            file_conflicts=file_conflicts,
            prd_summary=dag_state.prd_summary,
            architecture_summary=dag_state.architecture_summary,
            artifacts_dir=dag_state.artifacts_dir,
            level=level_result.level_index,
            model=config.merger_model,
            ai_provider=config.ai_provider,
        )
        return result

    # Dispatch all repo merges concurrently
    tasks = [
        _call_merger_for_repo(repo_name, issues)
        for repo_name, issues in by_repo.items()
    ]
    repo_names = list(by_repo.keys())
    results = await asyncio.gather(*tasks, return_exceptions=True)

    last_good: dict | None = None
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            if note_fn:
                note_fn(
                    f"Merge failed for repo '{repo_names[i]}': {result}",
                    tags=["execution", "merge", "error"],
                )
            continue
        dag_state.merge_results.append({**result, "repo_name": repo_names[i]})
        for b in result.get("merged_branches", []):
            if b not in dag_state.merged_branches:
                dag_state.merged_branches.append(b)
        for b in result.get("failed_branches", []):
            if b not in dag_state.unmerged_branches:
                dag_state.unmerged_branches.append(b)
        if result.get("success"):
            last_good = result

    if note_fn:
        note_fn(
            f"Multi-repo merge complete: repos={repo_names}, "
            f"merged={dag_state.merged_branches}",
            tags=["execution", "merge", "complete"],
        )

    return last_good


async def _run_integration_tests(
    dag_state: DAGState,
    merge_result: dict,
    level_result: LevelResult,
    call_fn: Callable,
    node_id: str,
    config: ExecutionConfig,
    issue_by_name: dict,
    note_fn: Callable | None = None,
) -> dict | None:
    """Run integration tests after a merge if needed.

    Returns the IntegrationTestResult dict, or None if skipped.
    """
    if not merge_result.get("needs_integration_test"):
        return None
    if not config.enable_integration_testing:
        return None

    merged_branches = []
    for r in level_result.completed:
        if r.branch_name and r.branch_name in merge_result.get("merged_branches", []):
            merged_branches.append({
                "branch_name": r.branch_name,
                "issue_name": r.issue_name,
                "result_summary": r.result_summary,
                "files_changed": r.files_changed,
            })

    if note_fn:
        note_fn(
            "Running integration tests",
            tags=["execution", "integration_test", "start"],
        )

    test_result = None
    for attempt in range(config.max_integration_test_retries + 1):
        test_result = await call_fn(
            f"{node_id}.run_integration_tester",
            repo_path=dag_state.repo_path,
            integration_branch=dag_state.git_integration_branch,
            merged_branches=merged_branches,
            prd_summary=dag_state.prd_summary,
            architecture_summary=dag_state.architecture_summary,
            conflict_resolutions=merge_result.get("conflict_resolutions", []),
            artifacts_dir=dag_state.artifacts_dir,
            level=level_result.level_index,
            model=config.integration_tester_model,
            ai_provider=config.ai_provider,
        )
        if test_result.get("passed"):
            break
        if note_fn and attempt < config.max_integration_test_retries:
            note_fn(
                f"Integration test failed (attempt {attempt + 1}), retrying...",
                tags=["execution", "integration_test", "retry"],
            )

    if test_result:
        dag_state.integration_test_results.append(test_result)
        if note_fn:
            note_fn(
                f"Integration test {'passed' if test_result.get('passed') else 'failed'}: "
                f"{test_result.get('summary', '')}",
                tags=["execution", "integration_test", "complete"],
            )

    return test_result


async def _cleanup_worktrees(
    dag_state: DAGState,
    branches_to_clean: list[str],
    call_fn: Callable,
    node_id: str,
    note_fn: Callable | None = None,
    level: int = 0,
    model: str = "sonnet",
    ai_provider: str = "claude",
) -> None:
    """Remove worktrees and clean up branches after merge.

    Retries once on failure to handle transient issues (locked worktrees, etc.).
    """
    if not branches_to_clean:
        return

    if note_fn:
        note_fn(
            f"Cleaning up {len(branches_to_clean)} worktrees",
            tags=["execution", "worktree_cleanup", "start"],
        )

    for attempt in range(2):  # up to 1 retry
        try:
            result = await call_fn(
                f"{node_id}.run_workspace_cleanup",
                repo_path=dag_state.repo_path,
                worktrees_dir=dag_state.worktrees_dir,
                branches_to_clean=branches_to_clean,
                artifacts_dir=dag_state.artifacts_dir,
                level=level,
                model=model,
                ai_provider=ai_provider,
            )
            if result.get("success"):
                if note_fn:
                    note_fn(
                        f"Worktree cleanup complete: {result.get('cleaned', [])}",
                        tags=["execution", "worktree_cleanup", "complete"],
                    )
                return
            # Cleanup agent reported failure
            if note_fn:
                note_fn(
                    f"Worktree cleanup returned success=false (attempt {attempt + 1}/2), "
                    f"cleaned={result.get('cleaned', [])}",
                    tags=["execution", "worktree_cleanup", "warning"],
                )
        except Exception as e:
            if note_fn:
                note_fn(
                    f"Worktree cleanup error (attempt {attempt + 1}/2): {e}",
                    tags=["execution", "worktree_cleanup", "error"],
                )

    if note_fn:
        note_fn(
            f"Worktree cleanup failed after retries for: {branches_to_clean}",
            tags=["execution", "worktree_cleanup", "error"],
        )


async def _init_all_repos(
    dag_state: DAGState,
    call_fn: Callable,
    node_id: str,
    git_model: str,
    ai_provider: str,
    permission_mode: str = "",
    build_id: str = "",
    note_fn: Callable | None = None,
) -> None:
    """Run git_init concurrently for all repos in workspace_manifest.

    When ``dag_state.workspace_manifest`` is None (single-repo path), returns
    immediately without invoking call_fn.

    After successful completion, ``dag_state.workspace_manifest`` is updated
    with ``git_init_result`` populated on each WorkspaceRepo entry.

    Args:
        dag_state: Mutated in-place. dag_state.workspace_manifest must be a
            dict (WorkspaceManifest.model_dump()) set before calling.
        call_fn: AgentField call function for invoking run_git_init.
        node_id: e.g. 'swe-planner'.
        git_model: Resolved model string. Source: config.git_model.
        ai_provider: 'claude' or 'opencode'. Source: config.ai_provider.
        permission_mode: Forwarded to run_git_init.
        build_id: Forwarded to run_git_init for branch namespace isolation.
        note_fn: Optional callback for observability.
    """
    if dag_state.workspace_manifest is None:
        return  # single-repo path: git_init already ran in build()

    manifest = WorkspaceManifest(**dag_state.workspace_manifest)

    if note_fn:
        repo_names = [r.repo_name for r in manifest.repos]
        note_fn(
            f"Initialising git for {len(manifest.repos)} repos: {repo_names}",
            tags=["execution", "init_all_repos", "start"],
        )

    async def _init_one(ws_repo) -> tuple[str, dict]:
        result = await call_fn(
            f"{node_id}.run_git_init",
            repo_path=ws_repo.absolute_path,
            goal="",  # goal not needed for dependency repos
            artifacts_dir=dag_state.artifacts_dir,
            model=git_model,
            permission_mode=permission_mode,
            ai_provider=ai_provider,
            build_id=build_id,
        )
        return ws_repo.repo_name, result

    tasks = [_init_one(r) for r in manifest.repos]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Write results back (WorkspaceRepo is mutable: model_config = ConfigDict(frozen=False))
    repo_map = {r.repo_name: r for r in manifest.repos}
    for item in results:
        if isinstance(item, Exception):
            # Non-fatal: single-repo git_init failure is already non-fatal
            if note_fn:
                note_fn(
                    f"git_init failed for a repo (non-fatal): {item}",
                    tags=["execution", "init_all_repos", "error"],
                )
            continue
        name, git_init_dict = item
        if name in repo_map:
            repo_map[name].git_init_result = git_init_dict

    # Replace dag_state manifest dict with updated version
    dag_state.workspace_manifest = manifest.model_dump()

    if note_fn:
        note_fn(
            "git init complete for all repos",
            tags=["execution", "init_all_repos", "complete"],
        )


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def _checkpoint_path(dag_state: DAGState) -> str:
    """Return the path to the checkpoint file, or empty string if no artifacts_dir."""
    return os.path.join(dag_state.artifacts_dir, "execution", "checkpoint.json") if dag_state.artifacts_dir else ""


def _save_checkpoint(dag_state: DAGState, note_fn: Callable | None = None) -> None:
    """Persist DAGState to a checkpoint file for crash recovery."""
    path = _checkpoint_path(dag_state)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(dag_state.model_dump(), f, indent=2, default=str)
    if note_fn:
        note_fn(f"Checkpoint saved: level={dag_state.current_level}", tags=["execution", "checkpoint"])


def _load_checkpoint(artifacts_dir: str) -> DAGState | None:
    """Load DAGState from a checkpoint file, or return None if not found."""
    path = os.path.join(artifacts_dir, "execution", "checkpoint.json")
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return DAGState(**json.load(f))


def _init_dag_state(
    plan_result: dict, repo_path: str, git_config: dict | None = None, build_id: str = "",
) -> DAGState:
    """Extract DAGState from a PlanResult dict.

    Populates all artifact paths, plan context summaries, and issue/level data
    so the executor and replanner have full context. Optionally populates git
    fields from ``git_config``.
    """
    artifacts_dir = plan_result.get("artifacts_dir", "")

    # Artifact paths
    prd_path = os.path.join(artifacts_dir, "plan", "prd.md") if artifacts_dir else ""
    architecture_path = os.path.join(artifacts_dir, "plan", "architecture.md") if artifacts_dir else ""
    issues_dir = os.path.join(artifacts_dir, "plan", "issues") if artifacts_dir else ""

    # PRD summary: validated_description + acceptance criteria
    prd = plan_result.get("prd", {})
    prd_summary_parts = [prd.get("validated_description", "")]
    ac = prd.get("acceptance_criteria", [])
    if ac:
        prd_summary_parts.append("\nAcceptance Criteria:")
        prd_summary_parts.extend(f"- {c}" for c in ac)
    prd_summary = "\n".join(prd_summary_parts)

    # Architecture summary
    architecture = plan_result.get("architecture", {})
    architecture_summary = architecture.get("summary", "")

    # Issues and levels
    issues = plan_result.get("issues", [])
    # Ensure issues are dicts (they might be Pydantic model instances)
    all_issues = [
        i if isinstance(i, dict) else i.model_dump() if hasattr(i, "model_dump") else dict(i)
        for i in issues
    ]
    levels = plan_result.get("levels", [])

    # Git fields (populated when git workflow is active)
    git_kwargs = {}
    if git_config:
        git_kwargs = {
            "git_integration_branch": git_config.get("integration_branch", ""),
            "git_original_branch": git_config.get("original_branch", ""),
            "git_initial_commit": git_config.get("initial_commit_sha", ""),
            "git_mode": git_config.get("mode", ""),
            "worktrees_dir": os.path.join(repo_path, ".worktrees"),
        }

    return DAGState(
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        prd_path=prd_path,
        architecture_path=architecture_path,
        issues_dir=issues_dir,
        original_plan_summary=plan_result.get("rationale", ""),
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        all_issues=all_issues,
        levels=levels,
        build_id=build_id,
        **git_kwargs,
    )


async def _execute_single_issue(
    issue: dict,
    dag_state: DAGState,
    execute_fn: Callable | None,
    config: ExecutionConfig,
    call_fn: Callable | None = None,
    node_id: str = "swe-planner",
    note_fn: Callable | None = None,
    memory_fn: Callable | None = None,
) -> IssueResult:
    """Execute a single issue with the Issue Advisor adaptation loop.

    When ``execute_fn`` is None and ``call_fn`` is available, uses the
    built-in coding loop. When ``execute_fn`` is provided, uses the external
    coder path.

    On failure, the Issue Advisor (middle loop) analyzes the failure and
    decides how to adapt: retry with modified ACs, retry with different
    approach, accept with debt, split, or escalate to the outer replanner.
    """
    issue_name = issue["name"]
    original_issue = dict(issue)  # preserve original before any modifications
    current_issue = dict(issue)
    adaptations: list[IssueAdaptation] = []
    debt_items: list[dict] = []
    last_result: IssueResult | None = None

    max_advisor = config.max_advisor_invocations if config.enable_issue_advisor else 0

    for advisor_round in range(max_advisor + 1):
        # --- Run the coding loop (or execute_fn) ---
        if execute_fn is None and call_fn is not None:
            from swe_af.execution.coding_loop import run_coding_loop
            result = await run_coding_loop(
                issue=current_issue,
                dag_state=dag_state,
                call_fn=call_fn,
                node_id=node_id,
                config=config,
                note_fn=note_fn,
                memory_fn=memory_fn,
            )
        elif execute_fn is not None:
            result = await _run_execute_fn(
                execute_fn, current_issue, dag_state, config, call_fn,
                node_id, issue_name,
            )
        else:
            raise ValueError("No execute_fn or call_fn — cannot execute issue")

        last_result = result

        # Success — return with any accumulated adaptations/debt
        if result.outcome in (IssueOutcome.COMPLETED, IssueOutcome.COMPLETED_WITH_DEBT):
            result.adaptations = adaptations
            result.debt_items = debt_items
            result.final_acceptance_criteria = current_issue.get("acceptance_criteria", [])
            return result

        # Advisor budget exhausted or disabled — return raw failure
        if advisor_round >= max_advisor or call_fn is None:
            break

        # --- Invoke the Issue Advisor ---
        if note_fn:
            note_fn(
                f"Issue Advisor invocation {advisor_round + 1}/{max_advisor} for {issue_name}",
                tags=["issue_advisor", "invoke", issue_name],
            )

        try:
            advisor_decision = await _call_with_timeout(
                call_fn(
                    f"{node_id}.run_issue_advisor",
                    issue=current_issue,
                    original_issue=original_issue,
                    failure_result=result.model_dump(),
                    iteration_history=result.iteration_history,
                    dag_state_summary={
                        "completed_issues": [r.model_dump() for r in dag_state.completed_issues],
                        "failed_issues": [r.model_dump() for r in dag_state.failed_issues],
                        "prd_summary": dag_state.prd_summary,
                        "architecture_summary": dag_state.architecture_summary,
                        "prd_path": dag_state.prd_path,
                        "architecture_path": dag_state.architecture_path,
                        "issues_dir": dag_state.issues_dir,
                        "artifacts_dir": dag_state.artifacts_dir,
                        "repo_path": dag_state.repo_path,
                    },
                    advisor_invocation=advisor_round + 1,
                    max_advisor_invocations=max_advisor,
                    previous_adaptations=[a.model_dump() for a in adaptations],
                    worktree_path=current_issue.get("worktree_path", dag_state.repo_path),
                    model=config.issue_advisor_model,
                    ai_provider=config.ai_provider,
                ),
                timeout=config.agent_timeout_seconds,
                label=f"issue_advisor:{issue_name}:{advisor_round + 1}",
            )
        except Exception as e:
            if note_fn:
                note_fn(
                    f"Issue Advisor failed for {issue_name}: {e}",
                    tags=["issue_advisor", "error", issue_name],
                )
            break  # advisor failed — return last coding loop result

        action = advisor_decision.get("action", "accept_with_debt")

        if note_fn:
            note_fn(
                f"Issue Advisor decision for {issue_name}: {action}",
                tags=["issue_advisor", "decision", issue_name],
            )

        if action == AdvisorAction.RETRY_MODIFIED.value:
            # Relax ACs, retry coding loop
            adaptation = IssueAdaptation(
                adaptation_type=AdvisorAction.RETRY_MODIFIED,
                original_acceptance_criteria=current_issue.get("acceptance_criteria", []),
                modified_acceptance_criteria=advisor_decision.get("modified_acceptance_criteria", []),
                dropped_criteria=advisor_decision.get("dropped_criteria", []),
                failure_diagnosis=advisor_decision.get("failure_diagnosis", ""),
                rationale=advisor_decision.get("rationale", ""),
                downstream_impact=advisor_decision.get("downstream_impact", ""),
            )
            adaptations.append(adaptation)

            # Record dropped criteria as debt
            for dropped in advisor_decision.get("dropped_criteria", []):
                debt_items.append({
                    "type": "dropped_acceptance_criterion",
                    "criterion": dropped,
                    "issue_name": issue_name,
                    "justification": advisor_decision.get("modification_justification", ""),
                    "severity": "medium",
                })

            current_issue["acceptance_criteria"] = advisor_decision.get(
                "modified_acceptance_criteria",
                current_issue.get("acceptance_criteria", []),
            )
            continue  # re-enter coding loop

        elif action == AdvisorAction.RETRY_APPROACH.value:
            # Keep ACs, different strategy
            adaptation = IssueAdaptation(
                adaptation_type=AdvisorAction.RETRY_APPROACH,
                failure_diagnosis=advisor_decision.get("failure_diagnosis", ""),
                rationale=advisor_decision.get("rationale", ""),
                new_approach=advisor_decision.get("new_approach", ""),
                downstream_impact=advisor_decision.get("downstream_impact", ""),
            )
            adaptations.append(adaptation)

            # Inject advisor guidance as additional context for the coder
            current_issue = {
                **current_issue,
                "retry_context": advisor_decision.get("new_approach", ""),
                "approach_changes": advisor_decision.get("approach_changes", []),
                "previous_error": result.error_message,
                "retry_diagnosis": advisor_decision.get("failure_diagnosis", ""),
            }
            continue  # re-enter coding loop

        elif action == AdvisorAction.ACCEPT_WITH_DEBT.value:
            # Close enough — record gaps
            adaptation = IssueAdaptation(
                adaptation_type=AdvisorAction.ACCEPT_WITH_DEBT,
                failure_diagnosis=advisor_decision.get("failure_diagnosis", ""),
                rationale=advisor_decision.get("rationale", ""),
                missing_functionality=advisor_decision.get("missing_functionality", []),
                severity=advisor_decision.get("debt_severity", "medium"),
                downstream_impact=advisor_decision.get("downstream_impact", ""),
            )
            adaptations.append(adaptation)

            for missing in advisor_decision.get("missing_functionality", []):
                debt_items.append({
                    "type": "missing_functionality",
                    "description": missing,
                    "issue_name": issue_name,
                    "severity": advisor_decision.get("debt_severity", "medium"),
                })

            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED_WITH_DEBT,
                result_summary=advisor_decision.get("summary", result.result_summary),
                files_changed=result.files_changed,
                branch_name=result.branch_name,
                attempts=result.attempts,
                advisor_invocations=advisor_round + 1,
                adaptations=adaptations,
                debt_items=debt_items,
                final_acceptance_criteria=current_issue.get("acceptance_criteria", []),
                iteration_history=result.iteration_history,
            )

        elif action == AdvisorAction.SPLIT.value:
            # Break into sub-issues — handled by the DAG split gate
            from swe_af.execution.schemas import SplitIssueSpec
            sub_issues = [
                SplitIssueSpec(**s) for s in advisor_decision.get("sub_issues", [])
            ]
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_NEEDS_SPLIT,
                result_summary=advisor_decision.get("split_rationale", ""),
                error_message=f"Issue advisor recommended splitting into {len(sub_issues)} sub-issues",
                files_changed=result.files_changed,
                branch_name=result.branch_name,
                attempts=result.attempts,
                advisor_invocations=advisor_round + 1,
                adaptations=adaptations,
                debt_items=debt_items,
                split_request=sub_issues,
                iteration_history=result.iteration_history,
            )

        elif action == AdvisorAction.ESCALATE_TO_REPLAN.value:
            # Flag for outer loop
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_ESCALATED,
                result_summary=advisor_decision.get("summary", ""),
                error_message=advisor_decision.get("escalation_reason", result.error_message),
                error_context=result.error_context,
                files_changed=result.files_changed,
                branch_name=result.branch_name,
                attempts=result.attempts,
                advisor_invocations=advisor_round + 1,
                adaptations=adaptations,
                debt_items=debt_items,
                escalation_context=advisor_decision.get("suggested_restructuring", ""),
                iteration_history=result.iteration_history,
            )

    # All advisor rounds exhausted — return last failure result with adaptations
    if last_result is not None:
        last_result.advisor_invocations = min(advisor_round + 1, max_advisor)
        last_result.adaptations = adaptations
        last_result.debt_items = debt_items
        return last_result

    return IssueResult(
        issue_name=issue_name,
        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
        error_message="No execution attempted",
    )


async def _run_execute_fn(
    execute_fn: Callable,
    issue: dict,
    dag_state: DAGState,
    config: ExecutionConfig,
    call_fn: Callable | None,
    node_id: str,
    issue_name: str,
) -> IssueResult:
    """Run the external execute_fn path with retry logic.

    Wraps execute_fn exceptions into IssueResult for the advisor loop.
    """
    last_error = ""
    last_context = ""
    issue_with_context = issue

    for attempt in range(1, config.max_retries_per_issue + 2):
        try:
            result = await execute_fn(issue_with_context, dag_state)

            if isinstance(result, IssueResult):
                result.attempts = attempt
                return result
            if isinstance(result, dict):
                return IssueResult(
                    issue_name=issue_name,
                    outcome=IssueOutcome(result.get("outcome", "completed")),
                    result_summary=result.get("result_summary", ""),
                    error_message=result.get("error_message", ""),
                    error_context=result.get("error_context", ""),
                    attempts=attempt,
                    files_changed=result.get("files_changed", []),
                    branch_name=result.get("branch_name", ""),
                )

            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED,
                result_summary=str(result)[:500] if result else "",
                attempts=attempt,
            )

        except Exception as e:
            last_error = str(e)
            last_context = traceback.format_exc()

            if attempt <= config.max_retries_per_issue and call_fn:
                try:
                    advice = await call_fn(
                        f"{node_id}.run_retry_advisor",
                        issue=issue_with_context,
                        error_message=last_error,
                        error_context=last_context,
                        attempt_number=attempt,
                        repo_path=dag_state.repo_path,
                        prd_summary=dag_state.prd_summary,
                        architecture_summary=dag_state.architecture_summary,
                        prd_path=dag_state.prd_path,
                        architecture_path=dag_state.architecture_path,
                        artifacts_dir=dag_state.artifacts_dir,
                        model=config.retry_advisor_model,
                        ai_provider=config.ai_provider,
                    )
                    if not advice.get("should_retry", False):
                        break
                    issue_with_context = {
                        **issue,
                        "retry_context": advice.get("modified_context", ""),
                        "previous_error": last_error,
                        "retry_diagnosis": advice.get("diagnosis", ""),
                    }
                    continue
                except Exception:
                    continue
            elif attempt <= config.max_retries_per_issue:
                continue

    return IssueResult(
        issue_name=issue_name,
        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
        error_message=last_error,
        error_context=last_context,
        attempts=config.max_retries_per_issue + 1,
    )


async def _execute_level(
    active_issues: list[dict],
    execute_fn: Callable | None,
    dag_state: DAGState,
    config: ExecutionConfig,
    level_index: int,
    call_fn: Callable | None = None,
    node_id: str = "swe-planner",
    note_fn: Callable | None = None,
    memory_fn: Callable | None = None,
) -> LevelResult:
    """Execute all issues in a level concurrently.

    Returns a LevelResult with issues classified into completed, failed, and
    skipped buckets.
    """
    tasks = [
        _execute_single_issue(
            issue, dag_state, execute_fn, config,
            call_fn=call_fn, node_id=node_id, note_fn=note_fn,
            memory_fn=memory_fn,
        )
        for issue in active_issues
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    level_result = LevelResult(level_index=level_index)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # asyncio.gather with return_exceptions=True wraps exceptions
            issue_name = active_issues[i]["name"]
            issue_result = IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message=str(result),
                error_context="".join(traceback.format_exception(type(result), result, result.__traceback__))
                if hasattr(result, "__traceback__") else str(result),
            )
            level_result.failed.append(issue_result)
        elif isinstance(result, IssueResult):
            # Backfill repo_name from issue's target_repo if CoderResult didn't set it
            if not result.repo_name:
                result.repo_name = active_issues[i].get("target_repo", "")
            if result.outcome in (IssueOutcome.COMPLETED, IssueOutcome.COMPLETED_WITH_DEBT):
                level_result.completed.append(result)
            elif result.outcome == IssueOutcome.SKIPPED:
                level_result.skipped.append(result)
            else:
                level_result.failed.append(result)
        else:
            # Shouldn't happen, but handle gracefully
            issue_name = active_issues[i]["name"]
            level_result.completed.append(IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED,
            ))

    return level_result


def _skip_downstream(dag_state: DAGState, failed: list[IssueResult]) -> DAGState:
    """Mark all issues downstream of failures as skipped."""
    for failure in failed:
        downstream = find_downstream(failure.issue_name, dag_state.all_issues)
        for name in downstream:
            if name not in dag_state.skipped_issues:
                dag_state.skipped_issues.append(name)
    return dag_state


def _enrich_downstream_with_failure_notes(
    dag_state: DAGState, failed: list[IssueResult]
) -> DAGState:
    """Add failure_notes to downstream issues so coder agents know what's missing.

    When the replanner decides CONTINUE, downstream issues need to know that an
    upstream issue failed and what was supposed to be provided.
    """
    for failure in failed:
        downstream = find_downstream(failure.issue_name, dag_state.all_issues)
        for i, issue in enumerate(dag_state.all_issues):
            if issue["name"] in downstream:
                notes = list(issue.get("failure_notes", []))
                notes.append(
                    f"WARNING: Upstream issue '{failure.issue_name}' failed. "
                    f"Error: {failure.error_message}. "
                    f"It was supposed to provide: {issue.get('depends_on', [])}. "
                    f"You may need to implement workarounds or stubs for missing functionality."
                )
                dag_state.all_issues[i] = {**issue, "failure_notes": notes}
    return dag_state


async def _invoke_replanner_via_call(
    dag_state: DAGState,
    unrecoverable: list[IssueResult],
    config: ExecutionConfig,
    call_fn: Callable,
    node_id: str,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Invoke the replanner via call_fn (app.call)."""
    if note_fn:
        failed_names = [f.issue_name for f in unrecoverable]
        note_fn(
            f"Replanning triggered (attempt {dag_state.replan_count + 1}/{config.max_replans}): "
            f"failed issues = {failed_names}",
            tags=["execution", "replan", "start"],
        )

    # Pass escalation context from Issue Advisor if available
    escalation_notes = []
    for f in unrecoverable:
        if f.escalation_context:
            escalation_notes.append({
                "issue_name": f.issue_name,
                "escalation_context": f.escalation_context,
                "adaptations": [a.model_dump() for a in f.adaptations],
            })

    decision_dict = await call_fn(
        f"{node_id}.run_replanner",
        dag_state=dag_state.model_dump(),
        failed_issues=[f.model_dump() for f in unrecoverable],
        replan_model=config.replan_model,
        ai_provider=config.ai_provider,
        escalation_notes=escalation_notes,
    )
    return ReplanDecision(**decision_dict)


async def _invoke_replanner_direct(
    dag_state: DAGState,
    unrecoverable: list[IssueResult],
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Invoke the replanner directly (backward compat, no call_fn)."""
    from swe_af.execution._replanner_compat import invoke_replanner
    return await invoke_replanner(dag_state, unrecoverable, config, note_fn)


async def _write_issue_files_for_replan(
    decision: ReplanDecision,
    dag_state: DAGState,
    config: ExecutionConfig,
    call_fn: Callable,
    node_id: str,
    note_fn: Callable | None = None,
) -> None:
    """Write issue-*.md files for new issues from the replanner (Pitfall 3 fix).

    Runs one issue_writer per new issue, all in parallel.
    """
    issues_to_write = list(decision.new_issues)
    # Also write files for updated issues with material changes
    for updated in decision.updated_issues:
        if updated.get("description"):
            issues_to_write.append(updated)

    if not issues_to_write:
        return

    # Assign sequence numbers to new issues (next-available after existing max)
    max_seq = max((i.get("sequence_number") or 0 for i in dag_state.all_issues), default=0)
    for issue in issues_to_write:
        if not issue.get("sequence_number"):
            max_seq += 1
            issue["sequence_number"] = max_seq

    if note_fn:
        names = [i.get("name", "?") for i in issues_to_write]
        note_fn(
            f"Writing issue files for {len(issues_to_write)} issues: {names}",
            tags=["execution", "issue_writer", "start"],
        )

    writer_tasks = [
        call_fn(
            f"{node_id}.run_issue_writer",
            issue=new_issue,
            prd_summary=dag_state.prd_summary,
            architecture_summary=dag_state.architecture_summary,
            issues_dir=dag_state.issues_dir,
            repo_path=dag_state.repo_path,
            model=config.issue_writer_model,
            ai_provider=config.ai_provider,
        )
        for new_issue in issues_to_write
    ]
    results = await asyncio.gather(*writer_tasks, return_exceptions=True)

    if note_fn:
        successes = sum(
            1 for r in results
            if isinstance(r, dict) and r.get("success", False)
        )
        note_fn(
            f"Issue writer complete: {successes}/{len(issues_to_write)} succeeded",
            tags=["execution", "issue_writer", "complete"],
        )


async def run_dag(
    plan_result: dict,
    repo_path: str,
    execute_fn: Callable | None = None,
    config: ExecutionConfig | None = None,
    note_fn: Callable | None = None,
    call_fn: Callable | None = None,
    node_id: str = "swe-planner",
    git_config: dict | None = None,
    resume: bool = False,
    build_id: str = "",
    workspace_manifest: dict | None = None,
) -> DAGState:
    """Execute a planned DAG with self-healing replanning.

    This is the main entry point for the execution engine. It:
    1. Initializes DAG state from the plan result
    2. Executes issues level by level (all issues in a level run in parallel)
    3. After each level, runs the merge gate (worktree setup → execute → merge → test → cleanup)
    4. At each level barrier, checks for unrecoverable failures
    5. If failures exist and replanning is enabled, invokes the replanner
    6. Applies the replan decision (restructure, reduce scope, or abort)
    7. Writes issue files for any new issues from the replanner
    8. Enriches downstream issues with failure notes when CONTINUE
    9. Continues with the next level

    Args:
        plan_result: Output of the planning pipeline (PlanResult dict).
        repo_path: Path to the target repository.
        execute_fn: Optional async callable ``(issue: dict, dag_state: DAGState) -> IssueResult``.
            This is the actual coding function — could be ClaudeAI, app.call(), etc.
            When None and ``call_fn`` is provided, uses the built-in coding loop.
        config: Execution configuration. Uses defaults if not provided.
        note_fn: Optional callback for observability (e.g., ``app.note``).
        call_fn: Optional ``app.call`` for invoking reasoners (retry advisor,
            replanner, issue writer). If None, falls back to direct invocation.
        node_id: Agent node_id for constructing call targets (e.g. "swe-planner").
        git_config: Optional git configuration from ``run_git_init``. When provided,
            enables branch-per-issue workflow with worktrees, merge, and integration testing.
        resume: If True, attempt to load a checkpoint and skip completed levels.

    Returns:
        Final DAGState with execution results, replan history, etc.
    """
    if config is None:
        config = ExecutionConfig()

    # Wrap call_fn to automatically unwrap execution envelopes returned by
    # the SDK's sync fallback path.
    if call_fn is not None:
        _raw_call_fn = call_fn

        async def call_fn(target: str, **kwargs):
            result = await _raw_call_fn(target, **kwargs)
            return unwrap_call_result(result, target)

    dag_state = _init_dag_state(plan_result, repo_path, git_config=git_config, build_id=build_id)
    dag_state.workspace_manifest = workspace_manifest
    dag_state.max_replans = config.max_replans

    # Resume from checkpoint if requested
    if resume:
        artifacts_dir = plan_result.get("artifacts_dir", "")
        if artifacts_dir:
            loaded = _load_checkpoint(artifacts_dir)
            if loaded:
                dag_state = loaded
                if note_fn:
                    note_fn(
                        f"Resumed from checkpoint: level={dag_state.current_level}, "
                        f"completed={len(dag_state.completed_issues)}, "
                        f"failed={len(dag_state.failed_issues)}",
                        tags=["execution", "resume"],
                    )

    if note_fn:
        note_fn(
            f"DAG execution {'resuming' if resume else 'starting'}: "
            f"{len(dag_state.all_issues)} issues, "
            f"{len(dag_state.levels)} levels",
            tags=["execution", "start"],
        )

    # Save initial checkpoint
    _save_checkpoint(dag_state, note_fn)

    # Per-repo git init for multi-repo builds
    if workspace_manifest and call_fn:
        await _init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id=node_id,
            git_model=config.git_model,
            ai_provider=config.ai_provider,
            build_id=build_id,
            note_fn=note_fn,
        )

    # Shared memory store for cross-issue learning within this run.
    # All issues share the same store via the memory_fn closure.
    _shared_memory: dict = {}

    async def _memory_fn(action: str, key: str, value=None):
        if action == "get":
            return _shared_memory.get(key)
        elif action == "set":
            _shared_memory[key] = value

    memory_fn = _memory_fn if (call_fn is not None and config.enable_learning) else None

    issue_by_name = {i["name"]: i for i in dag_state.all_issues}

    while dag_state.current_level < len(dag_state.levels):
        level_names = dag_state.levels[dag_state.current_level]

        # Filter to active issues (not skipped, not already completed/failed)
        completed_names = {r.issue_name for r in dag_state.completed_issues}
        failed_names = {r.issue_name for r in dag_state.failed_issues}
        done_names = completed_names | failed_names | set(dag_state.skipped_issues)

        active_issues = [
            issue_by_name[name]
            for name in level_names
            if name in issue_by_name and name not in done_names
        ]

        if not active_issues:
            dag_state.current_level += 1
            continue

        if note_fn:
            active_names = [i["name"] for i in active_issues]
            note_fn(
                f"Executing level {dag_state.current_level}: {active_names}",
                tags=["execution", "level", "start"],
            )

        # --- WORKTREE SETUP (git workflow) ---
        if call_fn and dag_state.git_integration_branch:
            active_issues = await _setup_worktrees(
                dag_state, active_issues, call_fn, node_id, config, note_fn,
                build_id=dag_state.build_id,
            )

        # Track in-flight issues and checkpoint before execution (Bug 4 fix)
        dag_state.in_flight_issues = [i["name"] for i in active_issues]
        _save_checkpoint(dag_state, note_fn)

        # Execute all issues in this level concurrently
        level_result = await _execute_level(
            active_issues, execute_fn, dag_state, config, dag_state.current_level,
            call_fn=call_fn, node_id=node_id, note_fn=note_fn,
            memory_fn=memory_fn,
        )

        dag_state.in_flight_issues = []  # level barrier reached

        # Checkpoint after level barrier
        _save_checkpoint(dag_state, note_fn)

        # Record results
        dag_state.completed_issues.extend(level_result.completed)
        dag_state.failed_issues.extend(level_result.failed)
        for skipped in level_result.skipped:
            if skipped.issue_name not in dag_state.skipped_issues:
                dag_state.skipped_issues.append(skipped.issue_name)

        if note_fn:
            completed_names_level = [r.issue_name for r in level_result.completed]
            failed_names_level = [r.issue_name for r in level_result.failed]
            note_fn(
                f"Level {dag_state.current_level} complete: "
                f"completed={completed_names_level}, failed={failed_names_level}",
                tags=["execution", "level", "complete"],
            )

        # --- MERGE GATE (git workflow) ---
        file_conflicts = plan_result.get("file_conflicts", [])
        if call_fn and dag_state.git_integration_branch:
            # Merge completed branches into integration branch
            merge_result = await _merge_level_branches(
                dag_state, level_result, call_fn, node_id, config,
                issue_by_name, file_conflicts, note_fn,
            )

            # Run integration tests if merger says so
            if merge_result:
                await _run_integration_tests(
                    dag_state, merge_result, level_result, call_fn,
                    node_id, config, issue_by_name, note_fn,
                )

            # Start cleanup in background (doesn't affect replan decisions)
            # Use branch_name if injected by _setup_worktrees (includes build_id prefix),
            # otherwise derive from build_id + sequence + name.
            _bid = dag_state.build_id
            branches_to_clean = [
                i["branch_name"] if i.get("branch_name") else (
                    f"issue/{_bid}-{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
                    if _bid else
                    f"issue/{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
                )
                for i in active_issues
            ]
            cleanup_task = asyncio.create_task(
                _cleanup_worktrees(
                    dag_state, branches_to_clean, call_fn, node_id, note_fn,
                    level=dag_state.current_level,
                    model=config.git_model,
                    ai_provider=config.ai_provider,
                )
            )
        else:
            cleanup_task = None

        # --- DEBT GATE: process COMPLETED_WITH_DEBT results ---
        debt_results = [
            r for r in level_result.completed
            if r.outcome == IssueOutcome.COMPLETED_WITH_DEBT
        ]
        if debt_results:
            for r in debt_results:
                for debt in r.debt_items:
                    dag_state.accumulated_debt.append(debt)
                for adapt in r.adaptations:
                    dag_state.adaptation_history.append(adapt.model_dump())
                # Enrich downstream issues with debt notes
                downstream = find_downstream(r.issue_name, dag_state.all_issues)
                for i, iss in enumerate(dag_state.all_issues):
                    if iss["name"] in downstream:
                        notes = list(iss.get("debt_notes", []))
                        debt_desc = "; ".join(
                            d.get("description", d.get("criterion", ""))
                            for d in r.debt_items
                        )
                        notes.append(
                            f"NOTE: Upstream '{r.issue_name}' completed with debt: {debt_desc}"
                        )
                        dag_state.all_issues[i] = {**iss, "debt_notes": notes}
            if note_fn:
                note_fn(
                    f"Debt gate: {len(debt_results)} issues accepted with debt, "
                    f"total debt items: {len(dag_state.accumulated_debt)}",
                    tags=["execution", "debt_gate"],
                )

        # --- SPLIT GATE: handle FAILED_NEEDS_SPLIT results ---
        split_results = [
            f for f in level_result.failed
            if f.outcome == IssueOutcome.FAILED_NEEDS_SPLIT and f.split_request
        ]
        if split_results and call_fn:
            for sr in split_results:
                # Build a synthetic replan from the split specs
                new_issues = []
                for sub in sr.split_request:
                    sub_dict = sub.model_dump()
                    sub_dict["parent_issue_name"] = sr.issue_name
                    new_issues.append(sub_dict)

                split_decision = ReplanDecision(
                    action=ReplanAction.MODIFY_DAG,
                    rationale=f"Issue '{sr.issue_name}' split into {len(new_issues)} sub-issues by Issue Advisor",
                    new_issues=new_issues,
                    removed_issue_names=[sr.issue_name],
                    summary=f"Split {sr.issue_name}",
                )
                try:
                    dag_state = apply_replan(dag_state, split_decision)
                    issue_by_name = {i["name"]: i for i in dag_state.all_issues}

                    await _write_issue_files_for_replan(
                        split_decision, dag_state, config, call_fn, node_id, note_fn,
                    )
                    if note_fn:
                        note_fn(
                            f"Split gate: {sr.issue_name} → {[s.name for s in sr.split_request]}",
                            tags=["execution", "split_gate"],
                        )
                except ValueError as e:
                    if note_fn:
                        note_fn(
                            f"Split produced invalid DAG (cycle): {e}",
                            tags=["execution", "split_gate", "error"],
                        )

            # Remove split results from the failed list (they've been handled)
            level_result.failed = [
                f for f in level_result.failed
                if f.outcome != IssueOutcome.FAILED_NEEDS_SPLIT
            ]
            _save_checkpoint(dag_state, note_fn)

        # --- REPLAN GATE: check for unrecoverable and escalated failures ---
        unrecoverable = [
            f for f in level_result.failed
            if f.outcome in (IssueOutcome.FAILED_UNRECOVERABLE, IssueOutcome.FAILED_ESCALATED)
        ]

        if unrecoverable:
            if config.enable_replanning and dag_state.replan_count < config.max_replans:
                # Invoke replanner (via call_fn if available, else direct)
                if call_fn:
                    decision = await _invoke_replanner_via_call(
                        dag_state, unrecoverable, config, call_fn, node_id, note_fn
                    )
                else:
                    decision = await _invoke_replanner_direct(
                        dag_state, unrecoverable, config, note_fn
                    )

                if decision.action == ReplanAction.ABORT:
                    dag_state.replan_count += 1
                    dag_state.replan_history.append(decision)
                    if note_fn:
                        note_fn(
                            f"Replanner decided to ABORT: {decision.rationale}",
                            tags=["execution", "abort"],
                        )
                    if cleanup_task:
                        await cleanup_task
                    break

                elif decision.action == ReplanAction.CONTINUE:
                    # Pitfall 6: enrich downstream with failure notes
                    dag_state = _enrich_downstream_with_failure_notes(
                        dag_state, unrecoverable
                    )
                    dag_state.replan_count += 1
                    dag_state.replan_history.append(decision)
                    # Skip downstream of failed issues
                    dag_state = _skip_downstream(dag_state, unrecoverable)

                else:
                    # MODIFY_DAG or REDUCE_SCOPE — apply the replan
                    try:
                        if cleanup_task:
                            await cleanup_task
                        dag_state = apply_replan(dag_state, decision)
                        # Rebuild issue lookup after replan
                        issue_by_name = {i["name"]: i for i in dag_state.all_issues}

                        # Pitfall 3: Write issue files for new/updated issues
                        if call_fn and (decision.new_issues or decision.updated_issues):
                            await _write_issue_files_for_replan(
                                decision, dag_state, config, call_fn, node_id, note_fn
                            )

                        # Checkpoint after replan applied
                        _save_checkpoint(dag_state, note_fn)

                        # current_level was reset to 0 by apply_replan
                        continue  # re-enter loop at new level 0
                    except ValueError as e:
                        if note_fn:
                            note_fn(
                                f"Replan produced invalid DAG (cycle): {e}",
                                tags=["execution", "replan", "error"],
                            )
                        dag_state = _skip_downstream(dag_state, unrecoverable)
            else:
                # Replanning exhausted or disabled — skip downstream
                dag_state = _skip_downstream(dag_state, unrecoverable)
                if note_fn:
                    skipped = dag_state.skipped_issues
                    note_fn(
                        f"No replanning available — skipping downstream: {skipped}",
                        tags=["execution", "skip"],
                    )

        # Ensure cleanup is done before advancing to next level's worktree setup
        if cleanup_task:
            await cleanup_task

        # Advance to next level
        dag_state.current_level += 1

    # Final worktree sweep — catch anything the per-level cleanup missed
    if call_fn and dag_state.worktrees_dir and dag_state.git_integration_branch:
        # Collect all issue branches that should have been cleaned.
        # Must use the same build_id-prefixed format that workspace setup created.
        _bid = dag_state.build_id
        all_branches = [
            f"issue/{_bid}-{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
            if _bid else
            f"issue/{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
            for i in dag_state.all_issues
        ]
        if all_branches:
            if note_fn:
                note_fn(
                    "Final cleanup sweep for any residual worktrees",
                    tags=["execution", "worktree_cleanup", "final_sweep"],
                )
            await _cleanup_worktrees(
                dag_state, all_branches, call_fn, node_id, note_fn,
                level=dag_state.current_level,
                model=config.git_model,
                ai_provider=config.ai_provider,
            )

    if note_fn:
        total = len(dag_state.all_issues)
        done = len(dag_state.completed_issues)
        failed = len(dag_state.failed_issues)
        skipped = len(dag_state.skipped_issues)
        note_fn(
            f"DAG execution complete: {done}/{total} completed, "
            f"{failed} failed, {skipped} skipped, "
            f"{dag_state.replan_count} replans",
            tags=["execution", "complete"],
        )

    # Final checkpoint
    _save_checkpoint(dag_state, note_fn)

    return dag_state

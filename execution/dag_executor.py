"""Core DAG execution loop with self-healing replanning."""

from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from typing import Callable

from execution.dag_utils import apply_replan, find_downstream
from execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
    LevelResult,
    ReplanAction,
    ReplanDecision,
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
        ai_provider=config.ai_provider,
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

    Returns the MergeResult dict, or None if nothing to merge.
    """
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
                ai_provider=config.ai_provider,
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
    plan_result: dict, repo_path: str, git_config: dict | None = None,
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
) -> IssueResult:
    """Execute a single issue with AI-driven retry logic.

    When ``execute_fn`` is provided, uses the external coder path.
    When ``execute_fn`` is None and ``call_fn`` is available, uses the
    built-in coding loop (coder → QA/review → synthesizer).

    On failure, consults the retry advisor agent (if ``call_fn`` is available)
    to diagnose the root cause and decide whether a retry with modified guidance
    could succeed. Falls back to blind retry if no ``call_fn`` is provided.
    """
    issue_name = issue["name"]

    # Built-in coding loop path (no external execute_fn)
    if execute_fn is None and call_fn is not None:
        from execution.coding_loop import run_coding_loop
        return await run_coding_loop(
            issue=issue,
            dag_state=dag_state,
            call_fn=call_fn,
            node_id=node_id,
            config=config,
            note_fn=note_fn,
        )

    if execute_fn is None:
        raise ValueError("No execute_fn or call_fn — cannot execute issue")

    last_error = ""
    last_context = ""
    issue_with_context = issue  # may be enriched by retry advisor

    for attempt in range(1, config.max_retries_per_issue + 2):  # +2 because range is exclusive
        try:
            result = await execute_fn(issue_with_context, dag_state)

            # execute_fn can return an IssueResult directly or a dict
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

            # If execute_fn returned something unexpected, treat as success
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED,
                result_summary=str(result)[:500] if result else "",
                attempts=attempt,
            )

        except Exception as e:
            last_error = str(e)
            last_context = traceback.format_exc()

            if attempt <= config.max_retries_per_issue:
                if call_fn:
                    # AI-driven retry: ask retry advisor for diagnosis
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
                            # Advisor says don't bother retrying
                            break
                        # Inject advisor's guidance into the issue for next attempt
                        issue_with_context = {
                            **issue,
                            "retry_context": advice.get("modified_context", ""),
                            "previous_error": last_error,
                            "retry_diagnosis": advice.get("diagnosis", ""),
                        }
                        continue
                    except Exception:
                        # Retry advisor itself failed — fall back to blind retry
                        continue
                else:
                    # No call_fn — fall back to blind retry (backward compat)
                    continue

    # All attempts exhausted
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
) -> LevelResult:
    """Execute all issues in a level concurrently.

    Returns a LevelResult with issues classified into completed, failed, and
    skipped buckets.
    """
    tasks = [
        _execute_single_issue(
            issue, dag_state, execute_fn, config,
            call_fn=call_fn, node_id=node_id, note_fn=note_fn,
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
            if result.outcome == IssueOutcome.COMPLETED:
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

    decision_dict = await call_fn(
        f"{node_id}.run_replanner",
        dag_state=dag_state.model_dump(),
        failed_issues=[f.model_dump() for f in unrecoverable],
        replan_model=config.replan_model,
        ai_provider=config.ai_provider,
    )
    return ReplanDecision(**decision_dict)


async def _invoke_replanner_direct(
    dag_state: DAGState,
    unrecoverable: list[IssueResult],
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Invoke the replanner directly (backward compat, no call_fn)."""
    from execution._replanner_compat import invoke_replanner
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

    dag_state = _init_dag_state(plan_result, repo_path, git_config=git_config)
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
            )

        # Track in-flight issues
        dag_state.in_flight_issues = [i["name"] for i in active_issues]

        # Execute all issues in this level concurrently
        level_result = await _execute_level(
            active_issues, execute_fn, dag_state, config, dag_state.current_level,
            call_fn=call_fn, node_id=node_id, note_fn=note_fn,
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

            # Cleanup worktrees
            branches_to_clean = [
                f"issue/{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}" for i in active_issues
            ]
            await _cleanup_worktrees(
                dag_state, branches_to_clean, call_fn, node_id, note_fn,
                level=dag_state.current_level,
            )

        # REPLAN GATE: check for unrecoverable failures
        unrecoverable = [
            f for f in level_result.failed
            if f.outcome == IssueOutcome.FAILED_UNRECOVERABLE
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

        # Advance to next level
        dag_state.current_level += 1

    # Final worktree sweep — catch anything the per-level cleanup missed
    if call_fn and dag_state.worktrees_dir and dag_state.git_integration_branch:
        # Collect all issue branches that should have been cleaned
        all_branches = [
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

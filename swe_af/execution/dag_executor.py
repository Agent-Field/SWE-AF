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
    MergeResult,
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

    Single-repo path (workspace_manifest is None): creates all worktrees in
    dag_state.repo_path as before.

    Multi-repo path (workspace_manifest is set): groups issues by target_repo
    and dispatches one run_workspace_setup call per repo.

    Returns the active_issues list with worktree_path and branch_name injected.
    """
    if note_fn:
        names = [i.get("name", "?") for i in active_issues]
        note_fn(
            f"Setting up worktrees for {names}",
            tags=["execution", "worktree_setup", "start"],
        )

    # --- Single-repo path: unchanged ---
    if dag_state.workspace_manifest is None:
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

        return _enrich_issues_from_setup(
            active_issues, setup, dag_state.git_integration_branch,
        )

    # --- Multi-repo path: group issues by target_repo ---
    manifest = WorkspaceManifest(**dag_state.workspace_manifest)
    by_repo: dict[str, list[dict]] = {}
    for issue in active_issues:
        repo = issue.get("target_repo", "") or manifest.primary_repo_name
        by_repo.setdefault(repo, []).append(issue)

    if note_fn:
        note_fn(
            f"Multi-repo worktree setup: dispatching to {list(by_repo.keys())}",
            tags=["execution", "worktree_setup", "multi-repo"],
        )

    all_enriched: list[dict] = []
    for repo_name, repo_issues in by_repo.items():
        ws_repo = next((r for r in manifest.repos if r.repo_name == repo_name), None)
        if ws_repo is None:
            issue_names = [i.get("name", "?") for i in repo_issues]
            if note_fn:
                note_fn(
                    f"WARNING: target_repo '{repo_name}' not found in workspace manifest. "
                    f"Issues {issue_names} will run without worktree isolation.",
                    tags=["execution", "worktree_setup", "warning"],
                )
            all_enriched.extend(repo_issues)
            continue
        git_init = ws_repo.git_init_result or {}
        integration_branch = git_init.get("integration_branch", "")
        if not integration_branch:
            issue_names = [i.get("name", "?") for i in repo_issues]
            if note_fn:
                note_fn(
                    f"WARNING: repo '{repo_name}' has no integration branch (git_init incomplete). "
                    f"Issues {issue_names} will run without worktree isolation.",
                    tags=["execution", "worktree_setup", "warning"],
                )
            all_enriched.extend(repo_issues)
            continue

        repo_worktrees_dir = os.path.join(ws_repo.absolute_path, ".worktrees")
        setup = await call_fn(
            f"{node_id}.run_workspace_setup",
            repo_path=ws_repo.absolute_path,
            integration_branch=integration_branch,
            issues=repo_issues,
            worktrees_dir=repo_worktrees_dir,
            artifacts_dir=dag_state.artifacts_dir,
            level=dag_state.current_level,
            model=config.git_model,
            ai_provider=config.ai_provider,
            build_id=build_id,
        )

        if not setup.get("success"):
            all_enriched.extend(repo_issues)
            continue

        all_enriched.extend(
            _enrich_issues_from_setup(repo_issues, setup, integration_branch)
        )

    if note_fn:
        note_fn(
            f"Worktree setup complete: {len(all_enriched)} issues enriched",
            tags=["execution", "worktree_setup", "complete"],
        )

    return all_enriched


def _enrich_issues_from_setup(
    issues: list[dict],
    setup: dict,
    integration_branch: str,
) -> list[dict]:
    """Match worktree setup results back to issues."""
    worktree_map: dict[str, dict] = {}
    for w in setup.get("workspaces", []):
        raw_name = w["issue_name"]
        worktree_map[raw_name] = w
        # Also strip leading NN- sequence prefix for fallback matching
        stripped = re.sub(r"^\d{2}-", "", raw_name)
        if stripped != raw_name:
            worktree_map[stripped] = w

    enriched = []
    for issue in issues:
        ws = worktree_map.get(issue["name"])
        if ws:
            enriched.append({
                **issue,
                "worktree_path": ws["worktree_path"],
                "branch_name": ws["branch_name"],
                "integration_branch": integration_branch,
            })
        else:
            enriched.append(issue)

    return enriched


# ---------------------------------------------------------------------------
# Merge conflict prediction gate (pure code — no LLM)
# ---------------------------------------------------------------------------


def _predict_merge_conflicts(
    completed_branches: list[dict],
) -> tuple[bool, list[str]]:
    """Predict whether branches will have merge conflicts based on file overlap.

    Args:
        completed_branches: List of dicts with 'files_changed' lists.

    Returns:
        (conflict_likely: bool, overlapping_files: list[str])

    This is pure code — no LLM needed. If branches touch disjoint files,
    git merge will succeed without conflicts.
    """
    from collections import Counter

    all_files: list[str] = []
    for branch in completed_branches:
        all_files.extend(branch.get("files_changed", []))

    file_counts = Counter(all_files)
    overlapping = [f for f, count in file_counts.items() if count > 1]
    return bool(overlapping), overlapping


async def _fast_git_merge(
    repo_path: str,
    integration_branch: str,
    branches: list[dict],
    call_fn: Callable,
    node_id: str,
    note_fn: Callable | None = None,
) -> dict:
    """Perform a simple git merge without LLM — for conflict-free merges.

    Uses the workspace cleanup harness-style approach: a lightweight harness
    call that just runs git merge commands sequentially.

    Returns a MergeResult-compatible dict. Falls back to None on failure,
    signalling the caller to retry with the full merger.
    """
    branch_names = [b.get("branch_name", "?") for b in branches]

    if note_fn:
        note_fn(
            f"Fast merge (no LLM): merging {branch_names} into {integration_branch}",
            tags=["execution", "merge", "fast_merge", "start"],
        )

    merged: list[str] = []
    failed: list[str] = []

    # Build a simple git merge script
    merge_commands = [f"cd {repo_path}", f"git checkout {integration_branch}"]
    for b in branches:
        name = b.get("branch_name", "")
        if name:
            merge_commands.append(
                f"git merge --no-edit {name} || echo 'MERGE_FAILED:{name}'"
            )

    script = " && ".join(merge_commands)

    try:
        # Use the workspace setup harness with a minimal merge task
        result = await call_fn(
            f"{node_id}.run_workspace_cleanup",
            repo_path=repo_path,
            worktrees_dir="",
            branches_to_clean=[],
            artifacts_dir="",
            level=0,
            model="haiku",
            ai_provider="claude",
        )
        # The cleanup call is just to have a valid harness — the real work
        # is done via direct git commands below. Since we can't run raw
        # subprocess from here (we go through call_fn), we use a minimal
        # merger call with trivial branches.
    except Exception:
        pass

    # Since we need to go through the AgentField call_fn interface and
    # there's no raw-subprocess reasoner, we use run_merger with the
    # knowledge that these branches have no conflicts. The merger harness
    # will simply run `git merge` for each branch and succeed quickly
    # because there are no conflicts to resolve. This is still cheaper
    # than a full conflict-resolution merge because the LLM has nothing
    # to reason about.
    #
    # The real savings come from skipping this call entirely when we add
    # a direct git merge reasoner. For now, we construct a MergeResult
    # optimistically and let the caller fall back if it fails.

    # Instead of calling the full merger, we call it with an explicit hint
    # that no conflicts are expected, which lets it exit early.
    merge_result = await call_fn(
        f"{node_id}.run_merger",
        repo_path=repo_path,
        integration_branch=integration_branch,
        branches_to_merge=branches,
        file_conflicts=[],  # Empty — no conflicts expected
        prd_summary="[Fast merge: no file overlaps detected — simple git merge only]",
        architecture_summary="",
        artifacts_dir="",
        level=0,
        model="haiku",  # Use cheapest model since no reasoning needed
        ai_provider="claude",
    )

    if merge_result.get("success"):
        if note_fn:
            note_fn(
                f"Fast merge succeeded: {merge_result.get('merged_branches', [])}",
                tags=["execution", "merge", "fast_merge", "complete"],
            )
    else:
        if note_fn:
            note_fn(
                "Fast merge failed — will fall back to full merger",
                tags=["execution", "merge", "fast_merge", "fallback"],
            )

    return merge_result


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
    # --- Single-repo path ---
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

        # --- Merge conflict prediction gate (pure code) ---
        conflict_likely, overlapping_files = _predict_merge_conflicts(
            completed_branches
        )

        use_fast_merge = False

        if not conflict_likely:
            # No file overlaps — fast merge is safe
            if note_fn:
                note_fn(
                    "Fast merge: no file overlaps detected — skipping LLM merger",
                    tags=["execution", "merge", "fast_merge", "gate"],
                )
            use_fast_merge = True
        else:
            # Files overlap — try .ai() gate as a second-level check
            if note_fn:
                note_fn(
                    f"File overlaps detected: {overlapping_files} — "
                    "running .ai() conflict gate",
                    tags=["execution", "merge", "conflict_gate", "start"],
                )
            try:
                gate_result = await call_fn(
                    f"{node_id}.run_merge_conflict_gate",
                    overlapping_files=overlapping_files,
                    branches_to_merge=completed_branches,
                    model=config.merge_conflict_gate_model,
                    ai_provider=config.ai_provider,
                )
                if (
                    not gate_result.get("will_conflict")
                    and gate_result.get("confident")
                ):
                    if note_fn:
                        note_fn(
                            f"Conflict gate says safe: {gate_result.get('reason', '')} "
                            "— using fast merge",
                            tags=["execution", "merge", "conflict_gate", "safe"],
                        )
                    use_fast_merge = True
                else:
                    if note_fn:
                        note_fn(
                            f"Conflict gate says conflict likely: "
                            f"{gate_result.get('reason', '')} — using full merger",
                            tags=["execution", "merge", "conflict_gate", "conflict"],
                        )
            except Exception as gate_err:
                if note_fn:
                    note_fn(
                        f"Conflict gate failed ({gate_err}) — using full merger",
                        tags=["execution", "merge", "conflict_gate", "error"],
                    )

        if use_fast_merge:
            # --- Fast merge path (cheap, no LLM reasoning) ---
            merge_result = await _fast_git_merge(
                repo_path=dag_state.repo_path,
                integration_branch=dag_state.git_integration_branch,
                branches=completed_branches,
                call_fn=call_fn,
                node_id=node_id,
                note_fn=note_fn,
            )

            # If fast merge failed unexpectedly, fall back to full merger
            if not merge_result.get("success"):
                if note_fn:
                    note_fn(
                        "Fast merge failed — falling back to full LLM merger",
                        tags=["execution", "merge", "fast_merge", "fallback"],
                    )
                use_fast_merge = False

        if not use_fast_merge:
            # --- Full merger path (expensive, handles conflicts) ---
            if conflict_likely and note_fn:
                note_fn(
                    f"Full merge: overlapping files {overlapping_files}",
                    tags=["execution", "merge", "full_merge", "start"],
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

            merge_result = await call_fn(
                f"{node_id}.run_merger", **merge_kwargs
            )

            # Retry once on failure (transient auth errors, network blips)
            if (
                not merge_result.get("success")
                and merge_result.get("failed_branches")
            ):
                if note_fn:
                    note_fn(
                        "Merge failed, retrying once...",
                        tags=["execution", "merge", "retry"],
                    )
                merge_result = await call_fn(
                    f"{node_id}.run_merger", **merge_kwargs
                )

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
        """Invoke run_merger for a single repo, with conflict prediction gate."""
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

        # --- Conflict prediction gate for this repo ---
        repo_conflict_likely, repo_overlapping = _predict_merge_conflicts(
            branches_to_merge
        )

        repo_use_fast = False
        if not repo_conflict_likely:
            if note_fn:
                note_fn(
                    f"Fast merge for repo '{repo_name}': no file overlaps",
                    tags=["execution", "merge", "fast_merge", "gate"],
                )
            repo_use_fast = True
        else:
            # Try .ai() gate
            try:
                gate_result = await call_fn(
                    f"{node_id}.run_merge_conflict_gate",
                    overlapping_files=repo_overlapping,
                    branches_to_merge=branches_to_merge,
                    model=config.merge_conflict_gate_model,
                    ai_provider=config.ai_provider,
                )
                if (
                    not gate_result.get("will_conflict")
                    and gate_result.get("confident")
                ):
                    repo_use_fast = True
                    if note_fn:
                        note_fn(
                            f"Conflict gate safe for repo '{repo_name}': "
                            f"{gate_result.get('reason', '')}",
                            tags=["execution", "merge", "conflict_gate", "safe"],
                        )
                else:
                    if note_fn:
                        note_fn(
                            f"Full merge for repo '{repo_name}': "
                            f"overlapping {repo_overlapping}",
                            tags=["execution", "merge", "conflict_gate", "conflict"],
                        )
            except Exception:
                if note_fn:
                    note_fn(
                        f"Conflict gate failed for repo '{repo_name}' — "
                        "using full merger",
                        tags=["execution", "merge", "conflict_gate", "error"],
                    )

        if repo_use_fast:
            result = await _fast_git_merge(
                repo_path=ws_repo.absolute_path,
                integration_branch=integration_branch,
                branches=branches_to_merge,
                call_fn=call_fn,
                node_id=node_id,
                note_fn=note_fn,
            )
            # Fall back to full merger if fast merge failed
            if result.get("success"):
                return result
            if note_fn:
                note_fn(
                    f"Fast merge failed for repo '{repo_name}' — "
                    "falling back to full merger",
                    tags=["execution", "merge", "fast_merge", "fallback"],
                )

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

    Single-repo path (workspace_manifest is None): runs integration tests in
    dag_state.repo_path as before.

    Multi-repo path: runs integration tests in the primary repo with
    workspace_manifest context so the agent can verify cross-repo interactions.

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
                "repo_name": r.repo_name or "",
            })

    if note_fn:
        repos_touched = {b["repo_name"] for b in merged_branches if b["repo_name"]}
        label = f" (repos: {repos_touched})" if repos_touched else ""
        note_fn(
            f"Running integration tests{label}",
            tags=["execution", "integration_test", "start"],
        )

    # In multi-repo mode, determine the best repo_path for integration tests.
    # If all merged branches belong to a single non-primary repo, run tests there.
    # Otherwise, run in the primary repo (which has workspace_manifest context).
    integration_test_repo_path = dag_state.repo_path
    if dag_state.workspace_manifest:
        repos_with_merges = {b["repo_name"] for b in merged_branches if b["repo_name"]}
        if len(repos_with_merges) == 1:
            repo_name = next(iter(repos_with_merges))
            manifest = WorkspaceManifest(**dag_state.workspace_manifest)
            ws_repo = next((r for r in manifest.repos if r.repo_name == repo_name), None)
            if ws_repo and ws_repo.absolute_path:
                integration_test_repo_path = ws_repo.absolute_path

    test_result = None
    for attempt in range(config.max_integration_test_retries + 1):
        test_result = await call_fn(
            f"{node_id}.run_integration_tester",
            repo_path=integration_test_repo_path,
            integration_branch=dag_state.git_integration_branch,
            merged_branches=merged_branches,
            prd_summary=dag_state.prd_summary,
            architecture_summary=dag_state.architecture_summary,
            conflict_resolutions=merge_result.get("conflict_resolutions", []),
            artifacts_dir=dag_state.artifacts_dir,
            level=level_result.level_index,
            model=config.integration_tester_model,
            ai_provider=config.ai_provider,
            workspace_manifest=dag_state.workspace_manifest,
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
    completed_results: list | None = None,
) -> None:
    """Remove worktrees and clean up branches after merge.

    Single-repo path (workspace_manifest is None): cleans up in dag_state.repo_path.
    Multi-repo path: groups branches by repo_name from completed_results and
    dispatches cleanup per repo.

    Retries once on failure to handle transient issues (locked worktrees, etc.).
    """
    if not branches_to_clean:
        return

    if note_fn:
        note_fn(
            f"Cleaning up {len(branches_to_clean)} worktrees",
            tags=["execution", "worktree_cleanup", "start"],
        )

    # --- Multi-repo path: group by repo and clean per-repo ---
    if dag_state.workspace_manifest is not None and completed_results:
        manifest = WorkspaceManifest(**dag_state.workspace_manifest)
        by_repo: dict[str, list[str]] = {}
        for r in completed_results:
            repo = getattr(r, "repo_name", "") or manifest.primary_repo_name
            if r.branch_name and r.branch_name in branches_to_clean:
                by_repo.setdefault(repo, []).append(r.branch_name)

        for repo_name, repo_branches in by_repo.items():
            ws_repo = next((r for r in manifest.repos if r.repo_name == repo_name), None)
            if ws_repo is None:
                continue
            repo_worktrees_dir = os.path.join(ws_repo.absolute_path, ".worktrees")
            await _cleanup_single_repo(
                call_fn, node_id, ws_repo.absolute_path, repo_worktrees_dir,
                repo_branches, dag_state.artifacts_dir, level, model, ai_provider,
                note_fn,
            )
        return

    # --- Single-repo path: unchanged ---
    await _cleanup_single_repo(
        call_fn, node_id, dag_state.repo_path, dag_state.worktrees_dir,
        branches_to_clean, dag_state.artifacts_dir, level, model, ai_provider,
        note_fn,
    )


async def _cleanup_single_repo(
    call_fn: Callable,
    node_id: str,
    repo_path: str,
    worktrees_dir: str,
    branches_to_clean: list[str],
    artifacts_dir: str,
    level: int,
    model: str,
    ai_provider: str,
    note_fn: Callable | None = None,
) -> None:
    """Clean up worktrees for a single repo. Retries once on failure."""
    for attempt in range(2):  # up to 1 retry
        try:
            result = await call_fn(
                f"{node_id}.run_workspace_cleanup",
                repo_path=repo_path,
                worktrees_dir=worktrees_dir,
                branches_to_clean=branches_to_clean,
                artifacts_dir=artifacts_dir,
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
                    workspace_manifest=dag_state.workspace_manifest,
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
                        workspace_manifest=dag_state.workspace_manifest,
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


async def _execute_level_compat(
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
    """Execute all issues in a level with bounded concurrency (legacy barrier mode).

    Uses ``config.max_concurrent_issues`` to cap parallel execution.
    A value of 0 means unlimited (original behavior).

    Returns a LevelResult with issues classified into completed, failed, and
    skipped buckets.

    NOTE: This is the legacy level-barrier implementation retained for backward
    compatibility. The primary execution path now uses _run_dep_based_loop().
    """
    max_concurrent = config.max_concurrent_issues

    if max_concurrent > 0 and len(active_issues) > max_concurrent:
        # Bounded concurrency: use a semaphore to limit parallel issues
        semaphore = asyncio.Semaphore(max_concurrent)

        if note_fn:
            note_fn(
                f"Concurrency limiter: {len(active_issues)} issues, "
                f"max {max_concurrent} parallel",
                tags=["execution", "concurrency_limit"],
            )

        async def _guarded_execute(issue: dict) -> IssueResult:
            async with semaphore:
                return await _execute_single_issue(
                    issue, dag_state, execute_fn, config,
                    call_fn=call_fn, node_id=node_id, note_fn=note_fn,
                    memory_fn=memory_fn,
                )

        tasks = [_guarded_execute(issue) for issue in active_issues]
    else:
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


# ---------------------------------------------------------------------------
# True dependency-based execution helpers
# ---------------------------------------------------------------------------


def _build_dependency_graph(all_issues: list[dict]) -> dict:
    """Build adjacency maps for true dependency tracking.

    Returns dict with:
      - dependents: {issue_name: [issues that depend on it]}
      - dependencies: {issue_name: set of issues it depends on}
      - in_degree: {issue_name: number of unresolved deps}
    """
    name_set = {i["name"] for i in all_issues}
    dependents: dict[str, list[str]] = {i["name"]: [] for i in all_issues}
    dependencies: dict[str, set[str]] = {i["name"]: set() for i in all_issues}
    in_degree: dict[str, int] = {i["name"]: 0 for i in all_issues}

    for issue in all_issues:
        for dep in issue.get("depends_on", []):
            if dep in name_set:
                dependencies[issue["name"]].add(dep)
                dependents.setdefault(dep, []).append(issue["name"])
                in_degree[issue["name"]] += 1

    return {
        "dependents": dependents,
        "dependencies": dependencies,
        "in_degree": in_degree,
    }


def _compute_ready_set(
    all_issues: list[dict],
    completed: set[str],
    failed: set[str],
    skipped: set[str],
    in_flight: set[str],
) -> list[dict]:
    """Return issues whose ALL dependencies are resolved and not already in-flight/done.

    An issue is ready when every entry in its depends_on list is in the completed
    set (or doesn't exist in all_issues). Failed/skipped deps do NOT satisfy --
    downstream of failures should be explicitly skipped before calling this.
    """
    done = completed | failed | skipped | in_flight
    name_set = {i["name"] for i in all_issues}
    ready = []
    for issue in all_issues:
        name = issue["name"]
        if name in done:
            continue
        deps = issue.get("depends_on", [])
        # All deps that are in the issue set must be completed
        all_deps_met = all(
            dep in completed or dep not in name_set
            for dep in deps
        )
        if all_deps_met:
            ready.append(issue)
    return ready


_MERGE_WAVE_BATCH_SIZE = 3  # merge when this many completed issues accumulate


def _should_merge_wave(
    pending: list[IssueResult],
    ready: list[dict],
    in_flight: dict,
    all_issues: list[dict],
    pending_names: set[str] | None = None,
) -> bool:
    """Decide when to trigger a merge wave.

    Merge when:
      - pending_merge has >= BATCH_SIZE completed issues, OR
      - No more ready issues AND no in-flight (natural sync point), OR
      - A ready issue DEPENDS on a pending-merge issue (needs merged code)
    """
    if not pending:
        return False

    # Natural sync point: nothing left to do until merge happens
    if not ready and not in_flight:
        return True

    # Batch threshold
    if len(pending) >= _MERGE_WAVE_BATCH_SIZE:
        return True

    # Dependency-driven: a ready issue depends on a pending-merge issue
    if pending_names is None:
        pending_names = {r.issue_name for r in pending}
    for issue in ready:
        for dep in issue.get("depends_on", []):
            if dep in pending_names:
                return True

    return False


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


async def _process_debt_gate(
    dag_state: DAGState,
    completed_results: list[IssueResult],
    note_fn: Callable | None = None,
) -> None:
    """Process COMPLETED_WITH_DEBT results: accumulate debt, enrich downstream."""
    debt_results = [
        r for r in completed_results
        if r.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    ]
    if not debt_results:
        return
    for r in debt_results:
        for debt in r.debt_items:
            dag_state.accumulated_debt.append(debt)
        for adapt in r.adaptations:
            dag_state.adaptation_history.append(adapt.model_dump())
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


async def _process_split_gate(
    dag_state: DAGState,
    failed_results: list[IssueResult],
    config: ExecutionConfig,
    call_fn: Callable,
    node_id: str,
    note_fn: Callable | None = None,
) -> tuple[DAGState, list[IssueResult]]:
    """Handle FAILED_NEEDS_SPLIT results. Returns updated dag_state and remaining failures."""
    split_results = [
        f for f in failed_results
        if f.outcome == IssueOutcome.FAILED_NEEDS_SPLIT and f.split_request
    ]
    if not split_results:
        return dag_state, failed_results

    for sr in split_results:
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
            await _write_issue_files_for_replan(
                split_decision, dag_state, config, call_fn, node_id, note_fn,
            )
            if note_fn:
                note_fn(
                    f"Split gate: {sr.issue_name} -> {[s.name for s in sr.split_request]}",
                    tags=["execution", "split_gate"],
                )
        except ValueError as e:
            if note_fn:
                note_fn(
                    f"Split produced invalid DAG (cycle): {e}",
                    tags=["execution", "split_gate", "error"],
                )

    remaining_failed = [
        f for f in failed_results
        if f.outcome != IssueOutcome.FAILED_NEEDS_SPLIT
    ]
    _save_checkpoint(dag_state, note_fn)
    return dag_state, remaining_failed


async def _process_replan_gate(
    dag_state: DAGState,
    failed_results: list[IssueResult],
    config: ExecutionConfig,
    call_fn: Callable | None,
    node_id: str,
    note_fn: Callable | None = None,
) -> tuple[DAGState, bool]:
    """Handle unrecoverable/escalated failures via replanner.

    Returns (updated dag_state, should_abort).
    """
    unrecoverable = [
        f for f in failed_results
        if f.outcome in (IssueOutcome.FAILED_UNRECOVERABLE, IssueOutcome.FAILED_ESCALATED)
    ]
    if not unrecoverable:
        return dag_state, False

    # Fast triage gate: classify failures before expensive replanning
    if call_fn and config.enable_replanning and dag_state.replan_count < config.max_replans:
        try:
            triage = await _call_with_timeout(
                call_fn(
                    f"{node_id}.run_replanner_triage_gate",
                    failed_issues=[f.model_dump() for f in unrecoverable],
                    dag_state_summary={
                        "total_issues": len(dag_state.all_issues),
                        "completed": len(dag_state.completed_issues),
                        "failed": len(dag_state.failed_issues),
                        "current_level": dag_state.current_level,
                        "replan_count": dag_state.replan_count,
                    },
                    model=config.replanner_triage_gate_model,
                    ai_provider=config.ai_provider,
                ),
                timeout=30,
                label="replanner_triage",
            )
            if not triage.get("needs_replan", True) and triage.get("confident", False):
                recommended = triage.get("recommended_action", "skip_downstream")
                if note_fn:
                    note_fn(
                        f"Replanner triage: {triage.get('failure_type')} failure, "
                        f"action={recommended} (skipping expensive replan)",
                        tags=["execution", "replan_triage", "skip"],
                    )
                dag_state = _skip_downstream(dag_state, unrecoverable)
                return dag_state, False
        except Exception:
            pass  # Gate failed — fall through to normal replanning

    if config.enable_replanning and dag_state.replan_count < config.max_replans:
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
            return dag_state, True

        elif decision.action == ReplanAction.CONTINUE:
            dag_state = _enrich_downstream_with_failure_notes(dag_state, unrecoverable)
            dag_state.replan_count += 1
            dag_state.replan_history.append(decision)
            dag_state = _skip_downstream(dag_state, unrecoverable)

        else:
            # MODIFY_DAG or REDUCE_SCOPE
            try:
                dag_state = apply_replan(dag_state, decision)
                if call_fn and (decision.new_issues or decision.updated_issues):
                    await _write_issue_files_for_replan(
                        decision, dag_state, config, call_fn, node_id, note_fn
                    )
                _save_checkpoint(dag_state, note_fn)
            except ValueError as e:
                if note_fn:
                    note_fn(
                        f"Replan produced invalid DAG (cycle): {e}",
                        tags=["execution", "replan", "error"],
                    )
                dag_state = _skip_downstream(dag_state, unrecoverable)
    else:
        dag_state = _skip_downstream(dag_state, unrecoverable)
        if note_fn:
            note_fn(
                f"No replanning available — skipping downstream: {dag_state.skipped_issues}",
                tags=["execution", "skip"],
            )

    return dag_state, False


async def _run_merge_wave(
    dag_state: DAGState,
    pending_merge: list[IssueResult],
    call_fn: Callable | None,
    node_id: str,
    config: ExecutionConfig,
    issue_by_name: dict,
    file_conflicts: list[dict],
    note_fn: Callable | None = None,
    pending_issues_for_cleanup: list[dict] | None = None,
) -> None:
    """Execute a merge wave: merge branches, run integration tests, cleanup worktrees.

    Modifies dag_state in place (merge_results, integration_test_results, etc.).
    """
    if not pending_merge or not call_fn or not dag_state.git_integration_branch:
        return

    wave_index = dag_state.merge_wave_count

    if note_fn:
        names = [r.issue_name for r in pending_merge]
        note_fn(
            f"Merge wave {wave_index}: merging {names}",
            tags=["execution", "merge_wave", "start"],
        )

    # Build a synthetic LevelResult for the merge helpers
    level_result = LevelResult(
        level_index=wave_index,
        completed=list(pending_merge),
    )

    merge_result = await _merge_level_branches(
        dag_state, level_result, call_fn, node_id, config,
        issue_by_name, file_conflicts, note_fn,
    )

    # Post-merge batch review: see the combined diff of all issues
    if merge_result and merge_result.get("success") and pending_merge:
        try:
            completed_for_review = [
                {
                    "issue_name": r.issue_name,
                    "files_changed": r.files_changed,
                    "summary": r.result_summary,
                }
                for r in pending_merge
            ]
            if note_fn:
                note_fn(
                    f"Running batch reviewer on {len(completed_for_review)} merged issues",
                    tags=["execution", "batch_review", "start"],
                )
            batch_review = await _call_with_timeout(
                call_fn(
                    f"{node_id}.run_batch_reviewer",
                    repo_path=dag_state.repo_path,
                    integration_branch=dag_state.git_integration_branch,
                    completed_issues=completed_for_review,
                    prd_summary=dag_state.prd_summary,
                    architecture_summary=dag_state.architecture_summary,
                    model=config.batch_reviewer_model,
                    permission_mode=config.permission_mode,
                    ai_provider=config.ai_provider,
                ),
                timeout=config.agent_timeout_seconds,
                label="batch_reviewer",
            )
            if note_fn:
                note_fn(
                    f"Batch review complete: approved={batch_review.get('approved', True)}, "
                    f"blocking_issues={len(batch_review.get('blocking_issues', []))}, "
                    f"cross_issue_concerns={len(batch_review.get('cross_issue_concerns', []))}",
                    tags=["execution", "batch_review", "complete"],
                )
            dag_state.merge_results.append({
                "type": "batch_review",
                "wave": wave_index,
                "result": batch_review,
            })
        except Exception as e:
            if note_fn:
                note_fn(
                    f"Batch reviewer failed (non-blocking): {e}",
                    tags=["execution", "batch_review", "error"],
                )

    if merge_result:
        await _run_integration_tests(
            dag_state, merge_result, level_result, call_fn,
            node_id, config, issue_by_name, note_fn,
        )

    # Cleanup worktrees for merged issues
    _bid = dag_state.build_id
    issues_to_clean = pending_issues_for_cleanup or []
    branches_to_clean = [
        i["branch_name"] if i.get("branch_name") else (
            f"issue/{_bid}-{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
            if _bid else
            f"issue/{str(i.get('sequence_number') or 0).zfill(2)}-{i['name']}"
        )
        for i in issues_to_clean
    ]
    if branches_to_clean:
        await _cleanup_worktrees(
            dag_state, branches_to_clean, call_fn, node_id, note_fn,
            level=wave_index,
            model=config.git_model,
            ai_provider=config.ai_provider,
            completed_results=list(pending_merge),
        )

    dag_state.merge_wave_count += 1
    dag_state.current_level = dag_state.merge_wave_count  # backward compat


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
    """Execute a planned DAG with true dependency-based scheduling.

    Instead of level barriers (where ALL issues in a level must complete before
    the next level starts), this uses an event-driven loop driven by the
    dependency graph:

    1. Compute ready set: issues with all dependencies resolved
    2. Launch ready issues (bounded by semaphore)
    3. Wait for ANY task to complete (FIRST_COMPLETED)
    4. On completion: mark done, check if merge wave should fire
    5. On failure: skip downstream, optionally trigger replanner
    6. Repeat until all issues are done/skipped/failed

    Merge waves fire when:
      - A batch of completions accumulates (default 3), OR
      - A natural sync point is reached (nothing in-flight, nothing ready), OR
      - A ready issue depends on a pending-merge issue (needs merged code)

    Levels are still computed for planning/visualization/checkpoint but no
    longer used as execution barriers.

    Args:
        plan_result: Output of the planning pipeline (PlanResult dict).
        repo_path: Path to the target repository.
        execute_fn: Optional async callable ``(issue: dict, dag_state: DAGState) -> IssueResult``.
        config: Execution configuration.
        note_fn: Optional callback for observability.
        call_fn: Optional ``app.call`` for invoking reasoners.
        node_id: Agent node_id for constructing call targets.
        git_config: Optional git configuration from ``run_git_init``.
        resume: If True, attempt to load a checkpoint and skip completed work.
        build_id: Unique per build() call; namespaces git branches/worktrees.
        workspace_manifest: Multi-repo workspace manifest dict.

    Returns:
        Final DAGState with execution results, replan history, etc.
    """
    if config is None:
        config = ExecutionConfig()

    # Wrap call_fn to automatically unwrap execution envelopes
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
                        f"Resumed from checkpoint: wave={dag_state.merge_wave_count}, "
                        f"completed={len(dag_state.completed_issues)}, "
                        f"failed={len(dag_state.failed_issues)}",
                        tags=["execution", "resume"],
                    )

    if note_fn:
        note_fn(
            f"DAG execution {'resuming' if resume else 'starting'} "
            f"(dependency-based): "
            f"{len(dag_state.all_issues)} issues, "
            f"{len(dag_state.levels)} levels (visualization only)",
            tags=["execution", "start"],
        )

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

    # Shared memory store for cross-issue learning
    _shared_memory: dict = {}

    async def _memory_fn(action: str, key: str, value=None):
        if action == "get":
            return _shared_memory.get(key)
        elif action == "set":
            _shared_memory[key] = value

    memory_fn = _memory_fn if (call_fn is not None and config.enable_learning) else None

    issue_by_name = {i["name"]: i for i in dag_state.all_issues}
    file_conflicts = plan_result.get("file_conflicts", [])

    # --- SINGLE-ISSUE FAST PATH ---
    # When there's exactly 1 issue, skip worktrees/merge/cleanup entirely.
    # The coder commits directly to the integration branch (or repo_path).
    if (
        len(dag_state.all_issues) == 1
        and call_fn is not None
        and config.enable_single_issue_fast_path
    ):
        single_issue = dag_state.all_issues[0]
        if note_fn:
            note_fn(
                f"Single-issue fast path: {single_issue['name']} — skipping worktrees/merge",
                tags=["execution", "single_issue_fast_path"],
            )

        # Execute the issue directly (no worktree setup)
        # The coder works in the repo_path directly on the integration branch
        single_issue_enriched = {
            **single_issue,
            "worktree_path": dag_state.repo_path,
            "branch_name": dag_state.git_integration_branch,
        }

        result = await _execute_single_issue(
            single_issue_enriched,
            dag_state,
            None,
            config,
            call_fn=call_fn,
            node_id=node_id,
            note_fn=note_fn,
            memory_fn=memory_fn,
        )

        if result.outcome in (IssueOutcome.COMPLETED, IssueOutcome.COMPLETED_WITH_DEBT):
            dag_state.completed_issues.append(result)
        else:
            dag_state.failed_issues.append(result)

        # No merge needed — coder committed directly to integration branch
        # No worktree cleanup needed — no worktrees were created
        # No batch review needed — only 1 issue

        # Still run verifier (handled by build() after run_dag returns)
        _save_checkpoint(dag_state, note_fn)

        if note_fn:
            outcome = result.outcome.value
            note_fn(
                f"Single-issue fast path complete: {outcome}",
                tags=["execution", "single_issue_fast_path", "complete"],
            )

        return dag_state

    # --- True dependency-based execution state ---
    completed_names: set[str] = {r.issue_name for r in dag_state.completed_issues}
    failed_names: set[str] = {r.issue_name for r in dag_state.failed_issues}
    skipped_names: set[str] = set(dag_state.skipped_issues)
    in_flight: dict[str, asyncio.Task] = {}  # issue_name -> Task
    in_flight_issues: dict[str, dict] = {}  # issue_name -> issue dict (for cleanup)
    pending_merge: list[IssueResult] = []  # completed issues awaiting merge
    pending_merge_issue_dicts: list[dict] = []  # matching issue dicts for cleanup

    # Semaphore for bounded concurrency
    max_concurrent = config.max_concurrent_issues
    semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None

    abort = False
    consecutive_failure_count = 0
    _FAILURE_ABORT_THRESHOLD = 5  # abort after N consecutive failures without completions

    while True:
        # Refresh issue_by_name after potential replans
        issue_by_name = {i["name"]: i for i in dag_state.all_issues}

        # 1. Compute ready set
        ready = _compute_ready_set(
            dag_state.all_issues,
            completed_names,
            failed_names,
            skipped_names,
            set(in_flight.keys()),
        )

        # 2. Check if we're done or stuck
        if not ready and not in_flight:
            # Trigger final merge wave if anything pending
            if pending_merge:
                await _run_merge_wave(
                    dag_state, pending_merge, call_fn, node_id, config,
                    issue_by_name, file_conflicts, note_fn,
                    pending_issues_for_cleanup=pending_merge_issue_dicts,
                )
                pending_merge.clear()
                pending_merge_issue_dicts.clear()
            break

        # 3. Check for merge wave trigger BEFORE launching new issues
        #    (a ready issue might depend on a pending-merge issue)
        if pending_merge and _should_merge_wave(
            pending_merge, ready, in_flight, dag_state.all_issues,
        ):
            await _run_merge_wave(
                dag_state, pending_merge, call_fn, node_id, config,
                issue_by_name, file_conflicts, note_fn,
                pending_issues_for_cleanup=pending_merge_issue_dicts,
            )
            pending_merge.clear()
            pending_merge_issue_dicts.clear()
            _save_checkpoint(dag_state, note_fn)

            # Recompute ready set after merge (some issues may now be unblocked)
            ready = _compute_ready_set(
                dag_state.all_issues,
                completed_names,
                failed_names,
                skipped_names,
                set(in_flight.keys()),
            )

        # 4. Setup worktrees for newly-ready issues (if git workflow active)
        if ready and call_fn and dag_state.git_integration_branch:
            ready = await _setup_worktrees(
                dag_state, ready, call_fn, node_id, config, note_fn,
                build_id=dag_state.build_id,
            )
            # Persist enriched data back to dag_state.all_issues
            enriched_by_name = {i["name"]: i for i in ready}
            for i, issue in enumerate(dag_state.all_issues):
                enriched = enriched_by_name.get(issue["name"])
                if enriched and "worktree_path" in enriched:
                    dag_state.all_issues[i] = enriched

        # 5. Launch newly-ready issues
        for issue in ready:
            issue_name = issue["name"]

            if note_fn:
                deps = issue.get("depends_on", [])
                note_fn(
                    f"Launching {issue_name} (deps satisfied: {deps})",
                    tags=["execution", "launch", issue_name],
                )

            async def _guarded_execute(
                iss: dict = issue,
            ) -> IssueResult:
                if semaphore:
                    async with semaphore:
                        return await _execute_single_issue(
                            iss, dag_state, execute_fn, config,
                            call_fn=call_fn, node_id=node_id,
                            note_fn=note_fn, memory_fn=memory_fn,
                        )
                return await _execute_single_issue(
                    iss, dag_state, execute_fn, config,
                    call_fn=call_fn, node_id=node_id,
                    note_fn=note_fn, memory_fn=memory_fn,
                )

            task = asyncio.create_task(_guarded_execute())
            in_flight[issue_name] = task
            in_flight_issues[issue_name] = issue

        # Update in-flight tracking on dag_state for checkpoint
        dag_state.in_flight_issues = list(in_flight.keys())
        _save_checkpoint(dag_state, note_fn)

        # 6. Wait for ANY one task to complete
        if in_flight:
            done_tasks, _ = await asyncio.wait(
                in_flight.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Process completed tasks
            wave_completed: list[IssueResult] = []
            wave_failed: list[IssueResult] = []

            for task in done_tasks:
                # Find which issue this task belongs to
                finished_name = None
                for name, t in in_flight.items():
                    if t is task:
                        finished_name = name
                        break

                if finished_name is None:
                    continue

                # Remove from in-flight
                del in_flight[finished_name]
                finished_issue = in_flight_issues.pop(finished_name, {})

                # Get result
                try:
                    result = task.result()
                except Exception as exc:
                    result = IssueResult(
                        issue_name=finished_name,
                        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                        error_message=str(exc),
                        error_context="".join(
                            traceback.format_exception(type(exc), exc, exc.__traceback__)
                        ) if hasattr(exc, "__traceback__") else str(exc),
                    )

                if not isinstance(result, IssueResult):
                    result = IssueResult(
                        issue_name=finished_name,
                        outcome=IssueOutcome.COMPLETED,
                    )

                # Backfill repo_name
                if not result.repo_name:
                    result.repo_name = finished_issue.get("target_repo", "")

                if note_fn:
                    note_fn(
                        f"Issue {finished_name} finished: {result.outcome.value}",
                        tags=["execution", "complete", finished_name],
                    )

                # Classify result
                if result.outcome in (IssueOutcome.COMPLETED, IssueOutcome.COMPLETED_WITH_DEBT):
                    completed_names.add(finished_name)
                    dag_state.completed_issues.append(result)
                    pending_merge.append(result)
                    pending_merge_issue_dicts.append(finished_issue)
                    wave_completed.append(result)
                    consecutive_failure_count = 0
                elif result.outcome == IssueOutcome.SKIPPED:
                    skipped_names.add(finished_name)
                    if finished_name not in dag_state.skipped_issues:
                        dag_state.skipped_issues.append(finished_name)
                else:
                    failed_names.add(finished_name)
                    dag_state.failed_issues.append(result)
                    wave_failed.append(result)
                    consecutive_failure_count += 1

            # Update in-flight on dag_state
            dag_state.in_flight_issues = list(in_flight.keys())
            _save_checkpoint(dag_state, note_fn)

            # --- DEBT GATE ---
            await _process_debt_gate(dag_state, wave_completed, note_fn)

            # --- Process failures ---
            if wave_failed:
                # Skip downstream of failed issues
                dag_state = _skip_downstream(dag_state, wave_failed)
                skipped_names = set(dag_state.skipped_issues)

                # Cancel in-flight tasks for newly-skipped issues
                for skip_name in list(in_flight.keys()):
                    if skip_name in skipped_names:
                        in_flight[skip_name].cancel()
                        del in_flight[skip_name]
                        in_flight_issues.pop(skip_name, None)

                # SPLIT GATE
                if call_fn:
                    dag_state, wave_failed = await _process_split_gate(
                        dag_state, wave_failed, config, call_fn, node_id, note_fn,
                    )
                    issue_by_name = {i["name"]: i for i in dag_state.all_issues}

                # REPLAN GATE
                dag_state, should_abort = await _process_replan_gate(
                    dag_state, wave_failed, config, call_fn, node_id, note_fn,
                )
                if should_abort:
                    abort = True
                    break

                # Refresh skipped set after replan
                skipped_names = set(dag_state.skipped_issues)

            # Abort on excessive consecutive failures
            if consecutive_failure_count >= _FAILURE_ABORT_THRESHOLD:
                if note_fn:
                    note_fn(
                        f"{consecutive_failure_count} consecutive failures — "
                        f"aborting DAG to prevent cascading failures",
                        tags=["execution", "abort", "consecutive_failures"],
                    )
                # Skip all remaining issues
                remaining = _compute_ready_set(
                    dag_state.all_issues, completed_names, failed_names,
                    skipped_names, set(),
                )
                for issue in remaining:
                    name = issue["name"]
                    if name not in dag_state.skipped_issues:
                        dag_state.skipped_issues.append(name)
                skipped_names = set(dag_state.skipped_issues)
                abort = True
                break

    # --- Cleanup: wait for any remaining in-flight tasks on abort ---
    if abort and in_flight:
        for name, task in in_flight.items():
            task.cancel()
        # Give tasks a moment to cancel
        if in_flight:
            await asyncio.wait(in_flight.values(), timeout=5.0)
        dag_state.in_flight_issues = []

    # Final worktree sweep
    if call_fn and dag_state.worktrees_dir and dag_state.git_integration_branch:
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
                level=dag_state.merge_wave_count,
                model=config.git_model,
                ai_provider=config.ai_provider,
            )

    if note_fn:
        total = len(dag_state.all_issues)
        done = len(dag_state.completed_issues)
        failed_count = len(dag_state.failed_issues)
        skipped_count = len(dag_state.skipped_issues)
        note_fn(
            f"DAG execution complete: {done}/{total} completed, "
            f"{failed_count} failed, {skipped_count} skipped, "
            f"{dag_state.replan_count} replans, "
            f"{dag_state.merge_wave_count} merge waves",
            tags=["execution", "complete"],
        )

    # Final checkpoint
    _save_checkpoint(dag_state, note_fn)

    return dag_state

"""Issue-level build orchestrator — the sub-harness entry point.

``implement_issue`` is the issue-level twin of ``build``: the caller
(typically a bigger coding harness such as Claude Code delegating a
well-scoped task) supplies a fully-scoped issue, so **no planning agents run
at all**. The flow is:

    deterministic git setup (worktree + branch off base, no LLM)
      → run_coding_loop (coder → reviewer, or QA/reviewer/synthesizer)
      → deterministic checkpoint commit of anything left uncommitted
      → optional single verifier pass against the acceptance criteria
      → optional GitHub PR (off by default — the caller owns merge/CI)
      → worktree cleanup (the branch is the deliverable)

Concurrent calls against the same ``repo_path`` are safe: every call gets its
own worktree and its own ``issue/<build_id>-<slug>`` branch, and the caller's
checkout and current branch are never touched.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Callable

from swe_af.execution.coding_loop import run_coding_loop
from swe_af.execution.envelope import unwrap_call_result as _unwrap
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome
from swe_af.issue import git_ops, issue_router
from swe_af.issue.schemas import IssueBuildConfig, IssueBuildResult, IssueSpec

_COMPLETED_OUTCOMES = ("completed", "completed_with_debt")


async def _run_verification(
    *,
    call_fn: Callable,
    node_id: str,
    spec: IssueSpec,
    planned: dict,
    worktree_path: str,
    artifacts_dir: str,
    exec_config: ExecutionConfig,
    cfg: IssueBuildConfig,
    loop_summary: str,
    note: Callable,
) -> dict:
    """One verifier pass. Unavailability reports passed=False, never success."""
    prd = {
        "validated_description": f"{spec.title}\n\n{spec.description}",
        "acceptance_criteria": list(spec.acceptance_criteria),
        "must_have": list(spec.acceptance_criteria),
        "nice_to_have": [],
        "out_of_scope": [],
    }
    try:
        result = await asyncio.wait_for(
            call_fn(
                f"{node_id}.run_verifier",
                prd=prd,
                repo_path=worktree_path,
                artifacts_dir=artifacts_dir,
                completed_issues=[
                    {"issue_name": planned["name"], "result_summary": loop_summary}
                ],
                failed_issues=[],
                skipped_issues=[],
                model=exec_config.verifier_model,
                permission_mode=cfg.permission_mode,
                ai_provider=exec_config.ai_provider,
            ),
            timeout=cfg.agent_timeout_seconds,
        )
        if isinstance(result, dict):
            return result
        return {"passed": False, "summary": f"Unexpected verifier output: {result!r}"}
    except Exception as e:  # noqa: BLE001 — verification must not sink the build
        note(
            f"Verifier unavailable: {e}",
            tags=["issue_build", "verify", "error"],
        )
        return {"passed": False, "summary": f"Verification unavailable: {e}"}


async def _maybe_create_pr(
    *,
    call_fn: Callable,
    node_id: str,
    cfg: IssueBuildConfig,
    exec_config: ExecutionConfig,
    spec: IssueSpec,
    planned: dict,
    repo_path: str,
    worktree_path: str,
    branch: str,
    base_ref: str,
    artifacts_dir: str,
    loop_summary: str,
    debt_items: list[dict],
    note: Callable,
) -> str:
    remote = await asyncio.to_thread(git_ops.remote_url, repo_path)
    if not remote:
        note(
            "No origin remote; skipping PR creation",
            tags=["issue_build", "github_pr", "skip"],
        )
        return ""
    base_for_pr = (
        cfg.github_pr_base
        or await asyncio.to_thread(git_ops.default_remote_branch, repo_path)
        or (base_ref if base_ref != "HEAD" else "main")
    )
    try:
        result = await asyncio.wait_for(
            call_fn(
                f"{node_id}.run_github_pr",
                repo_path=worktree_path,
                integration_branch=branch,
                base_branch=base_for_pr,
                goal=spec.title,
                build_summary=loop_summary or spec.title,
                completed_issues=[
                    {"issue_name": planned["name"], "result_summary": loop_summary}
                ],
                accumulated_debt=debt_items,
                artifacts_dir=artifacts_dir,
                model=exec_config.git_model,
                permission_mode=cfg.permission_mode,
                ai_provider=exec_config.ai_provider,
            ),
            timeout=cfg.agent_timeout_seconds,
        )
        return result.get("pr_url", "") if isinstance(result, dict) else ""
    except Exception as e:  # noqa: BLE001 — PR creation is best-effort
        note(
            f"PR creation failed (non-fatal): {e}",
            tags=["issue_build", "github_pr", "error"],
        )
        return ""


async def _implement_issue_impl(
    *,
    issue: dict,
    repo_path: str,
    base_branch: str,
    artifacts_dir: str,
    additional_context: str,
    config: dict | None,
    call_fn: Callable,
    note_fn: Callable | None,
    node_id: str,
) -> dict:
    def note(message: str, tags: list[str] | None = None) -> None:
        if note_fn:
            note_fn(message, tags=tags or [])

    cfg = IssueBuildConfig(**(config or {}))
    spec = IssueSpec.model_validate(issue)
    planned = spec.to_planned_issue(additional_context=additional_context)

    # --- Setup (validation errors raise: they are the caller's to fix) ------
    repo_path = os.path.abspath(repo_path)
    await asyncio.to_thread(git_ops.ensure_issue_ready_repo, repo_path)
    base_ref, base_sha = await asyncio.to_thread(
        git_ops.resolve_base, repo_path, base_branch
    )
    excludes = [".worktrees/"]
    if not os.path.isabs(artifacts_dir):
        top = artifacts_dir.strip("/").split("/")[0]
        if top and top not in (".", ".."):
            excludes.append(f"{top}/")
    await asyncio.to_thread(git_ops.ensure_local_excludes, repo_path, excludes)
    if await asyncio.to_thread(git_ops.is_dirty, repo_path):
        note(
            "Caller repo has uncommitted changes; the issue branch is created "
            "from the committed base state and will not see them",
            tags=["issue_build", "warning", "dirty_tree"],
        )

    build_id = uuid.uuid4().hex[:8]
    branch = f"{cfg.branch_prefix}{build_id}-{planned['name']}"
    worktree_path = os.path.join(
        repo_path, ".worktrees", f"{build_id}-{planned['name']}"
    )
    if os.path.isabs(artifacts_dir):
        abs_artifacts = os.path.join(artifacts_dir, "issue-builds", build_id)
    else:
        abs_artifacts = os.path.join(repo_path, artifacts_dir, "issue-builds", build_id)
    os.makedirs(abs_artifacts, exist_ok=True)

    await asyncio.to_thread(
        git_ops.add_worktree, repo_path, worktree_path, branch, base_sha
    )
    note(
        f"Issue build {build_id}: {planned['name']} on {branch} "
        f"(base {base_ref} @ {base_sha[:12]})",
        tags=["issue_build", "start"],
    )

    planned["worktree_path"] = worktree_path
    planned["branch_name"] = branch

    exec_config = ExecutionConfig(
        runtime=cfg.runtime,
        models=cfg.models,
        max_coding_iterations=cfg.max_coding_iterations,
        agent_max_turns=cfg.agent_max_turns,
        agent_timeout_seconds=cfg.agent_timeout_seconds,
        permission_mode=cfg.permission_mode,
        enable_learning=False,
        enable_replanning=False,
        enable_issue_advisor=False,
        enable_integration_testing=False,
        check_ci=False,
    )
    dag_state = DAGState(
        repo_path=worktree_path,
        artifacts_dir=abs_artifacts,
        build_id=build_id,
        all_issues=[planned],
    )

    # --- Execute (failures after branch creation return a structured result) -
    outcome_value = "error"
    loop_summary = ""
    error_message = ""
    iterations = 0
    iteration_history: list[dict] = []
    debt_items: list[dict] = []
    verification: dict | None = None
    pr_url = ""
    commits: list[str] = []
    files_changed: list[str] = []
    stat = ""

    try:
        loop_result = await run_coding_loop(
            issue=planned,
            dag_state=dag_state,
            call_fn=call_fn,
            node_id=node_id,
            config=exec_config,
            note_fn=note_fn,
            memory_fn=None,
        )
        outcome_value = loop_result.outcome.value
        loop_summary = loop_result.result_summary or loop_result.error_message
        error_message = loop_result.error_message
        iterations = loop_result.attempts
        iteration_history = loop_result.iteration_history
        debt_items = loop_result.debt_items

        await asyncio.to_thread(
            git_ops.scrub_tracked_junk, worktree_path, planned["name"]
        )
        await asyncio.to_thread(
            git_ops.commit_all,
            worktree_path,
            f"chore({planned['name']}): checkpoint uncommitted issue work",
        )
        commits = await asyncio.to_thread(
            git_ops.new_commits, repo_path, base_sha, branch
        )
        files_changed = await asyncio.to_thread(
            git_ops.changed_files, repo_path, base_sha, branch
        )
        stat = await asyncio.to_thread(git_ops.diff_stat, repo_path, base_sha, branch)

        coding_ok = loop_result.outcome in (
            IssueOutcome.COMPLETED,
            IssueOutcome.COMPLETED_WITH_DEBT,
        )
        if cfg.verify and coding_ok and commits:
            verification = await _run_verification(
                call_fn=call_fn,
                node_id=node_id,
                spec=spec,
                planned=planned,
                worktree_path=worktree_path,
                artifacts_dir=abs_artifacts,
                exec_config=exec_config,
                cfg=cfg,
                loop_summary=loop_summary,
                note=note,
            )
        if cfg.enable_github_pr and coding_ok and commits:
            pr_url = await _maybe_create_pr(
                call_fn=call_fn,
                node_id=node_id,
                cfg=cfg,
                exec_config=exec_config,
                spec=spec,
                planned=planned,
                repo_path=repo_path,
                worktree_path=worktree_path,
                branch=branch,
                base_ref=base_ref,
                artifacts_dir=abs_artifacts,
                loop_summary=loop_summary,
                debt_items=debt_items,
                note=note,
            )
    except Exception as e:  # noqa: BLE001 — surface as a structured failure
        error_message = error_message or str(e)
        note(f"Issue build failed: {e}", tags=["issue_build", "error"])
        # Salvage whatever the coder committed before the failure.
        commits = await asyncio.to_thread(
            git_ops.new_commits, repo_path, base_sha, branch
        )
        files_changed = await asyncio.to_thread(
            git_ops.changed_files, repo_path, base_sha, branch
        )
        stat = await asyncio.to_thread(git_ops.diff_stat, repo_path, base_sha, branch)
    finally:
        if not cfg.keep_worktree:
            await asyncio.to_thread(git_ops.remove_worktree, repo_path, worktree_path)
        if not commits:
            # A branch with zero commits is pure noise for the caller.
            await asyncio.to_thread(git_ops.delete_branch, repo_path, branch)

    coding_ok = outcome_value in _COMPLETED_OUTCOMES
    verify_ok = True if verification is None else bool(verification.get("passed"))
    success = coding_ok and bool(commits) and verify_ok

    summary = (
        f"{planned['name']}: {outcome_value}, {len(commits)} commit(s), "
        f"{len(files_changed)} file(s) changed"
    )
    summary += f" on {branch}" if commits else "; no commits produced"
    if verification is not None:
        summary += (
            "; verification passed"
            if verification.get("passed")
            else "; verification FAILED"
        )
    if loop_summary:
        summary += f" — {loop_summary}"

    note(summary, tags=["issue_build", "complete" if success else "failed"])

    return IssueBuildResult(
        success=success,
        outcome=outcome_value,
        summary=summary,
        build_id=build_id,
        branch=branch if commits else "",
        base_branch=base_ref,
        base_sha=base_sha,
        commits=commits,
        files_changed=files_changed,
        diff_stat=stat,
        iterations=iterations,
        iteration_history=iteration_history,
        debt_items=debt_items,
        verification=verification,
        pr_url=pr_url,
        error_message="" if success else error_message,
    ).model_dump()


@issue_router.reasoner()
async def implement_issue(
    issue: dict,
    repo_path: str,
    base_branch: str = "",
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    config: dict | None = None,
) -> dict:
    """Implement one fully-scoped issue on an isolated branch (sub-harness entry).

    Args:
        issue: IssueSpec dict — title + description required; optionally
            acceptance_criteria, files_to_create/modify, testing_strategy,
            needs_deeper_qa, estimated_complexity, name.
        repo_path: Existing local checkout (must have at least one commit).
        base_branch: Base ref for the issue branch (default: current branch).
        artifacts_dir: Where build artifacts go (relative to repo_path unless
            absolute).
        additional_context: Extra caller context appended to the description.
        config: IssueBuildConfig overrides as a dict.

    Returns an IssueBuildResult dict; ``branch`` carries the deliverable.
    """
    agent = issue_router.app

    async def call_fn(target: str, **kwargs):
        return _unwrap(await agent.call(target, **kwargs), target)

    return await _implement_issue_impl(
        issue=issue,
        repo_path=repo_path,
        base_branch=base_branch,
        artifacts_dir=artifacts_dir,
        additional_context=additional_context,
        config=config,
        call_fn=call_fn,
        note_fn=agent.note,
        node_id=agent.node_id,
    )

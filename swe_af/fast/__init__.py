"""swe_af.fast — speed-optimised single-pass build node.

Exports
-------
fast_router : AgentRouter
    Router tagged ``'swe-fast'`` with the five execution-phase thin wrappers
    registered: run_git_init, run_coder, run_verifier, run_repo_finalize,
    run_github_pr.

Intentionally does NOT import ``swe_af.reasoners.pipeline`` (nor trigger it
via ``swe_af.reasoners.__init__``) so that planning agents (run_architect,
run_tech_lead, run_sprint_planner, run_product_manager, run_issue_writer) are
never loaded into this process.  The execution_agents module is imported lazily
inside each wrapper to honour this contract.
"""

from __future__ import annotations

from agentfield import AgentRouter

fast_router = AgentRouter(tags=["swe-fast"])


# ---------------------------------------------------------------------------
# Thin wrappers — each uses a lazy import to avoid loading
# swe_af.reasoners.__init__ (which would pull in pipeline.py).
# ---------------------------------------------------------------------------


@fast_router.reasoner()
async def run_git_init(
    repo_path: str,
    goal: str,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    previous_error: str | None = None,
    build_id: str = "",
) -> dict:
    """Thin wrapper around execution_agents.run_git_init."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_git_init(
        repo_path=repo_path, goal=goal, artifacts_dir=artifacts_dir,
        model=model, permission_mode=permission_mode, ai_provider=ai_provider,
        previous_error=previous_error, build_id=build_id,
    )


@fast_router.reasoner()
async def run_coder(
    issue: dict,
    worktree_path: str,
    feedback: str = "",
    iteration: int = 1,
    iteration_id: str = "",
    project_context: dict | None = None,
    memory_context: dict | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Thin wrapper around execution_agents.run_coder."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_coder(
        issue=issue, worktree_path=worktree_path, feedback=feedback,
        iteration=iteration, iteration_id=iteration_id,
        project_context=project_context, memory_context=memory_context,
        model=model, permission_mode=permission_mode, ai_provider=ai_provider,
    )


@fast_router.reasoner()
async def run_verifier(
    prd: dict,
    repo_path: str,
    artifacts_dir: str,
    completed_issues: list[dict] | None = None,
    failed_issues: list[dict] | None = None,
    skipped_issues: list[str] | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Thin wrapper around execution_agents.run_verifier."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_verifier(
        prd=prd, repo_path=repo_path, artifacts_dir=artifacts_dir,
        completed_issues=completed_issues or [], failed_issues=failed_issues or [],
        skipped_issues=skipped_issues or [],
        model=model, permission_mode=permission_mode, ai_provider=ai_provider,
    )


@fast_router.reasoner()
async def run_repo_finalize(
    repo_path: str,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Thin wrapper around execution_agents.run_repo_finalize."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_repo_finalize(
        repo_path=repo_path, artifacts_dir=artifacts_dir,
        model=model, permission_mode=permission_mode, ai_provider=ai_provider,
    )


@fast_router.reasoner()
async def run_github_pr(
    repo_path: str,
    integration_branch: str,
    base_branch: str,
    goal: str,
    build_summary: str = "",
    completed_issues: list[dict] | None = None,
    accumulated_debt: list[dict] | None = None,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Thin wrapper around execution_agents.run_github_pr."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_github_pr(
        repo_path=repo_path, integration_branch=integration_branch,
        base_branch=base_branch, goal=goal, build_summary=build_summary,
        completed_issues=completed_issues, accumulated_debt=accumulated_debt,
        artifacts_dir=artifacts_dir, model=model,
        permission_mode=permission_mode, ai_provider=ai_provider,
    )


@fast_router.reasoner()
async def run_ci_watcher(
    repo_path: str,
    pr_number: int,
    wait_seconds: int = 1500,
    poll_seconds: int = 30,
) -> dict:
    """Thin wrapper around execution_agents.run_ci_watcher."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_ci_watcher(
        repo_path=repo_path, pr_number=pr_number,
        wait_seconds=wait_seconds, poll_seconds=poll_seconds,
    )


@fast_router.reasoner()
async def run_ci_fixer(
    repo_path: str,
    pr_number: int,
    pr_url: str,
    integration_branch: str,
    base_branch: str,
    failed_checks: list[dict],
    iteration: int = 1,
    max_iterations: int = 2,
    goal: str = "",
    completed_issues: list[dict] | None = None,
    previous_attempts: list[dict] | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Thin wrapper around execution_agents.run_ci_fixer."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_ci_fixer(
        repo_path=repo_path, pr_number=pr_number, pr_url=pr_url,
        integration_branch=integration_branch, base_branch=base_branch,
        failed_checks=failed_checks, iteration=iteration,
        max_iterations=max_iterations, goal=goal,
        completed_issues=completed_issues, previous_attempts=previous_attempts,
        model=model, permission_mode=permission_mode, ai_provider=ai_provider,
    )


from . import executor  # noqa: E402, F401 — registers fast_execute_tasks
from . import planner  # noqa: E402, F401 — registers fast_plan_tasks
from . import verifier  # noqa: E402, F401 — registers fast_verify

__all__ = [
    "fast_router",
    "run_git_init",
    "run_coder",
    "run_verifier",
    "run_repo_finalize",
    "run_github_pr",
    "run_ci_watcher",
    "run_ci_fixer",
    "executor",
    "planner",
    "verifier",
]

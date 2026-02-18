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
async def run_git_init(**kwargs) -> dict:  # type: ignore[override]
    """Thin wrapper around execution_agents.run_git_init."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_git_init(**kwargs)


@fast_router.reasoner()
async def run_coder(**kwargs) -> dict:  # type: ignore[override]
    """Thin wrapper around execution_agents.run_coder."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_coder(**kwargs)


@fast_router.reasoner()
async def run_verifier(**kwargs) -> dict:  # type: ignore[override]
    """Thin wrapper around execution_agents.run_verifier."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_verifier(**kwargs)


@fast_router.reasoner()
async def run_repo_finalize(**kwargs) -> dict:  # type: ignore[override]
    """Thin wrapper around execution_agents.run_repo_finalize."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_repo_finalize(**kwargs)


@fast_router.reasoner()
async def run_github_pr(**kwargs) -> dict:  # type: ignore[override]
    """Thin wrapper around execution_agents.run_github_pr."""
    import swe_af.reasoners.execution_agents as _ea  # noqa: PLC0415
    return await _ea.run_github_pr(**kwargs)


from . import executor  # noqa: E402, F401 — registers fast_execute_tasks

__all__ = [
    "fast_router",
    "run_git_init",
    "run_coder",
    "run_verifier",
    "run_repo_finalize",
    "run_github_pr",
    "executor",
]

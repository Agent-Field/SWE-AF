"""swe_af.issue — issue-level build entry point (sub-harness surface).

Registers ``implement_issue`` on an AgentRouter that is included by BOTH node
apps (``swe_af.app`` / swe-planner and ``swe_af.fast.app`` / swe-fast), so a
main harness can delegate a fully-scoped issue to whichever node it is
pointed at:

    POST /api/v1/execute/async/swe-planner.implement_issue
    POST /api/v1/execute/async/swe-fast.implement_issue

Deliberately imports only ``swe_af.execution.*`` (schemas, coding loop,
envelope) — never ``swe_af.reasoners`` — so importing this package pulls in
no planning agents and no LLM role implementations. Role reasoners
(run_coder, run_code_reviewer, run_verifier, …) are reached at runtime via
``call_fn`` targets on the current node.
"""

from __future__ import annotations

from agentfield import AgentRouter
from swe_af.runtime.codex_harness_patch import apply_codex_harness_patch

apply_codex_harness_patch()

issue_router = AgentRouter(tags=["swe-issue"])

from . import git_ops  # noqa: E402, F401 — deterministic git helpers
from . import build  # noqa: E402, F401 — registers implement_issue
from .schemas import (  # noqa: E402
    IssueBuildConfig,
    IssueBuildResult,
    IssueSpec,
)

__all__ = [
    "issue_router",
    "build",
    "git_ops",
    "IssueBuildConfig",
    "IssueBuildResult",
    "IssueSpec",
]

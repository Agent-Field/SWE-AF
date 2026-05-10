from agentfield import AgentRouter
from swe_af.runtime.codex_harness_patch import apply_codex_harness_patch

apply_codex_harness_patch()

router = AgentRouter(tags=["swe-planner"])

from . import execution_agents  # noqa: E402, F401 — registers execution reasoners
from . import pipeline  # noqa: E402, F401 — registers planning reasoners

__all__ = ["router"]

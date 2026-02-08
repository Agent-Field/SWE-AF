from agentfield import AgentRouter

router = AgentRouter(tags=["swe-planner"])

from . import execution_agents  # noqa: E402, F401 — registers execution reasoners
from . import pipeline  # noqa: E402, F401 — registers planning reasoners

__all__ = ["router"]

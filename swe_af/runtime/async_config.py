"""SWE-AF's AgentField async-execution defaults.

Both long phases of a feature-level build are a *single* reasoner call:

* ``plan`` runs product_manager → architect ⇄ tech_lead → sprint_planner →
  issue writers in one invocation.
* ``execute`` runs the whole issue DAG in one invocation.

The AgentField SDK enforces a wall-clock watchdog per reasoner, defaulting to
two hours (``AsyncConfig.default_execution_timeout``). A healthy build does not
fit in that budget — a measured 35-issue ``execute`` call ran 7h27m — so the
watchdog killed builds that were still making progress and reported them as
``Reasoner 'execute' timed out after 7200.0s of active time``.

This subclass keeps the SDK's watchdog (a genuinely wedged build must still
terminate) but raises SWE-AF's default to twelve hours. Per-agent timeouts and
the harness idle watchdog still catch a stalled agent within minutes, so the
longer reasoner budget is only consumed by work that keeps producing output.

The standard ``AGENTFIELD_ASYNC_MAX_EXECUTION_TIMEOUT`` /
``AGENTFIELD_ASYNC_DEFAULT_EXECUTION_TIMEOUT`` env vars still override both
values per deployment.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentfield.async_config import AsyncConfig

TWELVE_HOURS = 12 * 60 * 60


@dataclass
class SweAsyncConfig(AsyncConfig):
    """``AsyncConfig`` with a reasoner budget sized for a whole feature build."""

    max_execution_timeout: float = float(TWELVE_HOURS)
    default_execution_timeout: float = float(TWELVE_HOURS)

    @classmethod
    def from_environment(cls) -> "SweAsyncConfig":
        config = super().from_environment()
        # AsyncConfig rejects default > max, but from_environment() does not
        # validate. A deployment that only lowered MAX clearly wants that
        # shorter ceiling, so let it bound the default too instead of failing
        # later at an unrelated call site.
        if config.default_execution_timeout > config.max_execution_timeout:
            config.default_execution_timeout = config.max_execution_timeout
        return config  # type: ignore[return-value]

"""swe_af.fast.planner — fast_plan_tasks() reasoner registered on fast_router.

Single-pass flat task decomposition using one LLM call.  Returns a
FastPlanResult; on parse failure falls back to a single generic task named
'implement-goal'.
"""

from __future__ import annotations

import logging

from swe_af.agent_ai import AgentAI, AgentAIConfig
from swe_af.fast import fast_router
from swe_af.fast.prompts import FAST_PLANNER_SYSTEM_PROMPT, fast_planner_task_prompt
from swe_af.fast.schemas import FastPlanResult, FastTask

logger = logging.getLogger(__name__)


def _note(msg: str, tags: list[str] | None = None) -> None:
    """Log a message via fast_router.note() when attached, else fall back to logger."""
    try:
        fast_router.note(msg, tags=tags or [])
    except RuntimeError:
        logger.debug("[fast_planner] %s (tags=%s)", msg, tags)


# ---------------------------------------------------------------------------
# Fallback helpers
# ---------------------------------------------------------------------------


def _fallback_plan(goal: str) -> FastPlanResult:
    """Return a single-task fallback plan when the LLM call fails."""
    return FastPlanResult(
        tasks=[
            FastTask(
                name="implement-goal",
                title="Implement goal",
                description=goal,
                acceptance_criteria=["Goal is implemented successfully."],
            )
        ],
        rationale="Fallback plan: LLM did not return a parseable result.",
        fallback_used=True,
    )


# ---------------------------------------------------------------------------
# Reasoner
# ---------------------------------------------------------------------------


@fast_router.reasoner()
async def fast_plan_tasks(
    goal: str,
    repo_path: str,
    max_tasks: int = 10,
    pm_model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
    additional_context: str = "",
    artifacts_dir: str = "",
) -> dict:
    """Decompose a build goal into a flat ordered task list.

    Uses a single LLM call with structured output to produce a
    :class:`~swe_af.fast.schemas.FastPlanResult`.  On failure (LLM error or
    unparseable response) a fallback plan with one generic task named
    ``'implement-goal'`` is returned with ``fallback_used=True``.

    Args:
        goal: High-level build goal to decompose.
        repo_path: Absolute path to the target repository on disk.
        max_tasks: Maximum number of tasks to produce (default 10).
        pm_model: Model string to use for the planning LLM call.
        permission_mode: Optional permission mode forwarded to AgentAI.
        ai_provider: AI provider string (e.g. ``"claude"``).
        additional_context: Optional extra constraints or background info.
        artifacts_dir: Optional path for writing plan artefacts (unused by
            this reasoner but kept for pipeline compatibility).

    Returns:
        A ``dict`` produced by :meth:`FastPlanResult.model_dump`.
    """
    _note(
        f"fast_plan_tasks: starting decomposition for goal={goal!r} "
        f"max_tasks={max_tasks}",
        tags=["fast_planner", "start"],
    )

    task_prompt = fast_planner_task_prompt(
        goal=goal,
        repo_path=repo_path,
        max_tasks=max_tasks,
        additional_context=additional_context,
    )

    ai = AgentAI(
        AgentAIConfig(
            provider=ai_provider,
            model=pm_model,
            cwd=repo_path,
            max_turns=3,
            permission_mode=permission_mode or None,
        )
    )

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=FAST_PLANNER_SYSTEM_PROMPT,
            output_schema=FastPlanResult,
        )
    except Exception:
        logger.exception("fast_plan_tasks: AgentAI.run() raised an exception; using fallback")
        _note(
            "fast_plan_tasks: LLM call failed; returning fallback plan",
            tags=["fast_planner", "fallback"],
        )
        return _fallback_plan(goal).model_dump()

    if response.parsed is None:
        _note(
            "fast_plan_tasks: parsed response is None; returning fallback plan",
            tags=["fast_planner", "fallback"],
        )
        return _fallback_plan(goal).model_dump()

    plan: FastPlanResult = response.parsed
    # Truncate to max_tasks using model_copy to avoid class-identity issues
    if len(plan.tasks) > max_tasks:
        plan = plan.model_copy(update={"tasks": plan.tasks[:max_tasks]})

    _note(
        f"fast_plan_tasks: produced {len(plan.tasks)} task(s)",
        tags=["fast_planner", "done"],
    )
    return plan.model_dump()

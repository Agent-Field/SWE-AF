"""Tests for swe_af.fast.planner — fast_plan_tasks() reasoner.

Covers:
- Module imports without error (AC-1)
- inspect.getsource contains 'max_tasks' (AC-17 / AC-2)
- Forbidden pipeline identifiers not in source (AC-13 / AC-3)
- fast_plan_tasks is registered on fast_router (AC-4)
- Valid LLM response produces FastPlanResult with tasks list (AC-5)
- LLM failure (parsed=None) triggers fallback with task 'implement-goal' (AC-6)
- max_tasks=1 cap respected (edge case)
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentfield import AgentRouter
from swe_af.fast.schemas import FastPlanResult, FastTask


# ---------------------------------------------------------------------------
# Helper to get registered reasoner names from fast_router
# ---------------------------------------------------------------------------


def _registered_names(router: AgentRouter) -> set[str]:
    return {r["func"].__name__ for r in router.reasoners}


def _run(coro):
    """Run an async coroutine synchronously for tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC-1: Module imports without error
# ---------------------------------------------------------------------------


class TestModuleImport:
    def test_module_imports_cleanly(self) -> None:
        import swe_af.fast.planner  # noqa: F401

    def test_fast_plan_tasks_importable(self) -> None:
        from swe_af.fast.planner import fast_plan_tasks  # noqa: F401


# ---------------------------------------------------------------------------
# AC-2 / AC-17: inspect.getsource contains 'max_tasks'
# ---------------------------------------------------------------------------


class TestSourceContainsMaxTasks:
    def test_source_contains_max_tasks(self) -> None:
        import swe_af.fast.planner

        source = inspect.getsource(swe_af.fast.planner)
        assert "max_tasks" in source, "Module source must contain 'max_tasks'"


# ---------------------------------------------------------------------------
# AC-3 / AC-13: Forbidden pipeline identifiers not in source
# ---------------------------------------------------------------------------


_FORBIDDEN_IDENTIFIERS = [
    "run_architect",
    "run_tech_lead",
    "run_sprint_planner",
    "run_product_manager",
    "run_issue_writer",
]


class TestForbiddenIdentifiersAbsent:
    @pytest.mark.parametrize("identifier", _FORBIDDEN_IDENTIFIERS)
    def test_forbidden_identifier_not_in_source(self, identifier: str) -> None:
        import swe_af.fast.planner

        source = inspect.getsource(swe_af.fast.planner)
        assert identifier not in source, (
            f"Forbidden identifier {identifier!r} found in planner source"
        )


# ---------------------------------------------------------------------------
# AC-4: fast_plan_tasks is registered on fast_router
# ---------------------------------------------------------------------------


class TestFastPlanTasksRegistered:
    def test_fast_plan_tasks_registered_on_fast_router(self) -> None:
        import swe_af.fast.planner  # noqa: F401 — ensures registration side-effect
        from swe_af.fast import fast_router

        names = _registered_names(fast_router)
        assert "fast_plan_tasks" in names, (
            f"fast_plan_tasks not registered on fast_router. Found: {names}"
        )

    def test_fast_router_has_fast_plan_tasks_in_reasoners(self) -> None:
        import swe_af.fast.planner  # noqa: F401
        from swe_af.fast import fast_router

        names = _registered_names(fast_router)
        assert "fast_plan_tasks" in names


# ---------------------------------------------------------------------------
# Functional tests (mocked AgentAI)
# ---------------------------------------------------------------------------


def _make_fast_task(name: str = "do-something") -> FastTask:
    return FastTask(
        name=name,
        title="Do something",
        description="A test task.",
        acceptance_criteria=["It is done."],
    )


def _make_mock_response(parsed: FastPlanResult | None) -> MagicMock:
    response = MagicMock()
    response.parsed = parsed
    return response


class TestFastPlanTasksFunctional:
    def test_valid_llm_response_produces_fast_plan_result(self) -> None:
        """Mocked parsed response returns FastPlanResult with tasks list."""
        from swe_af.fast.planner import fast_plan_tasks

        plan = FastPlanResult(
            tasks=[_make_fast_task("step-one"), _make_fast_task("step-two")],
            rationale="Two logical steps.",
        )
        mock_response = _make_mock_response(plan)

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=mock_response)
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(
                goal="Build a REST API",
                repo_path="/tmp/repo",
                max_tasks=10,
            ))

        assert isinstance(result, dict)
        assert "tasks" in result
        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["name"] == "step-one"
        assert result["fallback_used"] is False

    def test_llm_parsed_none_triggers_fallback(self) -> None:
        """When parsed=None the fallback plan with 'implement-goal' is returned."""
        from swe_af.fast.planner import fast_plan_tasks

        mock_response = _make_mock_response(None)

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=mock_response)
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(
                goal="Build something",
                repo_path="/tmp/repo",
            ))

        assert isinstance(result, dict)
        assert result["fallback_used"] is True
        task_names = [t["name"] for t in result["tasks"]]
        assert "implement-goal" in task_names, (
            f"Expected 'implement-goal' in fallback tasks; got {task_names}"
        )

    def test_llm_exception_triggers_fallback(self) -> None:
        """When AgentAI.run() raises, the fallback plan is returned."""
        from swe_af.fast.planner import fast_plan_tasks

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(side_effect=RuntimeError("LLM connection error"))
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(
                goal="Build something",
                repo_path="/tmp/repo",
            ))

        assert result["fallback_used"] is True
        task_names = [t["name"] for t in result["tasks"]]
        assert "implement-goal" in task_names

    def test_fallback_contains_at_least_one_task(self) -> None:
        """Fallback plan must contain at least one task (AC-6)."""
        from swe_af.fast.planner import fast_plan_tasks

        mock_response = _make_mock_response(None)

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=mock_response)
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(goal="Any goal", repo_path="/repo"))

        assert len(result["tasks"]) >= 1


# ---------------------------------------------------------------------------
# Edge case: max_tasks=1 cap respected
# ---------------------------------------------------------------------------


class TestMaxTasksCap:
    def test_max_tasks_one_truncates_result(self) -> None:
        """When LLM returns more tasks than max_tasks, result is truncated."""
        from swe_af.fast.planner import fast_plan_tasks

        many_tasks = [_make_fast_task(f"task-{i}") for i in range(5)]
        plan = FastPlanResult(tasks=many_tasks, rationale="Many tasks.")
        mock_response = _make_mock_response(plan)

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=mock_response)
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(
                goal="Build a thing",
                repo_path="/tmp/repo",
                max_tasks=1,
            ))

        assert len(result["tasks"]) == 1

    def test_max_tasks_respected_when_llm_returns_exact_count(self) -> None:
        """When LLM returns exactly max_tasks tasks, all are preserved."""
        from swe_af.fast.planner import fast_plan_tasks

        tasks = [_make_fast_task(f"task-{i}") for i in range(3)]
        plan = FastPlanResult(tasks=tasks, rationale="Exactly 3 tasks.")
        mock_response = _make_mock_response(plan)

        with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=mock_response)
            MockAgentAI.return_value = instance

            result = _run(fast_plan_tasks(
                goal="Build a thing",
                repo_path="/tmp/repo",
                max_tasks=3,
            ))

        assert len(result["tasks"]) == 3

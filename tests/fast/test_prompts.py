"""Tests for swe_af.fast.prompts — FAST_PLANNER_SYSTEM_PROMPT and fast_planner_task_prompt()."""

from __future__ import annotations

import pytest

from swe_af.fast.prompts import FAST_PLANNER_SYSTEM_PROMPT, fast_planner_task_prompt


# ---------------------------------------------------------------------------
# Module importability (AC-1)
# ---------------------------------------------------------------------------


class TestModuleImport:
    def test_module_imports_cleanly(self) -> None:
        import swe_af.fast.prompts  # noqa: F401


# ---------------------------------------------------------------------------
# FAST_PLANNER_SYSTEM_PROMPT (AC-2)
# ---------------------------------------------------------------------------


class TestFastPlannerSystemPrompt:
    def test_is_non_empty_string(self) -> None:
        assert isinstance(FAST_PLANNER_SYSTEM_PROMPT, str)
        assert len(FAST_PLANNER_SYSTEM_PROMPT) > 0

    def test_does_not_contain_forbidden_identifiers(self) -> None:
        forbidden = [
            "run_architect",
            "run_tech_lead",
            "run_sprint_planner",
            "run_product_manager",
            "run_issue_writer",
        ]
        for identifier in forbidden:
            assert identifier not in FAST_PLANNER_SYSTEM_PROMPT, (
                f"Forbidden identifier {identifier!r} found in FAST_PLANNER_SYSTEM_PROMPT"
            )


# ---------------------------------------------------------------------------
# fast_planner_task_prompt — basic output (AC-3, AC-4, AC-5)
# ---------------------------------------------------------------------------


class TestFastPlannerTaskPrompt:
    def test_returns_non_empty_string(self) -> None:
        result = fast_planner_task_prompt(
            goal="x",
            repo_path="/r",
            max_tasks=5,
            additional_context="",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_goal_text(self) -> None:
        goal = "Build a REST API for user management"
        result = fast_planner_task_prompt(
            goal=goal,
            repo_path="/repo",
            max_tasks=5,
            additional_context="",
        )
        assert goal in result

    def test_contains_max_tasks_value(self) -> None:
        result = fast_planner_task_prompt(
            goal="some goal",
            repo_path="/repo",
            max_tasks=7,
            additional_context="",
        )
        assert "7" in result

    def test_contains_repo_path(self) -> None:
        result = fast_planner_task_prompt(
            goal="some goal",
            repo_path="/my/special/repo",
            max_tasks=5,
            additional_context="",
        )
        assert "/my/special/repo" in result

    def test_with_additional_context_includes_context(self) -> None:
        context = "Must use Python 3.12 and follow PEP 8."
        result = fast_planner_task_prompt(
            goal="some goal",
            repo_path="/repo",
            max_tasks=5,
            additional_context=context,
        )
        assert context in result

    def test_does_not_contain_forbidden_identifiers(self) -> None:
        forbidden = [
            "run_architect",
            "run_tech_lead",
            "run_sprint_planner",
            "run_product_manager",
            "run_issue_writer",
        ]
        result = fast_planner_task_prompt(
            goal="some goal",
            repo_path="/repo",
            max_tasks=5,
            additional_context="",
        )
        for identifier in forbidden:
            assert identifier not in result, (
                f"Forbidden identifier {identifier!r} found in task prompt output"
            )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestFastPlannerTaskPromptEdgeCases:
    def test_max_tasks_one(self) -> None:
        result = fast_planner_task_prompt(
            goal="minimal goal",
            repo_path="/r",
            max_tasks=1,
            additional_context="",
        )
        assert "1" in result
        assert "minimal goal" in result

    def test_empty_additional_context_excluded_from_output(self) -> None:
        result = fast_planner_task_prompt(
            goal="goal text",
            repo_path="/r",
            max_tasks=5,
            additional_context="",
        )
        # Should not include the "Additional Context" header if context is empty
        assert "Additional Context" not in result

    def test_non_empty_additional_context_included(self) -> None:
        result = fast_planner_task_prompt(
            goal="goal text",
            repo_path="/r",
            max_tasks=5,
            additional_context="Use async everywhere.",
        )
        assert "Additional Context" in result
        assert "Use async everywhere." in result

    def test_ac_example_call(self) -> None:
        """Replicates the exact call from the acceptance criteria."""
        result = fast_planner_task_prompt(
            goal="x",
            repo_path="/r",
            max_tasks=5,
            additional_context="",
        )
        assert isinstance(result, str)
        assert len(result) > 0
        assert "x" in result
        assert "5" in result

"""Tests for issue_advisor.py prompt compression.

This module verifies that the compressed issue_advisor.py prompt:
- Meets the LOC target (≤176 lines, 20% reduction from 220)
- Preserves AdvisorAction decision tree logic
- Maintains confidence scoring guidelines
- AC modification rules intact
- Adaptation strategy framework preserved
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _load_issue_advisor_content() -> str:
    """Load issue_advisor.py file content without importing."""
    repo_root = Path(__file__).parent.parent
    issue_advisor_path = repo_root / "swe_af" / "prompts" / "issue_advisor.py"
    return issue_advisor_path.read_text()


# ---------------------------------------------------------------------------
# AC1: LOC target (≤176 lines, 20% reduction from 220)
# ---------------------------------------------------------------------------


class TestLOCTarget:
    def test_issue_advisor_loc_within_target(self) -> None:
        """Verify issue_advisor.py is ≤176 LOC (20% reduction from 220)."""
        repo_root = Path(__file__).parent.parent
        issue_advisor_path = repo_root / "swe_af" / "prompts" / "issue_advisor.py"

        result = subprocess.run(
            ["wc", "-l", str(issue_advisor_path)],
            capture_output=True,
            text=True,
            check=True,
        )

        loc_count = int(result.stdout.split()[0])
        assert loc_count <= 176, (
            f"issue_advisor.py has {loc_count} LOC, must be ≤176 "
            f"(20% reduction from 220)"
        )


# ---------------------------------------------------------------------------
# AC2: AdvisorAction decision tree logic preserved
# ---------------------------------------------------------------------------


class TestAdvisorActionDecisionTree:
    """Verify all AdvisorAction types are documented."""

    REQUIRED_ACTIONS = [
        "RETRY_APPROACH",
        "RETRY_MODIFIED",
        "ACCEPT_WITH_DEBT",
        "SPLIT",
        "ESCALATE_TO_REPLAN",
    ]

    def test_all_advisor_actions_mentioned(self) -> None:
        """All AdvisorAction types must be documented in the prompt."""
        content = _load_issue_advisor_content()

        for action in self.REQUIRED_ACTIONS:
            assert action in content, (
                f"AdvisorAction '{action}' not found in issue_advisor.py"
            )

    def test_action_ordering_preserved(self) -> None:
        """Actions should be ordered from least to most disruptive."""
        content = _load_issue_advisor_content()

        # Should mention ordering
        assert (
            "least" in content.lower() and "disruptive" in content.lower()
        ), "Must mention least-to-most disruptive ordering"

    def test_retry_approach_description(self) -> None:
        """RETRY_APPROACH should describe alternative strategy with same ACs."""
        content = _load_issue_advisor_content()

        assert "RETRY_APPROACH" in content
        # Key concept: same ACs, different approach
        retry_section = content[content.index("RETRY_APPROACH"):content.index("RETRY_APPROACH") + 200]
        assert (
            "approach" in retry_section.lower() or
            "strategy" in retry_section.lower()
        )

    def test_retry_modified_description(self) -> None:
        """RETRY_MODIFIED should describe AC modification and debt tracking."""
        content = _load_issue_advisor_content()

        assert "RETRY_MODIFIED" in content
        # Key concept: relax/drop criteria → debt
        assert "debt" in content.lower()

    def test_accept_with_debt_description(self) -> None:
        """ACCEPT_WITH_DEBT should describe accepting partial implementation."""
        content = _load_issue_advisor_content()

        assert "ACCEPT_WITH_DEBT" in content
        # Key concept: good enough, record what's missing
        assert "debt" in content.lower()

    def test_split_description(self) -> None:
        """SPLIT should describe breaking into sub-issues."""
        content = _load_issue_advisor_content()

        assert "SPLIT" in content
        # Key concept: split into smaller issues
        assert (
            "sub" in content.lower() or
            "smaller" in content.lower()
        )

    def test_split_depth_guard(self) -> None:
        """Prompt should prevent infinite split recursion (depth ≥2)."""
        content = _load_issue_advisor_content()

        # Should mention not splitting already-split issues
        assert "depth" in content.lower() or "split" in content.lower()
        assert "≥2" in content or ">= 2" in content or "parent_issue_name" in content

    def test_escalate_description(self) -> None:
        """ESCALATE_TO_REPLAN should describe DAG restructuring."""
        content = _load_issue_advisor_content()

        assert "ESCALATE_TO_REPLAN" in content
        # Key concept: DAG structure problem
        assert "DAG" in content


# ---------------------------------------------------------------------------
# AC3: Confidence scoring guidelines maintained
# ---------------------------------------------------------------------------


class TestConfidenceScoring:
    """Verify decision framework guidance is preserved."""

    def test_decision_framework_present(self) -> None:
        """Prompt should have a decision framework section."""
        content = _load_issue_advisor_content()

        assert (
            "Decision Framework" in content or
            "decision" in content.lower()
        ), "Must include decision framework guidance"

    def test_iteration_history_evaluation(self) -> None:
        """Framework should mention analyzing iteration history."""
        content = _load_issue_advisor_content()

        assert (
            "iteration" in content.lower() and
            "history" in content.lower()
        ), "Must mention iteration history evaluation"

    def test_error_details_evaluation(self) -> None:
        """Framework should mention analyzing error/rejection details."""
        content = _load_issue_advisor_content()

        assert "error" in content.lower(), "Must mention error analysis"

    def test_worktree_inspection(self) -> None:
        """Framework should mention inspecting worktree code state."""
        content = _load_issue_advisor_content()

        assert "worktree" in content.lower(), "Must mention worktree inspection"

    def test_scarcity_awareness(self) -> None:
        """Prompt should mention limited advisor invocation budget."""
        content = _load_issue_advisor_content()

        assert (
            "budget" in content.lower() or
            "invocation" in content.lower() or
            "scarcity" in content.lower()
        ), "Must mention advisor invocation budget/scarcity"


# ---------------------------------------------------------------------------
# AC4: AC modification rules intact
# ---------------------------------------------------------------------------


class TestACModificationRules:
    """Verify AC modification guidance is preserved."""

    def test_full_modified_acs_required(self) -> None:
        """RETRY_MODIFIED output should require FULL AC list, not just changes."""
        content = _load_issue_advisor_content()

        # Should explicitly say FULL modified ACs
        assert "FULL" in content or "full" in content.lower()

    def test_dropped_criteria_become_debt(self) -> None:
        """Prompt should state dropped criteria become technical debt."""
        content = _load_issue_advisor_content()

        assert (
            "dropped" in content.lower() or
            "drop" in content.lower()
        )
        assert "debt" in content.lower()


# ---------------------------------------------------------------------------
# AC5: Adaptation strategy framework preserved
# ---------------------------------------------------------------------------


class TestAdaptationStrategyFramework:
    """Verify adaptation strategy principles are preserved."""

    def test_never_skip_never_abort_principle(self) -> None:
        """Prompt should state 'never skip, never abort' principle."""
        content = _load_issue_advisor_content()

        assert (
            "never" in content.lower() and
            ("skip" in content.lower() or "abort" in content.lower())
        ), "Must state 'never skip, never abort' principle"

    def test_always_find_way_forward(self) -> None:
        """Prompt should emphasize always finding a way forward."""
        content = _load_issue_advisor_content()

        assert (
            "always" in content.lower() or
            "way forward" in content.lower() or
            "forward" in content.lower()
        ), "Must emphasize finding way forward"

    def test_output_is_completed_repo_plus_debt(self) -> None:
        """Prompt should state final output is completed repo + debt register."""
        content = _load_issue_advisor_content()

        assert (
            "completed" in content.lower() and
            ("repo" in content.lower() or "repository" in content.lower()) and
            "debt" in content.lower()
        ), "Must describe output as completed repo + debt"

    def test_mentions_issue_advisor_decision_schema(self) -> None:
        """Prompt should reference IssueAdvisorDecision output schema."""
        content = _load_issue_advisor_content()

        assert "IssueAdvisorDecision" in content, (
            "Must reference IssueAdvisorDecision schema"
        )


# ---------------------------------------------------------------------------
# Functional correctness: basic structure validation
# ---------------------------------------------------------------------------


class TestPromptStructure:
    """Verify the compressed prompt maintains basic structure."""

    def test_contains_system_prompt_constant(self) -> None:
        """File should define SYSTEM_PROMPT constant."""
        content = _load_issue_advisor_content()
        assert 'SYSTEM_PROMPT = """' in content or "SYSTEM_PROMPT = '''" in content

    def test_contains_function_definition(self) -> None:
        """File should define issue_advisor_task_prompt function."""
        content = _load_issue_advisor_content()
        assert "def issue_advisor_task_prompt(" in content

    def test_function_has_required_parameters(self) -> None:
        """Function should accept all required parameters."""
        content = _load_issue_advisor_content()
        required_params = [
            "issue: dict",
            "original_issue: dict",
            "failure_result: dict",
            "iteration_history: list[dict]",
            "dag_state_summary: dict",
        ]

        for param in required_params:
            assert param in content, f"Missing parameter: {param}"

    def test_mentions_tools_available(self) -> None:
        """Prompt should list available tools (READ, GLOB, GREP, BASH)."""
        content = _load_issue_advisor_content()

        tools = ["READ", "GLOB", "GREP", "BASH"]
        found_tools = sum(1 for tool in tools if tool in content)
        assert found_tools >= 3, (
            "Should mention at least 3 tools (READ, GLOB, GREP, BASH)"
        )

    def test_budget_awareness_section(self) -> None:
        """Task prompt should include budget/invocation tracking."""
        content = _load_issue_advisor_content()

        assert "advisor_invocation" in content
        assert "max_advisor_invocations" in content

    def test_previous_adaptations_tracked(self) -> None:
        """Task prompt should track previous adaptations."""
        content = _load_issue_advisor_content()

        assert "previous_adaptations" in content

"""Tests for PlannedIssue.target_repo field (AC-10).

Covers:
- PlannedIssue without target_repo defaults to empty string
- PlannedIssue with target_repo='api' stores correctly
- model_dump() includes target_repo key
- Existing PlannedIssue construction without target_repo still works
"""

from __future__ import annotations

import pytest

from swe_af.reasoners.schemas import PlannedIssue


def _make_planned_issue(**kwargs) -> PlannedIssue:
    """Helper to construct a PlannedIssue with required fields."""
    defaults = dict(
        name="test-issue",
        title="Test Issue",
        description="A test description.",
        acceptance_criteria=["AC1"],
        depends_on=[],
        provides=[],
        files_to_create=[],
        files_to_modify=[],
        testing_strategy="pytest",
        sequence_number=1,
    )
    defaults.update(kwargs)
    return PlannedIssue(**defaults)


class TestPlannedIssueTargetRepo:
    def test_target_repo_defaults_to_empty_string(self) -> None:
        """PlannedIssue without target_repo has target_repo == '' (backward compat)."""
        pi = _make_planned_issue()
        assert hasattr(pi, "target_repo")
        assert pi.target_repo == ""

    def test_target_repo_stores_given_value(self) -> None:
        """PlannedIssue with target_repo='api' stores the value correctly."""
        pi = _make_planned_issue(target_repo="api")
        assert pi.target_repo == "api"

    def test_model_dump_includes_target_repo_key(self) -> None:
        """model_dump() output includes the target_repo key."""
        pi = _make_planned_issue(target_repo="backend")
        dumped = pi.model_dump()
        assert "target_repo" in dumped
        assert dumped["target_repo"] == "backend"

    def test_existing_construction_without_target_repo_still_works(self) -> None:
        """Existing PlannedIssue construction patterns without target_repo continue to pass."""
        # Minimal required fields only — no target_repo argument
        pi = PlannedIssue(
            name="test-issue",
            title="Test",
            description="desc",
            acceptance_criteria=["AC1"],
            depends_on=[],
            provides=[],
            files_to_create=[],
            files_to_modify=[],
            testing_strategy="pytest",
            sequence_number=1,
        )
        assert pi.target_repo == ""
        assert pi.name == "test-issue"

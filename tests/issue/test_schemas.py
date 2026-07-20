"""Tests for the issue-level build schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swe_af.issue.schemas import (
    IssueBuildConfig,
    IssueBuildResult,
    IssueSpec,
    slugify,
)


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Add retry helper!") == "add-retry-helper"

    def test_collapses_and_trims(self) -> None:
        assert slugify("  --Weird__ Name--  ") == "weird-name"

    def test_empty_falls_back(self) -> None:
        assert slugify("!!!") == "issue"

    def test_truncates(self) -> None:
        assert len(slugify("x" * 100)) <= 40


class TestIssueSpec:
    def test_requires_title_and_description(self) -> None:
        with pytest.raises(ValidationError):
            IssueSpec(title="", description="d")
        with pytest.raises(ValidationError):
            IssueSpec(title="t", description="   ")
        with pytest.raises(ValidationError):
            IssueSpec(description="d")  # type: ignore[call-arg]

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            IssueSpec(title="t", description="d", surprise=1)  # type: ignore[call-arg]

    def test_to_planned_issue_maps_onto_planned_issue_schema(self) -> None:
        # The dict must satisfy the internal PlannedIssue model 1:1.
        from swe_af.reasoners.schemas import PlannedIssue

        spec = IssueSpec(
            title="Add retry helper",
            description="Backoff retry in utils.",
            acceptance_criteria=["retries 3 times"],
            files_to_modify=["utils.py"],
            testing_strategy="pytest tests/test_utils.py",
            needs_deeper_qa=True,
        )
        planned = spec.to_planned_issue()
        model = PlannedIssue.model_validate(planned)

        assert model.name == "add-retry-helper"
        assert model.guidance is not None
        assert model.guidance.needs_deeper_qa is True
        assert model.guidance.testing_guidance == "pytest tests/test_utils.py"
        assert model.files_to_modify == ["utils.py"]
        assert model.sequence_number == 1

    def test_additional_context_appended(self) -> None:
        spec = IssueSpec(title="T", description="Base description.")
        planned = spec.to_planned_issue(additional_context="Use httpx, not requests.")
        assert "Base description." in planned["description"]
        assert "Use httpx, not requests." in planned["description"]

    def test_explicit_name_is_slugified(self) -> None:
        spec = IssueSpec(title="T", description="d", name="My Fancy Name!")
        assert spec.to_planned_issue()["name"] == "my-fancy-name"


class TestIssueBuildConfig:
    def test_defaults(self) -> None:
        cfg = IssueBuildConfig()
        assert cfg.max_coding_iterations == 3
        assert cfg.verify is True
        assert cfg.enable_github_pr is False
        assert cfg.branch_prefix == "issue/"
        assert cfg.keep_worktree is False

    def test_valid_model_keys(self) -> None:
        cfg = IssueBuildConfig(
            models={"default": "haiku", "coder": "sonnet", "verifier": "haiku"}
        )
        assert cfg.models["coder"] == "sonnet"

    def test_unknown_model_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown model keys"):
            IssueBuildConfig(models={"pm": "haiku"})

    def test_unknown_config_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IssueBuildConfig(max_tasks=5)  # type: ignore[call-arg]

    def test_models_flow_into_execution_config(self) -> None:
        # The subset of role keys accepted here must remain valid for the
        # ExecutionConfig that drives the coding loop.
        from swe_af.execution.schemas import ExecutionConfig

        cfg = IssueBuildConfig(
            runtime="claude_code",
            models={"default": "haiku", "coder": "sonnet"},
        )
        exec_config = ExecutionConfig(runtime=cfg.runtime, models=cfg.models)
        assert exec_config.coder_model == "sonnet"
        assert exec_config.code_reviewer_model == "haiku"
        assert exec_config.verifier_model == "haiku"


class TestIssueBuildResult:
    def test_minimal_result(self) -> None:
        result = IssueBuildResult(success=False, outcome="error", summary="boom")
        data = result.model_dump()
        assert data["branch"] == ""
        assert data["commits"] == []
        assert data["verification"] is None

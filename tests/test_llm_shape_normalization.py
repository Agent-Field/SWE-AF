"""Regression tests for LLM-emitted scalar shapes on issue data.

Incident (2026-07-20, live build on deepseek-v4-pro): `generate_fix_issues`
returned fix issues whose `acceptance_criteria` was a bare string. The raw
dicts flowed unvalidated into DAGState.all_issues, the executor assigned the
string onto IssueResult.final_acceptance_criteria (attribute assignment
bypasses Pydantic), the checkpoint serialized it, and the next DAGState
validation (replanner context / checkpoint reload) failed with six
`list_type` errors — killing a 48-minute build with zero deliverable.

Contract:
  - A bare-string value for any issue list field is coerced to a one-element
    list at every LLM ingestion boundary (fix generator, replan updates/adds).
  - Models that re-validate persisted state (IssueResult, SplitIssueSpec,
    PlannedIssue) tolerate the bare-string shape, so old poisoned checkpoints
    still load.
"""

from __future__ import annotations

import json

from swe_af.execution.dag_utils import apply_replan, normalize_issue_dict
from swe_af.execution.schemas import (
    DAGState,
    IssueOutcome,
    IssueResult,
    ReplanAction,
    ReplanDecision,
    SplitIssueSpec,
    ensure_str_list,
)
from swe_af.reasoners.schemas import PlannedIssue


class TestEnsureStrList:
    def test_str_becomes_singleton_list(self) -> None:
        assert ensure_str_list("AC-1: works") == ["AC-1: works"]

    def test_blank_str_becomes_empty(self) -> None:
        assert ensure_str_list("   ") == []

    def test_none_becomes_empty(self) -> None:
        assert ensure_str_list(None) == []

    def test_list_passes_through(self) -> None:
        assert ensure_str_list(["a", "b"]) == ["a", "b"]

    def test_non_str_scalar_passes_through_for_real_validation(self) -> None:
        assert ensure_str_list(42) == 42


class TestNormalizeIssueDict:
    def test_coerces_all_list_fields(self) -> None:
        issue = {
            "name": "fix-1",
            "acceptance_criteria": "AC-1: single string",
            "depends_on": "other-issue",
            "provides": None,
            "files_to_create": "a.py",
            "files_to_modify": ["b.py"],
        }
        normalize_issue_dict(issue)
        assert issue["acceptance_criteria"] == ["AC-1: single string"]
        assert issue["depends_on"] == ["other-issue"]
        assert issue["provides"] == []
        assert issue["files_to_create"] == ["a.py"]
        assert issue["files_to_modify"] == ["b.py"]

    def test_absent_fields_stay_absent(self) -> None:
        issue = {"name": "fix-1", "description": "d"}
        normalize_issue_dict(issue)
        assert "acceptance_criteria" not in issue


class TestTolerantModels:
    def test_issue_result_coerces_str_final_acceptance_criteria(self) -> None:
        result = IssueResult(
            issue_name="x",
            outcome=IssueOutcome.COMPLETED,
            final_acceptance_criteria="AC-1: single string",
        )
        assert result.final_acceptance_criteria == ["AC-1: single string"]

    def test_split_issue_spec_coerces_str_fields(self) -> None:
        spec = SplitIssueSpec(
            name="s", title="t", description="d",
            acceptance_criteria="only criterion",
            files_to_modify="one.py",
        )
        assert spec.acceptance_criteria == ["only criterion"]
        assert spec.files_to_modify == ["one.py"]

    def test_planned_issue_coerces_str_fields(self) -> None:
        issue = PlannedIssue(
            name="n", title="t", description="d",
            acceptance_criteria="only criterion",
        )
        assert issue.acceptance_criteria == ["only criterion"]


class TestIncidentRegression:
    def test_checkpoint_reload_survives_poisoned_completed_issue(self) -> None:
        """The exact crash: DAGState(**json) with str final_acceptance_criteria."""
        checkpoint = {
            "repo_path": "/tmp/repo",
            "all_issues": [{"name": "fix-ac1", "acceptance_criteria": "AC-1: str"}],
            "completed_issues": [
                {
                    "issue_name": "fix-ac1",
                    "outcome": "completed",
                    # Pre-fix builds serialized the bare string; reload must
                    # tolerate it so old checkpoints stay resumable.
                    "final_acceptance_criteria": 'AC-1: python -c "..." exits 0',
                }
            ],
        }
        state = DAGState(**json.loads(json.dumps(checkpoint)))
        assert state.completed_issues[0].final_acceptance_criteria == [
            'AC-1: python -c "..." exits 0'
        ]

    def test_apply_replan_normalizes_llm_issue_shapes(self) -> None:
        state = DAGState(
            repo_path="/tmp/repo",
            all_issues=[
                {"name": "keep", "depends_on": [], "acceptance_criteria": ["ok"]},
            ],
            levels=[["keep"]],
        )
        decision = ReplanDecision(
            action=ReplanAction.MODIFY_DAG,
            rationale="r",
            updated_issues=[{"name": "keep", "acceptance_criteria": "AC as string"}],
            new_issues=[
                {
                    "name": "new-1",
                    "depends_on": "keep",
                    "acceptance_criteria": "single new criterion",
                }
            ],
        )
        state = apply_replan(state, decision)
        by_name = {i["name"]: i for i in state.all_issues}
        assert by_name["keep"]["acceptance_criteria"] == ["AC as string"]
        assert by_name["new-1"]["acceptance_criteria"] == ["single new criterion"]
        assert by_name["new-1"]["depends_on"] == ["keep"]

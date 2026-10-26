"""Split children inherit the parent issue's guidance.

A split child keeps the parent's routing and proportionality guidance so a
flagged parent's children stay on the flagged path.
"""

from __future__ import annotations

from swe_af.execution.dag_executor import _split_child_issue_dicts
from swe_af.execution.schemas import SplitIssueSpec


class TestSplitGuidanceInheritance:
    def test_split_spec_guidance_is_optional(self) -> None:
        spec = SplitIssueSpec(
            name="child",
            title="Child issue",
            description="d",
            acceptance_criteria=["a"],
        )

        assert spec.guidance is None

    def test_child_inherits_parent_guidance_when_omitted(self) -> None:
        parent_guidance = {
            "needs_deeper_qa": True,
            "needs_new_tests": False,
            "estimated_scope": "small",
            "testing_guidance": "extend the existing suite",
        }
        child = SplitIssueSpec(
            name="child",
            title="Child issue",
            description="d",
            acceptance_criteria=["a"],
        )

        [child_dict] = _split_child_issue_dicts(
            [child], {"guidance": parent_guidance}, "parent"
        )

        assert child_dict["guidance"] == parent_guidance
        assert child_dict["parent_issue_name"] == "parent"
        # Copy, not a shared reference: mutating a child must not touch the parent.
        child_dict["guidance"]["needs_deeper_qa"] = False
        assert parent_guidance["needs_deeper_qa"] is True

    def test_child_guidance_wins_over_parent(self) -> None:
        child = SplitIssueSpec(
            name="child",
            title="Child issue",
            description="d",
            acceptance_criteria=["a"],
            guidance={"needs_deeper_qa": False},
        )

        [child_dict] = _split_child_issue_dicts(
            [child], {"guidance": {"needs_deeper_qa": True}}, "parent"
        )

        assert child_dict["guidance"] == {"needs_deeper_qa": False}

    def test_parent_without_guidance_leaves_child_empty(self) -> None:
        child = SplitIssueSpec(
            name="child",
            title="Child issue",
            description="d",
            acceptance_criteria=["a"],
        )

        [child_dict] = _split_child_issue_dicts([child], {}, "parent")

        assert not child_dict.get("guidance")

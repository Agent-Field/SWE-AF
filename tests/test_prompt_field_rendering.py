"""Prompt rendering of fields the pipeline already carries, and split guidance.

The coder renders description, retry context, approach changes, retry
diagnosis, upstream debt and needs_new_tests; the reviewer renders
estimated_scope and needs_new_tests; QA renders testing_guidance. Split children
inherit the parent issue's guidance.
"""

from __future__ import annotations

from swe_af.execution.dag_executor import _split_child_issue_dicts
from swe_af.execution.schemas import SplitIssueSpec
from swe_af.prompts.code_reviewer import code_reviewer_task_prompt
from swe_af.prompts.coder import coder_task_prompt
from swe_af.prompts.qa import qa_task_prompt


class TestCoderPromptRendersPipelineFields:
    def test_renders_description_retry_context_approach_and_debt(self) -> None:
        issue = {
            "name": "cache-read",
            "title": "Retry the cache read",
            "description": "DESCRIPTION-SENTINEL: read through the cache, then the store.",
            "acceptance_criteria": ["Reads fall back to the store"],
            "retry_context": "RETRY-CONTEXT-SENTINEL: look up the store before the index.",
            "approach_changes": ["APPROACH-SENTINEL: drop the custom index"],
            "retry_diagnosis": "DIAGNOSIS-SENTINEL: the index was never populated.",
            "debt_notes": ["DEBT-SENTINEL: upstream left batch mode unimplemented."],
            "guidance": {"needs_new_tests": True},
        }

        prompt = coder_task_prompt(issue=issue, worktree_path="/tmp/wt")

        for sentinel in (
            "DESCRIPTION-SENTINEL",
            "RETRY-CONTEXT-SENTINEL",
            "APPROACH-SENTINEL",
            "DIAGNOSIS-SENTINEL",
            "DEBT-SENTINEL",
        ):
            assert sentinel in prompt, f"{sentinel} did not reach the coder prompt"

    def test_retry_block_names_the_new_approach(self) -> None:
        prompt = coder_task_prompt(
            issue={"retry_context": "use a plain list"},
            worktree_path="/tmp/wt",
        )

        assert "Retry Context (from Issue Advisor)" in prompt
        assert "New approach" in prompt
        assert "use a plain list" in prompt

    def test_needs_new_tests_false_limits_the_coder_to_build_and_existing_suite(self) -> None:
        prompt = coder_task_prompt(
            issue={"guidance": {"needs_new_tests": False}},
            worktree_path="/tmp/wt",
        )

        assert "New tests needed" in prompt
        assert "do NOT create or extend test files" in prompt
        assert "Run the build and the existing test suite only" in prompt
        assert "unless the issue says no new tests are needed" in prompt

    def test_needs_new_tests_true_does_not_carry_the_skip_instruction(self) -> None:
        prompt = coder_task_prompt(
            issue={"guidance": {"needs_new_tests": True}},
            worktree_path="/tmp/wt",
        )

        assert "New tests needed" in prompt
        assert "do NOT create or extend test files" not in prompt


class TestReviewerPromptRendersScopeAndTests:
    def test_renders_estimated_scope(self) -> None:
        prompt = code_reviewer_task_prompt(
            worktree_path="/tmp/wt",
            coder_result={"summary": "done", "files_changed": ["app.py"]},
            issue={"guidance": {"estimated_scope": "SCOPE-SENTINEL-small"}},
        )

        assert "Scope and Test Expectations (from sprint planner)" in prompt
        assert "SCOPE-SENTINEL-small" in prompt

    def test_needs_new_tests_false_forbids_demanding_tests(self) -> None:
        prompt = code_reviewer_task_prompt(
            worktree_path="/tmp/wt",
            coder_result={"summary": "done", "files_changed": ["app.py"]},
            issue={"guidance": {"needs_new_tests": False}},
        )

        assert "New tests needed**: no" in prompt
        assert "not expected to add tests" in prompt
        assert "do not require new tests" in prompt
        assert "do not block on missing test coverage" in prompt

    def test_needs_new_tests_true_asks_for_test_coverage(self) -> None:
        prompt = code_reviewer_task_prompt(
            worktree_path="/tmp/wt",
            coder_result={"summary": "done", "files_changed": ["app.py"]},
            issue={"guidance": {"needs_new_tests": True}},
        )

        assert "New tests needed**: yes" in prompt
        assert "do not require new tests" not in prompt


class TestQAPromptRendersTestingGuidance:
    def test_renders_testing_guidance(self) -> None:
        guidance = "QA-GUIDANCE-SENTINEL: extend the existing CLI test; no new files."
        prompt = qa_task_prompt(
            worktree_path="/tmp/wt",
            coder_result={"summary": "done", "files_changed": ["app.py"]},
            issue={"guidance": {"testing_guidance": guidance}},
        )

        assert "Testing Guidance (from sprint planner)" in prompt
        assert guidance in prompt


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

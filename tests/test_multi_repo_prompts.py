"""Tests for prompt function signatures with workspace_manifest and related parameters.

Covers AC-16 through AC-22.
"""

from __future__ import annotations

import inspect

import pytest

from swe_af.execution.schemas import WorkspaceManifest, WorkspaceRepo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_multi_repo_manifest() -> WorkspaceManifest:
    """Create a multi-repo WorkspaceManifest for testing."""
    return WorkspaceManifest(
        workspace_root="/tmp",
        repos=[
            WorkspaceRepo(
                repo_name="api",
                repo_url="https://github.com/org/api.git",
                role="primary",
                absolute_path="/tmp/api",
                branch="main",
                sparse_paths=[],
                create_pr=True,
            ),
            WorkspaceRepo(
                repo_name="lib",
                repo_url="https://github.com/org/lib.git",
                role="dependency",
                absolute_path="/tmp/lib",
                branch="main",
                sparse_paths=[],
                create_pr=False,
            ),
        ],
        primary_repo_name="api",
    )


def _make_single_repo_manifest() -> WorkspaceManifest:
    """Create a single-repo WorkspaceManifest for testing."""
    return WorkspaceManifest(
        workspace_root="/tmp",
        repos=[
            WorkspaceRepo(
                repo_name="api",
                repo_url="https://github.com/org/api.git",
                role="primary",
                absolute_path="/tmp/api",
                branch="main",
                sparse_paths=[],
                create_pr=True,
            ),
        ],
        primary_repo_name="api",
    )


# ---------------------------------------------------------------------------
# AC-16: pm_task_prompt has workspace_manifest parameter
# ---------------------------------------------------------------------------

class TestPmTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-16: pm_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.product_manager import pm_task_prompt
        sig = inspect.signature(pm_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.product_manager import pm_task_prompt
        sig = inspect.signature(pm_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest should not raise and match None-param call."""
        from swe_af.prompts.product_manager import pm_task_prompt
        result = pm_task_prompt(goal="test goal", repo_path="/tmp/repo", prd_path="/tmp/prd.md")
        result_explicit = pm_task_prompt(
            goal="test goal", repo_path="/tmp/repo", prd_path="/tmp/prd.md", workspace_manifest=None
        )
        assert result == result_explicit

    def test_single_repo_no_extra_content(self):
        """Single-repo manifest should produce identical output to None."""
        from swe_af.prompts.product_manager import pm_task_prompt
        single = _make_single_repo_manifest()
        result_none = pm_task_prompt(goal="g", repo_path="/r", prd_path="/p")
        result_single = pm_task_prompt(goal="g", repo_path="/r", prd_path="/p", workspace_manifest=single)
        assert result_none == result_single


# ---------------------------------------------------------------------------
# AC-17: architect_task_prompt has workspace_manifest parameter
# ---------------------------------------------------------------------------

class TestArchitectTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-17: architect_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.architect import architect_task_prompt
        sig = inspect.signature(architect_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.architect import architect_task_prompt
        sig = inspect.signature(architect_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest should not raise and match None-param call."""
        from swe_af.prompts.architect import architect_task_prompt
        from swe_af.reasoners.schemas import PRD

        prd = PRD(
            validated_description="Test PRD",
            acceptance_criteria=["AC-1: test passes"],
            must_have=["feature A"],
            nice_to_have=[],
            out_of_scope=["feature B"],
        )
        result = architect_task_prompt(
            prd=prd, repo_path="/r", prd_path="/p", architecture_path="/a"
        )
        result_explicit = architect_task_prompt(
            prd=prd, repo_path="/r", prd_path="/p", architecture_path="/a", workspace_manifest=None
        )
        assert result == result_explicit


# ---------------------------------------------------------------------------
# AC-18: sprint_planner_task_prompt has workspace_manifest parameter,
#         multi-repo manifest causes 'target_repo' in output
# ---------------------------------------------------------------------------

class TestSprintPlannerTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-18: sprint_planner_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        sig = inspect.signature(sprint_planner_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        sig = inspect.signature(sprint_planner_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_multi_repo_manifest_includes_target_repo_in_output(self):
        """AC-18: multi-repo manifest causes 'target_repo' to appear in prompt."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        manifest = _make_multi_repo_manifest()
        prompt = sprint_planner_task_prompt(
            goal="test goal",
            prd={},
            architecture={},
            workspace_manifest=manifest,
        )
        assert "target_repo" in prompt

    def test_single_repo_no_target_repo_mandate(self):
        """Single-repo manifest should not add target_repo mandate."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        single = _make_single_repo_manifest()
        prompt = sprint_planner_task_prompt(
            goal="test goal", prd={}, architecture={}, workspace_manifest=single
        )
        # Single repo => no multi-repo block => no target_repo mandate
        assert "target_repo" not in prompt

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest should not raise and match None-param call."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        result = sprint_planner_task_prompt(goal="test goal", prd={}, architecture={})
        result_explicit = sprint_planner_task_prompt(
            goal="test goal", prd={}, architecture={}, workspace_manifest=None
        )
        assert result == result_explicit

    def test_goal_appears_in_output(self):
        """Goal string should always appear in the output."""
        from swe_af.prompts.sprint_planner import sprint_planner_task_prompt
        prompt = sprint_planner_task_prompt(goal="build the thing", prd={}, architecture={})
        assert "build the thing" in prompt


# ---------------------------------------------------------------------------
# AC-19: coder_task_prompt has workspace_manifest and target_repo parameters,
#         target_repo='lib' causes /tmp/lib in output
# ---------------------------------------------------------------------------

class TestCoderTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-19: coder_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.coder import coder_task_prompt
        sig = inspect.signature(coder_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_has_target_repo_param(self):
        """AC-19: coder_task_prompt must accept target_repo parameter."""
        from swe_af.prompts.coder import coder_task_prompt
        sig = inspect.signature(coder_task_prompt)
        assert "target_repo" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.coder import coder_task_prompt
        sig = inspect.signature(coder_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_target_repo_lib_includes_absolute_path(self):
        """AC-19: target_repo='lib' with multi-repo manifest includes /tmp/lib in output."""
        from swe_af.prompts.coder import coder_task_prompt
        manifest = _make_multi_repo_manifest()
        prompt = coder_task_prompt(
            issue={},
            worktree_path="/tmp/worktrees/issue-01",
            workspace_manifest=manifest,
            target_repo="lib",
        )
        assert "/tmp/lib" in prompt

    def test_target_repo_api_includes_api_path(self):
        """target_repo='api' should include /tmp/api in output."""
        from swe_af.prompts.coder import coder_task_prompt
        manifest = _make_multi_repo_manifest()
        prompt = coder_task_prompt(
            issue={},
            worktree_path="/tmp/worktrees/issue-01",
            workspace_manifest=manifest,
            target_repo="api",
        )
        assert "/tmp/api" in prompt

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest/target_repo should not raise and match None-param call."""
        from swe_af.prompts.coder import coder_task_prompt
        result = coder_task_prompt(issue={}, worktree_path="/tmp/wt")
        result_explicit = coder_task_prompt(
            issue={}, worktree_path="/tmp/wt", workspace_manifest=None, target_repo=""
        )
        assert result == result_explicit

    def test_single_repo_no_ws_block(self):
        """Single-repo manifest should not add workspace block."""
        from swe_af.prompts.coder import coder_task_prompt
        single = _make_single_repo_manifest()
        result_none = coder_task_prompt(issue={}, worktree_path="/tmp/wt")
        result_single = coder_task_prompt(
            issue={}, worktree_path="/tmp/wt", workspace_manifest=single
        )
        assert result_none == result_single


# ---------------------------------------------------------------------------
# AC-20: verifier_task_prompt has workspace_manifest parameter
# ---------------------------------------------------------------------------

class TestVerifierTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-20: verifier_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.verifier import verifier_task_prompt
        sig = inspect.signature(verifier_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.verifier import verifier_task_prompt
        sig = inspect.signature(verifier_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest should not raise and match None-param call."""
        from swe_af.prompts.verifier import verifier_task_prompt
        result = verifier_task_prompt(
            prd={}, artifacts_dir="/tmp", completed_issues=[], failed_issues=[], skipped_issues=[]
        )
        result_explicit = verifier_task_prompt(
            prd={}, artifacts_dir="/tmp", completed_issues=[], failed_issues=[],
            skipped_issues=[], workspace_manifest=None
        )
        assert result == result_explicit


# ---------------------------------------------------------------------------
# AC-21: workspace_setup_task_prompt has workspace_manifest parameter
# ---------------------------------------------------------------------------

class TestWorkspaceSetupTaskPromptSignature:
    def test_has_workspace_manifest_param(self):
        """AC-21: workspace_setup_task_prompt must accept workspace_manifest parameter."""
        from swe_af.prompts.workspace import workspace_setup_task_prompt
        sig = inspect.signature(workspace_setup_task_prompt)
        assert "workspace_manifest" in sig.parameters

    def test_workspace_manifest_defaults_to_none(self):
        """workspace_manifest parameter should default to None."""
        from swe_af.prompts.workspace import workspace_setup_task_prompt
        sig = inspect.signature(workspace_setup_task_prompt)
        assert sig.parameters["workspace_manifest"].default is None

    def test_backward_compat_no_new_params(self):
        """Calling without workspace_manifest should not raise and match None-param call."""
        from swe_af.prompts.workspace import workspace_setup_task_prompt
        result = workspace_setup_task_prompt(
            repo_path="/r", integration_branch="main", issues=[], worktrees_dir="/w"
        )
        result_explicit = workspace_setup_task_prompt(
            repo_path="/r", integration_branch="main", issues=[], worktrees_dir="/w",
            workspace_manifest=None
        )
        assert result == result_explicit


# ---------------------------------------------------------------------------
# AC-22: github_pr_task_prompt has all_pr_results parameter
# ---------------------------------------------------------------------------

class TestGithubPrTaskPromptSignature:
    def test_has_all_pr_results_param(self):
        """AC-22: github_pr_task_prompt must accept all_pr_results parameter."""
        from swe_af.prompts.github_pr import github_pr_task_prompt
        sig = inspect.signature(github_pr_task_prompt)
        assert "all_pr_results" in sig.parameters

    def test_all_pr_results_defaults_to_none(self):
        """all_pr_results parameter should default to None."""
        from swe_af.prompts.github_pr import github_pr_task_prompt
        sig = inspect.signature(github_pr_task_prompt)
        assert sig.parameters["all_pr_results"].default is None

    def test_backward_compat_no_new_params(self):
        """Calling without all_pr_results should not raise and match None-param call."""
        from swe_af.prompts.github_pr import github_pr_task_prompt
        result = github_pr_task_prompt(
            repo_path="/r", integration_branch="main", base_branch="main", goal="test"
        )
        result_explicit = github_pr_task_prompt(
            repo_path="/r", integration_branch="main", base_branch="main", goal="test",
            all_pr_results=None
        )
        assert result == result_explicit

    def test_all_pr_results_appears_in_output(self):
        """all_pr_results content should appear in prompt when provided."""
        from swe_af.prompts.github_pr import github_pr_task_prompt
        pr_results = [
            {"repo_name": "api", "success": True, "pr_url": "https://github.com/org/api/pull/1", "pr_number": 1},
            {"repo_name": "lib", "success": False, "error_message": "push failed"},
        ]
        prompt = github_pr_task_prompt(
            repo_path="/r", integration_branch="main", base_branch="main", goal="test",
            all_pr_results=pr_results,
        )
        assert "api" in prompt
        assert "lib" in prompt

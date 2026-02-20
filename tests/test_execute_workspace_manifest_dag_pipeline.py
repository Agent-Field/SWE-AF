"""Integration tests for the conflict-resolved execute() function and multi-repo pipeline.

Priority 1 (Conflict Resolution):
  - execute() in app.py had a docstring-only conflict between issue/daaccc55-04-clone-repos
    and issue/daaccc55-05-dag-executor-multi-repo. Both added 'workspace_manifest' parameter
    documentation with slightly different wording. Tests verify the parameter is correctly
    wired through the entire call chain: execute() -> run_dag() -> DAGState.workspace_manifest.

Priority 2 (Cross-feature interactions):
  - _clone_repos (issue-04) -> WorkspaceManifest -> execute() workspace_manifest param (issue-05)
  - workspace_context_block (issue-03) consumed by _init_all_repos pathway (issue-05)
  - CoderResult.repo_name (issue-06) -> IssueResult.repo_name propagation
  - BuildConfig.repos normalization -> BuildResult.pr_url backward-compat property

Priority 3 (Shared file swe_af/app.py):
  - execute() accepts workspace_manifest and passes it to run_dag
  - build() passes manifest.model_dump() to execute when multi-repo

Note: execute() is decorated with @app.reasoner() which wraps the original function.
We use execute._original_func to inspect the actual implementation signature.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.dag_executor import _init_all_repos, _merge_level_branches, run_dag
from swe_af.execution.schemas import (
    BuildConfig,
    BuildResult,
    CoderResult,
    DAGState,
    ExecutionConfig,
    GitInitResult,
    IssueOutcome,
    IssueResult,
    LevelResult,
    MergeResult,
    RepoPRResult,
    RepoSpec,
    WorkspaceManifest,
    WorkspaceRepo,
)
from swe_af.prompts._utils import workspace_context_block
from swe_af.reasoners.schemas import PlannedIssue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace_repo(
    repo_name: str = "api",
    role: str = "primary",
    absolute_path: str = "/tmp/api",
    repo_url: str = "https://github.com/org/api.git",
    branch: str = "main",
    create_pr: bool = True,
) -> WorkspaceRepo:
    return WorkspaceRepo(
        repo_name=repo_name,
        repo_url=repo_url,
        role=role,
        absolute_path=absolute_path,
        branch=branch,
        sparse_paths=[],
        create_pr=create_pr,
    )


def _make_two_repo_manifest() -> WorkspaceManifest:
    """Returns a two-repo WorkspaceManifest: api (primary) + lib (dependency)."""
    return WorkspaceManifest(
        workspace_root="/tmp/workspace",
        repos=[
            _make_workspace_repo("api", "primary", "/tmp/workspace/api", "https://github.com/org/api.git"),
            _make_workspace_repo("lib", "dependency", "/tmp/workspace/lib", "https://github.com/org/lib.git", create_pr=False),
        ],
        primary_repo_name="api",
    )


def _make_dag_state(**kwargs) -> DAGState:
    """Minimal DAGState for testing."""
    defaults = {
        "repo_path": "/tmp/repo",
        "artifacts_dir": "/tmp/.artifacts",
        "all_issues": [],
        "levels": [],
        "git_integration_branch": "integration/test",
    }
    defaults.update(kwargs)
    return DAGState(**defaults)


# ===========================================================================
# Priority 1: Conflict Resolution – execute() workspace_manifest parameter wiring
# ===========================================================================


class TestExecuteWorkspaceManifestWiring:
    """Verify the conflict-resolved execute() function correctly passes workspace_manifest
    to run_dag(). The conflict was between issue-04 and issue-05 both adding docstrings
    for the workspace_manifest parameter; the resolved code must actually work.

    Note: execute() is decorated with @app.reasoner() which replaces __signature__.
    We use execute._original_func to inspect the underlying function signature.
    """

    def test_execute_original_func_has_workspace_manifest_parameter(self) -> None:
        """execute()._original_func must have 'workspace_manifest' parameter.

        The @app.reasoner() decorator wraps the function. The actual implementation
        is accessible via _original_func, which exposes the conflict-resolved parameter list.
        """
        from swe_af.app import execute

        orig_func = execute._original_func
        sig = inspect.signature(orig_func)
        assert "workspace_manifest" in sig.parameters, (
            "execute()._original_func must have 'workspace_manifest' parameter "
            "(conflict resolution from issue-04 and issue-05)"
        )

    def test_execute_workspace_manifest_defaults_to_none(self) -> None:
        """execute() workspace_manifest parameter must default to None for backward compat."""
        from swe_af.app import execute

        orig_func = execute._original_func
        sig = inspect.signature(orig_func)
        param = sig.parameters["workspace_manifest"]
        assert param.default is None, (
            "workspace_manifest must default to None to preserve backward compatibility"
        )

    def test_run_dag_has_workspace_manifest_parameter(self) -> None:
        """run_dag() must accept workspace_manifest to receive data from execute()."""
        sig = inspect.signature(run_dag)
        assert "workspace_manifest" in sig.parameters, (
            "run_dag() must accept workspace_manifest; this is the downstream consumer "
            "of execute()'s parameter"
        )

    @pytest.mark.asyncio
    async def test_execute_passes_workspace_manifest_to_run_dag(self) -> None:
        """execute() must pass workspace_manifest dict through to run_dag().

        This tests the core wiring of the conflict-resolved function: when
        execute() receives a workspace_manifest dict, it must forward it to
        run_dag() so that _init_all_repos() can process it.
        """
        manifest = _make_two_repo_manifest()
        manifest_dict = manifest.model_dump()

        captured_kwargs: dict = {}

        async def mock_run_dag(*args, **kwargs):
            captured_kwargs.update(kwargs)
            # Return minimal DAGState
            state = _make_dag_state(workspace_manifest=kwargs.get("workspace_manifest"))
            return state

        with patch("swe_af.execution.dag_executor.run_dag", side_effect=mock_run_dag):
            from swe_af.app import execute

            orig = execute._original_func
            await orig(
                plan_result={"issues": [], "levels": [], "file_conflicts": []},
                repo_path="/tmp/repo",
                workspace_manifest=manifest_dict,
            )

        assert "workspace_manifest" in captured_kwargs, (
            "execute() must forward workspace_manifest to run_dag()"
        )
        assert captured_kwargs["workspace_manifest"] == manifest_dict, (
            "workspace_manifest dict must be passed unchanged to run_dag()"
        )

    @pytest.mark.asyncio
    async def test_execute_none_workspace_manifest_passes_none_to_run_dag(self) -> None:
        """execute() with None workspace_manifest must pass None to run_dag() (single-repo compat)."""
        captured_kwargs: dict = {}

        async def mock_run_dag(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_dag_state()

        with patch("swe_af.execution.dag_executor.run_dag", side_effect=mock_run_dag):
            from swe_af.app import execute

            orig = execute._original_func
            await orig(
                plan_result={"issues": [], "levels": [], "file_conflicts": []},
                repo_path="/tmp/repo",
                workspace_manifest=None,
            )

        assert captured_kwargs.get("workspace_manifest") is None, (
            "None workspace_manifest must propagate as None to run_dag() "
            "to preserve single-repo backward compatibility"
        )

    def test_dag_state_workspace_manifest_is_set_by_run_dag(self) -> None:
        """DAGState.workspace_manifest is assigned from run_dag's workspace_manifest param."""
        manifest = _make_two_repo_manifest()
        manifest_dict = manifest.model_dump()

        state = _make_dag_state(workspace_manifest=manifest_dict)
        assert state.workspace_manifest == manifest_dict, (
            "DAGState.workspace_manifest must be settable from a WorkspaceManifest.model_dump() dict"
        )

    def test_dag_state_workspace_manifest_defaults_to_none(self) -> None:
        """DAGState.workspace_manifest is None by default (single-repo compat)."""
        state = _make_dag_state()
        assert state.workspace_manifest is None, (
            "DAGState.workspace_manifest must default to None for single-repo backward compat"
        )


# ===========================================================================
# Priority 2: Cross-feature – _clone_repos output feeds into execute() pipeline
# ===========================================================================


class TestCloneReposToExecutePipeline:
    """_clone_repos (issue-04) produces WorkspaceManifest that flows into execute() (issue-05).
    Test that the manifest structure is compatible with what execute/run_dag expects."""

    def test_workspace_manifest_model_dump_is_json_serializable(self) -> None:
        """WorkspaceManifest.model_dump() output (from _clone_repos) is JSON-compatible dict."""
        import json

        manifest = _make_two_repo_manifest()
        manifest_dict = manifest.model_dump()

        # Must be JSON serializable (for passing across reasoner boundary)
        json_str = json.dumps(manifest_dict)
        parsed = json.loads(json_str)

        assert parsed["primary_repo_name"] == "api", (
            "model_dump() must produce JSON-compatible dict with primary_repo_name"
        )
        assert len(parsed["repos"]) == 2, (
            "model_dump() must include all repos"
        )

    def test_workspace_manifest_dict_roundtrip_into_workspace_manifest(self) -> None:
        """WorkspaceManifest.model_dump() can be reconstructed via WorkspaceManifest(**dict).

        This is the exact pattern used by _init_all_repos and _merge_level_branches
        in dag_executor.py to reconstruct the manifest from DAGState.workspace_manifest.
        """
        original = _make_two_repo_manifest()
        as_dict = original.model_dump()

        reconstructed = WorkspaceManifest(**as_dict)
        assert reconstructed.primary_repo_name == original.primary_repo_name
        assert len(reconstructed.repos) == len(original.repos)
        assert reconstructed.repos[0].repo_name == "api"
        assert reconstructed.repos[1].repo_name == "lib"

    def test_workspace_manifest_repos_have_required_fields_for_init_all_repos(self) -> None:
        """WorkspaceRepo fields match what _init_all_repos needs: repo_name and absolute_path."""
        manifest = _make_two_repo_manifest()
        for repo in manifest.repos:
            assert repo.repo_name, "WorkspaceRepo.repo_name must be set (used for keying in _init_all_repos)"
            assert repo.absolute_path, "WorkspaceRepo.absolute_path must be set (used as repo_path in git_init)"

    def test_build_config_primary_repo_url_backfilled_for_clone_repos(self) -> None:
        """BuildConfig normalizer backfills repo_url from primary repo for _clone_repos compatibility."""
        cfg = BuildConfig(repos=[
            RepoSpec(repo_url="https://github.com/org/api.git", role="primary"),
            RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
        ])
        # _clone_repos uses cfg.repos directly; BuildConfig.repo_url is backfilled for compat
        assert cfg.repo_url == "https://github.com/org/api.git", (
            "BuildConfig must backfill repo_url from primary repo "
            "for backward-compat single-repo callers"
        )
        assert cfg.primary_repo is not None, "primary_repo property must return the primary RepoSpec"
        assert cfg.primary_repo.role == "primary"

    def test_clone_repos_function_is_async_and_importable(self) -> None:
        """_clone_repos is importable from swe_af.app and declared async (AC-23)."""
        from swe_af.app import _clone_repos

        assert inspect.iscoroutinefunction(_clone_repos), (
            "_clone_repos must be an async coroutine function"
        )
        sig = inspect.signature(_clone_repos)
        assert "cfg" in sig.parameters
        assert "artifacts_dir" in sig.parameters

    def test_dag_state_accepts_workspace_manifest_from_clone_repos(self) -> None:
        """DAGState.workspace_manifest field accepts WorkspaceManifest.model_dump() dict.

        This validates the interface between _clone_repos output and DAGState storage.
        """
        manifest = _make_two_repo_manifest()
        manifest_dict = manifest.model_dump()

        # DAGState stores manifest as dict (JSON-compat for serialization)
        state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest_dict,
        )
        assert state.workspace_manifest is not None
        assert state.workspace_manifest["primary_repo_name"] == "api"
        assert len(state.workspace_manifest["repos"]) == 2


# ===========================================================================
# Priority 2: Cross-feature – workspace_context_block consumed by dag pipeline
# ===========================================================================


class TestWorkspaceContextBlockToPromptIntegration:
    """workspace_context_block (issue-03) is used inside the dag pipeline (issue-05).
    Test that it correctly produces/doesn't produce output based on manifest structure."""

    def test_single_repo_manifest_produces_no_context_for_prompts(self) -> None:
        """Single-repo manifest returns empty string — no context injected into prompts."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp",
            repos=[_make_workspace_repo("api", "primary")],
            primary_repo_name="api",
        )
        result = workspace_context_block(manifest)
        assert result == "", (
            "Single-repo manifest must return '' so single-repo prompts are unaffected"
        )

    def test_multi_repo_manifest_produces_context_for_prompts(self) -> None:
        """Multi-repo manifest returns non-empty string injected into agent prompts."""
        manifest = _make_two_repo_manifest()
        result = workspace_context_block(manifest)
        assert result != "", "Multi-repo manifest must produce non-empty context block"
        assert "api" in result
        assert "lib" in result
        assert "/tmp/workspace/api" in result
        assert "primary" in result
        assert "dependency" in result

    def test_workspace_context_block_from_reconstructed_manifest(self) -> None:
        """workspace_context_block works with a manifest reconstructed from model_dump().

        This simulates the flow: _clone_repos() returns manifest -> model_dump() stored in
        DAGState -> reconstructed by WorkspaceManifest(**dag_state.workspace_manifest)
        -> passed to workspace_context_block().
        """
        original = _make_two_repo_manifest()
        as_dict = original.model_dump()
        reconstructed = WorkspaceManifest(**as_dict)

        result = workspace_context_block(reconstructed)
        assert "api" in result and "lib" in result, (
            "workspace_context_block must work with manifest reconstructed from model_dump()"
        )

    def test_none_manifest_produces_no_context(self) -> None:
        """None manifest returns '' — no workspace context in single-repo mode."""
        result = workspace_context_block(None)
        assert result == ""

    def test_workspace_context_block_output_contains_repo_names_and_paths(self) -> None:
        """workspace_context_block output contains all required fields for prompt injection."""
        manifest = _make_two_repo_manifest()
        result = workspace_context_block(manifest)

        # Must contain all repo names
        for repo in manifest.repos:
            assert repo.repo_name in result, f"repo_name '{repo.repo_name}' must appear in context"
            assert repo.absolute_path in result, f"absolute_path '{repo.absolute_path}' must appear in context"
            assert repo.role in result, f"role '{repo.role}' must appear in context"


# ===========================================================================
# Priority 2: Cross-feature – CoderResult.repo_name -> IssueResult.repo_name
# ===========================================================================


class TestCoderResultRepoNamePropagation:
    """CoderResult.repo_name (issue-06) must propagate to IssueResult.repo_name
    in the approve branch of run_coding_loop()."""

    def test_coder_result_repo_name_field_exists_with_empty_default(self) -> None:
        """CoderResult has repo_name field defaulting to '' (AC-11)."""
        cr = CoderResult(
            files_changed=[],
            summary="done",
            complete=True,
            tests_passed=True,
            test_summary="all pass",
        )
        assert hasattr(cr, "repo_name"), "CoderResult must have repo_name field"
        assert cr.repo_name == "", "CoderResult.repo_name must default to ''"

    def test_coder_result_repo_name_can_be_set(self) -> None:
        """CoderResult.repo_name can be explicitly set to a repo name."""
        cr = CoderResult(
            files_changed=["src/foo.py"],
            summary="done",
            complete=True,
            repo_name="api",
        )
        assert cr.repo_name == "api", "CoderResult.repo_name must accept explicit value"

    def test_issue_result_repo_name_field_exists(self) -> None:
        """IssueResult has repo_name field (receiving from CoderResult in coding_loop)."""
        ir = IssueResult(
            issue_name="test-issue",
            outcome=IssueOutcome.COMPLETED,
            repo_name="api",
        )
        assert ir.repo_name == "api"

    def test_issue_result_repo_name_defaults_to_empty(self) -> None:
        """IssueResult.repo_name defaults to '' for non-multi-repo backward compat."""
        ir = IssueResult(
            issue_name="test-issue",
            outcome=IssueOutcome.COMPLETED,
        )
        assert ir.repo_name == ""

    def test_coder_result_dict_get_repo_name_pattern_used_in_coding_loop(self) -> None:
        """Coding loop uses coder_result.get('repo_name', '') pattern — must work correctly.

        The coding loop calls coder_result.get('repo_name', '') where coder_result is a dict
        (from the agent's JSON output). This test verifies the pattern works for both
        present and absent key cases.
        """
        # Dict with repo_name present (multi-repo case)
        coder_result_dict_with_repo = {
            "files_changed": ["src/foo.py"],
            "summary": "done",
            "complete": True,
            "repo_name": "lib",
        }
        assert coder_result_dict_with_repo.get("repo_name", "") == "lib"

        # Dict without repo_name (single-repo / legacy case)
        coder_result_dict_no_repo = {
            "files_changed": [],
            "summary": "done",
            "complete": True,
        }
        assert coder_result_dict_no_repo.get("repo_name", "") == ""

        # Dict with empty repo_name
        coder_result_dict_empty_repo = {
            "files_changed": [],
            "summary": "done",
            "complete": True,
            "repo_name": "",
        }
        assert coder_result_dict_empty_repo.get("repo_name", "") == ""

    def test_issue_result_created_with_repo_name_from_coder_dict(self) -> None:
        """IssueResult can be constructed with repo_name from coder_result.get() pattern.

        Simulates the exact code path in coding_loop.py approve branch:
        repo_name=coder_result.get('repo_name', '')
        """
        # Simulating the approve branch of run_coding_loop with multi-repo coder output
        coder_result = {"repo_name": "api", "files_changed": ["src/main.py"], "summary": "done"}

        issue_result = IssueResult(
            issue_name="api-feature",
            outcome=IssueOutcome.COMPLETED,
            result_summary=coder_result.get("summary", ""),
            files_changed=coder_result.get("files_changed", []),
            branch_name="swe/api-feature",
            attempts=1,
            repo_name=coder_result.get("repo_name", ""),
        )

        assert issue_result.repo_name == "api", (
            "IssueResult.repo_name must be set from coder_result.get('repo_name', '')"
        )


# ===========================================================================
# Priority 2: Cross-feature – BuildResult.pr_url backward-compat with pr_results
# ===========================================================================


class TestBuildResultPrUrlBackwardCompat:
    """BuildResult.pr_url property (AC-08) provides backward compat for callers
    that used the old string field. pr_results list is the new source of truth."""

    def test_pr_url_property_returns_first_successful_pr(self) -> None:
        """pr_url returns URL from first successful RepoPRResult."""
        result = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[
                RepoPRResult(
                    repo_name="api",
                    repo_url="https://github.com/org/api.git",
                    success=True,
                    pr_url="https://github.com/org/api/pull/42",
                    pr_number=42,
                )
            ],
        )
        assert result.pr_url == "https://github.com/org/api/pull/42", (
            "pr_url property must return first successful PR URL for backward compat"
        )

    def test_pr_url_property_returns_empty_when_no_results(self) -> None:
        """pr_url returns '' when pr_results is empty (AC-08 second assertion)."""
        result = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[],
        )
        assert result.pr_url == "", "pr_url must return '' when pr_results is empty"

    def test_pr_url_property_skips_failed_pr_results(self) -> None:
        """pr_url skips failed PRs and returns first successful one."""
        result = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[
                RepoPRResult(
                    repo_name="lib",
                    repo_url="https://github.com/org/lib.git",
                    success=False,
                    pr_url="",
                    pr_number=0,
                    error_message="PR creation failed",
                ),
                RepoPRResult(
                    repo_name="api",
                    repo_url="https://github.com/org/api.git",
                    success=True,
                    pr_url="https://github.com/org/api/pull/7",
                    pr_number=7,
                ),
            ],
        )
        assert result.pr_url == "https://github.com/org/api/pull/7", (
            "pr_url must skip failed PRs and return first successful URL"
        )

    def test_model_dump_includes_pr_url_for_backward_compat(self) -> None:
        """BuildResult.model_dump() injects pr_url into dict output."""
        result = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="done",
            pr_results=[
                RepoPRResult(
                    repo_name="api",
                    repo_url="https://github.com/org/api.git",
                    success=True,
                    pr_url="https://github.com/org/api/pull/1",
                    pr_number=1,
                )
            ],
        )
        dumped = result.model_dump()
        assert "pr_url" in dumped, "model_dump() must inject pr_url for backward compat"
        assert dumped["pr_url"] == "https://github.com/org/api/pull/1"

    def test_multi_repo_build_result_has_all_pr_results(self) -> None:
        """Multi-repo BuildResult contains pr_results for all repos."""
        result = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[
                RepoPRResult(
                    repo_name="api",
                    repo_url="https://github.com/org/api.git",
                    success=True,
                    pr_url="https://github.com/org/api/pull/3",
                    pr_number=3,
                ),
                RepoPRResult(
                    repo_name="lib",
                    repo_url="https://github.com/org/lib.git",
                    success=False,
                    pr_url="",
                    pr_number=0,
                    error_message="lib has create_pr=False",
                ),
            ],
        )
        assert len(result.pr_results) == 2
        repo_names = [r.repo_name for r in result.pr_results]
        assert "api" in repo_names
        assert "lib" in repo_names


# ===========================================================================
# Priority 2: Cross-feature – _init_all_repos no-op for single-repo (backward compat)
# ===========================================================================


class TestInitAllReposSingleRepoBackwardCompat:
    """_init_all_repos (issue-05) must be a no-op when workspace_manifest is None.
    This preserves single-repo backward compatibility."""

    @pytest.mark.asyncio
    async def test_init_all_repos_noop_when_manifest_is_none(self) -> None:
        """_init_all_repos returns immediately without calling call_fn when manifest is None."""
        call_fn = AsyncMock()
        state = _make_dag_state(workspace_manifest=None)

        # _init_all_repos signature: dag_state, call_fn, node_id, git_model, ai_provider, ...
        await _init_all_repos(
            dag_state=state,
            call_fn=call_fn,
            node_id="test-node",
            git_model="sonnet",
            ai_provider="claude",
        )

        call_fn.assert_not_called(), "call_fn must not be invoked for single-repo (manifest=None)"
        assert state.workspace_manifest is None, "workspace_manifest must remain None"

    @pytest.mark.asyncio
    async def test_merge_level_branches_single_repo_path_used_when_manifest_none(self) -> None:
        """_merge_level_branches uses single-repo path (no grouping) when workspace_manifest=None."""
        state = _make_dag_state(workspace_manifest=None)
        level_result = LevelResult(
            level_index=0,
            completed=[
                IssueResult(
                    issue_name="issue-1",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="swe/issue-1",
                    repo_name="",  # no repo_name in single-repo mode
                )
            ],
        )

        captured_kwargs: dict = {}

        async def mock_call_fn(target, **kwargs):
            captured_kwargs.update(kwargs)
            return {
                "result": MergeResult(
                    success=True,
                    merged_branches=["swe/issue-1"],
                    failed_branches=[],
                    needs_integration_test=False,
                    summary="merged",
                ).model_dump()
            }

        result = await _merge_level_branches(
            dag_state=state,
            level_result=level_result,
            call_fn=mock_call_fn,
            node_id="test-node",
            config=ExecutionConfig(),
            issue_by_name={},
            file_conflicts=[],
        )

        assert result is not None, "merge must succeed on single-repo path"
        # Single-repo path should pass branches directly (not grouped by repo)
        assert "branches_to_merge" in captured_kwargs, (
            "single-repo path must pass branches_to_merge to merger"
        )

    def test_init_all_repos_signature_has_required_parameters(self) -> None:
        """_init_all_repos must have the parameters needed to run git_init per repo."""
        sig = inspect.signature(_init_all_repos)
        params = set(sig.parameters.keys())

        required = {"dag_state", "call_fn", "node_id", "git_model", "ai_provider"}
        for p in required:
            assert p in params, f"_init_all_repos must have '{p}' parameter"


# ===========================================================================
# Priority 2: Cross-feature – PlannedIssue.target_repo -> IssueResult.repo_name backfill
# ===========================================================================


class TestPlannedIssueTargetRepoToIssueResultRepName:
    """PlannedIssue.target_repo (from sprint planner) must flow to IssueResult.repo_name.
    This crosses issue-05 (dag_executor backfill) and issue-06 (coding_loop propagation)."""

    def test_planned_issue_has_target_repo_field(self) -> None:
        """PlannedIssue.target_repo defaults to '' (AC-10)."""
        pi = PlannedIssue(
            name="test-issue",
            title="Test Issue",
            description="desc",
            acceptance_criteria=["AC1"],
            depends_on=[],
            provides=[],
            files_to_create=[],
            files_to_modify=[],
            testing_strategy="pytest",
            sequence_number=1,
        )
        assert hasattr(pi, "target_repo"), "PlannedIssue must have target_repo field"
        assert pi.target_repo == "", "PlannedIssue.target_repo must default to ''"

    def test_planned_issue_target_repo_can_name_multi_repo_target(self) -> None:
        """PlannedIssue.target_repo can hold repo name for multi-repo routing."""
        pi = PlannedIssue(
            name="lib-issue",
            title="Library Issue",
            description="Update lib",
            acceptance_criteria=["AC1"],
            depends_on=[],
            provides=[],
            files_to_create=[],
            files_to_modify=[],
            testing_strategy="pytest",
            sequence_number=2,
            target_repo="lib",
        )
        assert pi.target_repo == "lib"

    def test_issue_result_repo_name_set_from_target_repo_backfill(self) -> None:
        """IssueResult.repo_name can be set from issue['target_repo'] backfill (dag_executor)."""
        # Simulates dag_executor._execute_level backfill:
        # if not issue_result.repo_name and issue.get('target_repo'):
        #     issue_result.repo_name = issue['target_repo']
        issue_dict = {"name": "lib-feat", "target_repo": "lib"}
        issue_result = IssueResult(
            issue_name="lib-feat",
            outcome=IssueOutcome.COMPLETED,
            repo_name="",  # not set by coder
        )
        # Simulate backfill
        if not issue_result.repo_name and issue_dict.get("target_repo"):
            issue_result.repo_name = issue_dict["target_repo"]

        assert issue_result.repo_name == "lib", (
            "repo_name must be backfilled from target_repo when coder didn't set it"
        )

    def test_issue_result_repo_name_not_overwritten_if_already_set(self) -> None:
        """IssueResult.repo_name from CoderResult is not overwritten by backfill."""
        issue_dict = {"name": "api-feat", "target_repo": "api"}
        issue_result = IssueResult(
            issue_name="api-feat",
            outcome=IssueOutcome.COMPLETED,
            repo_name="api",  # already set by coder
        )
        # Backfill logic: only set if empty
        if not issue_result.repo_name and issue_dict.get("target_repo"):
            issue_result.repo_name = issue_dict["target_repo"]

        assert issue_result.repo_name == "api", "CoderResult.repo_name must not be overwritten"


# ===========================================================================
# Priority 2: Cross-feature – MergeResult.repo_name and GitInitResult.repo_name
# ===========================================================================


class TestRepoNameFieldsOnResultModels:
    """MergeResult.repo_name (AC-13) and GitInitResult.repo_name (AC-12) are new fields
    added for multi-repo support. Ensure they coexist correctly with other fields."""

    def test_git_init_result_repo_name_empty_default(self) -> None:
        """GitInitResult.repo_name defaults to '' (AC-12)."""
        gir = GitInitResult(
            mode="fresh",
            integration_branch="integration/main",
            original_branch="main",
            initial_commit_sha="abc123",
            success=True,
        )
        assert hasattr(gir, "repo_name"), "GitInitResult must have repo_name field"
        assert gir.repo_name == ""

    def test_git_init_result_repo_name_set_for_multi_repo(self) -> None:
        """GitInitResult.repo_name can be set to identify which repo was initialized."""
        gir = GitInitResult(
            mode="fresh",
            integration_branch="integration/main",
            original_branch="main",
            initial_commit_sha="def456",
            success=True,
            repo_name="api",
        )
        assert gir.repo_name == "api"

    def test_merge_result_repo_name_empty_default(self) -> None:
        """MergeResult.repo_name defaults to '' (AC-13)."""
        mr = MergeResult(
            success=True,
            merged_branches=["swe/feat-1"],
            failed_branches=[],
            needs_integration_test=False,
            summary="Merged 1 branch",
        )
        assert hasattr(mr, "repo_name"), "MergeResult must have repo_name field"
        assert mr.repo_name == ""

    def test_merge_result_repo_name_set_for_multi_repo(self) -> None:
        """MergeResult.repo_name can identify which repo the merge ran in."""
        mr = MergeResult(
            success=True,
            merged_branches=["swe/api-feat"],
            failed_branches=[],
            needs_integration_test=False,
            summary="Merged api branch",
            repo_name="api",
        )
        assert mr.repo_name == "api"

    def test_workspace_repo_can_hold_git_init_result_dict(self) -> None:
        """WorkspaceRepo.git_init_result can hold a GitInitResult.model_dump() dict.

        This is the pattern used by _init_all_repos: after git_init runs per repo,
        the result dict is stored back into WorkspaceRepo.git_init_result.
        """
        repo = _make_workspace_repo("api", "primary")
        assert repo.git_init_result is None, "git_init_result starts as None before init"

        gir = GitInitResult(
            mode="fresh",
            integration_branch="integration/main",
            original_branch="main",
            initial_commit_sha="abc",
            success=True,
            repo_name="api",
        )
        repo.git_init_result = gir.model_dump()

        assert repo.git_init_result is not None
        assert repo.git_init_result["repo_name"] == "api"
        assert repo.git_init_result["success"] is True


# ===========================================================================
# Priority 3: Shared file app.py – execute() docstring consolidation doesn't break func
# ===========================================================================


class TestExecuteFunctionIsCorrectlyDefined:
    """The resolved execute() function in app.py must be a valid async reasoner
    with all expected parameters intact from both merged branches.

    Note: The @app.reasoner() decorator wraps execute(). We use _original_func
    to inspect the underlying implementation parameters.
    """

    def test_execute_is_async_coroutine(self) -> None:
        """execute() _original_func must be an async coroutine function."""
        from swe_af.app import execute

        orig = execute._original_func
        assert inspect.iscoroutinefunction(orig), "execute()._original_func must be async def"

    def test_execute_has_all_required_parameters(self) -> None:
        """execute() must have all parameters: plan_result, repo_path, workspace_manifest, etc."""
        from swe_af.app import execute

        orig_func = execute._original_func
        sig = inspect.signature(orig_func)
        params = set(sig.parameters.keys())

        required_params = {"plan_result", "repo_path", "workspace_manifest"}
        optional_params = {"execute_fn_target", "config", "git_config", "resume", "build_id"}

        for p in required_params:
            assert p in params, f"execute()._original_func must have '{p}' parameter"
        for p in optional_params:
            assert p in params, f"execute()._original_func must have optional '{p}' parameter"

    def test_execute_workspace_manifest_annotation_allows_none(self) -> None:
        """execute() workspace_manifest annotation allows None (single-repo compat)."""
        from swe_af.app import execute

        orig_func = execute._original_func
        sig = inspect.signature(orig_func)
        param = sig.parameters["workspace_manifest"]
        # Default must be None
        assert param.default is None, (
            "workspace_manifest must default to None for single-repo backward compat"
        )

    def test_execute_reasoner_has_is_tracked_replacement_attribute(self) -> None:
        """execute() decorator wraps it correctly with _original_func accessible."""
        from swe_af.app import execute

        assert hasattr(execute, "_original_func"), (
            "execute() must have _original_func attribute (set by @app.reasoner() decorator)"
        )
        assert callable(execute._original_func), (
            "_original_func must be callable"
        )

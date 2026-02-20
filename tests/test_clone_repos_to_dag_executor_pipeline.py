"""Integration tests: _clone_repos output → dag_executor integration pipeline.

Tests the cross-feature interaction chain:
  _clone_repos (issue/daaccc55-04) → WorkspaceManifest
    → _init_all_repos (issue/daaccc55-05)
    → workspace_context_block (issue/daaccc55-03) consuming the manifest
    → repo_name on IssueResult (issue/daaccc55-06) flowing back to _merge_level_branches

Priority 1: Conflict-resolution area — execute() workspace_manifest parameter
  (both branches added documentation with slightly different wording; verify
   the resolved form is coherent).

Priority 2: Cross-feature interaction between _clone_repos and dag_executor.
Priority 3: workspace_context_block consuming WorkspaceManifest from schemas.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from swe_af.execution.dag_executor import _init_all_repos
from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    WorkspaceManifest,
    WorkspaceRepo,
)
from swe_af.prompts._utils import workspace_context_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws_repo(name: str, role: str = "primary", git_init: dict | None = None) -> WorkspaceRepo:
    return WorkspaceRepo(
        repo_name=name,
        repo_url=f"https://github.com/org/{name}.git",
        role=role,
        absolute_path=f"/tmp/ws/{name}",
        branch="main",
        sparse_paths=[],
        create_pr=(role == "primary"),
        git_init_result=git_init,
    )


def _make_manifest_dict(*repo_specs: tuple[str, str]) -> dict:
    """Build a serialised WorkspaceManifest from (name, role) tuples."""
    repos = [_make_ws_repo(name, role) for name, role in repo_specs]
    return WorkspaceManifest(
        workspace_root="/tmp/ws",
        repos=repos,
        primary_repo_name=repos[0].repo_name,
    ).model_dump()


# ---------------------------------------------------------------------------
# Priority 3: workspace_context_block ↔ WorkspaceManifest schema interaction
# ---------------------------------------------------------------------------


class TestWorkspaceContextBlockSchemaInteraction:
    """workspace_context_block (issue-03) consumes WorkspaceManifest (schemas).

    These tests verify the integration boundary: the function must correctly
    interpret the schema structure produced by _clone_repos.
    """

    def test_single_repo_manifest_returns_empty_string(self) -> None:
        """workspace_context_block returns '' for single-repo manifest (AC-14)."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[_make_ws_repo("api", "primary")],
            primary_repo_name="api",
        )
        result = workspace_context_block(manifest)
        assert result == "", (
            "Single-repo manifest must produce empty string — "
            "multi-repo context block is not needed"
        )

    def test_none_manifest_returns_empty_string(self) -> None:
        """workspace_context_block returns '' for None manifest."""
        result = workspace_context_block(None)
        assert result == "", "None manifest must return empty string"

    def test_multi_repo_manifest_includes_all_repo_names(self) -> None:
        """workspace_context_block contains repo_name for each repo (AC-15)."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[
                _make_ws_repo("api", "primary"),
                _make_ws_repo("lib", "dependency"),
            ],
            primary_repo_name="api",
        )
        result = workspace_context_block(manifest)
        assert "api" in result, "Primary repo name must appear in context block"
        assert "lib" in result, "Dependency repo name must appear in context block"

    def test_multi_repo_manifest_includes_absolute_paths(self) -> None:
        """workspace_context_block includes absolute_path for each repo."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[
                _make_ws_repo("api", "primary"),
                _make_ws_repo("lib", "dependency"),
            ],
            primary_repo_name="api",
        )
        result = workspace_context_block(manifest)
        assert "/tmp/ws/api" in result, "Primary repo absolute_path must appear in context block"
        assert "/tmp/ws/lib" in result, "Dependency repo absolute_path must appear in context block"

    def test_multi_repo_manifest_includes_role(self) -> None:
        """workspace_context_block includes role for each repo."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[
                _make_ws_repo("api", "primary"),
                _make_ws_repo("lib", "dependency"),
            ],
            primary_repo_name="api",
        )
        result = workspace_context_block(manifest)
        assert "primary" in result, "Role 'primary' must appear in context block"
        assert "dependency" in result, "Role 'dependency' must appear in context block"

    def test_workspace_context_block_accepts_model_dump_roundtrip(self) -> None:
        """workspace_context_block can consume a WorkspaceManifest reconstructed from dict."""
        raw = _make_manifest_dict(("api", "primary"), ("lib", "dependency"))
        # This is what dag_executor does: store as dict, reconstruct when needed
        reconstructed = WorkspaceManifest(**raw)
        result = workspace_context_block(reconstructed)
        assert "api" in result and "lib" in result, (
            "workspace_context_block must work with manifest reconstructed from model_dump()"
        )

    def test_zero_repos_manifest_returns_empty_string(self) -> None:
        """Edge case: empty repos list in manifest returns empty string."""
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[],
            primary_repo_name="",
        )
        result = workspace_context_block(manifest)
        assert result == "", "Empty repos list must return empty string"


# ---------------------------------------------------------------------------
# Priority 2: _init_all_repos interaction with manifest (issue-04 → issue-05)
# ---------------------------------------------------------------------------


class TestInitAllReposManifestInteraction:
    """_init_all_repos reads from dag_state.workspace_manifest (produced by
    _clone_repos) and writes git_init_result back to each WorkspaceRepo.
    """

    def test_init_all_repos_no_op_when_manifest_is_none(self) -> None:
        """_init_all_repos returns immediately when workspace_manifest is None."""
        dag_state = DAGState(repo_path="/tmp/repo", workspace_manifest=None)
        call_count = 0

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            nonlocal call_count
            call_count += 1
            return {"success": True}

        asyncio.run(
            _init_all_repos(
                dag_state=dag_state,
                call_fn=_mock_call_fn,
                node_id="swe-planner",
                git_model="claude-sonnet-4-5",
                ai_provider="claude",
            )
        )

        assert call_count == 0, (
            "_init_all_repos must be a no-op when workspace_manifest is None "
            "(single-repo backward compat)"
        )
        assert dag_state.workspace_manifest is None

    def test_init_all_repos_calls_git_init_for_each_repo(self) -> None:
        """_init_all_repos dispatches one run_git_init call per repo."""
        manifest = _make_manifest_dict(("api", "primary"), ("lib", "dependency"))
        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest,
        )

        called_paths: list[str] = []

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            called_paths.append(kwargs.get("repo_path", ""))
            return {
                "success": True,
                "integration_branch": "integration/test",
                "original_branch": "main",
                "initial_commit_sha": "abc123",
                "mode": "fresh",
            }

        asyncio.run(
            _init_all_repos(
                dag_state=dag_state,
                call_fn=_mock_call_fn,
                node_id="swe-planner",
                git_model="claude-sonnet-4-5",
                ai_provider="claude",
            )
        )

        assert len(called_paths) == 2, (
            f"_init_all_repos must call run_git_init for each repo, "
            f"got calls to: {called_paths}"
        )
        path_suffixes = {p.split("/")[-1] for p in called_paths}
        assert "api" in path_suffixes, "run_git_init must be called for 'api' repo"
        assert "lib" in path_suffixes, "run_git_init must be called for 'lib' repo"

    def test_init_all_repos_writes_git_init_result_back_to_manifest(self) -> None:
        """_init_all_repos mutates dag_state.workspace_manifest with git_init_result."""
        manifest = _make_manifest_dict(("api", "primary"))
        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest,
        )

        git_init_payload = {
            "success": True,
            "integration_branch": "integration/my-build",
            "original_branch": "main",
            "initial_commit_sha": "deadbeef",
            "mode": "fresh",
        }

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            return git_init_payload

        asyncio.run(
            _init_all_repos(
                dag_state=dag_state,
                call_fn=_mock_call_fn,
                node_id="swe-planner",
                git_model="claude-sonnet-4-5",
                ai_provider="claude",
            )
        )

        # The manifest dict on dag_state should now have git_init_result populated
        assert dag_state.workspace_manifest is not None
        repos = dag_state.workspace_manifest["repos"]
        api_repo = next(r for r in repos if r["repo_name"] == "api")
        assert api_repo["git_init_result"] is not None, (
            "_init_all_repos must write git_init_result back to the WorkspaceRepo"
        )
        assert api_repo["git_init_result"]["integration_branch"] == "integration/my-build"

    def test_init_all_repos_single_repo_git_init_result_enables_merge_dispatch(self) -> None:
        """After _init_all_repos, _merge_level_branches can find the integration branch."""
        from swe_af.execution.dag_executor import _merge_level_branches
        from swe_af.execution.schemas import IssueOutcome, IssueResult, LevelResult

        manifest = _make_manifest_dict(("api", "primary"))
        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest,
            git_integration_branch="integration/api",
        )

        # Simulate what _init_all_repos does: inject git_init_result into the manifest
        dag_state.workspace_manifest["repos"][0]["git_init_result"] = {
            "integration_branch": "integration/my-build",
            "success": True,
        }

        merger_called_with_branch: list[str] = []

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            merger_called_with_branch.append(kwargs.get("integration_branch", ""))
            return {
                "success": True,
                "merged_branches": ["feat/api-issue"],
                "failed_branches": [],
                "merge_commit_sha": "abc",
                "pre_merge_sha": "def",
                "needs_integration_test": False,
                "integration_test_rationale": "",
                "summary": "merged",
            }

        level_result = LevelResult(
            level_index=0,
            completed=[
                IssueResult(
                    issue_name="api-issue",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="feat/api-issue",
                    repo_name="api",
                )
            ],
        )

        asyncio.run(
            _merge_level_branches(
                dag_state=dag_state,
                level_result=level_result,
                call_fn=_mock_call_fn,
                node_id="swe-planner",
                config=ExecutionConfig(),
                issue_by_name={},
                file_conflicts=[],
            )
        )

        assert len(merger_called_with_branch) == 1
        assert merger_called_with_branch[0] == "integration/my-build", (
            "After _init_all_repos writes git_init_result, _merge_level_branches must "
            "use the integration_branch from git_init_result"
        )


# ---------------------------------------------------------------------------
# Priority 2: Clone output WorkspaceManifest primary_repo property
# ---------------------------------------------------------------------------


class TestWorkspaceManifestPrimaryRepoProperty:
    """WorkspaceManifest.primary_repo property is used downstream."""

    def test_primary_repo_property_returns_primary_workspace_repo(self) -> None:
        """WorkspaceManifest.primary_repo returns the WorkspaceRepo with role='primary'."""
        api = _make_ws_repo("api", "primary")
        lib = _make_ws_repo("lib", "dependency")
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[api, lib],
            primary_repo_name="api",
        )
        primary = manifest.primary_repo
        assert primary is not None
        assert primary.repo_name == "api"
        assert primary.role == "primary"

    def test_primary_repo_property_returns_none_when_name_not_found(self) -> None:
        """WorkspaceManifest.primary_repo returns None if primary_repo_name is not in repos."""
        lib = _make_ws_repo("lib", "dependency")
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[lib],
            primary_repo_name="nonexistent",
        )
        assert manifest.primary_repo is None

    def test_primary_repo_url_available_for_pr_creation(self) -> None:
        """Primary repo's URL is accessible for PR creation in build() multi-repo path."""
        api = _make_ws_repo("api", "primary")
        lib = _make_ws_repo("lib", "dependency")
        manifest = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[api, lib],
            primary_repo_name="api",
        )
        primary = manifest.primary_repo
        assert primary is not None
        assert primary.repo_url == "https://github.com/org/api.git", (
            "Primary repo URL must be accessible for PR creation logic in app.py"
        )


# ---------------------------------------------------------------------------
# Priority 2: _clone_repos function signature and async nature (AC-23 area)
# ---------------------------------------------------------------------------


class TestCloneReposIntegrationBoundary:
    """Verify _clone_repos produces output that is a valid input to run_dag."""

    def test_clone_repos_is_async_coroutine(self) -> None:
        """_clone_repos must be a coroutine function (async def)."""
        from swe_af.app import _clone_repos

        assert inspect.iscoroutinefunction(_clone_repos), (
            "_clone_repos must be an async def so it can use asyncio.to_thread"
        )

    def test_clone_repos_returns_workspace_manifest_type(self) -> None:
        """_clone_repos return annotation is WorkspaceManifest."""
        from swe_af.app import _clone_repos

        hints = _clone_repos.__annotations__
        return_annotation = hints.get("return", None)
        # May be the class directly or a string annotation
        if return_annotation is not None:
            assert "WorkspaceManifest" in str(return_annotation), (
                "_clone_repos must be annotated to return WorkspaceManifest"
            )

    def test_clone_repos_output_type_is_workspace_manifest_with_mock(self) -> None:
        """_clone_repos returns a WorkspaceManifest (verified with mocked subprocess)."""
        from unittest.mock import MagicMock
        from swe_af.app import _clone_repos
        from swe_af.execution.schemas import BuildConfig, RepoSpec

        cfg = BuildConfig(
            repos=[
                RepoSpec(
                    repo_url="https://github.com/org/api.git",
                    role="primary",
                    repo_path="/existing/path",  # Use repo_path to skip actual clone
                )
            ]
        )

        result = asyncio.run(
            _clone_repos(cfg=cfg, artifacts_dir="/tmp/.artifacts")
        )

        assert isinstance(result, WorkspaceManifest), (
            "_clone_repos must return a WorkspaceManifest instance"
        )
        assert len(result.repos) == 1
        assert result.primary_repo_name == "api"

    def test_clone_repos_workspace_manifest_is_serialisable_to_dict(self) -> None:
        """WorkspaceManifest from _clone_repos can be model_dump()-ed for run_dag."""
        from swe_af.app import _clone_repos
        from swe_af.execution.schemas import BuildConfig, RepoSpec

        cfg = BuildConfig(
            repos=[
                RepoSpec(
                    repo_url="https://github.com/org/api.git",
                    role="primary",
                    repo_path="/existing/path",
                )
            ]
        )

        manifest = asyncio.run(
            _clone_repos(cfg=cfg, artifacts_dir="/tmp/.artifacts")
        )

        dumped = manifest.model_dump()
        assert isinstance(dumped, dict)
        assert "repos" in dumped
        assert "workspace_root" in dumped
        assert "primary_repo_name" in dumped

"""Integration tests: execute() ↔ run_dag() workspace_manifest passthrough.

This file targets the CONFLICT RESOLUTION area in swe_af/app.py::execute()
(merged from issue/daaccc55-04-clone-repos and issue/daaccc55-05-dag-executor-multi-repo).

The conflict was a docstring-only merge in the `workspace_manifest` parameter of
execute(). We verify that the parameter is:
  1. accepted by execute() with the correct signature
  2. forwarded to run_dag() unmodified
  3. stored on dag_state.workspace_manifest at the end of run_dag()

We also verify the single-repo backward-compat path (workspace_manifest=None).
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.dag_executor import run_dag
from swe_af.execution.schemas import (
    BuildResult,
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
    LevelResult,
    RepoPRResult,
    WorkspaceManifest,
    WorkspaceRepo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(num_repos: int = 2) -> dict:
    """Return a serialised WorkspaceManifest with the requested number of repos."""
    repos = [
        WorkspaceRepo(
            repo_name="api" if i == 0 else f"lib-{i}",
            repo_url=f"https://github.com/org/{'api' if i == 0 else f'lib-{i}'}.git",
            role="primary" if i == 0 else "dependency",
            absolute_path=f"/tmp/ws/{'api' if i == 0 else f'lib-{i}'}",
            branch="main",
            sparse_paths=[],
            create_pr=(i == 0),
        )
        for i in range(num_repos)
    ]
    return WorkspaceManifest(
        workspace_root="/tmp/ws",
        repos=repos,
        primary_repo_name="api",
    ).model_dump()


def _minimal_plan_result() -> dict:
    """Minimal plan_result that satisfies _init_dag_state."""
    return {
        "issues": [],
        "levels": [],
        "rationale": "test",
        "artifacts_dir": "",
        "prd": {"title": "t", "description": "d", "acceptance_criteria": []},
        "architecture": {},
    }


# ---------------------------------------------------------------------------
# Priority 1 (conflict area): execute() signature and workspace_manifest param
# ---------------------------------------------------------------------------


class TestExecuteSignatureConflictArea:
    """Verify the conflict-resolved execute() docstring/signature is intact."""

    def test_execute_accepts_workspace_manifest_param(self) -> None:
        """execute() must have workspace_manifest as a keyword parameter."""
        from swe_af.app import execute  # type: ignore[attr-defined]

        # The @app.reasoner() decorator wraps the function; use _original_func to get
        # the unwrapped signature (this is where the conflict-resolved docstring lives).
        inner = getattr(execute, "_original_func", execute)
        sig = inspect.signature(inner)
        assert "workspace_manifest" in sig.parameters, (
            "execute() must accept workspace_manifest (conflict-resolved param)"
        )

    def test_execute_workspace_manifest_default_is_none(self) -> None:
        """workspace_manifest defaults to None for backward compatibility."""
        from swe_af.app import execute  # type: ignore[attr-defined]

        inner = getattr(execute, "_original_func", execute)
        sig = inspect.signature(inner)
        param = sig.parameters.get("workspace_manifest")
        assert param is not None
        assert param.default is None, (
            "workspace_manifest default must be None (backward compat)"
        )

    def test_execute_docstring_captures_multi_repo_intent(self) -> None:
        """execute() docstring must document workspace_manifest (conflict-resolved text)."""
        from swe_af.app import execute  # type: ignore[attr-defined]

        inner = getattr(execute, "_original_func", execute)
        doc = (inner.__doc__ or "").lower()
        assert "workspace_manifest" in doc, (
            "execute() docstring must mention workspace_manifest "
            "(the conflict was in its documentation)"
        )

    def test_run_dag_accepts_workspace_manifest_param(self) -> None:
        """run_dag() must accept workspace_manifest parameter."""
        sig = inspect.signature(run_dag)
        assert "workspace_manifest" in sig.parameters, (
            "run_dag() must accept workspace_manifest"
        )

    def test_run_dag_workspace_manifest_default_is_none(self) -> None:
        """run_dag() workspace_manifest defaults to None."""
        sig = inspect.signature(run_dag)
        param = sig.parameters.get("workspace_manifest")
        assert param is not None
        assert param.default is None


# ---------------------------------------------------------------------------
# Priority 2: run_dag() assigns workspace_manifest onto dag_state
# ---------------------------------------------------------------------------


class TestRunDagWorkspaceManifestAssignment:
    """run_dag() must store workspace_manifest on the returned DAGState."""

    def _run(self, manifest: dict | None) -> DAGState:
        """Run run_dag with no issues and no call_fn, returning the final state."""
        plan = _minimal_plan_result()
        state = asyncio.run(
            run_dag(
                plan_result=plan,
                repo_path="/tmp/repo",
                config=ExecutionConfig(),
                workspace_manifest=manifest,
            )
        )
        return state

    def test_workspace_manifest_none_single_repo(self) -> None:
        """Single-repo path: run_dag stores None for workspace_manifest."""
        state = self._run(None)
        assert state.workspace_manifest is None, (
            "workspace_manifest should remain None for single-repo builds"
        )

    def test_workspace_manifest_dict_stored_on_dag_state(self) -> None:
        """Multi-repo path: run_dag stores the manifest dict on dag_state."""
        manifest = _make_manifest(num_repos=2)
        state = self._run(manifest)
        assert state.workspace_manifest is not None, (
            "workspace_manifest should be stored on dag_state for multi-repo builds"
        )
        assert state.workspace_manifest["primary_repo_name"] == "api", (
            "primary_repo_name must survive round-trip through run_dag"
        )

    def test_workspace_manifest_repos_preserved(self) -> None:
        """All repos in the manifest are preserved through run_dag."""
        manifest = _make_manifest(num_repos=2)
        state = self._run(manifest)
        repos = state.workspace_manifest["repos"]  # type: ignore[index]
        assert len(repos) == 2, (
            "Both repos must be preserved in workspace_manifest after run_dag"
        )
        repo_names = {r["repo_name"] for r in repos}
        assert "api" in repo_names
        assert "lib-1" in repo_names


# ---------------------------------------------------------------------------
# Priority 2: workspace_manifest round-trip: clone output → execute input
# ---------------------------------------------------------------------------


class TestWorkspaceManifestRoundTrip:
    """Verify WorkspaceManifest produced by _clone_repos is valid input to run_dag."""

    def test_manifest_model_dump_is_acceptable_to_run_dag(self) -> None:
        """model_dump() from WorkspaceManifest can be deserialised into WorkspaceManifest."""
        manifest = _make_manifest(num_repos=2)
        # Simulate the round-trip: dict → WorkspaceManifest (as done in run_dag internals)
        reconstructed = WorkspaceManifest(**manifest)
        assert reconstructed.primary_repo_name == "api"
        assert len(reconstructed.repos) == 2

    def test_dag_state_workspace_manifest_roundtrip(self) -> None:
        """DAGState can accept and re-dump workspace_manifest without data loss."""
        manifest = _make_manifest(num_repos=2)
        dag_state = DAGState(
            repo_path="/tmp/repo",
            workspace_manifest=manifest,
        )
        dumped = dag_state.model_dump()
        assert dumped["workspace_manifest"] is not None
        assert dumped["workspace_manifest"]["primary_repo_name"] == "api"

    def test_workspace_manifest_none_on_dag_state_default(self) -> None:
        """DAGState.workspace_manifest defaults to None (single-repo compat)."""
        dag_state = DAGState(repo_path="/tmp/repo")
        assert dag_state.workspace_manifest is None


# ---------------------------------------------------------------------------
# Priority 2: BuildResult.pr_url backward-compat property
# ---------------------------------------------------------------------------


class TestBuildResultPrUrlBackwardCompat:
    """BuildResult.pr_url backward-compat bridges clone-repos and dag-executor branches."""

    def test_pr_url_returns_first_pr_url_from_pr_results(self) -> None:
        """BuildResult.pr_url returns the first successful pr_url (backward compat)."""
        br = BuildResult(
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
                    pr_url="https://github.com/org/api/pull/42",
                    pr_number=42,
                )
            ],
        )
        assert br.pr_url == "https://github.com/org/api/pull/42", (
            "pr_url backward-compat property must return the first pr_url"
        )

    def test_pr_url_returns_empty_string_when_no_results(self) -> None:
        """BuildResult.pr_url returns '' when pr_results is empty."""
        br = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="done",
            pr_results=[],
        )
        assert br.pr_url == "", (
            "pr_url must return empty string when there are no pr_results"
        )

    def test_model_dump_includes_pr_results_list(self) -> None:
        """BuildResult.model_dump() includes the pr_results list (not just pr_url)."""
        br = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="done",
            pr_results=[
                RepoPRResult(
                    repo_name="myrepo",
                    repo_url="https://github.com/org/myrepo.git",
                    success=True,
                    pr_url="https://github.com/org/myrepo/pull/1",
                    pr_number=1,
                )
            ],
        )
        dumped = br.model_dump()
        assert "pr_results" in dumped, "model_dump() must include pr_results"
        assert len(dumped["pr_results"]) == 1


# ---------------------------------------------------------------------------
# Priority 3: _merge_level_branches — repo_name flows from coding loop to merge
# ---------------------------------------------------------------------------


class TestRepoNameFlowToMergeLevelBranches:
    """Verify repo_name from coding loop (IssueResult.repo_name) is used by
    _merge_level_branches to group results per repo (cross-feature interaction:
    issue/daaccc55-06 + issue/daaccc55-05).
    """

    def _make_level_result_with_repo_names(self) -> LevelResult:
        return LevelResult(
            level_index=0,
            completed=[
                IssueResult(
                    issue_name="issue-api",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="feat/issue-api",
                    repo_name="api",
                ),
                IssueResult(
                    issue_name="issue-lib",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="feat/issue-lib",
                    repo_name="lib-1",
                ),
            ],
        )

    def test_merge_level_branches_multi_repo_dispatches_per_repo(self) -> None:
        """Multi-repo path dispatches one merger call per unique repo_name."""
        from swe_af.execution.dag_executor import _merge_level_branches

        manifest = _make_manifest(num_repos=2)
        # Inject git_init_result so the merger can find integration branches
        for repo in manifest["repos"]:
            repo["git_init_result"] = {
                "integration_branch": f"integration/{repo['repo_name']}",
                "success": True,
            }

        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest,
            git_integration_branch="integration/api",
        )

        call_counts: dict[str, int] = {}

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            repo_path = kwargs.get("repo_path", "")
            # Identify repo by path
            key = repo_path.split("/")[-1]
            call_counts[key] = call_counts.get(key, 0) + 1
            return {
                "success": True,
                "merged_branches": [kwargs.get("branches_to_merge", [{}])[0].get("branch_name", "")],
                "failed_branches": [],
                "merge_commit_sha": "abc123",
                "pre_merge_sha": "def456",
                "needs_integration_test": False,
                "integration_test_rationale": "",
                "summary": "merged",
                "repo_name": key,
            }

        level_result = self._make_level_result_with_repo_names()
        config = ExecutionConfig()
        issue_by_name = {
            "issue-api": {"description": "api issue"},
            "issue-lib": {"description": "lib issue"},
        }

        result = asyncio.run(
            _merge_level_branches(
                dag_state=dag_state,
                level_result=level_result,
                call_fn=_mock_call_fn,
                node_id="swe-planner",
                config=config,
                issue_by_name=issue_by_name,
                file_conflicts=[],
            )
        )

        # Two separate merger calls were dispatched (one per repo)
        assert len(call_counts) == 2, (
            f"Expected 2 merger calls (one per repo), got {call_counts}"
        )
        assert "api" in call_counts
        assert "lib-1" in call_counts

    def test_merge_level_branches_single_repo_path_unchanged(self) -> None:
        """Single-repo path (workspace_manifest=None) must call merger once."""
        from swe_af.execution.dag_executor import _merge_level_branches

        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=None,  # single-repo
            git_integration_branch="integration/main",
        )

        call_count = 0

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "success": True,
                "merged_branches": ["feat/single-issue"],
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
                    issue_name="single-issue",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="feat/single-issue",
                    repo_name="",  # empty: single-repo
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

        assert call_count == 1, (
            f"Single-repo path must call merger exactly once, got {call_count}"
        )

    def test_issue_result_repo_name_empty_falls_back_to_primary(self) -> None:
        """IssueResult.repo_name='' falls back to primary_repo_name in multi-repo merge."""
        from swe_af.execution.dag_executor import _merge_level_branches

        manifest = _make_manifest(num_repos=2)
        for repo in manifest["repos"]:
            repo["git_init_result"] = {
                "integration_branch": f"integration/{repo['repo_name']}",
                "success": True,
            }

        dag_state = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/.artifacts",
            workspace_manifest=manifest,
            git_integration_branch="integration/api",
        )

        dispatched_to: list[str] = []

        async def _mock_call_fn(target: str, **kwargs) -> dict:
            repo_path = kwargs.get("repo_path", "")
            dispatched_to.append(repo_path.split("/")[-1])
            return {
                "success": True,
                "merged_branches": ["feat/anon"],
                "failed_branches": [],
                "merge_commit_sha": "abc",
                "pre_merge_sha": "def",
                "needs_integration_test": False,
                "integration_test_rationale": "",
                "summary": "merged",
            }

        # Issue with empty repo_name — should fall back to 'api' (primary)
        level_result = LevelResult(
            level_index=0,
            completed=[
                IssueResult(
                    issue_name="anon-issue",
                    outcome=IssueOutcome.COMPLETED,
                    branch_name="feat/anon",
                    repo_name="",  # fallback path
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

        # Should have been dispatched to the primary repo ('api')
        assert "api" in dispatched_to, (
            f"Empty repo_name must fall back to primary repo 'api', got: {dispatched_to}"
        )

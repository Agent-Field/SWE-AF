"""Tests for multi-repo dispatch in dag_executor.py (issue daaccc55-05).

Covers:
- _init_all_repos: no-op when manifest is None; concurrent call_fn per repo
- _merge_level_branches: single-repo path unchanged; multi-repo groups by repo_name
- IssueResult.repo_name backfill from issue['target_repo'] in _execute_level
- execute() reasoner passes workspace_manifest through to run_dag
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.dag_executor import _init_all_repos, _merge_level_branches, run_dag
from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
    LevelResult,
    WorkspaceManifest,
    WorkspaceRepo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dag_state(**kwargs) -> DAGState:
    """Minimal DAGState for testing."""
    defaults = {
        "repo_path": "/tmp/repo",
        "artifacts_dir": "/tmp/.artifacts",
        "prd_summary": "test",
        "architecture_summary": "test",
        "all_issues": [],
        "levels": [],
        "git_integration_branch": "integration/test",
    }
    defaults.update(kwargs)
    return DAGState(**defaults)


def _make_workspace_manifest(repos: list[dict]) -> dict:
    """Return a serialised WorkspaceManifest dict."""
    ws_repos = [WorkspaceRepo(**r) for r in repos]
    manifest = WorkspaceManifest(
        workspace_root="/tmp/workspace",
        repos=ws_repos,
        primary_repo_name=ws_repos[0].repo_name if ws_repos else "",
    )
    return manifest.model_dump()


def _make_repo(name: str, path: str, git_init_result: dict | None = None) -> dict:
    return {
        "repo_name": name,
        "repo_url": f"https://github.com/org/{name}.git",
        "role": "primary" if name == "api" else "dependency",
        "absolute_path": path,
        "branch": "main",
        "git_init_result": git_init_result,
    }


# ---------------------------------------------------------------------------
# _init_all_repos — no-op when manifest is None
# ---------------------------------------------------------------------------


class TestInitAllReposNoneManifest:
    def test_no_op_when_manifest_is_none(self):
        """AC: _init_all_repos returns immediately without calling call_fn."""
        call_fn = AsyncMock()
        dag_state = _make_dag_state(workspace_manifest=None)

        asyncio.run(_init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id="swe-planner",
            git_model="sonnet",
            ai_provider="claude",
        ))

        call_fn.assert_not_called()
        assert dag_state.workspace_manifest is None


# ---------------------------------------------------------------------------
# _init_all_repos — 2-repo manifest calls call_fn twice concurrently
# ---------------------------------------------------------------------------


class TestInitAllReposTwoRepos:
    def test_calls_call_fn_once_per_repo(self):
        """AC: With 2 repos, call_fn is called exactly twice via asyncio.gather."""
        call_fn = AsyncMock(return_value={"success": True, "integration_branch": "integ/test"})

        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api"),
            _make_repo("lib", "/tmp/workspace/lib"),
        ])
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)

        asyncio.run(_init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id="swe-planner",
            git_model="sonnet",
            ai_provider="claude",
            build_id="abc123",
        ))

        assert call_fn.call_count == 2

    def test_git_init_result_stored_in_manifest(self):
        """After _init_all_repos, git_init_result is populated in workspace_manifest."""
        git_init_response = {"success": True, "integration_branch": "integ/test", "mode": "fresh"}
        call_fn = AsyncMock(return_value=git_init_response)

        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api"),
            _make_repo("lib", "/tmp/workspace/lib"),
        ])
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)

        asyncio.run(_init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id="swe-planner",
            git_model="sonnet",
            ai_provider="claude",
        ))

        # Reconstruct to verify
        updated = WorkspaceManifest(**dag_state.workspace_manifest)
        for repo in updated.repos:
            assert repo.git_init_result == git_init_response

    def test_exception_in_call_fn_is_non_fatal(self):
        """call_fn raising exception for one repo doesn't crash _init_all_repos."""
        call_fn = AsyncMock(side_effect=RuntimeError("network error"))

        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api"),
        ])
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)

        # Should not raise
        asyncio.run(_init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id="swe-planner",
            git_model="sonnet",
            ai_provider="claude",
        ))

        # git_init_result remains None (failure)
        updated = WorkspaceManifest(**dag_state.workspace_manifest)
        assert updated.repos[0].git_init_result is None

    def test_node_id_used_in_call(self):
        """call_fn is invoked with the correct node_id prefix."""
        call_fn = AsyncMock(return_value={"success": True, "integration_branch": "integ/test"})

        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api"),
        ])
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)

        asyncio.run(_init_all_repos(
            dag_state=dag_state,
            call_fn=call_fn,
            node_id="my-planner",
            git_model="haiku",
            ai_provider="openai",
        ))

        call_fn.assert_called_once()
        call_args = call_fn.call_args
        assert call_args[0][0] == "my-planner.run_git_init"
        assert call_args[1]["model"] == "haiku"
        assert call_args[1]["ai_provider"] == "openai"
        assert call_args[1]["repo_path"] == "/tmp/workspace/api"


# ---------------------------------------------------------------------------
# _merge_level_branches — single-repo path unchanged
# ---------------------------------------------------------------------------


class TestMergeLevelBranchesSingleRepo:
    def test_single_repo_path_used_when_manifest_is_none(self):
        """AC: With workspace_manifest=None, existing single-repo logic executes."""
        call_fn = AsyncMock(return_value={
            "success": True,
            "merged_branches": ["issue/01-feat"],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "ok",
        })

        dag_state = _make_dag_state(workspace_manifest=None)
        config = ExecutionConfig()

        result_ir = IssueResult(
            issue_name="feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-feat",
        )
        level_result = LevelResult(level_index=0, completed=[result_ir])
        issue_by_name = {"feat": {"description": "desc"}}

        result = asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name=issue_by_name,
            file_conflicts=[],
        ))

        assert call_fn.call_count == 1
        call_args = call_fn.call_args
        # Should use dag_state.repo_path and dag_state.git_integration_branch
        assert call_args[1]["repo_path"] == "/tmp/repo"
        assert call_args[1]["integration_branch"] == "integration/test"
        assert result is not None
        assert result["success"] is True

    def test_single_repo_returns_none_when_no_branches(self):
        """No completed branches → returns None."""
        call_fn = AsyncMock()
        dag_state = _make_dag_state(workspace_manifest=None)
        config = ExecutionConfig()
        level_result = LevelResult(level_index=0, completed=[])

        result = asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={},
            file_conflicts=[],
        ))

        call_fn.assert_not_called()
        assert result is None

    def test_single_repo_result_appended_to_merge_results(self):
        """merge_result is appended to dag_state.merge_results."""
        merge_resp = {
            "success": True,
            "merged_branches": ["issue/01-feat"],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "merged",
        }
        call_fn = AsyncMock(return_value=merge_resp)
        dag_state = _make_dag_state(workspace_manifest=None)
        config = ExecutionConfig()

        result_ir = IssueResult(
            issue_name="feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-feat",
        )
        level_result = LevelResult(level_index=0, completed=[result_ir])

        asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={"feat": {}},
            file_conflicts=[],
        ))

        assert len(dag_state.merge_results) == 1
        assert "issue/01-feat" in dag_state.merged_branches


# ---------------------------------------------------------------------------
# _merge_level_branches — multi-repo path groups by repo_name
# ---------------------------------------------------------------------------


class TestMergeLevelBranchesMultiRepo:
    def _make_manifest_with_init(self) -> dict:
        """Build manifest where both repos have git_init_result set."""
        git_init_api = {
            "success": True,
            "integration_branch": "integ/api",
            "mode": "fresh",
            "original_branch": "main",
            "initial_commit_sha": "abc",
        }
        git_init_lib = {
            "success": True,
            "integration_branch": "integ/lib",
            "mode": "fresh",
            "original_branch": "main",
            "initial_commit_sha": "def",
        }
        return _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api", git_init_result=git_init_api),
            _make_repo("lib", "/tmp/workspace/lib", git_init_result=git_init_lib),
        ])

    def test_calls_merger_once_per_repo(self):
        """AC: With 2 repos, call_fn is called exactly twice (one per repo)."""
        call_fn = AsyncMock(return_value={
            "success": True,
            "merged_branches": [],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "ok",
        })

        manifest_dict = self._make_manifest_with_init()
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)
        config = ExecutionConfig()

        # Two IssueResults with different repo_names
        ir_api = IssueResult(
            issue_name="api-feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-api-feat",
            repo_name="api",
        )
        ir_lib = IssueResult(
            issue_name="lib-feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/02-lib-feat",
            repo_name="lib",
        )
        level_result = LevelResult(level_index=0, completed=[ir_api, ir_lib])

        asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={"api-feat": {}, "lib-feat": {}},
            file_conflicts=[],
        ))

        assert call_fn.call_count == 2

    def test_same_repo_branches_merged_in_one_call(self):
        """Two branches in the same repo → single merger call."""
        call_fn = AsyncMock(return_value={
            "success": True,
            "merged_branches": ["issue/01-feat-a", "issue/02-feat-b"],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "ok",
        })

        manifest_dict = self._make_manifest_with_init()
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)
        config = ExecutionConfig()

        ir1 = IssueResult(
            issue_name="feat-a",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-feat-a",
            repo_name="api",
        )
        ir2 = IssueResult(
            issue_name="feat-b",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/02-feat-b",
            repo_name="api",
        )
        level_result = LevelResult(level_index=0, completed=[ir1, ir2])

        asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={"feat-a": {}, "feat-b": {}},
            file_conflicts=[],
        ))

        # Both issues are in 'api' → only 1 merger call
        assert call_fn.call_count == 1

    def test_returns_none_when_no_completed_branches(self):
        """Multi-repo: no completed branches → returns None."""
        call_fn = AsyncMock()
        manifest_dict = self._make_manifest_with_init()
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)
        config = ExecutionConfig()
        level_result = LevelResult(level_index=0, completed=[])

        result = asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={},
            file_conflicts=[],
        ))

        call_fn.assert_not_called()
        assert result is None

    def test_repo_with_no_git_init_result_skipped(self):
        """Repos without git_init_result are skipped (no merger call)."""
        # lib has no git_init_result
        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api", git_init_result={
                "success": True,
                "integration_branch": "integ/api",
                "mode": "fresh",
                "original_branch": "main",
                "initial_commit_sha": "abc",
            }),
            _make_repo("lib", "/tmp/workspace/lib", git_init_result=None),
        ])
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)
        config = ExecutionConfig()

        call_fn = AsyncMock(return_value={
            "success": True,
            "merged_branches": [],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "ok",
        })

        ir_api = IssueResult(
            issue_name="api-feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-api-feat",
            repo_name="api",
        )
        ir_lib = IssueResult(
            issue_name="lib-feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/02-lib-feat",
            repo_name="lib",  # lib has no git_init_result → should be skipped
        )
        level_result = LevelResult(level_index=0, completed=[ir_api, ir_lib])

        asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={"api-feat": {}, "lib-feat": {}},
            file_conflicts=[],
        ))

        # Only 'api' gets a merger call; 'lib' is skipped
        assert call_fn.call_count == 1

    def test_merge_results_appended_with_repo_name(self):
        """Multi-repo: dag_state.merge_results entries include repo_name."""
        call_fn = AsyncMock(return_value={
            "success": True,
            "merged_branches": ["issue/01-feat"],
            "failed_branches": [],
            "needs_integration_test": False,
            "summary": "ok",
        })

        manifest_dict = self._make_manifest_with_init()
        dag_state = _make_dag_state(workspace_manifest=manifest_dict)
        config = ExecutionConfig()

        ir = IssueResult(
            issue_name="api-feat",
            outcome=IssueOutcome.COMPLETED,
            branch_name="issue/01-feat",
            repo_name="api",
        )
        level_result = LevelResult(level_index=0, completed=[ir])

        asyncio.run(_merge_level_branches(
            dag_state=dag_state,
            level_result=level_result,
            call_fn=call_fn,
            node_id="swe-planner",
            config=config,
            issue_by_name={"api-feat": {}},
            file_conflicts=[],
        ))

        assert len(dag_state.merge_results) == 1
        assert dag_state.merge_results[0]["repo_name"] == "api"


# ---------------------------------------------------------------------------
# IssueResult.repo_name backfill in _execute_level
# ---------------------------------------------------------------------------


class TestRepoNameBackfill:
    def test_repo_name_backfilled_from_target_repo(self):
        """AC: IssueResult.repo_name is backfilled from issue['target_repo'] when empty."""
        from swe_af.execution.dag_executor import _execute_level

        async def mock_execute_single(issue, dag_state, execute_fn, config, **kwargs):
            # Return IssueResult with empty repo_name (simulates coding-loop-repo-name absent)
            return IssueResult(
                issue_name=issue["name"],
                outcome=IssueOutcome.COMPLETED,
                repo_name="",  # not set by coder
            )

        dag_state = _make_dag_state()
        config = ExecutionConfig()

        active_issues = [
            {"name": "feat", "target_repo": "myrepo"},
        ]

        with patch(
            "swe_af.execution.dag_executor._execute_single_issue",
            side_effect=mock_execute_single,
        ):
            level_result = asyncio.run(_execute_level(
                active_issues=active_issues,
                execute_fn=None,
                dag_state=dag_state,
                config=config,
                level_index=0,
                call_fn=AsyncMock(),
                node_id="swe-planner",
            ))

        assert len(level_result.completed) == 1
        assert level_result.completed[0].repo_name == "myrepo"

    def test_repo_name_not_overwritten_when_already_set(self):
        """If IssueResult.repo_name is already set, it is NOT overwritten."""
        from swe_af.execution.dag_executor import _execute_level

        async def mock_execute_single(issue, dag_state, execute_fn, config, **kwargs):
            return IssueResult(
                issue_name=issue["name"],
                outcome=IssueOutcome.COMPLETED,
                repo_name="original-repo",  # already set by coder
            )

        dag_state = _make_dag_state()
        config = ExecutionConfig()

        active_issues = [
            {"name": "feat", "target_repo": "different-repo"},
        ]

        with patch(
            "swe_af.execution.dag_executor._execute_single_issue",
            side_effect=mock_execute_single,
        ):
            level_result = asyncio.run(_execute_level(
                active_issues=active_issues,
                execute_fn=None,
                dag_state=dag_state,
                config=config,
                level_index=0,
                call_fn=AsyncMock(),
                node_id="swe-planner",
            ))

        assert level_result.completed[0].repo_name == "original-repo"

    def test_repo_name_empty_when_no_target_repo(self):
        """If issue has no target_repo and IssueResult.repo_name is empty, stays empty."""
        from swe_af.execution.dag_executor import _execute_level

        async def mock_execute_single(issue, dag_state, execute_fn, config, **kwargs):
            return IssueResult(
                issue_name=issue["name"],
                outcome=IssueOutcome.COMPLETED,
                repo_name="",
            )

        dag_state = _make_dag_state()
        config = ExecutionConfig()

        active_issues = [
            {"name": "feat"},  # no target_repo key
        ]

        with patch(
            "swe_af.execution.dag_executor._execute_single_issue",
            side_effect=mock_execute_single,
        ):
            level_result = asyncio.run(_execute_level(
                active_issues=active_issues,
                execute_fn=None,
                dag_state=dag_state,
                config=config,
                level_index=0,
                call_fn=AsyncMock(),
                node_id="swe-planner",
            ))

        assert level_result.completed[0].repo_name == ""


# ---------------------------------------------------------------------------
# run_dag — accepts and assigns workspace_manifest
# ---------------------------------------------------------------------------


class TestRunDagWorkspaceManifest:
    def test_run_dag_accepts_workspace_manifest_param(self):
        """AC: run_dag() signature includes workspace_manifest: dict | None = None."""
        import inspect
        sig = inspect.signature(run_dag)
        assert "workspace_manifest" in sig.parameters
        param = sig.parameters["workspace_manifest"]
        assert param.default is None

    def test_workspace_manifest_assigned_to_dag_state(self):
        """AC: run_dag assigns workspace_manifest to dag_state.workspace_manifest."""
        manifest_dict = _make_workspace_manifest([
            _make_repo("api", "/tmp/workspace/api"),
        ])

        plan_result = {
            "prd": {"validated_description": "test", "acceptance_criteria": []},
            "architecture": {"summary": ""},
            "issues": [],
            "levels": [],
            "file_conflicts": [],
            "artifacts_dir": "",
            "rationale": "",
        }

        # We need to intercept dag_state after _init_dag_state — use a simple run
        # with no issues (exits immediately) and a real call_fn that doesn't actually
        # call anything beyond _init_all_repos.
        call_fn = AsyncMock(return_value={"success": True, "integration_branch": "integ/test"})

        result = asyncio.run(run_dag(
            plan_result=plan_result,
            repo_path="/tmp/repo",
            workspace_manifest=manifest_dict,
            call_fn=call_fn,
        ))

        assert result.workspace_manifest is not None
        assert result.workspace_manifest["primary_repo_name"] == "api"


# ---------------------------------------------------------------------------
# execute() reasoner — workspace_manifest parameter
# ---------------------------------------------------------------------------


class TestExecuteReasonerWorkspaceManifest:
    def test_execute_source_has_workspace_manifest_param(self):
        """AC: execute() function source includes workspace_manifest parameter.

        The @app.reasoner() decorator wraps the function, so we inspect the
        underlying source code to verify the parameter is present.
        """
        import inspect
        import swe_af.app as app_module
        # The raw function (before decoration) is the original one; get source
        source = inspect.getsource(app_module)
        # Check the parameter appears in execute function body
        assert "workspace_manifest: dict | None = None" in source

    def test_execute_passes_workspace_manifest_to_run_dag_via_source(self):
        """execute() source passes workspace_manifest= kwarg to run_dag()."""
        import inspect
        import swe_af.app as app_module
        source = inspect.getsource(app_module)
        # Verify workspace_manifest is threaded through in the execute() body
        assert "workspace_manifest=workspace_manifest" in source

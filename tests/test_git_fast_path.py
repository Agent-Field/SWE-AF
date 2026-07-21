"""Tests for the deterministic git fast-paths in the DAG executor.

Validation contract:
  - Worktree setup / conflict-free merges / cleanup complete with ZERO agent
    calls, producing the same result shapes and the same branch/worktree
    naming as the agent path (prompts/workspace.py conventions).
  - A conflicting merge falls back to the merger agent with ONLY the
    conflicted branches, and the integration branch is left clean (no
    half-finished merge state).
  - Any fast-path failure falls back to the agent path, so builds never get
    worse than the old behavior; deterministic_git=False restores it outright.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from swe_af.execution import git_fast_path
from swe_af.execution.dag_executor import (
    _cleanup_single_repo,
    _dispatch_merge,
    _dispatch_workspace_setup,
)
from swe_af.execution.schemas import ExecutionConfig


def run_git(cwd: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> str:
    """Repo with an ``integration`` branch checked out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    r = str(repo)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    run_git(r, "checkout", "-q", "-b", "main")
    run_git(r, "config", "user.email", "t@example.com")
    run_git(r, "config", "user.name", "T")
    (repo / "base.txt").write_text("base\n")
    run_git(r, "add", "base.txt")
    run_git(r, "commit", "-q", "-m", "base")
    run_git(r, "checkout", "-q", "-b", "integration")
    return r


def _branch_with_file(repo: str, branch: str, filename: str, content: str) -> None:
    run_git(repo, "checkout", "-q", "-b", branch, "integration")
    with open(os.path.join(repo, filename), "w") as f:
        f.write(content)
    run_git(repo, "add", filename)
    run_git(repo, "commit", "-q", "-m", f"work on {branch}")
    run_git(repo, "checkout", "-q", "integration")


class TestSetupWorktrees:
    def test_naming_matches_agent_convention(self, repo: str) -> None:
        issues = [
            {"name": "lexer", "sequence_number": 1},
            {"name": "parser", "sequence_number": 2},
        ]
        wt_dir = os.path.join(repo, ".worktrees")
        result = git_fast_path.setup_worktrees(
            repo, "integration", issues, wt_dir, build_id="ab12cd34",
        )
        assert result["success"] is True
        ws = {w["issue_name"]: w for w in result["workspaces"]}
        assert ws["lexer"]["branch_name"] == "issue/ab12cd34-01-lexer"
        assert ws["lexer"]["worktree_path"] == os.path.join(wt_dir, "issue-ab12cd34-01-lexer")
        assert os.path.isdir(ws["parser"]["worktree_path"])
        # Worktree dir name == branch name with "/" -> "-" (cleanup contract).
        assert os.path.basename(ws["lexer"]["worktree_path"]) == \
            ws["lexer"]["branch_name"].replace("/", "-")

    def test_no_build_id_naming(self, repo: str) -> None:
        result = git_fast_path.setup_worktrees(
            repo, "integration", [{"name": "fix-ac1"}], os.path.join(repo, ".worktrees"),
        )
        assert result["workspaces"][0]["branch_name"] == "issue/00-fix-ac1"

    def test_resume_reuses_existing_branch_and_worktree(self, repo: str) -> None:
        issues = [{"name": "lexer", "sequence_number": 1}]
        wt_dir = os.path.join(repo, ".worktrees")
        first = git_fast_path.setup_worktrees(repo, "integration", issues, wt_dir)
        second = git_fast_path.setup_worktrees(repo, "integration", issues, wt_dir)
        assert first == second

    def test_missing_integration_branch_raises(self, repo: str) -> None:
        with pytest.raises(git_fast_path.GitFastPathError):
            git_fast_path.setup_worktrees(
                repo, "", [{"name": "x"}], os.path.join(repo, ".worktrees"),
            )


class TestMergeBranches:
    def test_clean_merges_no_conflicts(self, repo: str) -> None:
        _branch_with_file(repo, "issue/01-a", "a.txt", "a\n")
        _branch_with_file(repo, "issue/02-b", "b.txt", "b\n")
        result = git_fast_path.merge_branches(
            repo, "integration", ["issue/01-a", "issue/02-b"], level=0,
        )
        assert result["success"] is True
        assert result["merged_branches"] == ["issue/01-a", "issue/02-b"]
        assert result["failed_branches"] == []
        assert result["needs_integration_test"] is True  # >1 branch merged
        assert run_git(repo, "show", "integration:a.txt") == "a"
        assert run_git(repo, "show", "integration:b.txt") == "b"

    def test_single_branch_skips_integration_test(self, repo: str) -> None:
        _branch_with_file(repo, "issue/01-a", "a.txt", "a\n")
        result = git_fast_path.merge_branches(repo, "integration", ["issue/01-a"])
        assert result["needs_integration_test"] is False

    def test_conflict_is_aborted_and_reported(self, repo: str) -> None:
        _branch_with_file(repo, "issue/01-a", "same.txt", "version a\n")
        _branch_with_file(repo, "issue/02-b", "same.txt", "version b\n")
        result = git_fast_path.merge_branches(
            repo, "integration", ["issue/01-a", "issue/02-b"], level=1,
        )
        assert result["merged_branches"] == ["issue/01-a"]
        assert result["failed_branches"] == ["issue/02-b"]
        assert result["success"] is False
        # Integration branch left clean: no in-progress merge, no dirt.
        assert run_git(repo, "status", "--porcelain") == ""
        assert not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))


class TestCleanupWorktrees:
    def test_removes_worktrees_and_branches(self, repo: str) -> None:
        wt_dir = os.path.join(repo, ".worktrees")
        git_fast_path.setup_worktrees(
            repo, "integration", [{"name": "a", "sequence_number": 1}], wt_dir,
        )
        result = git_fast_path.cleanup_worktrees(repo, wt_dir, ["issue/01-a"])
        assert result == {"success": True, "cleaned": ["issue/01-a"]}
        assert not os.path.isdir(os.path.join(wt_dir, "issue-01-a"))
        assert run_git(repo, "branch", "--list", "issue/01-a") == ""


class TestCombineMergeResults:
    def test_combines_fast_and_agent(self) -> None:
        fast = {
            "merged_branches": ["a"], "failed_branches": ["b"],
            "merge_commit_sha": "fast-sha", "pre_merge_sha": "pre",
            "summary": "fast",
        }
        agent = {
            "merged_branches": ["b"], "failed_branches": [],
            "conflict_resolutions": [{"file": "x"}],
            "merge_commit_sha": "agent-sha", "summary": "agent",
        }
        combined = git_fast_path.combine_merge_results(fast, agent)
        assert combined["success"] is True
        assert combined["merged_branches"] == ["a", "b"]
        assert combined["failed_branches"] == []
        assert combined["merge_commit_sha"] == "agent-sha"
        assert combined["needs_integration_test"] is True


class TestDispatchers:
    """The executor seams: fast path means zero agent calls; failures fall back."""

    def _recording_call_fn(self, response: dict):
        calls: list = []

        async def call_fn(target, **kwargs):
            calls.append((target, kwargs))
            return response

        return call_fn, calls

    def test_setup_dispatch_uses_no_agent_on_real_repo(self, repo: str) -> None:
        call_fn, calls = self._recording_call_fn({"success": True, "workspaces": []})
        setup = asyncio.run(_dispatch_workspace_setup(
            call_fn, "node", ExecutionConfig(),
            repo_path=repo, integration_branch="integration",
            issues=[{"name": "a", "sequence_number": 1}],
            worktrees_dir=os.path.join(repo, ".worktrees"),
            artifacts_dir="", level=0, build_id="", note_fn=None,
        ))
        assert setup["success"] is True and len(setup["workspaces"]) == 1
        assert calls == []

    def test_setup_dispatch_falls_back_on_bad_repo(self, tmp_path) -> None:
        agent_setup = {"success": True, "workspaces": [{"issue_name": "a",
                       "branch_name": "issue/01-a", "worktree_path": "/x"}]}
        call_fn, calls = self._recording_call_fn(agent_setup)
        setup = asyncio.run(_dispatch_workspace_setup(
            call_fn, "node", ExecutionConfig(),
            repo_path=str(tmp_path / "not-a-repo"), integration_branch="integration",
            issues=[{"name": "a"}], worktrees_dir=str(tmp_path / "wt"),
            artifacts_dir="", level=0, build_id="", note_fn=None,
        ))
        assert setup == agent_setup
        assert len(calls) == 1 and calls[0][0] == "node.run_workspace_setup"

    def test_setup_dispatch_respects_flag_off(self, repo: str) -> None:
        agent_setup = {"success": True, "workspaces": []}
        call_fn, calls = self._recording_call_fn(agent_setup)
        asyncio.run(_dispatch_workspace_setup(
            call_fn, "node", ExecutionConfig(deterministic_git=False),
            repo_path=repo, integration_branch="integration",
            issues=[{"name": "a"}], worktrees_dir=os.path.join(repo, ".worktrees"),
            artifacts_dir="", level=0, build_id="", note_fn=None,
        ))
        assert len(calls) == 1

    def test_merge_dispatch_conflict_free_uses_no_agent(self, repo: str) -> None:
        _branch_with_file(repo, "issue/01-a", "a.txt", "a\n")
        call_fn, calls = self._recording_call_fn({})
        result = asyncio.run(_dispatch_merge(
            call_fn, "node", ExecutionConfig(),
            repo_path=repo, integration_branch="integration",
            completed_branches=[{"branch_name": "issue/01-a"}],
            merge_kwargs={"branches_to_merge": [{"branch_name": "issue/01-a"}]},
            level=0, note_fn=None,
        ))
        assert result["success"] is True
        assert calls == []

    def test_merge_dispatch_hands_only_conflicts_to_agent(self, repo: str) -> None:
        _branch_with_file(repo, "issue/01-a", "same.txt", "a\n")
        _branch_with_file(repo, "issue/02-b", "same.txt", "b\n")
        agent_result = {
            "success": True, "merged_branches": ["issue/02-b"],
            "failed_branches": [], "summary": "agent resolved",
        }
        call_fn, calls = self._recording_call_fn(agent_result)
        branches = [{"branch_name": "issue/01-a"}, {"branch_name": "issue/02-b"}]
        result = asyncio.run(_dispatch_merge(
            call_fn, "node", ExecutionConfig(),
            repo_path=repo, integration_branch="integration",
            completed_branches=branches,
            merge_kwargs={"branches_to_merge": branches},
            level=0, note_fn=None,
        ))
        assert len(calls) == 1
        assert calls[0][1]["branches_to_merge"] == [{"branch_name": "issue/02-b"}]
        assert sorted(result["merged_branches"]) == ["issue/01-a", "issue/02-b"]
        assert result["success"] is True

    def test_cleanup_dispatch_uses_no_agent(self, repo: str) -> None:
        wt_dir = os.path.join(repo, ".worktrees")
        git_fast_path.setup_worktrees(
            repo, "integration", [{"name": "a", "sequence_number": 1}], wt_dir,
        )
        call_fn, calls = self._recording_call_fn({"success": True, "cleaned": []})
        asyncio.run(_cleanup_single_repo(
            call_fn, "node", repo, wt_dir, ["issue/01-a"], "", 0, "m", "claude",
            None, deterministic_git=True,
        ))
        assert calls == []
        assert run_git(repo, "branch", "--list", "issue/01-a") == ""

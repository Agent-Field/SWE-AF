"""Tests for the deterministic git helpers used by the issue-level build."""

from __future__ import annotations

import os
import subprocess

import pytest

from swe_af.issue import git_ops
from tests.issue.conftest import run_git


class TestEnsureIssueReadyRepo:
    def test_ok(self, git_repo: str) -> None:
        git_ops.ensure_issue_ready_repo(git_repo)  # must not raise

    def test_missing_dir(self, tmp_path) -> None:
        with pytest.raises(git_ops.GitOpsError, match="does not exist"):
            git_ops.ensure_issue_ready_repo(str(tmp_path / "nope"))

    def test_not_a_repo(self, tmp_path) -> None:
        with pytest.raises(git_ops.GitOpsError, match="not a git repository"):
            git_ops.ensure_issue_ready_repo(str(tmp_path))

    def test_no_commits(self, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(empty), check=True)
        with pytest.raises(git_ops.GitOpsError, match="no commits"):
            git_ops.ensure_issue_ready_repo(str(empty))


class TestResolveBase:
    def test_defaults_to_current_branch(self, git_repo: str) -> None:
        ref, sha = git_ops.resolve_base(git_repo)
        assert ref == "main"
        assert sha == run_git(git_repo, "rev-parse", "HEAD")

    def test_named_ref(self, git_repo: str) -> None:
        run_git(git_repo, "branch", "other")
        ref, sha = git_ops.resolve_base(git_repo, "other")
        assert ref == "other"
        assert len(sha) == 40

    def test_missing_ref(self, git_repo: str) -> None:
        with pytest.raises(git_ops.GitOpsError, match="base branch"):
            git_ops.resolve_base(git_repo, "ghost")


class TestWorktreeLifecycle:
    def test_add_commit_inspect_remove(self, git_repo: str) -> None:
        _, base_sha = git_ops.resolve_base(git_repo)
        worktree = os.path.join(git_repo, ".worktrees", "wt1")

        git_ops.add_worktree(git_repo, worktree, "issue/test-1", base_sha)
        assert os.path.isdir(worktree)

        # Nothing to commit yet.
        assert git_ops.commit_all(worktree, "noop") == ""

        with open(os.path.join(worktree, "new.txt"), "w") as f:
            f.write("hi\n")
        sha = git_ops.commit_all(worktree, "add new.txt")
        assert len(sha) == 40

        assert git_ops.new_commits(git_repo, base_sha, "issue/test-1") == [sha]
        assert git_ops.changed_files(git_repo, base_sha, "issue/test-1") == ["new.txt"]
        assert "new.txt" in git_ops.diff_stat(git_repo, base_sha, "issue/test-1")

        git_ops.remove_worktree(git_repo, worktree)
        assert not os.path.isdir(worktree)
        # Branch survives worktree removal — it is the deliverable.
        assert run_git(git_repo, "rev-parse", "issue/test-1") == sha

    def test_commit_identity_fallback(self, git_repo: str, monkeypatch) -> None:
        _, base_sha = git_ops.resolve_base(git_repo)
        worktree = os.path.join(git_repo, ".worktrees", "wt2")
        git_ops.add_worktree(git_repo, worktree, "issue/test-2", base_sha)
        with open(os.path.join(worktree, "x.txt"), "w") as f:
            f.write("x\n")

        monkeypatch.setattr(git_ops, "_has_commit_identity", lambda _: False)
        sha = git_ops.commit_all(worktree, "identity fallback")
        author = run_git(worktree, "log", "-1", "--format=%ae")
        assert sha
        assert author == "swe-af@agentfield.local"

    def test_delete_branch_and_missing_branch_queries(self, git_repo: str) -> None:
        _, base_sha = git_ops.resolve_base(git_repo)
        run_git(git_repo, "branch", "issue/tmp")
        git_ops.delete_branch(git_repo, "issue/tmp")
        assert run_git(git_repo, "branch", "--list", "issue/tmp") == ""
        # Queries against a deleted branch degrade to empty results.
        assert git_ops.new_commits(git_repo, base_sha, "issue/tmp") == []
        assert git_ops.changed_files(git_repo, base_sha, "issue/tmp") == []


class TestLocalExcludes:
    def test_adds_patterns_and_masks_status(self, git_repo: str) -> None:
        os.makedirs(os.path.join(git_repo, ".artifacts"), exist_ok=True)
        with open(os.path.join(git_repo, ".artifacts", "x.json"), "w") as f:
            f.write("{}")
        git_ops.ensure_local_excludes(git_repo, [".artifacts/", ".worktrees/"])
        assert run_git(git_repo, "status", "--porcelain") == ""

    def test_idempotent(self, git_repo: str) -> None:
        git_ops.ensure_local_excludes(git_repo, [".artifacts/"])
        git_ops.ensure_local_excludes(git_repo, [".artifacts/"])
        exclude = os.path.join(git_repo, ".git", "info", "exclude")
        with open(exclude) as f:
            lines = [line.strip() for line in f if line.strip()]
        assert lines.count(".artifacts/") == 1


class TestRemotes:
    def test_no_remote(self, git_repo: str) -> None:
        assert git_ops.remote_url(git_repo) == ""
        assert git_ops.default_remote_branch(git_repo) == ""

    def test_with_remote(self, git_repo: str, tmp_path) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(origin)], check=True
        )
        run_git(git_repo, "remote", "add", "origin", str(origin))
        assert git_ops.remote_url(git_repo) == str(origin)

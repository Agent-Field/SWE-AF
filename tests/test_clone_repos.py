"""Tests for _clone_repos async function in swe_af.app (issue daaccc55-04-clone-repos).

Covers AC-23: _clone_repos is async, has cfg + artifacts_dir params, returns
WorkspaceManifest with correct structure for 1-repo and 2-repo configs.

Note: Does NOT test actual git clone execution — all subprocess calls are mocked.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.schemas import BuildConfig, RepoSpec, WorkspaceManifest


# ---------------------------------------------------------------------------
# Inspect tests (AC-23)
# ---------------------------------------------------------------------------


class TestCloneReposInspect:
    """Verify _clone_repos has the correct signature and is async."""

    def test_importable_from_swe_af_app(self) -> None:
        """_clone_repos is importable from swe_af.app."""
        from swe_af.app import _clone_repos  # noqa: F401

    def test_is_coroutine_function(self) -> None:
        """_clone_repos is declared as async def (coroutine function)."""
        from swe_af.app import _clone_repos

        assert inspect.iscoroutinefunction(_clone_repos), (
            "_clone_repos must be an async def coroutine function"
        )

    def test_has_cfg_parameter(self) -> None:
        """_clone_repos signature has 'cfg' parameter."""
        from swe_af.app import _clone_repos

        sig = inspect.signature(_clone_repos)
        assert "cfg" in sig.parameters, "_clone_repos must have a 'cfg' parameter"

    def test_has_artifacts_dir_parameter(self) -> None:
        """_clone_repos signature has 'artifacts_dir' parameter."""
        from swe_af.app import _clone_repos

        sig = inspect.signature(_clone_repos)
        assert "artifacts_dir" in sig.parameters, (
            "_clone_repos must have an 'artifacts_dir' parameter"
        )

    def test_ac23_combined(self) -> None:
        """AC-23: _clone_repos is async and has cfg + artifacts_dir params."""
        from swe_af.app import _clone_repos

        sig = inspect.signature(_clone_repos)
        params = list(sig.parameters.keys())
        assert "cfg" in params
        assert "artifacts_dir" in params
        assert inspect.iscoroutinefunction(_clone_repos)


# ---------------------------------------------------------------------------
# Unit tests for _clone_repos behavior
# ---------------------------------------------------------------------------


def _make_subprocess_result(returncode: int = 0, stdout: str = "main\n", stderr: str = "") -> MagicMock:
    """Create a mock subprocess.CompletedProcess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestCloneReposSingleRepo:
    """_clone_repos with single-repo BuildConfig returns WorkspaceManifest with one repo."""

    def test_single_repo_returns_manifest(self, tmp_path) -> None:
        """Single-repo BuildConfig returns WorkspaceManifest with one WorkspaceRepo."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[RepoSpec(repo_url="https://github.com/org/myrepo.git", role="primary")]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        # Mock asyncio.to_thread to avoid real subprocess calls
        clone_result = _make_subprocess_result(returncode=0)
        branch_result = _make_subprocess_result(returncode=0, stdout="main\n")

        call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = fn()
            return result

        import subprocess as sp

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                # First call is git clone, second is rev-parse
                mock_run.side_effect = [clone_result, branch_result]
                # Also mock os.path.exists to simulate no existing .git dir
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        assert isinstance(manifest, WorkspaceManifest)
        assert len(manifest.repos) == 1
        assert manifest.repos[0].repo_url == "https://github.com/org/myrepo.git"
        assert manifest.repos[0].role == "primary"

    def test_single_repo_primary_repo_name(self, tmp_path) -> None:
        """primary_repo_name is set from the RepoSpec with role='primary'."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[RepoSpec(repo_url="https://github.com/org/myrepo.git", role="primary")]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_result = _make_subprocess_result(returncode=0)
        branch_result = _make_subprocess_result(returncode=0, stdout="main\n")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_result, branch_result]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        assert manifest.primary_repo_name == "myrepo"
        assert manifest.primary_repo is not None
        assert manifest.primary_repo.role == "primary"


class TestCloneReposTwoRepos:
    """_clone_repos with two-repo BuildConfig calls asyncio.to_thread for each repo."""

    def test_two_repos_manifest_has_two_repos(self, tmp_path) -> None:
        """Two-repo config returns WorkspaceManifest with two WorkspaceRepos."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/primary.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_ok = _make_subprocess_result(returncode=0, stdout="main\n")

        to_thread_calls: list = []

        async def fake_to_thread(fn, *args, **kwargs):
            to_thread_calls.append(fn)
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                # 2 clones + 2 branch resolutions = 4 calls
                mock_run.side_effect = [clone_ok, clone_ok, branch_ok, branch_ok]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        assert isinstance(manifest, WorkspaceManifest)
        assert len(manifest.repos) == 2

    def test_two_repos_to_thread_called_twice_for_clones(self, tmp_path) -> None:
        """asyncio.to_thread is called at least twice (once per repo clone)."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/primary.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_ok = _make_subprocess_result(returncode=0, stdout="main\n")
        to_thread_call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal to_thread_call_count
            to_thread_call_count += 1
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, clone_ok, branch_ok, branch_ok]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        asyncio.run(_clone_repos(cfg, artifacts_dir))

        # At minimum 2 clone calls + 2 branch calls = 4 total to_thread calls
        assert to_thread_call_count >= 2

    def test_primary_repo_name_from_primary_spec(self, tmp_path) -> None:
        """primary_repo_name is set from the RepoSpec with role='primary'."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/primary.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_ok = _make_subprocess_result(returncode=0, stdout="main\n")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, clone_ok, branch_ok, branch_ok]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        assert manifest.primary_repo_name == "primary"
        # Dependency repo should also be in manifest
        repo_names = [r.repo_name for r in manifest.repos]
        assert "lib" in repo_names


class TestCloneReposPartialFailureCleanup:
    """Partial failure triggers cleanup of already-cloned dirs."""

    def test_partial_failure_removes_cloned_dirs(self, tmp_path) -> None:
        """If second clone fails, already-cloned first repo's dir is removed."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/primary.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        clone_fail = _make_subprocess_result(returncode=1, stderr="fatal: not found")

        call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                # First repo clones ok, second fails
                mock_run.side_effect = [clone_ok, clone_fail]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        with patch("shutil.rmtree") as mock_rmtree:
                            with pytest.raises(RuntimeError, match="Multi-repo clone failed"):
                                asyncio.run(_clone_repos(cfg, artifacts_dir))

                            # shutil.rmtree should have been called for cleanup
                            assert mock_rmtree.called, (
                                "shutil.rmtree should be called to clean up partial clones"
                            )

    def test_partial_failure_raises_runtime_error(self, tmp_path) -> None:
        """RuntimeError is raised when any clone subprocess fails."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/primary.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        clone_fail = _make_subprocess_result(returncode=128, stderr="fatal: repo not found")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, clone_fail]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        with patch("shutil.rmtree"):
                            with pytest.raises(RuntimeError):
                                asyncio.run(_clone_repos(cfg, artifacts_dir))


class TestCloneReposRepoPath:
    """spec.repo_path given → no clone subprocess invoked."""

    def test_repo_path_skips_clone(self, tmp_path) -> None:
        """When spec.repo_path is set, no git clone subprocess is run."""
        from swe_af.app import _clone_repos

        # Use repo_path-only spec (no URL)
        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_path="/existing/local/repo", role="primary"),
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        branch_result = _make_subprocess_result(returncode=0, stdout="develop\n")
        to_thread_call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal to_thread_call_count
            to_thread_call_count += 1
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run", return_value=branch_result) as mock_run:
                with patch("os.makedirs"):
                    manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        # Only the branch resolution call should have happened (no git clone call)
        # subprocess.run called for branch resolution only
        for call in mock_run.call_args_list:
            args = call[0][0] if call[0] else call[1].get("args", [])
            # Should not have a "clone" subprocess call
            assert "clone" not in args, f"Unexpected git clone call: {args}"


class TestCloneReposBranchResolution:
    """Branch resolution fallback when git rev-parse returns non-zero."""

    def test_branch_fallback_on_rev_parse_failure(self, tmp_path) -> None:
        """When git rev-parse fails, branch falls back to spec.branch or 'HEAD'."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(
                    repo_url="https://github.com/org/myrepo.git",
                    role="primary",
                    branch="feature-x",
                )
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        # Branch resolution fails
        branch_fail = _make_subprocess_result(returncode=128, stdout="", stderr="fatal: not a git repo")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, branch_fail]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        # Branch should fall back to spec.branch
        assert manifest.repos[0].branch == "feature-x"

    def test_branch_fallback_to_head_when_no_spec_branch(self, tmp_path) -> None:
        """When git rev-parse fails and spec.branch is empty, falls back to 'HEAD'."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/myrepo.git", role="primary")
            ]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_fail = _make_subprocess_result(returncode=128, stdout="", stderr="fatal error")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, branch_fail]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        # Branch should fall back to 'HEAD'
        assert manifest.repos[0].branch == "HEAD"


class TestCloneReposWorkspaceManifestStructure:
    """WorkspaceManifest returned has correct workspace_root, repos, primary_repo_name."""

    def test_workspace_root_set(self, tmp_path) -> None:
        """WorkspaceManifest.workspace_root is set from artifacts_dir parent."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[RepoSpec(repo_url="https://github.com/org/repo.git", role="primary")]
        )
        artifacts_dir = str(tmp_path / "myproject" / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_ok = _make_subprocess_result(returncode=0, stdout="main\n")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, branch_ok]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        # workspace_root should be artifacts_dir parent + "workspace"
        import os
        expected_root = os.path.join(
            os.path.dirname(artifacts_dir), "workspace"
        )
        assert manifest.workspace_root == expected_root

    def test_git_init_result_is_none(self, tmp_path) -> None:
        """git_init_result on each WorkspaceRepo is None (populated later by dag_executor)."""
        from swe_af.app import _clone_repos

        cfg = BuildConfig(
            repos=[RepoSpec(repo_url="https://github.com/org/repo.git", role="primary")]
        )
        artifacts_dir = str(tmp_path / "artifacts")

        clone_ok = _make_subprocess_result(returncode=0)
        branch_ok = _make_subprocess_result(returncode=0, stdout="main\n")

        async def fake_to_thread(fn, *args, **kwargs):
            return fn()

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [clone_ok, branch_ok]
                with patch("os.path.exists", return_value=False):
                    with patch("os.makedirs"):
                        manifest = asyncio.run(_clone_repos(cfg, artifacts_dir))

        for repo in manifest.repos:
            assert repo.git_init_result is None

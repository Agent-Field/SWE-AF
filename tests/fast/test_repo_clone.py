"""Repository preparation contracts for the real fast build orchestrator."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


def _commit(cwd: Path, message: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fast Build Test",
        "GIT_AUTHOR_EMAIL": "fast-build@example.com",
        "GIT_COMMITTER_NAME": "Fast Build Test",
        "GIT_COMMITTER_EMAIL": "fast-build@example.com",
    }
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git commit failed: {result.stderr}")


def _make_local_remote(
    root: Path,
    tracked_name: str = "tracked.txt",
    content: str = "from remote\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    remote = root / "remote.git"
    seed = root / "seed"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("init", "--initial-branch=main", str(seed))
    (seed / tracked_name).write_text(content, encoding="utf-8")
    _git("add", tracked_name, cwd=seed)
    _commit(seed, "seed")
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    return remote


@pytest.fixture
def local_remote(tmp_path: Path) -> Path:
    return _make_local_remote(tmp_path)


class BuildStub:
    def __init__(self, expected_file: str = "tracked.txt") -> None:
        self.calls: list[str] = []
        self.git_init_observations: list[dict[str, object]] = []
        self.expected_file = expected_file

    async def __call__(self, target: str, **kwargs: object) -> dict[str, object]:
        name = target.rsplit(".", 1)[-1]
        self.calls.append(name)
        if name == "run_git_init":
            repo_path = Path(str(kwargs["repo_path"]))
            remote = _git("remote", "get-url", "origin", cwd=repo_path, check=False)
            initial_commit_sha = (
                _git("rev-parse", "HEAD", cwd=repo_path)
                if (repo_path / ".git").exists()
                else ""
            )
            self.git_init_observations.append(
                {
                    "git_exists": (repo_path / ".git").exists(),
                    "tracked_exists": (repo_path / self.expected_file).exists(),
                    "remote_url": remote,
                }
            )
            return {
                "result": {
                    "success": True,
                    "integration_branch": "feature/test",
                    "original_branch": "main",
                    "initial_commit_sha": initial_commit_sha,
                    "mode": "branch",
                    "remote_url": remote,
                    "remote_default_branch": "main",
                }
            }
        if name == "fast_plan_tasks":
            return {"result": {"tasks": [], "rationale": "test"}}
        if name == "fast_execute_tasks":
            return {
                "result": {
                    "task_results": [],
                    "completed_count": 0,
                    "failed_count": 0,
                    "timed_out": False,
                }
            }
        if name == "fast_verify":
            return {"result": {"passed": True, "summary": "verified"}}
        if name == "run_repo_finalize":
            return {"result": {"success": True}}
        if name == "run_github_pr":
            return {"result": {"pr_url": "https://example.test/pr/52"}}
        raise AssertionError(f"unexpected app.call target: {target}")


async def _build(
    fast_app: object,
    stub: BuildStub,
    **kwargs: object,
) -> dict[str, object]:
    with (
        patch.object(fast_app.app, "call", stub),  # type: ignore[attr-defined]
        patch.object(fast_app.app, "note", lambda *args, **kw: None),  # type: ignore[attr-defined]
    ):
        build_fn = getattr(fast_app.build, "_original_func", fast_app.build)  # type: ignore[attr-defined]
        return await build_fn(goal="test repository preparation", **kwargs)


@pytest.mark.asyncio
async def test_vc1_clone_is_ready_before_git_init(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC1: repo_url-only builds clone tracked files and origin before git_init."""
    import swe_af.fast.app as fast_app

    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    stub = BuildStub()

    await _build(fast_app, stub, repo_url=str(local_remote))

    clone = workspace / "remote"
    assert stub.calls[0] == "run_git_init"
    assert stub.git_init_observations == [
        {
            "git_exists": True,
            "tracked_exists": True,
            "remote_url": str(local_remote),
        }
    ]
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "from remote\n"


@pytest.mark.asyncio
async def test_vc2_valid_origin_reaches_github_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC2: a cloned origin enables the PR call and its URL is returned."""
    import swe_af.fast.app as fast_app

    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    stub = BuildStub()

    result = await _build(fast_app, stub, repo_url=str(local_remote))

    assert "run_github_pr" in stub.calls
    assert result["pr_url"] == "https://example.test/pr/52"


@pytest.mark.asyncio
async def test_vc3_existing_clone_is_reset_to_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC3: repeat builds discard stale worktrees, commits, and dirty files."""
    import swe_af.fast.app as fast_app

    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    await _build(fast_app, BuildStub(), repo_url=str(local_remote))
    clone = workspace / "remote"

    (clone / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (clone / "stray.txt").write_text("local commit\n", encoding="utf-8")
    _git("add", "tracked.txt", "stray.txt", cwd=clone)
    _commit(clone, "stray local commit")
    (clone / "tracked.txt").write_text("dirty after commit\n", encoding="utf-8")
    (clone / ".worktrees").mkdir()
    (clone / ".worktrees" / "stale").write_text("stale", encoding="utf-8")

    await _build(fast_app, BuildStub(), repo_url=str(local_remote))

    assert not (clone / ".worktrees").exists()
    assert not (clone / "stray.txt").exists()
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "from remote\n"
    assert _git("rev-parse", "HEAD", cwd=clone) == _git(
        "rev-parse", "origin/main", cwd=clone
    )


@pytest.mark.asyncio
async def test_vc4_unresettable_repo_is_recloned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC4: a repo without origin/main is removed and re-cloned successfully."""
    import swe_af.fast.app as fast_app

    workspace = tmp_path / "workspace"
    clone = workspace / "remote"
    clone.mkdir(parents=True)
    _git("init", "--initial-branch=main", cwd=clone)
    (clone / "wrong.txt").write_text("not the remote\n", encoding="utf-8")
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    stub = BuildStub()

    await _build(fast_app, stub, repo_url=str(local_remote))

    assert not (clone / "wrong.txt").exists()
    assert (clone / "tracked.txt").exists()
    assert _git("remote", "get-url", "origin", cwd=clone) == str(local_remote)
    assert stub.calls[0] == "run_git_init"


@pytest.mark.asyncio
async def test_vc5_clone_failure_raises_with_git_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """VC5: clone failures raise with stderr and never enter the build pipeline."""
    import swe_af.fast.app as fast_app

    workspace = tmp_path / "workspace"
    missing = tmp_path / "does-not-exist.git"
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    stub = BuildStub()

    with pytest.raises(RuntimeError) as exc_info:
        await _build(fast_app, stub, repo_url=str(missing))

    message = str(exc_info.value)
    assert "git clone failed (exit" in message
    assert "does not exist" in message
    assert stub.calls == []
    assert not (workspace / "does-not-exist").exists()


@pytest.mark.asyncio
async def test_vc6_repo_path_without_url_is_untouched(
    tmp_path: Path,
) -> None:
    """VC6: local-only paths are created if needed and existing files survive."""
    import swe_af.fast.app as fast_app

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep me\n", encoding="utf-8")
    await _build(fast_app, BuildStub("keep.txt"), repo_path=str(existing))

    missing = tmp_path / "new" / "workspace"
    await _build(fast_app, BuildStub("absent.txt"), repo_path=str(missing))

    assert marker.read_text(encoding="utf-8") == "keep me\n"
    assert not (existing / ".git").exists()
    assert missing.is_dir()
    assert not (missing / ".git").exists()


@pytest.mark.asyncio
async def test_vc7_disabled_github_pr_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC7: enable_github_pr=False skips PR creation despite a valid origin."""
    import swe_af.fast.app as fast_app

    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    stub = BuildStub()

    result = await _build(
        fast_app,
        stub,
        repo_url=str(local_remote),
        config={"enable_github_pr": False},
    )

    assert "run_github_pr" not in stub.calls
    assert result["pr_url"] == ""


@pytest.mark.asyncio
async def test_vcd_derived_basename_collision_reclones_requested_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC-D: a derived clone from another org is replaced, not reset in place."""
    import swe_af.fast.app as fast_app

    requested_remote = _make_local_remote(
        tmp_path / "other-org",
        tracked_name="requested.txt",
        content="from requested remote\n",
    )
    assert local_remote.name == requested_remote.name

    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    await _build(fast_app, BuildStub(), repo_url=str(local_remote))
    clone = workspace / "remote"
    (clone / "other-org-only.txt").write_text("must not survive\n", encoding="utf-8")

    stub = BuildStub("requested.txt")
    await _build(fast_app, stub, repo_url=str(requested_remote))

    assert stub.git_init_observations[0]["remote_url"] == str(requested_remote)
    assert _git("remote", "get-url", "origin", cwd=clone) == str(requested_remote)
    assert (clone / "requested.txt").read_text(encoding="utf-8") == (
        "from requested remote\n"
    )
    assert not (clone / "tracked.txt").exists()
    assert not (clone / "other-org-only.txt").exists()


@pytest.mark.asyncio
async def test_vce_caller_supplied_git_repo_is_not_mutated(
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC-E: a caller-owned checkout keeps its origin, branch, commit, and dirt."""
    import swe_af.fast.app as fast_app

    requested_remote = _make_local_remote(tmp_path / "requested-org", "requested.txt")
    checkout = tmp_path / "caller-checkout"
    _git("clone", str(local_remote), str(checkout))
    _git("checkout", "-b", "local-only", cwd=checkout)
    (checkout / "local-commit.txt").write_text("local-only commit\n", encoding="utf-8")
    _git("add", "local-commit.txt", cwd=checkout)
    _commit(checkout, "local-only commit")
    (checkout / "untracked.txt").write_text("untracked work\n", encoding="utf-8")
    head_before = _git("rev-parse", "HEAD", cwd=checkout)

    await _build(
        fast_app,
        BuildStub(),
        repo_path=str(checkout),
        repo_url=str(requested_remote),
    )

    assert _git("remote", "get-url", "origin", cwd=checkout) == str(local_remote)
    assert _git("branch", "--show-current", cwd=checkout) == "local-only"
    assert _git("rev-parse", "HEAD", cwd=checkout) == head_before
    assert (checkout / "local-commit.txt").read_text(encoding="utf-8") == (
        "local-only commit\n"
    )
    assert (checkout / "untracked.txt").read_text(encoding="utf-8") == (
        "untracked work\n"
    )


@pytest.mark.asyncio
async def test_vcf_caller_supplied_non_repo_path_is_cloned(
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC-F: repo_url clones into a caller-specified path that is not a repo."""
    import swe_af.fast.app as fast_app

    checkout = tmp_path / "chosen" / "checkout"
    stub = BuildStub()
    await _build(
        fast_app,
        stub,
        repo_path=str(checkout),
        repo_url=str(local_remote),
    )

    assert stub.calls[0] == "run_git_init"
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "from remote\n"
    assert _git("remote", "get-url", "origin", cwd=checkout) == str(local_remote)


def test_vci_credentials_are_redacted_but_raw_url_is_passed_to_git(
    tmp_path: Path,
) -> None:
    """VC-I: notes and errors hide URL userinfo while git receives it intact."""
    import swe_af.fast.app as fast_app

    repo_url = "https://x-access-token:SECRET@host/o/r.git"
    commands: list[list[str]] = []
    notes: list[str] = []
    completed_process = subprocess.CompletedProcess

    def failing_clone(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return completed_process(
            args,
            128,
            stdout="",
            stderr=f"fatal: unable to access '{repo_url}': token SECRET rejected",
        )

    with (
        patch.object(fast_app.subprocess, "run", side_effect=failing_clone),
        patch.object(
            fast_app.app,
            "note",
            side_effect=lambda message, **_: notes.append(message),
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        fast_app._prepare_repo(repo_url, str(tmp_path / "r"), "main", True)

    emitted = "\n".join([*notes, str(exc_info.value)])
    assert commands == [["git", "clone", repo_url, str(tmp_path / "r")]]
    assert "SECRET" not in emitted
    assert "x-access-token" not in emitted
    assert "https://***@host/o/r.git" in emitted


@pytest.mark.asyncio
async def test_vcj_stale_derived_workspace_without_git_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC-J: leftovers in a derived workspace that is not a repo do not block the clone."""
    import swe_af.fast.app as fast_app

    workspace = tmp_path / "workspace"
    stale = workspace / "remote"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("from a build that died\n", encoding="utf-8")
    monkeypatch.setenv("SWE_WORKSPACE_ROOT", str(workspace))
    stub = BuildStub()

    await _build(fast_app, stub, repo_url=str(local_remote))

    assert not (stale / "leftover.txt").exists()
    assert stub.git_init_observations == [
        {
            "git_exists": True,
            "tracked_exists": True,
            "remote_url": str(local_remote),
        }
    ]


@pytest.mark.asyncio
async def test_vck_populated_caller_path_is_never_cleared(
    tmp_path: Path,
    local_remote: Path,
) -> None:
    """VC-K: a caller's non-empty directory is never deleted to make room for a clone."""
    import swe_af.fast.app as fast_app

    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "keep.txt").write_text("caller's work\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        await _build(
            fast_app,
            BuildStub(),
            repo_path=str(theirs),
            repo_url=str(local_remote),
        )

    assert "git clone failed (exit" in str(exc_info.value)
    assert (theirs / "keep.txt").read_text(encoding="utf-8") == "caller's work\n"

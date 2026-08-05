"""Deterministic (non-LLM) git operations for the issue-level build path.

The full pipeline delegates git work to LLM agents (run_git_init, run_merger,
run_workspace_cleanup) because it juggles many branches and conflict
resolution. The issue-level path owns exactly one worktree + one branch off a
known base, so plain git commands are sufficient — and keep the LLM budget
for coding.

All functions are synchronous; callers wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time


class GitOpsError(RuntimeError):
    """A git operation failed in a way the caller must handle."""


def _git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise GitOpsError(f"git {' '.join(args)} failed: {detail}")
    return proc


def ensure_issue_ready_repo(repo_path: str) -> None:
    """Validate that repo_path is a git repository with at least one commit."""
    if not os.path.isdir(repo_path):
        raise GitOpsError(f"repo_path does not exist or is not a directory: {repo_path}")
    if _git(repo_path, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise GitOpsError(f"repo_path is not a git repository: {repo_path}")
    if _git(repo_path, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        raise GitOpsError(
            "repository has no commits; issue-level builds require an existing "
            "base commit (use the feature-level build for empty repos)"
        )


def current_branch(repo_path: str) -> str:
    """Current branch name, or "HEAD" when detached."""
    return _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def resolve_base(repo_path: str, base_branch: str = "") -> tuple[str, str]:
    """Resolve the base ref for the issue branch.

    Returns (base_ref, base_sha). Falls back to the caller's current branch
    (or literal "HEAD" when detached) so "work off what's checked out" is the
    zero-config behaviour.
    """
    ref = base_branch or current_branch(repo_path)
    proc = _git(repo_path, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if proc.returncode != 0:
        raise GitOpsError(f"base branch/ref not found: {ref}")
    return ref, proc.stdout.strip()


def is_dirty(repo_path: str) -> bool:
    return bool(_git(repo_path, "status", "--porcelain").stdout.strip())


def add_worktree(
    repo_path: str,
    worktree_path: str,
    branch: str,
    base_sha: str,
    attempts: int = 3,
) -> None:
    """Create an isolated worktree on a new branch off base_sha.

    Retries a few times because concurrent ``git worktree add`` calls against
    the same repository can briefly contend on the repo lock — the exact
    scenario a fan-out caller creates.
    """
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    last_detail = ""
    for attempt in range(1, attempts + 1):
        proc = _git(
            repo_path, "worktree", "add", "-b", branch, worktree_path, base_sha,
            check=False,
        )
        if proc.returncode == 0:
            return
        last_detail = proc.stderr.strip() or proc.stdout.strip()
        if attempt < attempts:
            time.sleep(0.5 * attempt)
    raise GitOpsError(f"git worktree add failed after {attempts} attempts: {last_detail}")


def _has_commit_identity(repo_path: str) -> bool:
    return bool(_git(repo_path, "config", "user.email", check=False).stdout.strip())


# Bytecode/cache junk that must never be versioned on the issue branch. The
# coder runs tests inside the worktree, so these appear as a side effect and
# an indiscriminate `git add` (ours or the coder's) would sweep them in.
_JUNK_PATHSPECS: tuple[str, ...] = ("*__pycache__*", "*.pyc", "*.pyo")


def _commit_index(worktree_path: str, message: str) -> str:
    """Commit whatever is staged. Returns the sha, or "" when index is clean."""
    identity: list[str] = []
    if not _has_commit_identity(worktree_path):
        identity = [
            "-c", "user.name=SWE-AF",
            "-c", "user.email=swe-af@agentfield.local",
        ]
    proc = _git(worktree_path, *identity, "commit", "-m", message, check=False)
    if proc.returncode != 0:
        out = f"{proc.stdout}\n{proc.stderr}".lower()
        if "nothing to commit" in out or "nothing added to commit" in out:
            return ""
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitOpsError(f"git commit failed: {detail}")
    return _git(worktree_path, "rev-parse", "HEAD").stdout.strip()


def scrub_tracked_junk(worktree_path: str, issue_name: str) -> str:
    """Untrack bytecode junk an agent committed on the issue branch.

    Returns the scrub commit sha, or "" when the branch was already clean.
    History stays append-only: the agent's commit is not rewritten.
    """
    listed = _git(worktree_path, "ls-files", "--", *_JUNK_PATHSPECS, check=False)
    if listed.returncode != 0 or not listed.stdout.strip():
        return ""
    _git(
        worktree_path, "rm", "-r", "--cached", "-q", "--ignore-unmatch",
        "--", *_JUNK_PATHSPECS,
    )
    return _commit_index(
        worktree_path, f"chore({issue_name}): untrack bytecode caches"
    )


def commit_all(worktree_path: str, message: str) -> str:
    """Commit any uncommitted changes inside the (isolated) worktree.

    ``add -A`` is safe here precisely because the worktree belongs to this
    build alone; bytecode junk is excluded. Returns the new commit sha, or
    "" when there was nothing to commit.
    """
    if not is_dirty(worktree_path):
        return ""
    excludes = [f":(exclude){p}" for p in _JUNK_PATHSPECS]
    _git(worktree_path, "add", "-A", "--", ".", *excludes)
    return _commit_index(worktree_path, message)


def new_commits(repo_path: str, base_sha: str, branch: str) -> list[str]:
    """Commits on branch since base_sha, oldest first. [] if branch is gone."""
    proc = _git(repo_path, "rev-list", "--reverse", f"{base_sha}..{branch}", check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def changed_files(repo_path: str, base_sha: str, branch: str) -> list[str]:
    proc = _git(repo_path, "diff", "--name-only", f"{base_sha}..{branch}", check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def diff_stat(repo_path: str, base_sha: str, branch: str) -> str:
    proc = _git(repo_path, "diff", "--stat", f"{base_sha}..{branch}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def remove_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove the worktree (the branch survives). Best-effort, never raises."""
    proc = _git(repo_path, "worktree", "remove", "--force", worktree_path, check=False)
    if proc.returncode != 0 and os.path.isdir(worktree_path):
        shutil.rmtree(worktree_path, ignore_errors=True)
        _git(repo_path, "worktree", "prune", check=False)


def delete_branch(repo_path: str, branch: str) -> None:
    """Delete a branch (used only when it holds no commits). Best-effort."""
    _git(repo_path, "branch", "-D", branch, check=False)


def ensure_local_excludes(repo_path: str, patterns: list[str]) -> None:
    """Add patterns to the repo-local ignore file (``.git/info/exclude``).

    Keeps issue-build bookkeeping (.artifacts, .worktrees) out of the
    caller's ``git status`` without touching the working tree — unlike
    ``.gitignore``, this file is not versioned and never shows in a diff.
    """
    git_dir = _git(repo_path, "rev-parse", "--git-dir").stdout.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo_path, git_dir)
    exclude_path = os.path.join(git_dir, "info", "exclude")
    os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
    existing: set[str] = set()
    if os.path.exists(exclude_path):
        with open(exclude_path, "r", encoding="utf-8") as f:
            existing = {line.strip() for line in f}
    to_add = [p for p in patterns if p not in existing]
    if to_add:
        with open(exclude_path, "a", encoding="utf-8") as f:
            f.write("\n".join(to_add) + "\n")


def remote_url(repo_path: str) -> str:
    proc = _git(repo_path, "remote", "get-url", "origin", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def default_remote_branch(repo_path: str) -> str:
    """Default branch of origin (e.g. "main"), or "" when unknown."""
    proc = _git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode != 0:
        return ""
    ref = proc.stdout.strip()
    prefix = "refs/remotes/origin/"
    return ref[len(prefix):] if ref.startswith(prefix) else ""

"""Deterministic git fast-paths for the DAG executor.

The workspace/merge/cleanup steps between levels are mechanical git in the
common case, but the executor historically delegated them to LLM agents —
in a live benchmark that was 23 of 88 agent calls and ~20 minutes of a
103-minute build. These helpers do the same work with plain git:

  - worktree setup: always mechanical (create branch + worktree per issue)
  - merge: mechanical when conflict-free; real conflicts fall back to the
    LLM merger (the caller passes only the conflicted branches)
  - cleanup: always mechanical (remove worktrees, delete merged branches)

``ExecutionConfig.deterministic_git=False`` restores the agent-driven path
end to end. Branch/worktree naming matches prompts/workspace.py exactly
(``issue/<BUILD_ID>-<NN>-<name>``, worktree dir = branch with ``/`` → ``-``)
so fast-path and agent-created state stay interchangeable across resume,
mixed fallbacks, and cleanup.

All functions are synchronous; the executor wraps them in asyncio.to_thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time


class GitFastPathError(RuntimeError):
    """A fast-path git operation failed; the caller should fall back to the
    agent-driven path (or record the failure) rather than crash the build."""


def _git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise GitFastPathError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _identity_args(repo_path: str) -> list[str]:
    """-c identity overrides when the repo has none configured (CI, containers)."""
    if _git(repo_path, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", "user.name=SWE-AF", "-c", "user.email=swe-af@agentfield.local"]


def branch_core(issue: dict, build_id: str = "") -> str:
    """The shared naming core: ``[<build_id>-]<NN>-<name>``."""
    name = issue.get("name") or "issue"
    seq = int(issue.get("sequence_number") or 0)
    core = f"{seq:02d}-{name}"
    return f"{build_id}-{core}" if build_id else core


def setup_worktrees(
    repo_path: str,
    integration_branch: str,
    issues: list[dict],
    worktrees_dir: str,
    build_id: str = "",
) -> dict:
    """Create one worktree + branch per issue off the integration branch.

    Returns the exact shape run_workspace_setup returns:
    ``{"success": True, "workspaces": [{issue_name, branch_name, worktree_path}]}``.
    Raises GitFastPathError when any worktree cannot be created (caller falls
    back to the agent path).
    """
    if not integration_branch:
        raise GitFastPathError("no integration branch — cannot create worktrees")
    os.makedirs(worktrees_dir, exist_ok=True)

    workspaces: list[dict] = []
    for issue in issues:
        core = branch_core(issue, build_id)
        branch = f"issue/{core}"
        worktree_path = os.path.join(worktrees_dir, f"issue-{core}")

        created = False
        last_detail = ""
        for attempt in range(1, 4):
            # Fresh branch first; if the branch survived a prior attempt or a
            # resume, attach a worktree to it instead.
            proc = _git(
                repo_path, "worktree", "add", "-b", branch,
                worktree_path, integration_branch, check=False,
            )
            if proc.returncode == 0:
                created = True
                break
            if _git(repo_path, "rev-parse", "--verify", branch, check=False).returncode == 0:
                if os.path.isdir(worktree_path):
                    created = True  # both already exist (resume) — reuse
                    break
                proc = _git(repo_path, "worktree", "add", worktree_path, branch, check=False)
                if proc.returncode == 0:
                    created = True
                    break
            last_detail = proc.stderr.strip() or proc.stdout.strip()
            time.sleep(0.3 * attempt)  # concurrent worktree adds contend on the repo lock

        if not created:
            raise GitFastPathError(
                f"worktree add failed for {branch}: {last_detail}"
            )
        workspaces.append({
            "issue_name": issue.get("name", ""),
            "branch_name": branch,
            "worktree_path": worktree_path,
        })

    return {"success": True, "workspaces": workspaces}


def merge_branches(
    repo_path: str,
    integration_branch: str,
    branch_names: list[str],
    level: int = 0,
) -> dict:
    """Merge branches into the integration branch, sequentially, --no-ff.

    A conflicting merge is aborted (the integration branch stays clean) and
    the branch lands in ``failed_branches`` for the caller to hand to the LLM
    merger. Returns a MergeResult-shaped dict.

    needs_integration_test heuristic: True only when more than one branch
    merged this level — a single branch's code already passed its per-issue
    tests, but merged siblings have never run together.
    """
    _git(repo_path, "checkout", integration_branch)
    pre_merge_sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    identity = _identity_args(repo_path)

    merged: list[str] = []
    failed: list[str] = []
    for branch in branch_names:
        proc = _git(
            repo_path, *identity, "merge", "--no-ff", branch,
            "-m", f"merge(level-{level}): {branch}",
            check=False,
        )
        if proc.returncode == 0:
            merged.append(branch)
        else:
            _git(repo_path, "merge", "--abort", check=False)
            failed.append(branch)

    summary = f"Fast-path merged {len(merged)}/{len(branch_names)} branch(es)"
    if failed:
        summary += f"; conflicts need the merger agent: {failed}"
    return {
        "success": not failed,
        "merged_branches": merged,
        "failed_branches": failed,
        "conflict_resolutions": [],
        "merge_commit_sha": _git(repo_path, "rev-parse", "HEAD").stdout.strip(),
        "pre_merge_sha": pre_merge_sha,
        "needs_integration_test": len(merged) > 1,
        "integration_test_rationale": (
            "multiple branches merged this level; they have not run together"
            if len(merged) > 1
            else "single branch merged; per-issue tests already covered it"
        ),
        "summary": summary,
    }


def cleanup_worktrees(
    repo_path: str,
    worktrees_dir: str,
    branches: list[str],
) -> dict:
    """Remove worktrees and force-delete their branches (post-merge).

    Mirrors the cleanup agent's contract: worktree dir is the branch name
    with ``/`` → ``-``. Best-effort per entry; returns {success, cleaned}.
    Raises GitFastPathError when repo_path is not a git repository at all —
    the caller falls back to the agent path rather than reporting a silent
    no-op success.
    """
    if _git(repo_path, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise GitFastPathError(f"not a git repository: {repo_path}")
    cleaned: list[str] = []
    for branch in branches:
        worktree_path = os.path.join(worktrees_dir, branch.replace("/", "-"))
        proc = _git(repo_path, "worktree", "remove", "--force", worktree_path, check=False)
        if proc.returncode != 0 and os.path.isdir(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)
        _git(repo_path, "branch", "-D", branch, check=False)
        cleaned.append(branch)
    _git(repo_path, "worktree", "prune", check=False)
    return {"success": True, "cleaned": cleaned}


def combine_merge_results(fast: dict, agent: dict) -> dict:
    """Merge the fast-path result with the LLM merger's conflict-resolution
    result (which handled only the branches the fast path could not)."""
    merged = list(fast.get("merged_branches", []))
    for b in agent.get("merged_branches", []):
        if b not in merged:
            merged.append(b)
    failed = [b for b in agent.get("failed_branches", []) if b not in merged]
    return {
        "success": not failed,
        "merged_branches": merged,
        "failed_branches": failed,
        "conflict_resolutions": agent.get("conflict_resolutions", []),
        "merge_commit_sha": agent.get("merge_commit_sha") or fast.get("merge_commit_sha", ""),
        "pre_merge_sha": fast.get("pre_merge_sha", ""),
        "needs_integration_test": True,  # conflicts were resolved — always retest
        "integration_test_rationale": "merger agent resolved conflicts this level",
        "summary": f"{fast.get('summary', '')} | merger agent: {agent.get('summary', '')}",
    }

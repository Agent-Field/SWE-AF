"""Shared fixtures for the issue-level build tests.

Everything is real except ``call_fn``: tests run against a real temp git
repository, real worktrees, and the real coding loop — only the AI agent
dispatch is scripted (same philosophy as tests/test_coding_loop.py).
"""

from __future__ import annotations

import os
import subprocess

import pytest

os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


def run_git(cwd: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", cwd, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> str:
    """A real git repository on branch ``main`` with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_s = str(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo_s, check=True)
    run_git(repo_s, "checkout", "-q", "-b", "main")
    run_git(repo_s, "config", "user.email", "test@example.com")
    run_git(repo_s, "config", "user.name", "Test")
    (repo / "README.md").write_text("# Test repo\n")
    run_git(repo_s, "add", "README.md")
    run_git(repo_s, "commit", "-q", "-m", "initial commit")
    return repo_s


PLANNING_TARGETS = {
    "run_product_manager",
    "run_architect",
    "run_tech_lead",
    "run_sprint_planner",
    "run_issue_writer",
    "run_environment_scout",
}


def make_call_fn(
    recorded: list,
    *,
    coder_commits: bool = True,
    coder_writes: bool = True,
    reviewer_responses: list[dict] | None = None,
    verifier_response: dict | None = None,
    coder_exception: Exception | None = None,
):
    """Build a scripted async ``call_fn`` that mimics the role reasoners.

    The fake coder writes (and by default commits) one file per iteration in
    the worktree it was pointed at, which is exactly what the real coder role
    does. ``reviewer_responses`` is consumed one per iteration; the last entry
    repeats once exhausted.
    """
    reviewer_responses = reviewer_responses or [
        {"approved": True, "blocking": False, "summary": "LGTM"}
    ]
    review_calls = {"n": 0}

    async def call_fn(target: str, **kwargs):
        recorded.append((target, kwargs))
        name = target.split(".", 1)[1]

        if name == "run_coder":
            if coder_exception is not None:
                raise coder_exception
            worktree = kwargs["worktree_path"]
            iteration = kwargs.get("iteration", 1)
            if coder_writes:
                path = os.path.join(worktree, "feature.py")
                with open(path, "w") as f:
                    f.write(f"VALUE = {iteration}\n")
                if coder_commits:
                    run_git(worktree, "add", "feature.py")
                    run_git(worktree, "commit", "-q", "-m", f"feat: iteration {iteration}")
            return {
                "files_changed": ["feature.py"] if coder_writes else [],
                "summary": f"iteration {iteration}",
                "complete": True,
            }

        if name == "run_code_reviewer":
            idx = min(review_calls["n"], len(reviewer_responses) - 1)
            review_calls["n"] += 1
            return dict(reviewer_responses[idx])

        if name == "run_qa":
            return {"passed": True, "summary": "qa ok", "test_failures": []}

        if name == "run_qa_synthesizer":
            return {"action": "approve", "summary": "synth ok"}

        if name == "run_verifier":
            return dict(
                verifier_response
                or {"passed": True, "criteria_results": [], "summary": "verified"}
            )

        raise AssertionError(f"unexpected call target: {target}")

    return call_fn

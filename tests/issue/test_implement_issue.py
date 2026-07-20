"""Contract tests for the issue-level build orchestrator.

Validation contract (each test names the item it covers):

  C1. Given a checkout and a scoped issue, the result branch contains commits
      implementing it; the caller's working tree and current branch are
      untouched.
  C2. N concurrent calls on the same repo_path yield N independent branches.
  C3. No planning roles are ever invoked; total agent calls stay within the
      configured budget.
  C4. Default config pushes nothing anywhere and opens no PR.
  C5. Verifier failure surfaces (success=False) instead of claiming success.
  C6. Iteration exhaustion / blocking reviews surface debt or failure.

Everything is real except the scripted ``call_fn`` (see conftest).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from swe_af.issue.build import _implement_issue_impl
from swe_af.issue.git_ops import GitOpsError
from tests.issue.conftest import PLANNING_TARGETS, make_call_fn, run_git

ISSUE = {
    "title": "Add retry helper",
    "description": "Add a retry helper with exponential backoff to utils.",
    "acceptance_criteria": ["retry() retries up to 3 times"],
    "files_to_create": ["feature.py"],
}


def _run(git_repo: str, call_fn, *, issue: dict | None = None, config: dict | None = None, **kwargs) -> dict:
    return asyncio.run(
        _implement_issue_impl(
            issue=issue or ISSUE,
            repo_path=git_repo,
            base_branch=kwargs.pop("base_branch", ""),
            artifacts_dir=kwargs.pop("artifacts_dir", ".artifacts"),
            additional_context=kwargs.pop("additional_context", ""),
            config=config,
            call_fn=call_fn,
            note_fn=kwargs.pop("note_fn", None),
            node_id="test-node",
            **kwargs,
        )
    )


class TestHappyPath:
    """C1 — branch with commits, caller untouched."""

    def test_creates_branch_and_leaves_caller_untouched(self, git_repo: str) -> None:
        head_before = run_git(git_repo, "rev-parse", "HEAD")
        recorded: list = []
        result = _run(git_repo, make_call_fn(recorded))

        assert result["success"] is True
        assert result["outcome"] == "completed"
        assert result["branch"].startswith("issue/")
        assert len(result["commits"]) == 1
        assert "feature.py" in result["files_changed"]
        assert result["base_branch"] == "main"
        assert result["verification"] is not None
        assert result["verification"]["passed"] is True
        assert result["error_message"] == ""

        # Caller repo untouched: same branch, same HEAD, clean status (the
        # artifacts dir exists but is masked via .git/info/exclude).
        assert run_git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert run_git(git_repo, "rev-parse", "HEAD") == head_before
        assert run_git(git_repo, "status", "--porcelain") == ""
        assert os.path.isdir(os.path.join(git_repo, ".artifacts", "issue-builds"))

        # The branch is the deliverable and carries the implementation.
        assert run_git(git_repo, "show", f"{result['branch']}:feature.py")

        # The worktree was cleaned up.
        worktrees = os.path.join(git_repo, ".worktrees")
        assert not os.path.isdir(worktrees) or os.listdir(worktrees) == []

    def test_uncommitted_coder_work_gets_checkpoint_commit(self, git_repo: str) -> None:
        recorded: list = []
        call_fn = make_call_fn(recorded, coder_commits=False)
        result = _run(git_repo, call_fn, config={"verify": False})

        assert result["success"] is True
        assert len(result["commits"]) == 1
        message = run_git(git_repo, "log", "-1", "--format=%s", result["branch"])
        assert "checkpoint" in message

    def test_bytecode_junk_never_lands_on_branch(self, git_repo: str) -> None:
        # The real coder runs pytest in the worktree, generating __pycache__,
        # and a sloppy model may even commit it. Neither may reach the branch.
        recorded: list = []
        base_call_fn = make_call_fn(recorded, coder_commits=False)

        async def call_fn(target, **kwargs):
            if target.endswith("run_coder"):
                worktree = kwargs["worktree_path"]
                pycache = os.path.join(worktree, "taskstore", "__pycache__")
                os.makedirs(pycache, exist_ok=True)
                with open(os.path.join(pycache, "x.cpython-312.pyc"), "wb") as f:
                    f.write(b"\x00")
            return await base_call_fn(target, **kwargs)

        result = _run(git_repo, call_fn, config={"verify": False})

        assert result["success"] is True
        assert not any("__pycache__" in f or f.endswith(".pyc")
                       for f in result["files_changed"])
        tracked = run_git(git_repo, "ls-tree", "-r", "--name-only", result["branch"])
        assert "__pycache__" not in tracked

    def test_base_branch_override(self, git_repo: str) -> None:
        run_git(git_repo, "checkout", "-q", "-b", "dev")
        with open(os.path.join(git_repo, "dev.txt"), "w") as f:
            f.write("dev\n")
        run_git(git_repo, "add", "dev.txt")
        run_git(git_repo, "commit", "-q", "-m", "dev work")
        run_git(git_repo, "checkout", "-q", "main")

        recorded: list = []
        result = _run(
            git_repo, make_call_fn(recorded),
            base_branch="dev", config={"verify": False},
        )

        assert result["success"] is True
        assert result["base_branch"] == "dev"
        # Branch includes dev's history, not just main's.
        assert run_git(git_repo, "show", f"{result['branch']}:dev.txt") == "dev"


class TestParallelFanOut:
    """C2 — concurrent calls yield independent branches."""

    def test_two_concurrent_builds_on_same_repo(self, git_repo: str) -> None:
        recorded_a: list = []
        recorded_b: list = []

        async def _both() -> tuple[dict, dict]:
            return await asyncio.gather(
                _implement_issue_impl(
                    issue={**ISSUE, "title": "Issue A"},
                    repo_path=git_repo,
                    base_branch="",
                    artifacts_dir=".artifacts",
                    additional_context="",
                    config={"verify": False},
                    call_fn=make_call_fn(recorded_a),
                    note_fn=None,
                    node_id="test-node",
                ),
                _implement_issue_impl(
                    issue={**ISSUE, "title": "Issue B"},
                    repo_path=git_repo,
                    base_branch="",
                    artifacts_dir=".artifacts",
                    additional_context="",
                    config={"verify": False},
                    call_fn=make_call_fn(recorded_b),
                    note_fn=None,
                    node_id="test-node",
                ),
            )

        result_a, result_b = asyncio.run(_both())

        assert result_a["success"] and result_b["success"]
        assert result_a["branch"] != result_b["branch"]
        branches = run_git(git_repo, "branch", "--list", "issue/*")
        assert result_a["branch"].split("/", 1)[1] in branches
        assert result_b["branch"].split("/", 1)[1] in branches
        assert run_git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


class TestCallBudget:
    """C3 — no planning agents, bounded call count."""

    def test_no_planning_agents_and_bounded_calls(self, git_repo: str) -> None:
        recorded: list = []
        result = _run(git_repo, make_call_fn(recorded))

        targets = {t.split(".", 1)[1] for t, _ in recorded}
        assert targets & PLANNING_TARGETS == set()
        assert targets <= {"run_coder", "run_code_reviewer", "run_verifier"}
        # 1 iteration: coder + reviewer, then one verifier pass.
        assert len(recorded) == 3
        assert result["iterations"] == 1

    def test_flagged_path_uses_qa_and_synthesizer(self, git_repo: str) -> None:
        recorded: list = []
        result = _run(
            git_repo,
            make_call_fn(recorded),
            issue={**ISSUE, "needs_deeper_qa": True},
            config={"verify": False},
        )

        assert result["success"] is True
        targets = [t.split(".", 1)[1] for t, _ in recorded]
        assert "run_qa" in targets
        assert "run_qa_synthesizer" in targets
        assert set(targets) & PLANNING_TARGETS == set()


class TestNoSideEffectsByDefault:
    """C4 — nothing pushed, no PR."""

    def test_no_pr_and_no_push_targets(self, git_repo: str) -> None:
        recorded: list = []
        result = _run(git_repo, make_call_fn(recorded))

        targets = {t.split(".", 1)[1] for t, _ in recorded}
        assert "run_github_pr" not in targets
        assert result["pr_url"] == ""
        # No remotes were ever configured, so nothing could have been pushed.
        assert run_git(git_repo, "remote") == ""

    def test_pr_skipped_without_remote_even_when_enabled(self, git_repo: str) -> None:
        recorded: list = []
        notes: list = []
        result = _run(
            git_repo,
            make_call_fn(recorded),
            config={"verify": False, "enable_github_pr": True},
            note_fn=lambda msg, tags=None: notes.append(msg),
        )

        assert result["success"] is True
        assert result["pr_url"] == ""
        targets = {t.split(".", 1)[1] for t, _ in recorded}
        assert "run_github_pr" not in targets
        assert any("skipping PR" in n for n in notes)


class TestVerification:
    """C5 — verifier failure surfaces."""

    def test_verifier_failure_fails_build_but_keeps_branch(self, git_repo: str) -> None:
        recorded: list = []
        call_fn = make_call_fn(
            recorded,
            verifier_response={
                "passed": False,
                "criteria_results": [
                    {"criterion": "retry() retries", "passed": False, "evidence": "no test"}
                ],
                "summary": "AC not met",
            },
        )
        result = _run(git_repo, call_fn)

        assert result["success"] is False
        assert result["outcome"] == "completed"
        assert result["verification"]["passed"] is False
        # Partial work is still handed to the caller for triage.
        assert result["branch"].startswith("issue/")
        assert len(result["commits"]) == 1

    def test_verify_disabled_skips_verifier(self, git_repo: str) -> None:
        recorded: list = []
        result = _run(git_repo, make_call_fn(recorded), config={"verify": False})

        targets = {t.split(".", 1)[1] for t, _ in recorded}
        assert "run_verifier" not in targets
        assert result["verification"] is None
        assert result["success"] is True


class TestFailureModes:
    """C6 — exhaustion, blocking review, coder crash."""

    def test_exhaustion_completes_with_debt(self, git_repo: str) -> None:
        recorded: list = []
        call_fn = make_call_fn(
            recorded,
            reviewer_responses=[
                {"approved": False, "blocking": False, "summary": "needs polish"}
            ],
        )
        result = _run(
            git_repo, call_fn,
            config={"verify": False, "max_coding_iterations": 2},
        )

        assert result["outcome"] == "completed_with_debt"
        assert result["success"] is True
        assert result["iterations"] == 2
        assert len(result["iteration_history"]) == 2

    def test_blocking_review_fails_but_salvages_commits(self, git_repo: str) -> None:
        recorded: list = []
        call_fn = make_call_fn(
            recorded,
            reviewer_responses=[
                {"approved": False, "blocking": True, "summary": "introduces data loss"}
            ],
        )
        result = _run(git_repo, call_fn, config={"verify": False})

        assert result["success"] is False
        assert result["outcome"] == "failed_unrecoverable"
        assert result["error_message"]
        # The coder committed before the block: the branch is kept for triage.
        assert result["branch"].startswith("issue/")
        assert len(result["commits"]) == 1

    def test_no_commits_deletes_branch(self, git_repo: str) -> None:
        recorded: list = []
        call_fn = make_call_fn(recorded, coder_writes=False)
        result = _run(git_repo, call_fn, config={"verify": False})

        assert result["success"] is False
        assert result["branch"] == ""
        assert result["commits"] == []
        assert run_git(git_repo, "branch", "--list", "issue/*") == ""

    def test_unexpected_error_returns_structured_failure_and_cleans_up(
        self, git_repo: str
    ) -> None:
        from swe_af.execution.fatal_error import FatalHarnessError

        recorded: list = []
        call_fn = make_call_fn(
            recorded, coder_exception=FatalHarnessError("credit balance too low")
        )
        result = _run(git_repo, call_fn, config={"verify": False})

        assert result["success"] is False
        assert result["outcome"] == "error"
        assert "credit balance" in result["error_message"]
        assert result["branch"] == ""
        assert run_git(git_repo, "branch", "--list", "issue/*") == ""
        worktrees = os.path.join(git_repo, ".worktrees")
        assert not os.path.isdir(worktrees) or os.listdir(worktrees) == []


class TestSetupValidation:
    """Setup errors raise — they are the caller's to fix."""

    def test_missing_repo_raises(self, tmp_path) -> None:
        with pytest.raises(GitOpsError, match="does not exist"):
            _run(str(tmp_path / "nope"), make_call_fn([]))

    def test_non_git_dir_raises(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitOpsError, match="not a git repository"):
            _run(str(plain), make_call_fn([]))

    def test_repo_without_commits_raises(self, tmp_path) -> None:
        import subprocess

        empty = tmp_path / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(empty), check=True)
        with pytest.raises(GitOpsError, match="no commits"):
            _run(str(empty), make_call_fn([]))

    def test_unknown_base_branch_raises(self, git_repo: str) -> None:
        with pytest.raises(GitOpsError, match="base branch"):
            _run(git_repo, make_call_fn([]), base_branch="does-not-exist")

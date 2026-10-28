"""Escalation fixes L1 and L2: blocking means fix, and advisor retries really run.

L1 — on the default path a reviewer ``blocking=True`` maps to a fix iteration
(the reviewer's own definition of blocking is "must be fixed before merge"), the
blocking findings lead the feedback, and three fix iterations on the same
blocking finding count as stuck.

L2 — an advisor retry gets a real iteration window (per-round iteration
checkpoint), a loop that ran zero iterations is never reported as a completion,
and every ``COMPLETED_WITH_DEBT`` records at least one debt item built from the
last review's blocking and should-fix findings.

The only mock is ``call_fn``; DAGState, ExecutionConfig, artifacts and
iteration bookkeeping are real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from swe_af.execution.coding_loop import run_coding_loop
from swe_af.execution.dag_executor import _execute_single_issue
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome

BLOCKING_FINDING = {
    "severity": "blocking",
    "title": "Unjustified changed files",
    "description": "src/extra_helper.py is not justified by any criterion",
    "file_path": "src/extra_helper.py",
}
SHOULD_FIX_FINDINGS = [
    {
        "severity": "should_fix",
        "title": "Weak error handling",
        "description": "parse() swallows decode errors",
        "file_path": "src/app.py",
    },
    {
        "severity": "suggestion",
        "title": "Style nit",
        "description": "prefer a named constant",
        "file_path": "src/app.py",
    },
]


def _dag_state(tmp_path: Path, build_id: str = "build-l12") -> DAGState:
    return DAGState(
        repo_path=str(tmp_path),
        artifacts_dir=str(tmp_path / "artifacts"),
        build_id=build_id,
    )


def _issue(name: str = "escalation-issue") -> dict:
    return {
        "name": name,
        "title": "Test issue",
        "description": "A test issue for escalation fixes",
        "acceptance_criteria": ["AC-1: it works"],
        "worktree_path": "/tmp/fake-repo",
        "branch_name": "test/escalation-issue",
    }


def _config(**overrides) -> ExecutionConfig:
    defaults = {
        "max_coding_iterations": 5,
        "agent_timeout_seconds": 30,
    }
    defaults.update(overrides)
    return ExecutionConfig(**defaults)


def _blocking_review(summary: str = "Summary: fix the blocking finding.") -> dict:
    return {
        "approved": False,
        "blocking": True,
        "summary": summary,
        "debt_items": [dict(BLOCKING_FINDING)],
    }


def _approved_review() -> dict:
    return {"approved": True, "blocking": False, "summary": "LGTM", "debt_items": []}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# L1 — default-path blocking means "fix before merge"
# ---------------------------------------------------------------------------


def test_default_path_blocking_review_gets_fix_iteration_with_findings_first(
    tmp_path: Path,
) -> None:
    """A blocking review produces a fix iteration whose feedback leads with the finding."""
    feedbacks: list[str] = []
    coder_calls = 0

    def call_fn(agent: str, **kwargs):
        async def _invoke():
            nonlocal coder_calls
            if agent.endswith(".run_coder"):
                coder_calls += 1
                feedbacks.append(kwargs.get("feedback", ""))
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                return _blocking_review() if coder_calls == 1 else _approved_review()
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(tmp_path),
        call_fn=call_fn,
        node_id="test-node",
        config=_config(max_coding_iterations=3),
    ))

    assert result.outcome == IssueOutcome.COMPLETED
    assert result.attempts == 2
    assert result.iteration_history[0]["action"] == "fix"
    assert result.iteration_history[0]["review_blocking"] is True

    # The fix pass is real and the blocking finding comes before the summary.
    second_feedback = feedbacks[1]
    assert "Blocking Review Issues (fix before merge)" in second_feedback
    assert second_feedback.index(BLOCKING_FINDING["title"]) < second_feedback.index(
        "Summary: fix the blocking finding."
    )


def test_same_blocking_findings_three_times_counts_as_stuck(tmp_path: Path) -> None:
    """Three fix iterations on the same blocking finding stop the loop."""
    coder_calls = 0

    def call_fn(agent: str, **kwargs):
        async def _invoke():
            nonlocal coder_calls
            if agent.endswith(".run_coder"):
                coder_calls += 1
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                return _blocking_review()
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(tmp_path),
        call_fn=call_fn,
        node_id="test-node",
        config=_config(max_coding_iterations=5),
    ))

    # Stuck on iteration 3, and still blocking → terminal failure, never debt.
    assert result.attempts == 3
    assert result.outcome == IssueOutcome.FAILED_UNRECOVERABLE
    assert result.debt_items == []


def test_different_blocking_findings_are_not_stuck(tmp_path: Path) -> None:
    """Blocking findings that change each pass are progress, not a stuck loop."""
    coder_calls = 0
    review_calls = 0

    def call_fn(agent: str, **kwargs):
        async def _invoke():
            nonlocal coder_calls, review_calls
            if agent.endswith(".run_coder"):
                coder_calls += 1
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                review_calls += 1
                if review_calls < 4:
                    finding = {
                        "severity": "blocking",
                        "title": f"Finding {review_calls}",
                        "description": f"blocking issue {review_calls}",
                    }
                    return {
                        "approved": False,
                        "blocking": True,
                        "summary": f"Fix finding {review_calls}",
                        "debt_items": [finding],
                    }
                return _approved_review()
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(tmp_path),
        call_fn=call_fn,
        node_id="test-node",
        config=_config(max_coding_iterations=5),
    ))

    assert result.outcome == IssueOutcome.COMPLETED
    assert result.attempts == 4


# ---------------------------------------------------------------------------
# L2 — each advisor retry gets a real coding loop
# ---------------------------------------------------------------------------


def test_advisor_retry_runs_coder_iterations(tmp_path: Path) -> None:
    """A RETRY_APPROACH round starts a fresh window instead of resuming an exhausted one."""
    coder_calls = 0
    advisor_calls = 0

    def call_fn(agent: str, **kwargs):
        async def _invoke():
            nonlocal coder_calls, advisor_calls
            if agent.endswith(".run_coder"):
                coder_calls += 1
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                # Round 0 exhausts its two iterations still blocking; the retry
                # (coder call 3) passes.
                return _blocking_review() if coder_calls <= 2 else _approved_review()
            if agent.endswith(".run_issue_advisor"):
                advisor_calls += 1
                return {
                    "action": "retry_approach",
                    "failure_diagnosis": "Coder kept the extra file",
                    "rationale": "Remove the unrequested file",
                    "new_approach": "Use a simpler data model",
                    "approach_changes": ["delete src/extra_helper.py"],
                    "summary": "Retry with a simpler approach",
                }
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    config = _config(
        max_coding_iterations=2,
        enable_issue_advisor=True,
        max_advisor_invocations=1,
    )
    result = _run(_execute_single_issue(
        issue=_issue("retry-issue"),
        dag_state=_dag_state(tmp_path),
        execute_fn=None,
        config=config,
        call_fn=call_fn,
        node_id="test-node",
    ))

    assert advisor_calls == 1
    assert coder_calls == 3  # two in round 0, one real iteration in round 1
    assert result.outcome == IssueOutcome.COMPLETED
    assert result.attempts == 1  # the retry window's own counter, not the old one

    retry_checkpoint = (
        tmp_path / "artifacts" / "execution" / "iterations" / "build-l12"
        / "retry-issue.r1.json"
    )
    assert retry_checkpoint.exists()
    assert json.loads(retry_checkpoint.read_text())["iteration"] == 1


def test_exhausted_window_never_reports_completion(tmp_path: Path) -> None:
    """A window that starts past max_coding_iterations fails; it is never a completion."""
    state_path = (
        tmp_path / "artifacts" / "execution" / "iterations" / "build-l12"
        / "stale-issue.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "iteration": 2,
        "feedback": "still blocked",
        "files_changed": ["src/app.py"],
        "iteration_history": [
            {
                "iteration": 2,
                "action": "fix",
                "summary": "still blocked",
                "review_approved": False,
                "review_blocking": True,
                "path": "default",
            },
        ],
    }))

    coder_calls = 0

    def call_fn(agent: str, **kwargs):
        async def _invoke():
            nonlocal coder_calls
            if agent.endswith(".run_coder"):
                coder_calls += 1
                return {"files_changed": ["src/app.py"]}
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(run_coding_loop(
        issue=_issue("stale-issue"),
        dag_state=_dag_state(tmp_path),
        call_fn=call_fn,
        node_id="test-node",
        config=_config(max_coding_iterations=2),
    ))

    assert coder_calls == 0
    assert result.outcome == IssueOutcome.FAILED_UNRECOVERABLE
    assert result.attempts == 0
    assert result.debt_items == []
    assert "zero iterations" in result.error_message


def test_completed_with_debt_records_review_findings(tmp_path: Path) -> None:
    """Accepted-with-debt carries the last review's should-fix findings into the DAG debt."""
    def call_fn(agent: str, **kwargs):
        async def _invoke():
            if agent.endswith(".run_coder"):
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                return {
                    "approved": False,
                    "blocking": False,
                    "summary": "Functionally complete, polish left",
                    "debt_items": [dict(f) for f in SHOULD_FIX_FINDINGS],
                }
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(_execute_single_issue(
        issue=_issue("debt-issue"),
        dag_state=_dag_state(tmp_path),
        execute_fn=None,
        config=_config(max_coding_iterations=2, enable_issue_advisor=False),
        call_fn=call_fn,
        node_id="test-node",
    ))

    assert result.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    assert result.debt_items, "completed_with_debt must record debt items"
    titles = {item["title"] for item in result.debt_items}
    assert "Weak error handling" in titles      # should_fix is debt
    assert "Style nit" not in titles            # suggestions are not
    assert all(item["severity"] == "should_fix" for item in result.debt_items)


def test_advisor_accept_with_debt_always_records_an_item(tmp_path: Path) -> None:
    """COMPLETED_WITH_DEBT records an item even when the advisor itemises nothing."""
    def call_fn(agent: str, **kwargs):
        async def _invoke():
            if agent.endswith(".run_coder"):
                return {"files_changed": ["src/app.py"]}
            if agent.endswith(".run_code_reviewer"):
                return _blocking_review()
            if agent.endswith(".run_issue_advisor"):
                return {
                    "action": "accept_with_debt",
                    "failure_diagnosis": "Acceptance criterion cannot be met",
                    "rationale": "Ship the rest",
                    "missing_functionality": [],
                    "summary": "Accepted with debt",
                }
            raise AssertionError(f"Unexpected agent call: {agent}")

        return _invoke()

    result = _run(_execute_single_issue(
        issue=_issue("accept-debt-issue"),
        dag_state=_dag_state(tmp_path),
        execute_fn=None,
        config=_config(
            max_coding_iterations=1,
            enable_issue_advisor=True,
            max_advisor_invocations=1,
        ),
        call_fn=call_fn,
        node_id="test-node",
    ))

    assert result.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    assert result.debt_items
    assert result.debt_items[0]["type"] == "advisor_accepted_failure"

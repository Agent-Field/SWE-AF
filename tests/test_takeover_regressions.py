"""Model-free crash-resume, checkpoint namespace and reviewer-outage regressions."""

import asyncio
import json
from pathlib import Path

import pytest

from swe_af.execution.coding_loop import (
    _iteration_state_key,
    _iteration_state_path,
    run_coding_loop,
)
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome


def _run(root, call_fn, *, name="resume", budget=1, round=0):
    return asyncio.run(run_coding_loop(
        issue={"name": name, "branch_name": "test/resume"},
        dag_state=DAGState(repo_path=str(root), artifacts_dir=str(root), build_id="build"),
        call_fn=call_fn,
        node_id="test",
        config=ExecutionConfig(max_coding_iterations=budget, enable_issue_advisor=False),
        advisor_round=round,
    ))


async def _no_calls(*args, **kwargs):
    pytest.fail("An exhausted window must use history without agent calls")


def _checkpoint(root, history, *, files=None, name="resume", round=0):
    path = Path(_iteration_state_path(str(root), _iteration_state_key(name, round), "build"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "iteration": 1, "feedback": "recorded feedback",
        "files_changed": ["app.py"] if files is None else files,
        "iteration_history": history,
    }))
    return path


@pytest.mark.parametrize("round", [0, 1])
@pytest.mark.parametrize("approved", [True, False])
def test_finished_one_iteration_window_recovers_from_history(tmp_path, round, approved):
    calls = []

    async def call_fn(target, **kwargs):
        calls.append(target)
        if target.endswith("run_coder"):
            return {"files_changed": ["app.py"], "repo_name": "api"}
        return {"approved": approved, "blocking": False, "summary": "Verified feedback",
                "debt_items": [{"severity": "should_fix", "title": "Decode errors"}]}

    first = _run(tmp_path, call_fn, round=round)
    assert len(calls) == 2
    resumed = _run(tmp_path, _no_calls, round=round)
    expected = IssueOutcome.COMPLETED if approved else IssueOutcome.COMPLETED_WITH_DEBT
    assert first.outcome == resumed.outcome == expected
    assert first.attempts == resumed.attempts == 1
    assert first.result_summary == resumed.result_summary
    assert first.files_changed == resumed.files_changed == ["app.py"]
    assert first.branch_name == resumed.branch_name == "test/resume"
    if approved:
        assert resumed.repo_name == first.repo_name == "api"
    else:
        assert resumed.debt_items == first.debt_items
        assert len(resumed.debt_items) >= 1
    state = json.loads(_checkpoint_path(tmp_path, "resume", round).read_text())
    assert "result" not in state and "issue" not in state  # No whole-result replay/ownership.


def _checkpoint_path(root, name, round):
    return Path(_iteration_state_path(str(root), _iteration_state_key(name, round), "build"))


@pytest.mark.parametrize("entry,files,expected", [
    ({"iteration": 1, "action": "approve", "review_approved": True,
      "review_blocking": False, "summary": "Approved"}, [], IssueOutcome.COMPLETED),
    ({"iteration": 1, "action": "approve", "review_approved": False,
      "review_blocking": False, "path": "flagged"}, ["app.py"], IssueOutcome.COMPLETED),
    ({"iteration": 1, "action": "fix", "review_approved": False,
      "review_blocking": False, "summary": "Polish"}, ["app.py"], IssueOutcome.COMPLETED_WITH_DEBT),
    ({"iteration": 1, "action": "fix", "review_blocking": True}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    (None, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "action": "fix"}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "action": "approve"}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "action": "approve", "review_blocking": False}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "action": "fix", "review_blocking": False}, [], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "action": "block", "review_blocking": False}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": 1, "review_blocking": False}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"action": "fix", "review_blocking": False}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
    ({"iteration": None, "action": "fix", "review_blocking": False}, ["app.py"], IssueOutcome.FAILED_UNRECOVERABLE),
])
def test_legacy_history_requires_positive_evidence(tmp_path, entry, files, expected):
    _checkpoint(tmp_path, [] if entry is None else [entry], files=files)
    result = _run(tmp_path, _no_calls)
    assert result.outcome == expected
    if expected == IssueOutcome.COMPLETED_WITH_DEBT:
        assert result.attempts == 1
        assert result.debt_items[0]["description"] == "Polish"


def test_remaining_budget_runs_next_iteration_even_after_approval(tmp_path):
    _checkpoint(tmp_path, [{"iteration": 1, "action": "approve", "review_approved": True,
                            "review_blocking": False}])
    iterations = []

    async def call_fn(target, **kwargs):
        if target.endswith("run_coder"):
            iterations.append(kwargs["iteration"])
            return {"files_changed": ["restored.py"]}
        return {"approved": True, "blocking": False, "summary": "Fresh review"}

    result = _run(tmp_path, call_fn, budget=3)
    assert iterations == [2]
    assert result.outcome == IssueOutcome.COMPLETED
    assert "restored.py" in result.files_changed


def test_null_history_is_unestablished(tmp_path):
    _checkpoint(tmp_path, None)
    assert _run(tmp_path, _no_calls).outcome == IssueOutcome.FAILED_UNRECOVERABLE


@pytest.mark.parametrize("reverse", [False, True])
def test_advisor_round_and_literal_issue_never_share_state(tmp_path, reverse):
    windows = [("parent", 1), ("parent.r1", 0)]
    if reverse:
        windows.reverse()
    paths = [_checkpoint_path(tmp_path, name, round) for name, round in windows]
    assert paths[0] != paths[1]
    for name, round in windows:
        calls = []

        async def call_fn(target, **kwargs):
            calls.append(target)
            if target.endswith("run_coder"):
                assert kwargs["iteration"] == 1
                return {"files_changed": [f"{name}.py"]}
            return {"approved": True, "blocking": False, "summary": name}

        result = _run(tmp_path, call_fn, name=name, round=round)
        assert len(calls) == 2
        assert result.files_changed == [f"{name}.py"]
    for (name, round), path in zip(windows, paths):
        assert path.exists()
        resumed = _run(tmp_path, _no_calls, name=name, round=round)
        assert resumed.result_summary == name
        assert resumed.files_changed == [f"{name}.py"]


@pytest.mark.parametrize("name", ["parent", "parent.r1", "parent%2Er1", "advisor-rounds", ".", ".."])
@pytest.mark.parametrize("build_id", ["", "build"])
def test_round_zero_path_matches_base(tmp_path, name, build_id):
    assert _iteration_state_key(name, 0) == name
    base = tmp_path / "execution" / "iterations"
    if build_id:
        base /= build_id
    assert _iteration_state_path(str(tmp_path), _iteration_state_key(name, 0), build_id) == str(base / f"{name}.json")


@pytest.mark.parametrize("name", ["parent", "parent.r1", "parent%2Er1", "advisor-rounds", ".", ".."])
def test_round_path_is_below_separate_directory(tmp_path, name):
    base = tmp_path / "execution" / "iterations" / "build"
    assert _iteration_state_path(str(tmp_path), _iteration_state_key(name, 1), "build") == str(base / "advisor-rounds" / "r1" / f"{name}.json")


@pytest.mark.parametrize("budget,attempts", [(2, 2), (5, 3)])
def test_reviewer_outage_preserves_known_blocker_and_stuck_fingerprint(tmp_path, budget, attempts):
    reviews = 0
    feedback = []

    async def call_fn(target, **kwargs):
        nonlocal reviews
        if target.endswith("run_coder"):
            feedback.append(kwargs["feedback"])
            return {"files_changed": ["query.py"]}
        reviews += 1
        if reviews > 1:
            raise RuntimeError("Reviewer unavailable")
        return {"approved": False, "blocking": True, "summary": "Unsafe query",
                "debt_items": [{"severity": "blocking", "title": "SQL injection",
                                "description": "Parameterize input", "file_path": "query.py"}]}

    result = _run(tmp_path, call_fn, budget=budget)
    assert result.outcome == IssueOutcome.FAILED_UNRECOVERABLE
    assert result.attempts == reviews == attempts
    assert all(e["action"] == "fix" and e["review_blocking"] for e in result.iteration_history)
    assert {e["review_findings"] for e in result.iteration_history} == {"sql injection"}
    assert "Parameterize input" in feedback[1]
    if attempts == 3:
        assert "Stuck loop" in result.error_message


@pytest.mark.parametrize("legacy", [False, True])
def test_reviewer_outage_preserves_blocker_across_resume(tmp_path, legacy):
    finding = {"severity": "blocking", "title": "SQL injection", "description": "Parameterize input"}
    entry = {"iteration": 1, "action": "fix", "review_approved": False,
             "review_blocking": True, "review_findings": "sql injection", "summary": "Unsafe query"}
    if not legacy:
        entry["review_debt_items"] = [finding]
    _checkpoint(tmp_path, [entry])

    async def call_fn(target, **kwargs):
        if target.endswith("run_coder"):
            return {"files_changed": ["app.py"]}
        raise RuntimeError("Reviewer unavailable")

    result = _run(tmp_path, call_fn, budget=5)
    assert result.outcome == IssueOutcome.FAILED_UNRECOVERABLE
    assert result.attempts == 3
    assert {e["review_findings"] for e in result.iteration_history} == {"sql injection"}


@pytest.mark.parametrize("cleared_blocker", [False, True])
def test_reviewer_outage_without_unresolved_blocker_keeps_fail_open(tmp_path, cleared_blocker):
    reviews = 0

    async def call_fn(target, **kwargs):
        nonlocal reviews
        if target.endswith("run_coder"):
            return {"files_changed": ["app.py"]}
        reviews += 1
        if cleared_blocker and reviews <= 2:
            return {"approved": False, "blocking": reviews == 1, "summary": "Known feedback"}
        raise RuntimeError("Reviewer unavailable")

    result = _run(tmp_path, call_fn, budget=3)
    assert result.outcome == IssueOutcome.COMPLETED
    assert reviews == (3 if cleared_blocker else 1)

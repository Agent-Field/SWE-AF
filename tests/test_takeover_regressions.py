"""Model-free crash-resume, checkpoint namespace and reviewer-outage regressions."""

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

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
    path = _checkpoint_path(root, name, round)
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
    if round:
        return root / "execution" / "iteration-rounds" / "build" / f"r{round}" / f"{quote(name, safe='')}.json"
    return Path(_iteration_state_path(str(root), _iteration_state_key(name, 0), "build"))


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


@pytest.mark.parametrize("windows", [
    [("parent", 1), ("parent.r1", 0)],
    [("victim", 1), ("advisor-rounds/r1/victim", 0)],
    [("victim", 0), ("../../victim", 1)],
])
@pytest.mark.parametrize("reverse", [False, True])
def test_advisor_round_and_literal_issue_never_share_state(tmp_path, reverse, windows):
    # Establish the old retry directories too, so HEAD traversal resolves.
    (tmp_path / "execution" / "iterations" / "build" / "advisor-rounds" / "r1").mkdir(parents=True)
    windows = list(windows)
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


@pytest.mark.parametrize("name", ["parent", "parent.r1", "parent%2Er1", "advisor-rounds", ".", "..", "", "advisor-rounds/r1/victim", "../../victim", "a\\b", "C:drive", "a" * 300])
@pytest.mark.parametrize("build_id", ["", "build"])
def test_round_zero_path_matches_base(tmp_path, name, build_id):
    assert _iteration_state_key(name, 0) == name
    base = tmp_path / "execution" / "iterations"
    if build_id:
        base /= build_id
    assert _iteration_state_path(str(tmp_path), _iteration_state_key(name, 0), build_id) == str(base / f"{name}.json")


@pytest.mark.parametrize("name", ["parent", "parent.r1", "parent%2Er1", "advisor-rounds", ".", ".."])
def test_round_path_is_below_separate_directory(tmp_path, name):
    async def call_fn(target, **kwargs):
        if target.endswith("run_coder"):
            return {"files_changed": ["app.py"]}
        return {"approved": True, "blocking": False}

    _run(tmp_path, call_fn, name=name, round=1)
    assert _checkpoint_path(tmp_path, name, 1).exists()


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


@pytest.mark.parametrize("name,encoded", [
    ("a/b", "a%2Fb"), ("a\\b", "a%5Cb"), ("a%b", "a%25b"),
    ("C:drive", "C%3Adrive"), ("a%2Fb", "a%252Fb"),
    (".", "."), ("..", ".."), ("", ""),
    ("CON", "%43ON"), ("nul.txt", "%6Eul.txt"),
    ('a<>"|?*\x00\x1f\x7f', "a%3C%3E%22%7C%3F%2A%00%1F%7F"),
    ("café", "caf%C3%A9"), ("space ", "space%20"),
])
@pytest.mark.parametrize("build_id,round", [("", 1), ("build", 2)])
def test_retry_name_is_one_encoded_component_and_resumes(tmp_path, name, encoded, build_id, round):
    state = DAGState(repo_path=str(tmp_path), artifacts_dir=str(tmp_path), build_id=build_id)
    calls = []

    async def call_fn(target, **kwargs):
        calls.append(target)
        if target.endswith("run_coder"):
            assert kwargs["iteration"] == 1
            return {"files_changed": ["app.py"]}
        return {"approved": True, "blocking": False, "summary": name}

    def run(fn):
        return asyncio.run(run_coding_loop(
            issue={"name": name}, dag_state=state, call_fn=fn, node_id="test",
            config=ExecutionConfig(max_coding_iterations=1), advisor_round=round,
        ))

    first = run(call_fn)
    expected = tmp_path / "execution" / "iteration-rounds"
    if build_id:
        expected /= build_id
    expected = expected / f"r{round}" / f"{encoded}.json"
    assert expected.is_file()
    assert len(calls) == 2
    assert run(_no_calls).model_dump() == first.model_dump()


def test_very_long_retry_name_is_not_truncated_or_aliased(tmp_path):
    name = "x" * 300
    async def call_fn(target, **kwargs):
        if target.endswith("run_coder"):
            return {"files_changed": ["app.py"]}
        return {"approved": True, "blocking": False}

    # Base also rejects an overlong component. Retrying must not silently
    # truncate it into another issue's valid checkpoint name.
    for round in (0, 1):
        with pytest.raises(OSError):
            _run(tmp_path, call_fn, name=name, round=round)
    assert not list((tmp_path / "execution").rglob("*.json"))


@pytest.mark.parametrize("retry_action", ["retry_approach", "retry_modified"])
@pytest.mark.parametrize("terminal,expected", [
    ("completed", IssueOutcome.COMPLETED),
    ("debt", IssueOutcome.COMPLETED_WITH_DEBT),
    ("failed", IssueOutcome.FAILED_UNRECOVERABLE),
    ("accept_with_debt", IssueOutcome.COMPLETED_WITH_DEBT),
    ("escalate_to_replan", IssueOutcome.FAILED_ESCALATED),
    ("split", IssueOutcome.FAILED_NEEDS_SPLIT),
    ("advisor_error", IssueOutcome.FAILED_UNRECOVERABLE),
    ("unknown", IssueOutcome.FAILED_UNRECOVERABLE),
])
def test_advisor_terminal_results_keep_ordered_files_from_all_windows(tmp_path, retry_action, terminal, expected):
    from swe_af.execution.dag_executor import _execute_single_issue

    round = 0
    resumed = False
    iterations = []

    async def call_fn(target, **kwargs):
        nonlocal round
        if target.endswith("run_issue_advisor"):
            round = kwargs["advisor_invocation"]
            if round == 1:
                return {"action": retry_action, "modified_acceptance_criteria": ["Works"]}
            if terminal == "advisor_error":
                raise RuntimeError("Advisor unavailable")
            return {"action": terminal, "sub_issues": []}
        assert not resumed, "Completed coding windows must resume without agent calls"
        if target.endswith("run_coder"):
            iterations.append(kwargs["iteration"])
            return {"files_changed": ["core.py", "shared.py"] if round == 0 else ["fix.py", "shared.py"]}
        return {"approved": round > 0 and terminal == "completed",
                "blocking": round == 0 or terminal not in ("completed", "debt"),
                "summary": "Window review"}

    max_advisor = 2 if terminal in ("accept_with_debt", "escalate_to_replan", "split", "advisor_error", "unknown") else 1
    config = ExecutionConfig(max_coding_iterations=1, max_advisor_invocations=max_advisor)
    state = DAGState(repo_path=str(tmp_path), artifacts_dir=str(tmp_path), build_id="build")

    def run():
        return asyncio.run(_execute_single_issue(
            issue={"name": "files", "acceptance_criteria": ["Works"]},
            dag_state=state, execute_fn=None, call_fn=call_fn, config=config, node_id="test",
        ))

    result = run()
    assert result.outcome == expected
    assert result.files_changed == ["core.py", "shared.py", "fix.py"]
    assert result.attempts == 1
    assert len(result.iteration_history) == 1
    expected_rounds = 3 if terminal == "unknown" else 2
    assert iterations == [1] * expected_rounds
    for window in range(expected_rounds):
        checkpoint = json.loads(_checkpoint_path(tmp_path, "files", window).read_text())
        assert checkpoint["iteration"] == 1
        assert checkpoint["files_changed"] == (["core.py", "shared.py"] if window == 0 else ["fix.py", "shared.py"])
    round = 0
    resumed = True
    again = run()
    assert again.outcome == expected
    assert again.files_changed == result.files_changed

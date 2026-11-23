"""An unrun reviewer or QA agent must never read as an approval or a pass.

Contract (behavior, not implementation):

* When the code-reviewer agent cannot produce a verdict (harness raises, or
  returns nothing parseable), the reasoner's result says so
  (``review_unavailable``) and is not an approval; it is also not a blocking
  finding.
* Same for QA: ``qa_unavailable``, ``passed`` is False.
* The flags are not part of the schemas the agents are asked to fill.
* When the synthesizer also cannot decide, QA passing plus an unavailable
  reviewer still resolves to the pre-existing ``approve`` decision, and the
  coding loop records it as accepted with debt; a failing or missing QA verdict
  keeps resolving to ``fix``.
* Through the whole loop (real reasoners, only the harness is mocked) an unrun
  reviewer ends as COMPLETED_WITH_DEBT with a ``review_unavailable`` debt item,
  never as a clean COMPLETED, and a blocking finding from the previous iteration
  survives a reviewer outage.
* A checkpointed accept-with-debt decision replays as COMPLETED_WITH_DEBT.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe_af.execution.coding_loop import run_coding_loop
from swe_af.execution.schemas import (
    CodeReviewResult,
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    QAResult,
    QASynthesisResult,
)
from swe_af.reasoners import execution_agents


def _harness_result(parsed) -> SimpleNamespace:
    return SimpleNamespace(parsed=parsed, is_error=False, error_message=None, result="")


def _router(harness) -> MagicMock:
    return MagicMock(harness=harness)


REVIEWER_KW = {"worktree_path": "/tmp/fake-repo", "coder_result": {}, "issue": {"name": "I-1"}}


# ---------------------------------------------------------------------------
# Reasoner level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "harness",
    [
        AsyncMock(return_value=_harness_result(None)),
        AsyncMock(side_effect=RuntimeError("provider exploded")),
    ],
    ids=["no-parsed-verdict", "harness-raises"],
)
async def test_reviewer_outage_is_flagged_and_not_an_approval(monkeypatch, harness) -> None:
    monkeypatch.setattr(execution_agents, "router", _router(harness))

    out = await execution_agents.run_code_reviewer(iteration_id="it-1", **REVIEWER_KW)

    assert out["review_unavailable"] is True
    assert out["approved"] is False
    assert out["blocking"] is False
    assert out["iteration_id"] == "it-1"


async def test_reviewer_real_verdict_carries_no_outage_flag(monkeypatch) -> None:
    harness = AsyncMock(return_value=_harness_result(CodeReviewResult(approved=True, summary="ok")))
    monkeypatch.setattr(execution_agents, "router", _router(harness))

    out = await execution_agents.run_code_reviewer(**REVIEWER_KW)

    assert out["approved"] is True
    assert "review_unavailable" not in out


async def test_outage_flags_are_not_part_of_the_agent_output_schemas() -> None:
    assert "review_unavailable" not in CodeReviewResult.model_json_schema()["properties"]
    assert "qa_unavailable" not in QAResult.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "harness",
    [
        AsyncMock(return_value=_harness_result(None)),
        AsyncMock(side_effect=RuntimeError("provider exploded")),
    ],
    ids=["no-parsed-verdict", "harness-raises"],
)
async def test_qa_outage_is_flagged_and_not_a_pass(monkeypatch, harness) -> None:
    monkeypatch.setattr(execution_agents, "router", _router(harness))

    out = await execution_agents.run_qa(**REVIEWER_KW)

    assert out["qa_unavailable"] is True
    assert out["passed"] is False


async def test_qa_real_verdict_carries_no_outage_flag(monkeypatch) -> None:
    harness = AsyncMock(return_value=_harness_result(QAResult(passed=False, summary="2 fail")))
    monkeypatch.setattr(execution_agents, "router", _router(harness))

    out = await execution_agents.run_qa(**REVIEWER_KW)

    assert out["passed"] is False
    assert "qa_unavailable" not in out


@pytest.mark.parametrize(
    "qa,review,expected",
    [
        ({"passed": True}, {"approved": False, "blocking": False, "review_unavailable": True}, "approve"),
        # Real failing tests or a missing QA verdict are not made acceptable by an outage elsewhere.
        ({"passed": False}, {"approved": False, "blocking": False, "review_unavailable": True}, "fix"),
        (
            {"passed": False, "qa_unavailable": True},
            {"approved": False, "blocking": False, "review_unavailable": True},
            "fix",
        ),
        # An unflagged non-approval is still a fix: the flag is what makes the difference.
        ({"passed": True}, {"approved": False, "blocking": False}, "fix"),
        ({"passed": True}, {"approved": False, "blocking": True, "review_unavailable": True}, "block"),
    ],
)
async def test_synthesizer_fallback_with_unavailable_reviewer(monkeypatch, qa, review, expected) -> None:
    harness = AsyncMock(return_value=_harness_result(None))
    monkeypatch.setattr(execution_agents, "router", _router(harness))

    out = await execution_agents.run_qa_synthesizer(
        qa_result=qa, review_result=review, iteration_history=[],
    )

    assert out["action"] == expected


# ---------------------------------------------------------------------------
# Whole loop, real reasoners, mocked harness
# ---------------------------------------------------------------------------


def _dag_state(artifacts_dir: str) -> DAGState:
    return DAGState(
        repo_path="/tmp/fake-repo",
        artifacts_dir=artifacts_dir,
        prd_path="",
        architecture_path="",
        issues_dir="",
    )


def _issue(**extra) -> dict:
    issue = {
        "name": "ISSUE-1",
        "title": "Test issue",
        "description": "A test issue",
        "acceptance_criteria": ["AC-1"],
        "depends_on": [],
        "provides": [],
        "files_to_create": [],
        "files_to_modify": [],
        "worktree_path": "/tmp/fake-repo",
        "branch_name": "test/issue-1",
        "branch": "test/issue-1",
        "repo_name": "",
    }
    issue.update(extra)
    return issue


def _call_fn(monkeypatch, *, reviewer_script, files=("src/app.py",), qa_passed=True):
    """Dispatch reviewer calls to the REAL reasoner with a scripted harness.

    ``reviewer_script`` is a list indexed by reviewer call: a CodeReviewResult
    (real verdict) or None (the harness produced nothing).
    """
    calls = {"reviewer": 0}

    async def harness(*args, schema, **kwargs):
        if schema is CodeReviewResult:
            verdict = reviewer_script[min(calls["reviewer"], len(reviewer_script) - 1)]
            calls["reviewer"] += 1
            return _harness_result(verdict)
        raise AssertionError(f"unexpected harness schema {schema}")

    monkeypatch.setattr(execution_agents, "router", _router(AsyncMock(side_effect=harness)))

    async def call_fn(agent_name: str, **kwargs):
        if agent_name.endswith(".run_coder"):
            return {"files_changed": list(files), "summary": "done", "complete": True}
        if agent_name.endswith(".run_code_reviewer"):
            return await execution_agents.run_code_reviewer(**kwargs)
        if agent_name.endswith(".run_qa_synthesizer"):
            return await execution_agents.run_qa_synthesizer(**kwargs)
        if agent_name.endswith(".run_qa"):
            return {"passed": qa_passed, "summary": "qa", "test_failures": []}
        raise AssertionError(f"unexpected call {agent_name}")

    return call_fn


async def test_loop_with_unrunnable_reviewer_reasoner_accepts_with_debt(monkeypatch, tmp_path) -> None:
    call_fn = _call_fn(monkeypatch, reviewer_script=[None])

    result = await run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(str(tmp_path)),
        call_fn=call_fn,
        node_id="n",
        config=ExecutionConfig(max_coding_iterations=3, agent_timeout_seconds=30),
    )

    assert result.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    assert result.attempts == 1
    assert [d["type"] for d in result.debt_items] == ["review_unavailable"]


async def test_loop_with_real_approval_is_still_a_clean_completion(monkeypatch, tmp_path) -> None:
    call_fn = _call_fn(
        monkeypatch,
        reviewer_script=[CodeReviewResult(approved=True, blocking=False, summary="LGTM")],
    )

    result = await run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(str(tmp_path)),
        call_fn=call_fn,
        node_id="n",
        config=ExecutionConfig(max_coding_iterations=3, agent_timeout_seconds=30),
    )

    assert result.outcome == IssueOutcome.COMPLETED
    assert result.debt_items == []


async def test_flagged_loop_with_everything_down_but_passing_qa_accepts_with_debt(
    monkeypatch, tmp_path
) -> None:
    """Reviewer reasoner and synthesizer harness both produce nothing; QA passed."""
    call_fn = _call_fn(monkeypatch, reviewer_script=[None], qa_passed=True)

    async def harness(*args, schema, **kwargs):
        return _harness_result(None)  # reviewer AND synthesizer produce nothing

    monkeypatch.setattr(execution_agents, "router", _router(AsyncMock(side_effect=harness)))

    result = await run_coding_loop(
        issue=_issue(guidance={"needs_deeper_qa": True}),
        dag_state=_dag_state(str(tmp_path)),
        call_fn=call_fn,
        node_id="n",
        config=ExecutionConfig(max_coding_iterations=3, agent_timeout_seconds=30),
    )

    assert result.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    assert result.attempts == 1
    assert result.debt_items[0]["type"] == "review_unavailable"


async def test_previous_blocking_finding_survives_a_reviewer_outage(monkeypatch, tmp_path) -> None:
    """Iteration 1 blocks; iteration 2's reviewer produces nothing. The blocker
    must not be erased by the outage, so the issue cannot complete."""
    blocking = CodeReviewResult(approved=False, blocking=True, summary="SQL injection in handler")
    call_fn = _call_fn(monkeypatch, reviewer_script=[blocking, None])

    result = await run_coding_loop(
        issue=_issue(),
        dag_state=_dag_state(str(tmp_path)),
        call_fn=call_fn,
        node_id="n",
        config=ExecutionConfig(max_coding_iterations=2, agent_timeout_seconds=30),
    )

    assert result.outcome == IssueOutcome.FAILED_UNRECOVERABLE
    assert result.debt_items == []


async def test_checkpointed_accept_with_debt_replays_as_debt(monkeypatch, tmp_path) -> None:
    """Resuming after the last iteration's accept-with-debt decision must not
    turn it into a failure or a clean approval."""
    config = ExecutionConfig(max_coding_iterations=1, agent_timeout_seconds=30)
    dag = _dag_state(str(tmp_path))

    first = await run_coding_loop(
        issue=_issue(),
        dag_state=dag,
        call_fn=_call_fn(monkeypatch, reviewer_script=[None]),
        node_id="n",
        config=config,
    )
    assert first.outcome == IssueOutcome.COMPLETED_WITH_DEBT

    async def never_called(agent_name: str, **kwargs):
        raise AssertionError(f"resume must not re-run agents: {agent_name}")

    replay = await run_coding_loop(
        issue=_issue(),
        dag_state=dag,
        call_fn=never_called,
        node_id="n",
        config=config,
    )

    assert replay.outcome == IssueOutcome.COMPLETED_WITH_DEBT
    assert [d["type"] for d in replay.debt_items] == ["review_unavailable"]
    assert replay.files_changed == first.files_changed

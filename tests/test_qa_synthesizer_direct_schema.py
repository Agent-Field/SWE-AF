"""Regression tests for the QA synthesizer dropping the AI's decision (#113).

``run_qa_synthesizer`` originally called ``router.ai()``, which returns the
validated schema instance directly; reading ``.parsed`` off it raised, the broad
``except`` swallowed that, and the crude tests_passed/review_approved heuristic
replaced the synthesizer's decision on *every* call.

It now goes through ``router.harness()`` like every other agent (so it shares
the runtime's credentials, e.g. a Claude subscription login), which returns a
``HarnessResult`` wrapper carrying ``.parsed``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from swe_af.execution.schemas import QASynthesisAction, QASynthesisResult
from swe_af.reasoners import execution_agents

# Inputs the heuristic fallback resolves to APPROVE, so any other action in the
# result can only have come from the synthesizer itself.
FALLBACK_APPROVES = {
    "qa_result": {"passed": True},
    "review_result": {"approved": True, "blocking": False},
}


def _harness_result(parsed) -> SimpleNamespace:
    return SimpleNamespace(parsed=parsed, is_error=False, error_message=None, result="")


def _error_notes(router: MagicMock) -> list:
    return [
        c for c in router.note.call_args_list if "error" in c.kwargs.get("tags", [])
    ]


async def test_ai_decision_wins_over_heuristic_fallback(monkeypatch) -> None:
    """A BLOCK from the synthesizer survives inputs the fallback would approve."""
    decision = QASynthesisResult(
        action=QASynthesisAction.BLOCK,
        summary="Tests pass but the fix regresses the public API.",
        stuck=True,
    )
    router = MagicMock(harness=AsyncMock(return_value=_harness_result(decision)))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-7", **FALLBACK_APPROVES
    )

    assert router.harness.await_args.kwargs["schema"] is QASynthesisResult
    assert out["action"] == QASynthesisAction.BLOCK
    assert out["summary"] == decision.summary
    assert out["stuck"] is True
    assert out["iteration_id"] == "iter-7"
    assert _error_notes(router) == []


async def test_non_schema_response_falls_back_and_says_so(monkeypatch) -> None:
    """An unparseable response reaches the heuristic, but not silently.

    Issue #113 was diagnosed from the note this path emits, so the fallback
    must stay loud.
    """
    router = MagicMock(harness=AsyncMock(return_value=_harness_result(None)))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-8", **FALLBACK_APPROVES
    )

    assert out["action"] == QASynthesisAction.APPROVE
    assert _error_notes(router), "operators lose their only signal for this path"

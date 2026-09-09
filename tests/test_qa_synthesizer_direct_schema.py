"""Regression tests for the QA synthesizer dropping the AI's decision (#113).

``run_qa_synthesizer`` is the only reasoner that calls ``router.ai()`` — every
other agent in ``execution_agents`` goes through ``router.harness()``, which
returns a ``HarnessResult`` wrapper carrying a ``.parsed`` attribute.
``router.ai(..., schema=X)`` instead returns the validated ``X`` instance
directly, so reading ``.parsed`` off it raised ``AttributeError``.  The
surrounding broad ``except Exception`` swallowed that, logged "QA synthesizer
agent failed" and fell through to the crude tests_passed/review_approved
heuristic — discarding the synthesizer's decision on *every* call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from swe_af.execution.schemas import QASynthesisAction, QASynthesisResult
from swe_af.reasoners import execution_agents

# Inputs the heuristic fallback resolves to APPROVE, so any other action in the
# result can only have come from the synthesizer itself.
FALLBACK_APPROVES = {
    "qa_result": {"passed": True},
    "review_result": {"approved": True, "blocking": False},
}


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
    router = MagicMock(ai=AsyncMock(return_value=decision))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-7", **FALLBACK_APPROVES
    )

    assert router.ai.await_args.kwargs["schema"] is QASynthesisResult
    assert out["action"] == QASynthesisAction.BLOCK
    assert out["summary"] == decision.summary
    assert out["stuck"] is True
    assert out["iteration_id"] == "iter-7"
    assert _error_notes(router) == []


async def test_non_schema_response_falls_back_and_says_so(monkeypatch) -> None:
    """An unparseable response reaches the heuristic, but not silently.

    ``ai()`` hands back a ``ToolCallResponse`` when the model's content is not
    parseable JSON.  Issue #113 was diagnosed from the note this path emits, so
    the fallback must stay loud.
    """
    router = MagicMock(ai=AsyncMock(return_value=object()))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-8", **FALLBACK_APPROVES
    )

    assert out["action"] == QASynthesisAction.APPROVE
    assert _error_notes(router), "operators lose their only signal for this path"

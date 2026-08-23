"""Regression test for the QA synthesizer dropping the AI's decision (#113).

``run_qa_synthesizer`` is the only reasoner that calls ``router.ai()`` — every
other agent in ``execution_agents`` goes through ``router.harness()``, which
returns a ``HarnessResult`` wrapper carrying a ``.parsed`` attribute.
``router.ai(..., schema=X)`` instead returns the validated ``X`` instance
directly, so reading ``.parsed`` off it raised ``AttributeError``.  The
surrounding broad ``except Exception`` swallowed that, logged "QA synthesizer
agent failed" and fell through to the crude tests_passed/review_approved
heuristic — discarding the synthesizer's decision on *every* call.

Contract: whatever ``router.ai()`` returns for the schema is the decision that
comes back, even when the heuristic fallback would have decided otherwise.
"""

from __future__ import annotations

from typing import Any

from swe_af.execution.schemas import QASynthesisAction, QASynthesisResult
from swe_af.reasoners import execution_agents


class _StubRouter:
    """Stands in for the module-level router, returning *decision* from ai()."""

    def __init__(self, decision: QASynthesisResult) -> None:
        self._decision = decision

    async def ai(self, *args: Any, **kwargs: Any) -> QASynthesisResult:
        return self._decision

    def note(self, *args: Any, **kwargs: Any) -> None:
        pass


async def test_ai_decision_wins_over_heuristic_fallback(monkeypatch) -> None:
    """A BLOCK from the synthesizer survives inputs the fallback would approve."""
    decision = QASynthesisResult(
        action=QASynthesisAction.BLOCK,
        summary="Tests pass but the fix regresses the public API.",
        stuck=True,
    )
    monkeypatch.setattr(execution_agents, "router", _StubRouter(decision))

    out = await execution_agents.run_qa_synthesizer(
        qa_result={"passed": True},
        review_result={"approved": True, "blocking": False},
        iteration_history=[],
        iteration_id="iter-7",
    )

    assert out["action"] == QASynthesisAction.BLOCK
    assert out["summary"] == decision.summary
    assert out["stuck"] is True
    assert out["iteration_id"] == "iter-7"

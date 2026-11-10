"""Fan-out bounds for issue-writer agents.

Issue writers are full harness runs (one provider CLI subprocess each), so the
number started at once is a hard memory bound. These tests cover the shared
``gather_bounded`` helper and both call sites that fan writers out:
``plan()``'s Phase 4b and the replanner's post-replan writer pass.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from swe_af.execution.concurrency import gather_bounded
from swe_af.execution.dag_executor import _write_issue_files_for_replan
from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    ReplanAction,
    ReplanDecision,
)

# ---------------------------------------------------------------------------
# gather_bounded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gather_bounded_caps_concurrency():
    state = {"current": 0, "peak": 0}

    async def work() -> int:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        try:
            await asyncio.sleep(0.01)
            return state["current"]
        finally:
            state["current"] -= 1

    await gather_bounded([work() for _ in range(12)], limit=3)

    assert state["peak"] <= 3
    assert state["peak"] >= 2, "bounded gather must still run work in parallel"


@pytest.mark.asyncio
async def test_gather_bounded_preserves_order_and_exceptions():
    async def ok(value: int) -> int:
        await asyncio.sleep(0.01 if value % 2 else 0)
        return value

    async def boom() -> int:
        raise RuntimeError("writer exploded")

    results = await gather_bounded([ok(1), boom(), ok(3)], limit=2)

    assert results[0] == 1
    assert isinstance(results[1], RuntimeError)
    assert results[2] == 3


@pytest.mark.asyncio
async def test_gather_bounded_unlimited_when_limit_is_zero():
    state = {"current": 0, "peak": 0}

    async def work() -> None:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1

    await gather_bounded([work() for _ in range(5)], limit=0)

    assert state["peak"] == 5


# ---------------------------------------------------------------------------
# Replanner writer pass
# ---------------------------------------------------------------------------

def _dag_state(tmp_path: str) -> DAGState:
    return DAGState(
        repo_path=tmp_path,
        artifacts_dir=tmp_path,
        prd_summary="PRD",
        architecture_summary="ARCH",
        issues_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_replan_writer_pass_respects_configured_cap(tmp_path):
    state = {"current": 0, "peak": 0}

    async def call_fn(agent_name: str, **_: Any) -> dict:
        assert agent_name.endswith(".run_issue_writer")
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        try:
            await asyncio.sleep(0.01)
            return {"success": True}
        finally:
            state["current"] -= 1

    decision = ReplanDecision(
        action=ReplanAction.MODIFY_DAG,
        rationale="split a failed issue",
        new_issues=[{"name": f"new-{i}", "title": "t"} for i in range(9)],
    )

    await _write_issue_files_for_replan(
        decision,
        _dag_state(str(tmp_path)),
        ExecutionConfig(max_concurrent_issue_writers=2),
        call_fn,
        "swe-planner",
    )

    assert state["peak"] == 2


# ---------------------------------------------------------------------------
# plan() Phase 4b
# ---------------------------------------------------------------------------

def _prd() -> dict[str, Any]:
    return {
        "validated_description": "A test goal.",
        "acceptance_criteria": ["AC-1: does something"],
        "must_have": ["feature-x"],
        "nice_to_have": [],
        "out_of_scope": [],
        "assumptions": [],
        "risks": [],
    }


def _arch() -> dict[str, Any]:
    return {
        "summary": "Simple architecture.",
        "components": [
            {
                "name": "component-a",
                "responsibility": "Does A",
                "touches_files": ["a.py"],
                "depends_on": [],
            }
        ],
        "interfaces": ["interface-1"],
        "decisions": [{"decision": "Use Python", "rationale": "Available."}],
        "file_changes_overview": "Only a.py is changed.",
    }


def _review() -> dict[str, Any]:
    return {
        "approved": True,
        "feedback": "Looks good.",
        "scope_issues": [],
        "complexity_assessment": "appropriate",
        "summary": "Architecture approved.",
    }


def _sprint(issue_count: int) -> dict[str, Any]:
    return {
        "issues": [
            {
                "name": f"issue-{i}",
                "title": f"Issue {i}",
                "description": "Do the thing.",
                "acceptance_criteria": ["AC-1"],
                "depends_on": [],
                "provides": [f"thing-{i}"],
                "estimated_complexity": "small",
                "files_to_create": [f"thing_{i}.py"],
                "files_to_modify": [],
                "testing_strategy": "pytest",
                "sequence_number": None,
                "guidance": None,
            }
            for i in range(issue_count)
        ],
        "rationale": "Rationale.",
    }


async def _invoke_plan(tmp_path: str, **kwargs: Any) -> dict:
    import swe_af.app as app_module

    real_fn = getattr(app_module.plan, "_original_func", app_module.plan)
    defaults = {
        "goal": "Build a test app",
        "repo_path": tmp_path,
        "artifacts_dir": ".artifacts",
        "pm_model": "sonnet",
        "architect_model": "sonnet",
        "tech_lead_model": "sonnet",
        "sprint_planner_model": "sonnet",
        "issue_writer_model": "sonnet",
        "ai_provider": "claude",
    }
    defaults.update(kwargs)
    return await real_fn(**defaults)


@pytest.mark.asyncio
async def test_phase_4b_bounds_issue_writer_fan_out(tmp_path):
    """A wide sprint must not start every writer agent at once."""
    state = {"current": 0, "peak": 0}
    sprint = _sprint(12)

    async def fake_call(agent_name: str, **_: Any) -> dict:
        if agent_name.endswith(".run_product_manager"):
            return _prd()
        if agent_name.endswith(".run_architect"):
            return _arch()
        if agent_name.endswith(".run_tech_lead"):
            return _review()
        if agent_name.endswith(".run_sprint_planner"):
            return sprint
        if agent_name.endswith(".run_issue_writer"):
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            try:
                await asyncio.sleep(0.01)
                return {"success": True, "issue_file_path": "x.md"}
            finally:
                state["current"] -= 1
        raise AssertionError(f"unexpected agent call: {agent_name}")

    with patch("swe_af.app.app.call", new=fake_call):
        await _invoke_plan(str(tmp_path), max_concurrent_issue_writers=3)

    assert state["peak"] == 3

"""Error-path tests for malformed AI responses in fast planner and executor.

Covers:
- AC-10: Both test names are discoverable via --collect-only
- AC-11: test_fast_execute_tasks_unwrap_key_error_produces_failed_outcome passes
- AC-16: No real API calls in any test
- AC-17: test_fast_plan_tasks_missing_tasks_field_triggers_fallback passes
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure AGENTFIELD_SERVER is set to a safe local address if not already provided.
# The agentfield_server_guard fixture in conftest.py enforces this at session scope.
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously in tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC-17 / AC-10: Planner fallback on missing 'tasks' field
# ---------------------------------------------------------------------------


def test_fast_plan_tasks_missing_tasks_field_triggers_fallback() -> None:
    """When parsed response has no 'tasks' key (parsed=None), fallback_used=True.

    Satisfies AC-17 and AC-10.

    Patches AgentAI in swe_af.fast.planner to return a response whose .parsed
    attribute is None (simulating an AI response that cannot be structured into
    a FastPlanResult). Asserts that the planner falls back to a single-task plan
    with fallback_used=True.
    """
    from swe_af.fast.planner import fast_plan_tasks

    # Simulate a malformed AI response: .parsed is None (no structured output)
    mock_response = MagicMock()
    mock_response.parsed = None

    with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
        instance = MagicMock()
        instance.run = AsyncMock(return_value=mock_response)
        MockAgentAI.return_value = instance

        result = _run(
            fast_plan_tasks(
                goal="Build a feature",
                repo_path="/tmp/repo",
            )
        )

    assert isinstance(result, dict), "Result must be a dict"
    assert result.get("fallback_used") is True, (
        f"Expected fallback_used=True when parsed=None, got: {result}"
    )
    tasks = result.get("tasks", [])
    assert len(tasks) >= 1, "Fallback must contain at least one task"
    task_names = [t["name"] for t in tasks]
    assert "implement-goal" in task_names, (
        f"Expected 'implement-goal' in fallback tasks; got {task_names}"
    )


def test_fast_plan_tasks_exception_in_run_triggers_fallback() -> None:
    """When AgentAI.run raises an exception, the planner falls back gracefully."""
    from swe_af.fast.planner import fast_plan_tasks

    with patch("swe_af.fast.planner.AgentAI") as MockAgentAI:
        instance = MagicMock()
        instance.run = AsyncMock(
            side_effect=ValueError("Response missing required 'tasks' field")
        )
        MockAgentAI.return_value = instance

        result = _run(
            fast_plan_tasks(
                goal="Build a REST API",
                repo_path="/tmp/repo",
            )
        )

    assert result.get("fallback_used") is True
    assert len(result.get("tasks", [])) >= 1


# ---------------------------------------------------------------------------
# AC-11 / AC-10: Executor produces failed outcome on unwrap KeyError
# ---------------------------------------------------------------------------


class _KeyErrorEnvelope(dict):
    """A dict with envelope keys that raises KeyError when _unwrap reads 'status'.

    swe_af.execution.envelope.unwrap_call_result checks:
      1. isinstance(result, dict)              → True  (it IS a dict)
      2. _ENVELOPE_KEYS.intersection(result)   → non-empty (has 'execution_id')
      3. result.get("status", "")              → raises KeyError here

    This exercises the _unwrap code path and triggers the except-Exception
    block in fast_execute_tasks, producing outcome='failed'.
    """

    def get(self, key, default=None):
        if key == "status":
            raise KeyError(key)
        return super().get(key, default)


def test_fast_execute_tasks_unwrap_key_error_produces_failed_outcome() -> None:
    """When app.call returns a malformed envelope causing _unwrap to raise KeyError,
    the executor catches it and returns outcome='failed' for that task.

    Satisfies AC-11 and AC-10.

    The envelope dict contains the 'execution_id' key (recognised as an
    AgentField execution envelope by unwrap_call_result), but its .get()
    raises KeyError for 'status', causing _unwrap to raise inside the
    except-Exception handler, which records outcome='failed'.
    """
    from swe_af.fast.executor import fast_execute_tasks

    tasks = [
        {
            "name": "task-malformed",
            "title": "Malformed task",
            "description": "A task whose executor response is malformed.",
            "acceptance_criteria": ["It completes."],
            "files_to_create": [],
            "files_to_modify": [],
        }
    ]

    # Return a dict subclass that passes the isinstance(result, dict) check and
    # has an envelope key, but raises KeyError when _unwrap reads result.get("status").
    malformed_envelope = _KeyErrorEnvelope({"execution_id": "fake-id"})

    with patch("swe_af.fast.app.app.call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = malformed_envelope

        result = _run(
            fast_execute_tasks(
                tasks=tasks,
                repo_path="/tmp/repo",
            )
        )

    assert isinstance(result, dict), "Result must be a dict"
    task_results = result.get("task_results", [])
    assert len(task_results) == 1, (
        f"Expected 1 task result, got {len(task_results)}"
    )
    task_result = task_results[0]
    assert task_result["outcome"] == "failed", (
        f"Expected outcome='failed' on KeyError from _unwrap, "
        f"got outcome={task_result['outcome']!r}"
    )


def test_fast_execute_tasks_malformed_envelope_no_result_key_produces_failed() -> None:
    """Envelope with status='success' but inner result=None falls back to failed outcome.

    When the unwrapped result dict is the envelope itself (result=None path in
    _unwrap), and the executor cannot extract a 'complete' flag, the task is
    recorded as outcome='failed'.
    """
    from swe_af.fast.executor import fast_execute_tasks

    tasks = [
        {
            "name": "task-bad-envelope",
            "title": "Bad envelope task",
            "description": "Executor receives a bad envelope.",
            "acceptance_criteria": [],
            "files_to_create": [],
            "files_to_modify": [],
        }
    ]

    # Envelope with recognised keys, status='success', but result=None.
    # _unwrap returns the envelope dict itself (no 'complete' key → outcome='failed').
    good_envelope_no_result = {
        "execution_id": "fake-id",
        "status": "success",
        "result": None,
    }

    with patch("swe_af.fast.app.app.call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = good_envelope_no_result

        result = _run(
            fast_execute_tasks(
                tasks=tasks,
                repo_path="/tmp/repo",
            )
        )

    task_results = result.get("task_results", [])
    assert len(task_results) == 1
    # 'complete' not in envelope → .get("complete", False) == False → outcome='failed'
    assert task_results[0]["outcome"] == "failed"

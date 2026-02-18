"""Tests for swe_af.fast.executor — fast_execute_tasks reasoner.

Covers:
- Module imports without error (AC-1)
- Source contains 'task_timeout_seconds' and 'wait_for' (AC-11)
- Forbidden identifiers not in source (AC-12)
- fast_execute_tasks is registered on fast_router
- Successful task produces outcome='completed'
- asyncio.TimeoutError produces outcome='timeout' and execution continues
- Generic exception produces outcome='failed' and execution continues
- completed_count and failed_count are accurate
- Empty tasks list returns FastExecutionResult with completed_count=0
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agentfield import AgentRouter


@contextlib.contextmanager
def _patch_router_note():
    """Patch fast_router.note by injecting a no-op into the instance __dict__."""
    import swe_af.fast.executor as _exe  # noqa: PLC0415
    router = _exe.fast_router
    _sentinel = object()
    old_note = router.__dict__.get("note", _sentinel)
    router.__dict__["note"] = MagicMock(return_value=None)
    try:
        yield router.__dict__["note"]
    finally:
        if old_note is _sentinel:
            router.__dict__.pop("note", None)
        else:
            router.__dict__["note"] = old_note

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORBIDDEN_IDENTIFIERS = {
    "run_qa",
    "run_code_reviewer",
    "run_qa_synthesizer",
    "run_replanner",
    "run_issue_advisor",
    "run_retry_advisor",
}

_SAMPLE_TASK = {
    "name": "sample-task",
    "title": "Sample Task",
    "description": "Do something useful.",
    "acceptance_criteria": ["Thing works"],
    "files_to_create": [],
    "files_to_modify": [],
}


def _registered_names(router: AgentRouter) -> set[str]:
    """Return the set of function names registered on *router*."""
    return {r["func"].__name__ for r in router.reasoners}


# ---------------------------------------------------------------------------
# Unit tests: module importability and source content (AC-1, AC-11, AC-12)
# ---------------------------------------------------------------------------


class TestModuleImport:
    def test_module_imports_without_error(self) -> None:
        """AC-1: swe_af.fast.executor imports successfully."""
        import swe_af.fast.executor  # noqa: PLC0415

        assert swe_af.fast.executor is not None

    def test_fast_execute_tasks_is_callable(self) -> None:
        """fast_execute_tasks function exists and is callable."""
        from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

        assert callable(fast_execute_tasks)


class TestSourceContent:
    """AC-11: Source must contain 'task_timeout_seconds' and 'wait_for'."""

    def _get_source(self) -> str:
        import swe_af.fast.executor as executor_module  # noqa: PLC0415
        return inspect.getsource(executor_module)

    def test_source_contains_task_timeout_seconds(self) -> None:
        assert "task_timeout_seconds" in self._get_source()

    def test_source_contains_wait_for(self) -> None:
        assert "wait_for" in self._get_source()


class TestForbiddenIdentifiers:
    """AC-12: Forbidden planning agent identifiers must NOT be in source."""

    def _get_source(self) -> str:
        import swe_af.fast.executor as executor_module  # noqa: PLC0415
        return inspect.getsource(executor_module)

    def test_run_qa_not_in_source(self) -> None:
        assert "run_qa" not in self._get_source()

    def test_run_code_reviewer_not_in_source(self) -> None:
        assert "run_code_reviewer" not in self._get_source()

    def test_run_qa_synthesizer_not_in_source(self) -> None:
        assert "run_qa_synthesizer" not in self._get_source()

    def test_run_replanner_not_in_source(self) -> None:
        assert "run_replanner" not in self._get_source()

    def test_run_issue_advisor_not_in_source(self) -> None:
        assert "run_issue_advisor" not in self._get_source()

    def test_run_retry_advisor_not_in_source(self) -> None:
        assert "run_retry_advisor" not in self._get_source()


# ---------------------------------------------------------------------------
# Registration test: fast_execute_tasks is on fast_router
# ---------------------------------------------------------------------------


class TestReasonerRegistration:
    def test_fast_execute_tasks_registered_on_fast_router(self) -> None:
        """fast_execute_tasks is registered as a reasoner on fast_router."""
        from swe_af.fast import fast_router  # noqa: PLC0415

        assert "fast_execute_tasks" in _registered_names(fast_router)


# ---------------------------------------------------------------------------
# Functional tests: mock app.call inside function body
# ---------------------------------------------------------------------------


class TestFastExecuteTasksFunctional:
    """Functional tests using mocked app.call."""

    def _make_app_module_mock(self, call_return: object) -> MagicMock:
        """Create a mock _app_module with app.call returning call_return."""
        mock_app_module = MagicMock()
        mock_app_module.app.call = AsyncMock(return_value=call_return)
        return mock_app_module

    @pytest.mark.asyncio
    async def test_successful_task_produces_completed_outcome(self) -> None:
        """Successful task call produces outcome='completed' in task_results."""
        # _unwrap returns a dict with complete=True
        coder_result = {"complete": True, "files_changed": ["foo.py"], "summary": "Done"}
        # app.call returns a raw envelope; _unwrap will parse it — we mock _unwrap too
        raw_response = {"result": coder_result}

        mock_app = self._make_app_module_mock(raw_response)

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[_SAMPLE_TASK],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["task_results"][0]["outcome"] == "completed"
        assert result["task_results"][0]["task_name"] == "sample-task"

    @pytest.mark.asyncio
    async def test_timeout_error_produces_timeout_outcome_and_continues(self) -> None:
        """asyncio.TimeoutError on a task produces outcome='timeout' and execution continues."""
        # First task: timeout; second task: success
        coder_result = {"complete": True, "files_changed": [], "summary": "done"}

        call_side_effects: list = [
            asyncio.TimeoutError(),  # first task times out via wait_for
            {"result": coder_result},  # second task succeeds
        ]

        mock_app = MagicMock()

        async def _call_side_effect(*args, **kwargs):
            effect = call_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect

        mock_app.app.call = _call_side_effect

        second_task = {
            "name": "second-task",
            "title": "Second Task",
            "description": "Do something else.",
            "acceptance_criteria": ["Other thing works"],
        }

        # wait_for passes through; the TimeoutError comes from app.call itself
        async def _passthrough_wait_for(coro, timeout):
            return await coro

        with (
            patch("asyncio.wait_for", side_effect=_passthrough_wait_for),
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[_SAMPLE_TASK, second_task],
                repo_path="/tmp/repo",
                task_timeout_seconds=1,
            )

        assert len(result["task_results"]) == 2
        assert result["task_results"][0]["outcome"] == "timeout"
        assert result["task_results"][1]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_asyncio_timeout_via_wait_for_mock(self) -> None:
        """asyncio.TimeoutError raised by wait_for produces outcome='timeout'."""
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={})

        async def _timeout_wait_for(coro, timeout):
            raise asyncio.TimeoutError()

        with (
            patch("asyncio.wait_for", side_effect=_timeout_wait_for),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[_SAMPLE_TASK],
                repo_path="/tmp/repo",
                task_timeout_seconds=1,
            )

        assert result["task_results"][0]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_generic_exception_produces_failed_outcome_and_continues(self) -> None:
        """Generic exception on a task produces outcome='failed' and execution continues."""
        coder_result = {"complete": True, "files_changed": [], "summary": "done"}

        mock_app = MagicMock()
        call_calls = 0

        async def _call_side_effect(*args, **kwargs):
            nonlocal call_calls
            call_calls += 1
            if call_calls == 1:
                raise RuntimeError("Some unexpected error")
            return {"result": coder_result}

        mock_app.app.call = _call_side_effect

        second_task = {
            "name": "second-task",
            "title": "Second Task",
            "description": "Do something else.",
            "acceptance_criteria": ["Other thing works"],
        }

        async def _passthrough_wait_for(coro, timeout):
            return await coro

        with (
            patch("asyncio.wait_for", side_effect=_passthrough_wait_for),
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[_SAMPLE_TASK, second_task],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert len(result["task_results"]) == 2
        assert result["task_results"][0]["outcome"] == "failed"
        assert "Some unexpected error" in result["task_results"][0]["error"]
        assert result["task_results"][1]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_completed_count_and_failed_count_accurate(self) -> None:
        """completed_count and failed_count are accurate in FastExecutionResult."""
        # 2 tasks: one completes, one fails (complete=False → outcome='failed')
        coder_success = {"complete": True, "files_changed": [], "summary": "done"}
        coder_failure = {"complete": False, "files_changed": [], "summary": "partial"}

        call_calls = 0

        mock_app = MagicMock()

        async def _call_side_effect(*args, **kwargs):
            nonlocal call_calls
            call_calls += 1
            if call_calls == 1:
                return {"result": coder_success}
            return {"result": coder_failure}

        mock_app.app.call = _call_side_effect

        second_task = {
            "name": "second-task",
            "title": "Second Task",
            "description": "Failing task",
            "acceptance_criteria": ["Should fail"],
        }

        unwrap_calls = 0

        def _unwrap_side_effect(raw, name):
            nonlocal unwrap_calls
            unwrap_calls += 1
            if unwrap_calls == 1:
                return coder_success
            return coder_failure

        async def _passthrough_wait_for(coro, timeout):
            return await coro

        with (
            patch("asyncio.wait_for", side_effect=_passthrough_wait_for),
            patch("swe_af.fast.executor._unwrap", side_effect=_unwrap_side_effect),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[_SAMPLE_TASK, second_task],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["completed_count"] == 1
        assert result["failed_count"] == 1
        assert len(result["task_results"]) == 2

    @pytest.mark.asyncio
    async def test_empty_tasks_list_returns_completed_count_zero(self) -> None:
        """Edge case: empty tasks list returns FastExecutionResult with completed_count=0."""
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={})

        with (
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=[],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["completed_count"] == 0
        assert result["failed_count"] == 0
        assert result["task_results"] == []

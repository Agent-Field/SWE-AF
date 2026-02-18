"""Tests for swe_af.fast.verifier — fast_verify reasoner.

Covers:
- Module imports without error (AC importability)
- Forbidden identifiers not in source (AC-14): generate_fix_issues,
  max_verify_fix_cycles, fix_cycles
- fast_verify registered on fast_router
- fast_verify signature includes required parameters
- Successful agent call returns FastVerificationResult with passed=True
- Agent exception returns FastVerificationResult(passed=False) with
  'Verification agent failed' in summary
- Edge case: empty task_results
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentfield import AgentRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registered_names(router: AgentRouter) -> set[str]:
    """Return the set of function names registered on *router*."""
    return {r["func"].__name__ for r in router.reasoners}


_CALL_KWARGS: dict[str, Any] = {
    "prd": "Build a CLI tool",
    "repo_path": "/tmp/repo",
    "task_results": [{"task_name": "init", "outcome": "completed"}],
    "verifier_model": "haiku",
    "permission_mode": "default",
    "ai_provider": "anthropic",
    "artifacts_dir": "/tmp/artifacts",
}


def _run(coro: Any) -> Any:
    """Run an async coroutine in a synchronous test context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# AC: module imports without error
# ---------------------------------------------------------------------------

class TestModuleImport:
    def test_verifier_module_imports(self) -> None:
        """swe_af.fast.verifier must import without raising."""
        import swe_af.fast.verifier  # noqa: F401, PLC0415

    def test_fast_verify_is_callable(self) -> None:
        """fast_verify must be importable and callable."""
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        assert callable(fast_verify)


# ---------------------------------------------------------------------------
# AC-14: forbidden identifiers not in source
# ---------------------------------------------------------------------------

class TestForbiddenIdentifiers:
    """AC-14: fix-cycle logic must NOT appear in verifier source."""

    _FORBIDDEN = [
        "generate_fix_issues",
        "max_verify_fix_cycles",
        "fix_cycles",
    ]

    def _source(self) -> str:
        import swe_af.fast.verifier as mod  # noqa: PLC0415

        return inspect.getsource(mod)

    @pytest.mark.parametrize("identifier", _FORBIDDEN)
    def test_forbidden_identifier_absent(self, identifier: str) -> None:
        source = self._source()
        assert identifier not in source, (
            f"Forbidden identifier '{identifier}' found in verifier source "
            f"(AC-14 violation)"
        )


# ---------------------------------------------------------------------------
# AC: fast_verify registered on fast_router
# ---------------------------------------------------------------------------

class TestFastVerifyRegistration:
    def test_fast_verify_registered_on_fast_router(self) -> None:
        """fast_verify must be registered as a reasoner on fast_router."""
        import swe_af.fast.verifier  # noqa: F401, PLC0415 — triggers registration
        from swe_af.fast import fast_router  # noqa: PLC0415

        names = _registered_names(fast_router)
        assert "fast_verify" in names, (
            f"'fast_verify' not found in registered reasoners: {names}"
        )


# ---------------------------------------------------------------------------
# AC: function signature
# ---------------------------------------------------------------------------

class TestFastVerifySignature:
    _REQUIRED_PARAMS = {
        "prd",
        "repo_path",
        "task_results",
        "verifier_model",
        "permission_mode",
        "ai_provider",
        "artifacts_dir",
    }

    def test_signature_contains_required_params(self) -> None:
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        sig = inspect.signature(fast_verify)
        params = set(sig.parameters.keys())
        missing = self._required_params - params
        assert not missing, f"Missing parameters in fast_verify signature: {missing}"

    @pytest.mark.parametrize("param", sorted(_REQUIRED_PARAMS))
    def test_each_required_param(self, param: str) -> None:
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        sig = inspect.signature(fast_verify)
        assert param in sig.parameters, (
            f"Required parameter '{param}' missing from fast_verify signature"
        )

    @property
    def _required_params(self) -> set[str]:
        return self._REQUIRED_PARAMS


# ---------------------------------------------------------------------------
# Functional tests — mock app.call
# ---------------------------------------------------------------------------

class TestFastVerifySuccess:
    """Functional: successful agent call produces FastVerificationResult."""

    def test_successful_call_returns_passed_true(self) -> None:
        """When app.call returns a successful result, passed=True is propagated."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(return_value={
            "passed": True,
            "summary": "All checks passed",
            "criteria_results": [{"criterion": "Tests pass", "passed": True}],
            "suggested_fixes": [],
        })

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**_CALL_KWARGS))

        assert result["passed"] is True
        assert result["summary"] == "All checks passed"
        assert result["criteria_results"] == [{"criterion": "Tests pass", "passed": True}]
        assert result["suggested_fixes"] == []

    def test_successful_call_returns_fast_verification_result_keys(self) -> None:
        """Result dict must have all FastVerificationResult fields."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(return_value={
            "passed": True,
            "summary": "Verification complete",
            "criteria_results": [],
            "suggested_fixes": ["Consider adding more tests"],
        })

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**_CALL_KWARGS))

        assert set(result.keys()) == {"passed", "summary", "criteria_results", "suggested_fixes"}


class TestFastVerifyFailure:
    """Functional: agent exception produces safe fallback FastVerificationResult."""

    def test_exception_returns_passed_false(self) -> None:
        """When app.call raises, result must have passed=False."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(side_effect=RuntimeError("Agent timed out"))

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**_CALL_KWARGS))

        assert result["passed"] is False

    def test_exception_summary_contains_verification_agent_failed(self) -> None:
        """Summary must contain 'Verification agent failed' on exception."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(side_effect=ValueError("Connection refused"))

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**_CALL_KWARGS))

        assert "Verification agent failed" in result["summary"]
        assert "Connection refused" in result["summary"]

    def test_exception_result_has_empty_criteria_and_fixes(self) -> None:
        """Fallback result must have empty criteria_results and suggested_fixes."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(side_effect=Exception("Unknown error"))

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**_CALL_KWARGS))

        assert result["criteria_results"] == []
        assert result["suggested_fixes"] == []


# ---------------------------------------------------------------------------
# Edge case: empty task_results
# ---------------------------------------------------------------------------

class TestFastVerifyEdgeCases:
    def test_empty_task_results_success(self) -> None:
        """empty task_results is a valid call; should propagate agent result."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(return_value={
            "passed": True,
            "summary": "Nothing to verify",
            "criteria_results": [],
            "suggested_fixes": [],
        })

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        kwargs = {**_CALL_KWARGS, "task_results": []}
        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**kwargs))

        assert result["passed"] is True
        # Verify app.call was invoked with empty task_results
        mock_app.call.assert_called_once()
        call_kwargs = mock_app.call.call_args.kwargs
        assert call_kwargs["task_results"] == []

    def test_empty_task_results_exception_fallback(self) -> None:
        """empty task_results + exception still returns safe fallback."""
        mock_app = MagicMock()
        mock_app.call = AsyncMock(side_effect=RuntimeError("empty tasks"))

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        kwargs = {**_CALL_KWARGS, "task_results": []}
        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app_module}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(**kwargs))

        assert result["passed"] is False
        assert "Verification agent failed" in result["summary"]

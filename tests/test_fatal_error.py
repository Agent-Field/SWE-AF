"""Tests for fatal API error detection and propagation.

Validates that non-retryable API errors (credit exhaustion, invalid API key,
disabled accounts) are detected and raised as FatalHarnessError, preventing
silent retries across all retry layers.

Ref: https://github.com/Agent-Field/SWE-AF/issues/49
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from swe_af.execution.fatal_error import (
    FatalHarnessError,
    check_fatal_harness_error,
    is_fatal_error,
)


# ---------------------------------------------------------------------------
# is_fatal_error — pattern matching
# ---------------------------------------------------------------------------


class TestIsFatalError:
    """Test that known fatal error patterns are detected."""

    @pytest.mark.parametrize(
        "message",
        [
            "Credit balance is too low",
            "credit balance is too low to run this request",
            "Your API key is not valid",
            "Invalid API key provided",
            "invalid x-api-key",
            "Authentication failed",
            "authentication failed: check your credentials",
            "Account has been disabled",
            "account is disabled",
            "Unauthorized",
            "unauthorized access",
            "Insufficient credits remaining",
            "insufficient credits",
            "billing expired",
            "billing inactive",
            "billing suspended",
            "Quota exceeded for this model",
            "quota has been exceeded",
        ],
    )
    def test_fatal_patterns_detected(self, message: str) -> None:
        assert is_fatal_error(message), f"Should detect as fatal: {message!r}"

    @pytest.mark.parametrize(
        "message",
        [
            "Rate limit exceeded",
            "Service temporarily unavailable",
            "Internal server error",
            "Connection reset by peer",
            "timeout waiting for response",
            "overloaded — try again later",
            "Product manager failed to produce a valid PRD",
            "",
            "Some random error",
        ],
    )
    def test_transient_errors_not_fatal(self, message: str) -> None:
        assert not is_fatal_error(message), f"Should NOT detect as fatal: {message!r}"

    def test_empty_and_none(self) -> None:
        assert not is_fatal_error("")
        assert not is_fatal_error(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FatalHarnessError
# ---------------------------------------------------------------------------


class TestFatalHarnessError:
    def test_is_runtime_error_subclass(self) -> None:
        err = FatalHarnessError("Credit balance is too low")
        assert isinstance(err, RuntimeError)

    def test_message_includes_non_retryable_prefix(self) -> None:
        err = FatalHarnessError("Credit balance is too low")
        assert "non-retryable" in str(err)
        assert "Credit balance is too low" in str(err)

    def test_original_message_preserved(self) -> None:
        err = FatalHarnessError("some error")
        assert err.original_message == "some error"


# ---------------------------------------------------------------------------
# check_fatal_harness_error — HarnessResult inspection
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    is_error: bool = False
    error_message: Optional[str] = None


class TestCheckFatalHarnessError:
    def test_no_error_passes(self) -> None:
        result = FakeResult(is_error=False, error_message="Credit balance is too low")
        check_fatal_harness_error(result)  # Should not raise

    def test_transient_error_passes(self) -> None:
        result = FakeResult(is_error=True, error_message="Rate limit exceeded")
        check_fatal_harness_error(result)  # Should not raise

    def test_fatal_error_raises(self) -> None:
        result = FakeResult(is_error=True, error_message="Credit balance is too low")
        with pytest.raises(FatalHarnessError, match="Credit balance is too low"):
            check_fatal_harness_error(result)

    def test_fatal_error_invalid_key_raises(self) -> None:
        result = FakeResult(is_error=True, error_message="Invalid API key")
        with pytest.raises(FatalHarnessError):
            check_fatal_harness_error(result)

    def test_none_error_message_passes(self) -> None:
        result = FakeResult(is_error=True, error_message=None)
        check_fatal_harness_error(result)  # Should not raise

    def test_empty_error_message_passes(self) -> None:
        result = FakeResult(is_error=True, error_message="")
        check_fatal_harness_error(result)  # Should not raise


# ---------------------------------------------------------------------------
# envelope.py integration — FatalHarnessError on envelope errors
# ---------------------------------------------------------------------------


class TestEnvelopeFatalError:
    """Verify unwrap_call_result raises FatalHarnessError for fatal envelope errors."""

    def test_envelope_fatal_error_raises(self) -> None:
        envelope = {
            "execution_id": "test-123",
            "status": "failed",
            "error_message": "Credit balance is too low",
            "result": None,
        }
        with pytest.raises(FatalHarnessError, match="Credit balance is too low"):
            from swe_af.execution.envelope import unwrap_call_result
            unwrap_call_result(envelope, label="test")

    def test_envelope_transient_error_raises_runtime(self) -> None:
        envelope = {
            "execution_id": "test-123",
            "status": "failed",
            "error_message": "some transient failure",
            "result": None,
        }
        with pytest.raises(RuntimeError, match="some transient failure"):
            from swe_af.execution.envelope import unwrap_call_result
            unwrap_call_result(envelope, label="test")
        # Ensure it's NOT a FatalHarnessError
        try:
            from swe_af.execution.envelope import unwrap_call_result
            unwrap_call_result(envelope, label="test")
        except FatalHarnessError:
            pytest.fail("Transient error should not raise FatalHarnessError")
        except RuntimeError:
            pass  # Expected

    def test_envelope_success_passes(self) -> None:
        envelope = {
            "execution_id": "test-123",
            "status": "success",
            "result": {"plan": []},
        }
        from swe_af.execution.envelope import unwrap_call_result
        result = unwrap_call_result(envelope, label="test")
        assert result == {"plan": []}

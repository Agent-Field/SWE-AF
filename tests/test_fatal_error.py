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
    EmptyHarnessCompletionError,
    FatalHarnessError,
    HarnessTimeoutError,
    check_empty_harness_completion,
    check_fatal_harness_error,
    is_fatal_error,
    is_timeout_error,
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
            # Codex model/auth mismatches (#82 Gap 3) — non-retryable.
            "The 'gpt-5.3-codex' model is not supported when using Codex with a ChatGPT account.",
            "The 'gpt-5.5' model requires a newer version of Codex. Please upgrade.",
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
    parsed: object | None = None
    result: Optional[str] = None
    text: str = ""
    failure_type: Optional[str] = None


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
# Empty harness completion timeout classification
# ---------------------------------------------------------------------------


class TestCheckEmptyHarnessCompletionTimeout:
    @pytest.mark.parametrize(
        ("failure_type", "detail"),
        [
            ("timeout", "worker was killed"),
            (None, "CLI command timed out after 5400s: opencode run ..."),
            (None, "CLI command made no progress for 300.0s: opencode run ..."),
        ],
    )
    def test_timeout_has_actionable_message(
        self, failure_type: Optional[str], detail: str
    ) -> None:
        """VC1: empty timeout results are reported as timeouts, not auth errors."""
        result = FakeResult(
            is_error=True,
            error_message=detail,
            failure_type=failure_type,
        )

        with pytest.raises(HarnessTimeoutError) as exc_info:
            check_empty_harness_completion(
                result,
                role="Architect",
                provider="opencode",
                model="openrouter/example-model",
            )

        message = str(exc_info.value)
        for expected in (
            "Architect",
            "opencode",
            "openrouter/example-model",
            detail,
            "AGENTFIELD_HARNESS_TIMEOUT_SECONDS",
            "AGENTFIELD_HARNESS_IDLE_SECONDS",
        ):
            assert expected in message
        assert "empty completion" not in message
        assert "check provider auth/model compatibility" not in message
        assert exc_info.value.original_message == detail

    def test_genuine_empty_completion_message_is_unchanged(self) -> None:
        """VC2: genuine empty output retains its exact compatibility message."""
        result = FakeResult(is_error=True, error_message="provider returned nothing")

        with pytest.raises(EmptyHarnessCompletionError) as exc_info:
            check_empty_harness_completion(
                result, role="Architect", provider="opencode", model="model-x"
            )

        assert str(exc_info.value) == (
            "Architect harness returned an empty completion "
            "(provider=opencode, model=model-x) — check provider "
            "auth/model compatibility: provider returned nothing"
        )

    def test_timeout_without_detail_drops_colon_cleanly(self) -> None:
        """VC1: a timeout token alone still produces grammatical guidance."""
        result = FakeResult(is_error=True, failure_type="timeout")

        with pytest.raises(HarnessTimeoutError) as exc_info:
            check_empty_harness_completion(
                result, role="Architect", provider="opencode", model="model-x"
            )

        assert "writing any output. Raise" in str(exc_info.value)
        assert "writing any output:" not in str(exc_info.value)

    @pytest.mark.parametrize(
        "result",
        [
            FakeResult(
                is_error=True,
                error_message="CLI command timed out after 5s",
                result="schema-invalid raw output",
            ),
            FakeResult(
                is_error=True,
                error_message="CLI command timed out after 5s",
                failure_type="schema",
            ),
        ],
    )
    def test_schema_invalid_result_remains_a_noop(self, result: FakeResult) -> None:
        """VC3: raw output and schema failures stay on the caller's schema path."""
        check_empty_harness_completion(
            result, role="Architect", provider="opencode", model="model-x"
        )

    @pytest.mark.parametrize(
        "message",
        [
            "CLI command timed out after 5s",
            "CLI command made no progress for 300.0s",
            "request timed out while waiting for CLI",
            "request timeout exceeded while waiting for CLI",
            "request timeout after 30 seconds",
            "request deadline exceeded",
        ],
    )
    def test_timeout_text_patterns(self, message: str) -> None:
        assert is_timeout_error(message)

    @pytest.mark.parametrize(
        "message",
        ["", "invalid timeout value", "timeout must be a positive integer"],
    )
    def test_non_event_timeout_text_does_not_match(self, message: str) -> None:
        assert not is_timeout_error(message)

    def test_failure_type_remains_primary_for_ambiguous_timeout_text(self) -> None:
        result = FakeResult(
            is_error=True,
            error_message="invalid timeout value",
            failure_type="timeout",
        )

        with pytest.raises(HarnessTimeoutError):
            check_empty_harness_completion(
                result, role="Architect", provider="opencode", model="model-x"
            )

    def test_invalid_timeout_value_without_failure_type_is_empty_completion(self) -> None:
        result = FakeResult(is_error=True, error_message="invalid timeout value")

        with pytest.raises(EmptyHarnessCompletionError):
            check_empty_harness_completion(
                result, role="Architect", provider="opencode", model="model-x"
            )

    def test_empty_timeout_text_does_not_match(self) -> None:
        assert not is_timeout_error("")


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

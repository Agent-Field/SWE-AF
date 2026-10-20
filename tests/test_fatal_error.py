"""Tests for fatal API error detection and propagation.

Validates that non-retryable API errors (credit exhaustion, invalid API key,
disabled accounts) are detected and raised as FatalHarnessError, preventing
silent retries across all retry layers.

Ref: https://github.com/Agent-Field/SWE-AF/issues/49
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, UserMessage
from claude_agent_sdk._internal.message_parser import parse_message

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
            # Claude subscription caps and expired logins — every call fails alike.
            "You've hit your session limit · resets 9:50pm (UTC)",
            "Claude AI usage limit reached",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
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
    messages: list = field(default_factory=list)


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
# Claude subscription caps / expired logins surfaced only as the CLI's reply
# ---------------------------------------------------------------------------

SCHEMA_FAILURE = (
    "Schema validation failed after 2 retry attempt(s). "
    "Last error: The output file was NOT created."
)


class _TextBlock:
    """Mimics claude_agent_sdk.TextBlock as it appears in HarnessResult.messages."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"TextBlock(text={self.text!r})"


class TestClaudeUnavailableReplies:
    @pytest.mark.parametrize(
        "reply",
        [
            "You've hit your session limit · resets 9:50pm (UTC)",
            "You've hit your limit · resets 3pm (UTC)",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        ],
    )
    def test_raw_completion_reply_raises(self, reply: str) -> None:
        result = FakeResult(is_error=True, error_message=SCHEMA_FAILURE, result=reply)
        with pytest.raises(FatalHarnessError, match="Claude unavailable"):
            check_fatal_harness_error(result)

    def test_reply_only_in_session_messages_raises(self) -> None:
        """Planning stages can return no raw text; the reply survives in messages."""
        reply = "You've hit your session limit · resets 1:30pm (UTC)"
        result = FakeResult(
            is_error=True,
            error_message=SCHEMA_FAILURE,
            messages=[
                {"subtype": "init"},
                {"content": [_TextBlock(reply)], "model": "<synthetic>"},
            ],
        )
        with pytest.raises(FatalHarnessError, match="session limit"):
            check_fatal_harness_error(result)

    @pytest.mark.parametrize(
        "prose",
        [
            "Added a banner for users who hit your limit of 5 retries.",
            "Handle the case where merchants fail to authenticate.",
        ],
    )
    def test_model_prose_does_not_raise(self, prose: str) -> None:
        result = FakeResult(
            is_error=True,
            error_message=SCHEMA_FAILURE,
            result=prose,
            messages=[{"content": [_TextBlock(prose)]}],
        )
        check_fatal_harness_error(result)  # Should not raise

    def test_successful_result_is_not_inspected(self) -> None:
        result = FakeResult(
            is_error=False,
            result="You've hit your session limit · resets 9:50pm (UTC)",
        )
        check_fatal_harness_error(result)  # Should not raise


# claude-agent-sdk==0.1.20 bundles CLI 2.1.9. BL9/_FH format these cap
# replies, AG/HQD create model="<synthetic>" assistant text, and Od9 emits
# the error at the top level of stream JSON. The SDK parser reads the error
# from message.error instead, losing it; the synthetic model survives.
CLAUDE_CAP_REPLIES = [
    "You've hit your session limit · resets 9:50pm (UTC)",
    "You've hit your session limit",
    "You've hit your weekly limit",
    "You've hit your Opus limit",
    "You've hit your Sonnet limit",
    "You've hit your usage limit",
    "You've hit your limit",
    "You're out of extra usage · resets 9:50pm (UTC)",
    "You're out of extra usage",
]
CLAUDE_AUTH_REPLIES = [
    "Invalid API key · Please run /login",
    "Invalid API key · Fix external API key",
    "OAuth token revoked · Please run /login",
    "Your account does not have access to Claude Code. Please run /login.",
    'API Error: 401 {"error":{"message":"OAuth token has expired"}} · Please run /login',
]


def _claude_message(reply, *, error="rate_limit", form="harness", model="<synthetic>"):
    message = AssistantMessage(content=[TextBlock(reply)], model=model, error=error)
    wire = {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "text", "text": reply}],
        },
        "error": error,
    }
    return {
        "object": message,
        # AgentField's Claude adapter copies msg.__dict__, retaining block objects.
        "harness": vars(message),
        "serialized": asdict(message),
        "wire": wire,
        "parsed_wire": parse_message(wire),
    }[form]


class TestClaudeProviderReplies:
    @pytest.mark.parametrize("form", ["object", "harness", "serialized", "wire", "parsed_wire"])
    @pytest.mark.parametrize("reply", CLAUDE_CAP_REPLIES)
    def test_messages_only_cap_is_fatal(self, reply, form):
        result = FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE,
            messages=[_claude_message(reply, form=form)],
        )
        with pytest.raises(FatalHarnessError, match="Claude unavailable"):
            check_fatal_harness_error(result)

    @pytest.mark.parametrize("form", ["harness", "wire", "parsed_wire"])
    @pytest.mark.parametrize("reply", CLAUDE_AUTH_REPLIES)
    def test_messages_only_auth_is_fatal(self, reply, form):
        result = FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE,
            messages=[_claude_message(reply, error="authentication_failed", form=form)],
        )
        with pytest.raises(FatalHarnessError, match="Claude unavailable"):
            check_fatal_harness_error(result)

    @pytest.mark.parametrize("reply", CLAUDE_CAP_REPLIES)
    def test_raw_only_cap_is_fatal(self, reply):
        with pytest.raises(FatalHarnessError, match="Claude unavailable"):
            check_fatal_harness_error(FakeResult(is_error=True, result=reply))

    @pytest.mark.parametrize("reply", CLAUDE_AUTH_REPLIES)
    def test_raw_only_auth_is_fatal(self, reply):
        with pytest.raises(FatalHarnessError, match="Claude unavailable"):
            check_fatal_harness_error(FakeResult(is_error=True, result=reply))

    @pytest.mark.parametrize("quote", [
        CLAUDE_CAP_REPLIES[0],
        "Failed to authenticate: OAuth session expired and could not be refreshed",
    ])
    @pytest.mark.parametrize("source", ["user", "tool", "assistant", "tool_input", "metadata"])
    @pytest.mark.parametrize("raw_present", [False, True])
    def test_quoted_examples_remain_recoverable(self, quote, source, raw_present):
        messages = {
            "user": UserMessage(content=[TextBlock(quote)]),
            "tool": UserMessage(content=[ToolResultBlock("read-1", content=quote)]),
            "assistant": AssistantMessage(content=[TextBlock(quote)], model="claude-sonnet-4-5"),
            "tool_input": {
                "type": "assistant", "model": "claude-sonnet-4-5",
                "content": [{"type": "tool_use", "input": {"example": quote}}],
            },
            "metadata": {"subtype": "init", "example": quote},
        }
        message = messages[source]
        check_fatal_harness_error(FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE,
            result=quote if raw_present else None,
            messages=[vars(message) if hasattr(message, "__dict__") else message],
        ))

    @pytest.mark.parametrize("form", ["object", "harness", "serialized", "wire", "parsed_wire"])
    def test_ordinary_model_prose_has_no_provider_error(self, form):
        quote = CLAUDE_CAP_REPLIES[0]
        message = _claude_message(quote, error=None, model="claude-sonnet-4-5", form=form)
        check_fatal_harness_error(FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE, result=quote, messages=[message],
        ))

    @pytest.mark.parametrize("reply", [
        "API Error: Rate limit reached",  # generic 429, not a subscription cap
        "Opus is experiencing high load, please use /model to switch to Sonnet",
        "Request timed out",
        "You're close to your extra usage spending limit",
        "You're now using extra usage",
        "Example: You've hit your session limit · resets 9:50pm (UTC)",
    ])
    def test_recoverable_synthetic_reply(self, reply):
        check_fatal_harness_error(FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE,
            messages=[_claude_message(reply)],
        ))

    def test_successful_result_with_synthetic_reply_is_ignored(self):
        check_fatal_harness_error(FakeResult(
            is_error=False, messages=[_claude_message(CLAUDE_CAP_REPLIES[0])],
        ))

    @pytest.mark.parametrize("block", [
        {"type": "tool_result", "text": CLAUDE_CAP_REPLIES[0]},
        {"type": "tool_use", "input": {"example": CLAUDE_CAP_REPLIES[0]}},
    ])
    def test_nontext_blocks_are_not_provider_replies(self, block):
        check_fatal_harness_error(FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE,
            messages=[{"model": "<synthetic>", "content": [block]}],
        ))

    def test_user_role_cannot_supply_provider_provenance(self):
        message = _claude_message(CLAUDE_CAP_REPLIES[0], form="wire")
        message["type"] = "user"
        check_fatal_harness_error(FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE, messages=[message],
        ))

    @pytest.mark.parametrize("provider_error", [False, True])
    async def test_planning_retries_quotes_but_aborts_provider_caps(
        self, provider_error, tmp_path, monkeypatch,
    ):
        from pydantic import BaseModel

        from swe_af.reasoners import pipeline

        class Output(BaseModel):
            value: str

        reply = CLAUDE_CAP_REPLIES[0]
        message = _claude_message(reply) if provider_error else vars(
            UserMessage(content=[ToolResultBlock("read-1", content=reply)])
        )
        failed = FakeResult(
            is_error=True, error_message=SCHEMA_FAILURE, failure_type="schema",
            messages=[message],
        )
        success = FakeResult(parsed=Output(value="done"))
        invoke = AsyncMock(side_effect=[failed, success])
        monkeypatch.setattr(pipeline, "router", MagicMock())
        call = pipeline._run_planning_call_with_schema_retries(
            invoke, stage="PM", failure_label="No valid output", provider="claude",
            model="sonnet", schema=Output, raw_response_path=str(tmp_path / "raw.txt"),
            max_schema_retries=1,
        )
        if provider_error:
            with pytest.raises(FatalHarnessError):
                await call
            assert invoke.await_count == 1
        else:
            assert await call is success
            assert invoke.await_count == 2


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

"""Independent, offline review probes. No provider or model is contacted."""
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentfield.harness import HarnessRunner, _runner
from agentfield.harness._result import RawResult
from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, UserMessage
from claude_agent_sdk._internal.message_parser import parse_message

from swe_af.execution.envelope import unwrap_call_result
from swe_af.execution.fatal_error import FatalHarnessError, check_fatal_harness_error
from swe_af.execution.schemas import QASynthesisResult

MARKER = "Fatal API error (non-retryable): provider unavailable"


@pytest.mark.parametrize("provider", ["claude-code", "opencode", "codex"])
@pytest.mark.asyncio
async def test_marker_in_real_schema_diagnosis_stays_recoverable(monkeypatch, tmp_path, provider):
    class OfflineProvider:
        async def execute(self, prompt, options):
            output_dir = next(tmp_path.glob(".agentfield-out-*"))
            (output_dir / ".agentfield_output.json").write_text(
                '{"summary": "Example exception: ' + MARKER + '"', encoding="utf-8",
            )
            return RawResult(messages=[vars(UserMessage(content=[
                ToolResultBlock("read-1", content="Example exception: " + MARKER),
            ]))])

    monkeypatch.setattr(_runner, "build_provider", lambda config: OfflineProvider())
    result = await HarnessRunner().run(
        "Summarize the example exception", provider=provider,
        schema=QASynthesisResult, cwd=str(tmp_path), max_retries=0, schema_max_retries=0,
    )
    assert result.is_error and result.failure_type.value == "schema"
    assert MARKER in result.error_message
    assert result.messages[0]["content"][0].tool_use_id == "read-1"
    check_fatal_harness_error(result)


@pytest.mark.parametrize("status", ["failed", "error", "cancelled", "timeout"])
@pytest.mark.parametrize("field", ["error_message", "error"])
def test_marker_quoted_by_unrelated_failed_command_stays_recoverable(status, field):
    with pytest.raises(RuntimeError) as exc:
        unwrap_call_result({
            "status": status,
            field: 'pytest failed: expected exception template "' + MARKER + '"',
        })
    assert not isinstance(exc.value, FatalHarnessError)


@pytest.mark.asyncio
async def test_qa_falls_back_when_schema_diagnosis_quotes_marker(monkeypatch):
    from swe_af.reasoners import execution_agents
    result = SimpleNamespace(
        is_error=True, parsed=None, result=None,
        error_message='Schema validation failed. Invalid output: {"example": "' + MARKER + '"',
        messages=[vars(UserMessage(content=[ToolResultBlock("read-1", content=MARKER)]))],
    )
    router = MagicMock(harness=AsyncMock(return_value=result))
    monkeypatch.setattr(execution_agents, "router", router)
    output = await execution_agents.run_qa_synthesizer(
        qa_result={"passed": True}, review_result={"approved": True},
        iteration_history=[], ai_provider="opencode",
    )
    assert output["action"] == "approve"


RECOVERABLE = [
    "Error: failed to authenticate with upstream: connection reset by peer; please retry",
    "OAuth refresh temporarily unavailable: connection timed out",
    "API Error: 429 {\"error\": {\"type\": \"rate_limit_error\"}}",
    "API Error: Rate limit reached",
    "Rate limit exceeded",
    "Repeated 529 Overloaded errors",
    "API Error: 529 {\"error\": {\"type\": \"overloaded_error\"}}",
    "Opus is experiencing high load, please use /model to switch to Sonnet",
    "OpenRouter: warning, remaining credits are low; try a cheaper model",
    "You've used 90% of your weekly limit · resets 9:50pm (UTC)",
    "Approaching session limit · resets 9:50pm (UTC)",
    "You're close to your extra usage spending limit",
    "You're now using extra usage",
]


@pytest.mark.parametrize("reply", RECOVERABLE)
@pytest.mark.parametrize("path", ["error_message", "raw", "synthetic", "envelope"])
def test_transient_and_warning_replies_remain_recoverable(reply, path):
    result = SimpleNamespace(is_error=True, error_message="Schema validation failed", messages=[])
    if path == "error_message":
        result.error_message = reply
    elif path == "raw":
        result.result = reply
    elif path == "synthetic":
        result.messages = [AssistantMessage(content=[TextBlock(reply)], model="<synthetic>", error="rate_limit")]
    else:
        with pytest.raises(RuntimeError) as exc:
            unwrap_call_result({"status": "failed", "error_message": reply})
        assert not isinstance(exc.value, FatalHarnessError)
        return
    check_fatal_harness_error(result)


CAPS = [
    "You've hit your session limit · resets 9:50pm (UTC)",
    "You've hit your session limit",
    "You've hit your weekly limit · resets Oct 23, 3pm (America/Toronto)",
    "You've hit your Opus limit · resets Jan 1, 2027, 9:50pm (Etc/GMT+5)",
    "You've hit your Sonnet limit",
    "You've hit your usage limit",
    "You've hit your limit",
    "You're out of extra usage · resets 9:50pm (UTC)",
    "You're out of extra usage",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Invalid API key · Please run /login",
    "Invalid API key · Fix external API key",
    "OAuth token revoked · Please run /login",
    "Your account does not have access to Claude Code. Please run /login.",
    'API Error: 401 {"error":{"message":"OAuth token has expired"}} · Please run /login',
]


@pytest.mark.parametrize("reply", CAPS)
@pytest.mark.parametrize("path", ["raw", "error_message", "object", "harness", "serialized", "wire", "parsed_wire", "envelope"])
def test_genuine_provider_replies_propagate(reply, path):
    wire = {"type": "assistant", "message": {
        "model": "<synthetic>", "content": [{"type": "text", "text": reply}],
    }, "error": "rate_limit"}
    obj = AssistantMessage(content=[TextBlock(reply)], model="<synthetic>", error="rate_limit")
    result = SimpleNamespace(is_error=True, error_message="Schema validation failed", messages=[])
    if path == "raw":
        result.result = reply
    elif path == "error_message":
        # The maintainer contract preserves main's generic text classifier.
        # Bare diagnoses gain no fatal rules; only the existing invalid-key
        # pattern applies here. Raw replies and synthetic messages are below.
        result.error_message = reply
        if not reply.startswith("Invalid API key"):
            check_fatal_harness_error(result)
            return
    elif path == "envelope":
        with pytest.raises(FatalHarnessError):
            unwrap_call_result({"status": "failed", "error_message": str(FatalHarnessError(reply))})
        return
    else:
        result.messages = [{
            "object": obj, "harness": vars(obj), "serialized": asdict(obj),
            "wire": wire, "parsed_wire": parse_message(wire),
        }[path]]
    with pytest.raises(FatalHarnessError):
        check_fatal_harness_error(result)


@pytest.mark.parametrize("source", ["tool", "user", "assistant", "nested_wire"])
def test_nested_fatal_looking_tool_output_is_ignored(source):
    nested = {"type": "assistant", "message": {
        "model": "<synthetic>", "content": [{"type": "text", "text": MARKER}],
    }, "error": "authentication_failed"}
    messages = {
        "tool": vars(UserMessage(content=[ToolResultBlock("read-1", content=MARKER)])),
        "user": vars(UserMessage(content=[TextBlock(MARKER)])),
        "assistant": vars(AssistantMessage(content=[TextBlock(MARKER)], model="claude-sonnet")),
        "nested_wire": {"type": "user", "message": {"content": [{"type": "tool_result", "content": nested}]}},
    }
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message="Schema validation failed", messages=[messages[source]], result=MARKER,
    ))

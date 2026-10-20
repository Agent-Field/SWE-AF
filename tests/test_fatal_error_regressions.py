"""Offline regressions for quoted output and cross-process fatal failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentfield.harness import HarnessRunner, _runner
from agentfield.harness._result import RawResult

from swe_af.execution.envelope import unwrap_call_result
from swe_af.execution.fatal_error import FatalHarnessError, check_fatal_harness_error
from swe_af.execution.schemas import QASynthesisResult


@pytest.mark.parametrize("provider", ["claude-code", "opencode", "codex"])
@pytest.mark.parametrize("quote", [
    "failed to authenticate",
    "usage limit reached",
    "out of extra usage",
    "hit your session limit",
    "oauth session expired",
])
async def test_quoted_invalid_json_remains_schema_failure(monkeypatch, tmp_path, provider, quote):
    """Keep the SDK's real file parser and quoted schema-error diagnosis."""
    class OfflineProvider:
        async def execute(self, prompt, options):
            output_dir = next(tmp_path.glob(".agentfield-out-*"))
            (output_dir / ".agentfield_output.json").write_text(
                '{"summary": "Example: ' + quote + '"', encoding="utf-8",
            )
            # No completion text: schema repair cannot invoke a real model.
            return RawResult(messages=[{
                "model": "ordinary-model",
                "content": [{"type": "text", "text": quote}],
            }])

    monkeypatch.setattr(_runner, "build_provider", lambda config: OfflineProvider())
    result = await HarnessRunner().run(
        "Summarize authentication troubleshooting",
        provider=provider, schema=QASynthesisResult, cwd=str(tmp_path),
        max_retries=0, schema_max_retries=0,
    )
    assert result.is_error and result.failure_type.value == "schema"
    assert quote in result.error_message
    check_fatal_harness_error(result)


async def test_qa_synthesizer_transient_auth_failure_can_fall_back(monkeypatch):
    from swe_af.reasoners import execution_agents

    result = SimpleNamespace(
        is_error=True, parsed=None, result=None, messages=[],
        error_message=(
            "Error: failed to authenticate with upstream: "
            "connection reset by peer; please retry"
        ),
    )
    router = MagicMock(harness=AsyncMock(return_value=result))
    monkeypatch.setattr(execution_agents, "router", router)
    output = await execution_agents.run_qa_synthesizer(
        qa_result={"passed": True}, review_result={"approved": True},
        iteration_history=[], ai_provider="opencode",
    )
    assert output["action"] == "approve"
    router.harness.assert_awaited_once()


def test_unrelated_transient_execution_error_is_not_fatal():
    with pytest.raises(RuntimeError) as raised:
        unwrap_call_result({
            "status": "failed",
            "error_message": "git helper failed to authenticate: agent temporarily unavailable",
        })
    assert not isinstance(raised.value, FatalHarnessError)


@pytest.mark.parametrize("status", ["failed", "error", "cancelled", "timeout"])
@pytest.mark.parametrize("error_field", ["error_message", "error"])
@pytest.mark.parametrize("detail", [
    "Claude unavailable: You're out of extra usage",
    "Claude unavailable: provider unavailable",
])
def test_cross_process_fatal_marker_survives(status, error_field, detail):
    # Serialize the actual exception as a remote execution would. The opaque
    # detail ensures this depends on the marker, not on a provider substring.
    remote_error = str(FatalHarnessError(detail))
    with pytest.raises(FatalHarnessError, match="Claude unavailable"):
        unwrap_call_result({
            "status": status,
            error_field: f"Remote reasoner failed: {remote_error}",
        })


@pytest.mark.parametrize("reply", [
    "You've hit your session limit",
    "You're out of extra usage",
    "Claude AI usage limit reached",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
])
@pytest.mark.parametrize("template", [
    "Schema validation failed. Invalid output: {reply}",
    "{reply} is an example from the documentation.",
    "{reply}\nPlease add a test for this example.",
])
def test_complete_reply_inside_diagnosis_is_recoverable(reply, template):
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message=template.format(reply=reply), messages=[],
    ))


@pytest.mark.parametrize("reply", [
    "You've hit your session limit",
    "You're out of extra usage",
    "Claude AI usage limit reached",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
])
def test_raw_completion_must_be_a_complete_reply(reply):
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message="Schema validation failed", messages=[],
        result=f"{reply}\nPlease add a test for this example.",
    ))


@pytest.mark.parametrize("field", ["error_message", "result"])
def test_reset_suffix_cannot_swallow_trailing_prose(field):
    values = dict(is_error=True, error_message="Schema validation failed", messages=[])
    values[field] = (
        "You've hit your session limit · resets 9:50pm (UTC) is an example in the docs."
    )
    check_fatal_harness_error(SimpleNamespace(**values))

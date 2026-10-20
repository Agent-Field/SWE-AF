"""Keep new fatal signals at the provider and serialized-exception boundaries."""

import json
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe_af.execution.envelope import unwrap_call_result
from swe_af.execution.fatal_error import (
    FatalHarnessError,
    check_fatal_harness_error,
    is_fatal_error,
)


MARKER = "Fatal API error (non-retryable): opaque provider failure"
NEW_REPLIES = [
    "You've hit your session limit",
    "You're out of extra usage",
    "Claude AI usage limit reached",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "OAuth token revoked · Please run /login",
    "Your account does not have access to Claude Code. Please run /login.",
    "API Error: 401 status code (no body) · Please run /login",
    "API Error: 403 OAuth token has expired · Please run /login",
    MARKER,
]


@pytest.mark.parametrize("reply", NEW_REPLIES)
def test_generic_classifier_and_bare_diagnoses_keep_main_behavior(reply):
    assert is_fatal_error(reply) is False
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message=reply, messages=[],
    ))
    with pytest.raises(RuntimeError) as exc:
        # Only an outer exception marker gains propagation at this boundary.
        unwrap_call_result({"status": "failed", "error_message": "diagnosis: " + reply})
    assert not isinstance(exc.value, FatalHarnessError)


@pytest.mark.parametrize("prefix", [
    "pytest failed: ",
    "Schema validation failed. Invalid output: ",
    "tool output: ",
    "Remote: ",
    "Remote reasoner failed: ",
    "FatalHarnessError: ",  # The SDK serializes str(exc), without this prefix.
    "RuntimeError: ",
    '"',
    "Traceback (most recent call last):\n",
])
@pytest.mark.parametrize("field", ["error_message", "error"])
def test_embedded_marker_and_unverified_wrappers_are_recoverable(prefix, field):
    with pytest.raises(RuntimeError) as exc:
        unwrap_call_result({"status": "failed", field: prefix + MARKER})
    assert not isinstance(exc.value, FatalHarnessError)


@pytest.mark.parametrize("reply", NEW_REPLIES[:-1])
@pytest.mark.parametrize("suffix", [" is an example in the docs.", "\nAdd a regression test."])
def test_raw_reply_cannot_include_appended_prose(reply, suffix):
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message="Schema validation failed", messages=[],
        result=reply + suffix,
    ))


@pytest.mark.parametrize("reply", [
    "API Error: 401",
    "API Error: 403 {}",
    "API Error: 401 status code (no body)",
    "API Error: 403 OAuth token has expired",
    "API Error: 401 status code (no body) · Please run /login and retry the test",
    "API Error: 400 status code (no body) · Please run /login",
])
def test_raw_auth_requires_a_complete_known_login_reply(reply):
    check_fatal_harness_error(SimpleNamespace(
        is_error=True, error_message="Schema validation failed", messages=[], result=reply,
    ))


@pytest.mark.parametrize("fatal", [False, True])
async def test_sdk_exception_round_trip_across_processes(fatal, tmp_path):
    """Run the SDK HTTP endpoint/callback in a child, then unwrap its JSON.

    Only transport is replaced: Agent.reasoner, execution, exception handling,
    callback JSON encoding, and the client envelope builder are the real SDK.
    No control plane or model is contacted.
    """
    program = textwrap.dedent('''
        import asyncio
        import json
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from agentfield import Agent
        from httpx import ASGITransport, AsyncClient
        from swe_af.execution.fatal_error import FatalHarnessError

        async def main():
            agent = Agent(
                node_id="fatal-boundary-probe",
                agentfield_server="http://localhost:9999",
                callback_url="http://localhost:8000",
                auto_register=False, enable_did=False, vc_enabled=False,
            )
            @agent.reasoner()
            async def fail() -> dict:
                if sys.argv[1] == "True":
                    raise FatalHarnessError("opaque provider failure")
                raise RuntimeError(
                    'pytest expected "Fatal API error (non-retryable): opaque provider failure"'
                )

            # Disable workflow notification I/O; retain real exception handling
            # and the asynchronous execution status callback.
            agent.workflow_handler = None
            agent.client._async_request = AsyncMock(
                return_value=SimpleNamespace(status_code=200)
            )
            async with AsyncClient(
                transport=ASGITransport(app=agent), base_url="http://agent"
            ) as client:
                response = await client.post(
                    "/reasoners/fail", json={},
                    headers={"X-Execution-ID": "opaque-1"},
                )
                assert response.status_code == 202
                await asyncio.gather(*list(agent._background_tasks))
            call = agent.client._async_request.await_args
            assert call.args[0] == "POST"
            assert call.args[1].endswith("/executions/opaque-1/status")
            Path(sys.argv[2]).write_text(json.dumps(call.kwargs["json"]))
            await agent.client.aclose()

        asyncio.run(main())
    ''')
    payload_path = tmp_path / "callback.json"
    child = subprocess.run(
        [sys.executable, "-c", program, str(fatal), str(payload_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 0, child.stderr
    payload = json.loads(payload_path.read_text())
    assert payload["status"] == "failed"
    expected = MARKER if fatal else f'pytest expected "{MARKER}"'
    assert payload["error"] == expected

    from agentfield.client import AgentFieldClient
    from httpx import Request, Response

    client = AgentFieldClient(base_url="http://localhost:9999")
    client._async_request = AsyncMock(side_effect=[
        Response(202, json={
            "execution_id": "opaque-1", "run_id": "run-1", "status": "queued",
        }),
        Response(200, json=payload, request=Request("GET", "http://localhost:9999")),
    ])
    try:
        envelope = await client.execute("fatal-boundary-probe.fail", {})
        assert [call.args[0] for call in client._async_request.await_args_list] == ["POST", "GET"]
    finally:
        await client.aclose()
    assert envelope["error_message"] == expected
    with pytest.raises(RuntimeError) as exc:
        unwrap_call_result(envelope)
    assert isinstance(exc.value, FatalHarnessError) is fatal

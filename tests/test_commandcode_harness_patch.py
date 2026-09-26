from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from swe_af.runtime.commandcode_harness_patch import (
    CommandCodeProvider,
    apply_commandcode_harness_patch,
)


def _provider() -> CommandCodeProvider:
    return CommandCodeProvider()


def _run(monkeypatch, stdout: str, stderr: str, returncode: int, options: dict | None = None):
    """Drive CommandCodeProvider.execute with a fake CLI runner."""
    import agentfield.harness._cli as sdk_cli

    async def fake_run_cli(cmd, *, env=None, cwd=None, timeout=None, input_text=None):
        return stdout, stderr, returncode

    monkeypatch.setattr(sdk_cli, "run_cli", fake_run_cli)
    return asyncio.run(_provider().execute("prompt", options or {}))


# --- registration -----------------------------------------------------------


def test_patch_registers_provider_with_agentfield_factory() -> None:
    from agentfield.harness import _runner
    from agentfield.harness.providers import _factory

    apply_commandcode_harness_patch()

    assert "command-code" in _factory.SUPPORTED_PROVIDERS
    built = _runner.build_provider(SimpleNamespace(provider="command-code"))
    assert isinstance(built, CommandCodeProvider)


def test_patch_passes_unknown_providers_through_to_original_factory() -> None:
    from agentfield.harness import _runner
    from agentfield.harness.providers import _factory

    apply_commandcode_harness_patch()

    with pytest.raises(ValueError):
        _runner.build_provider(SimpleNamespace(provider="definitely-not-real"))
    assert "claude-code" in _factory.SUPPORTED_PROVIDERS


def test_codex_patch_entrypoint_also_registers_command_code() -> None:
    """The existing codex call sites must register command-code too — that is
    what keeps the AgentField provider wiring confined to swe_af/runtime/."""
    from agentfield.harness import _runner

    from swe_af.runtime.codex_harness_patch import apply_codex_harness_patch

    apply_codex_harness_patch()

    built = _runner.build_provider(SimpleNamespace(provider="command-code"))
    assert isinstance(built, CommandCodeProvider)


# --- argv construction ------------------------------------------------------


def test_default_permission_grants_writes_and_shell() -> None:
    cmd = _provider()._build_command("do it", {}, stdin_prompt=False)
    assert cmd[0] == "cmd"
    assert cmd[1] == "-p"
    assert "do it" in cmd
    assert "--yolo" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_plan_permission_is_read_only() -> None:
    cmd = _provider()._build_command("x", {"permission_mode": "plan"}, stdin_prompt=False)
    assert "--plan" in cmd
    assert "--yolo" not in cmd


def test_dont_ask_permission_passes_through_verbatim() -> None:
    cmd = _provider()._build_command(
        "x", {"permission_mode": "dont-ask"}, stdin_prompt=False
    )
    assert cmd[cmd.index("--permission-mode") + 1] == "dont-ask"
    assert "--yolo" not in cmd


def test_blank_or_absent_model_omits_dash_m() -> None:
    assert "-m" not in _provider()._build_command("x", {}, stdin_prompt=False)
    assert "-m" not in _provider()._build_command("x", {"model": ""}, stdin_prompt=False)


def test_model_and_variant_are_forwarded() -> None:
    cmd = _provider()._build_command(
        "x", {"model": "future/model-9#high"}, stdin_prompt=False
    )
    assert cmd[cmd.index("-m") + 1] == "future/model-9"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_max_turns_resume_and_add_dir() -> None:
    cmd = _provider()._build_command(
        "x",
        {"max_turns": 7, "resume_session_id": "abc", "cwd": "/w", "project_dir": "/p"},
        stdin_prompt=False,
    )
    assert cmd[cmd.index("--max-turns") + 1] == "7"
    assert cmd[cmd.index("--resume") + 1] == "abc"
    assert cmd[cmd.index("--add-dir") + 1] == "/p"


def test_stdin_mode_omits_positional_query() -> None:
    cmd = _provider()._build_command("do it", {}, stdin_prompt=True)
    assert "do it" not in cmd
    assert cmd[1] == "-p"


# --- output parsing ---------------------------------------------------------


def test_result_line_is_parsed_into_raw_result(monkeypatch) -> None:
    stdout = (
        '{"type": "event", "event": {"type": "tool_running"}}\n'
        '{"type": "result", "subtype": "success", "sessionId": "s1", '
        '"stopReason": "end_turn", "usage": {"input_tokens": 11, "output_tokens": 22}, '
        '"durationMs": 1234, "finalText": "{\\"ok\\": true}"}\n'
    )

    raw = _run(monkeypatch, stdout, "", 0)

    assert raw.is_error is False
    assert raw.result == '{"ok": true}'
    assert raw.metrics.session_id == "s1"
    assert raw.metrics.duration_api_ms == 1234
    assert raw.metrics.input_tokens == 11
    assert raw.metrics.output_tokens == 22


# Shape captured verbatim from a real `cmd -p --output-format json` run:
# `usage` is camelCase and lives either top-level on the final `result` line or
# nested under the `run_end` frame. The SDK's snake_case extractor misses both,
# which silently zeroed token accounting until this was fixed.
_LIVE_USAGE = '{"inputTokens": 14958, "outputTokens": 2, "cacheReadTokens": 5632, "cacheWriteTokens": 0}'
_LIVE_STREAM = (
    '{"type":"event","event":{"type":"run_start","sessionId":"s1"}}\n'
    '{"type":"event","event":{"type":"model_request_end","model":"deepseek/deepseek-v4-flash",'
    '"usage":' + _LIVE_USAGE + ',"stopReason":"stop"}}\n'
    '{"type":"event","event":{"type":"run_end","result":{"finalText":"OK",'
    '"usage":' + _LIVE_USAGE + '}}}\n'
    '{"type":"result","subtype":"success","sessionId":"s1","stopReason":"end_turn",'
    '"usage":' + _LIVE_USAGE + ',"durationMs":3403,"finalText":"OK"}\n'
)


def test_camelcase_usage_from_live_stream_is_recorded(monkeypatch) -> None:
    raw = _run(monkeypatch, _LIVE_STREAM, "", 0)

    assert raw.result == "OK"
    assert raw.metrics.input_tokens == 14958
    assert raw.metrics.output_tokens == 2
    assert raw.metrics.cache_read_tokens == 5632
    assert raw.metrics.cache_creation_tokens == 0


def test_usage_is_read_from_run_end_when_result_line_is_absent(monkeypatch) -> None:
    stdout = (
        '{"type":"event","event":{"type":"run_end","result":{"finalText":"OK",'
        '"usage":{"inputTokens":7,"outputTokens":3}}}}\n'
    )

    raw = _run(monkeypatch, stdout, "", 0)

    assert raw.metrics.input_tokens == 7
    assert raw.metrics.output_tokens == 3


# --- exit-code handling -----------------------------------------------------


@pytest.mark.parametrize(
    "code,failure",
    [(5, "api_error"), (6, "api_error"), (7, "api_error"), (8, "no_output"), (9, "no_output")],
)
def test_exit_codes_map_to_failure_types(monkeypatch, code, failure) -> None:
    raw = _run(monkeypatch, "", "", code)
    assert raw.is_error is True
    assert raw.failure_type.value == failure


def test_auth_failure_is_classified_fatal(monkeypatch) -> None:
    from swe_af.execution.fatal_error import is_fatal_error

    raw = _run(monkeypatch, "", "not logged in", 3)
    assert raw.is_error is True
    assert is_fatal_error(raw.error_message)


def test_insufficient_credits_is_classified_fatal(monkeypatch) -> None:
    from swe_af.execution.fatal_error import is_fatal_error

    raw = _run(monkeypatch, "", "", 10)
    assert is_fatal_error(raw.error_message)


def test_rate_limit_message_is_retryable(monkeypatch) -> None:
    raw = _run(monkeypatch, "", "", 5)
    assert raw.failure_type.value == "api_error"
    assert "rate limit" in raw.error_message.lower()


def test_missing_binary_returns_crash(monkeypatch) -> None:
    import agentfield.harness._cli as sdk_cli

    async def boom(*args, **kwargs):
        raise FileNotFoundError("no cmd")

    monkeypatch.setattr(sdk_cli, "run_cli", boom)
    raw = asyncio.run(_provider().execute("p", {}))

    assert raw.is_error is True
    assert raw.failure_type.value == "crash"
    assert "Command Code CLI not found" in raw.error_message

"""Command Code (`cmd`) harness provider for AgentField.

AgentField ships a fixed provider set (``claude-code``, ``codex``, ``opencode``,
``gemini``, …) resolved by :func:`agentfield.harness.providers._factory.build_provider`,
which raises ``ValueError`` for any name outside its ``SUPPORTED_PROVIDERS``
set. ``apply_commandcode_harness_patch`` registers a ``command-code`` provider
without touching AgentField: it adds the name to that set and wraps the factory
(both on ``_factory`` and on ``_runner``, which imported the name by value).

The provider drives Command Code's headless mode:

    cmd -p "<prompt>" --output-format json --yolo -m <model> -t ...

Model ids are never baked in here: a blank model omits ``-m``, so Command Code
uses whatever model it is configured with — every model Command Code supports
today or adds later works without a SWE-AF change.
"""

from __future__ import annotations

import os
import time
from typing import Any

_PATCHED = False

# Command Code headless exit codes (see commandcode.ai/docs, Headless Mode).
_EXIT_AUTH_ERROR = 3
_EXIT_PERMISSION_DENIED = 4
_EXIT_RATE_LIMITED = 5
_EXIT_CONNECTION_ERROR = 6
_EXIT_SERVER_ERROR = 7
_EXIT_MAX_TURNS_REACHED = 8
_EXIT_NO_RESPONSE = 9
_EXIT_INSUFFICIENT_CREDITS = 10

# Codes the runner should retry (transient transport/provider failures).
_TRANSIENT_EXIT_CODES = frozenset(
    {_EXIT_RATE_LIMITED, _EXIT_CONNECTION_ERROR, _EXIT_SERVER_ERROR}
)
# Codes where the process ran but produced no usable final answer.
_NO_OUTPUT_EXIT_CODES = frozenset({_EXIT_MAX_TURNS_REACHED, _EXIT_NO_RESPONSE})

# Canonical phrasing per exit code. The wording deliberately carries the tokens
# SWE-AF's fatal/transient detectors look for (swe_af/execution/fatal_error.py,
# and the runner's TRANSIENT_PATTERNS) so auth/credit failures short-circuit
# instead of burning the retry budget, and rate-limit/5xx failures are retried.
_EXIT_MESSAGES: dict[int, str] = {
    _EXIT_AUTH_ERROR: "Command Code authentication failed",
    _EXIT_PERMISSION_DENIED: "Command Code permission denied",
    _EXIT_RATE_LIMITED: "Command Code rate limit exceeded",
    _EXIT_CONNECTION_ERROR: "Command Code connection error",
    _EXIT_SERVER_ERROR: "Command Code API server error (503)",
    _EXIT_MAX_TURNS_REACHED: "Command Code reached --max-turns before a final answer",
    _EXIT_NO_RESPONSE: "Command Code model produced no response",
    _EXIT_INSUFFICIENT_CREDITS: "Command Code reported insufficient credits",
}


def _exit_error_message(code: int, result_error: str, stderr: str) -> str:
    base = _EXIT_MESSAGES.get(code, f"Command Code CLI exited with code {code}")
    detail = result_error or stderr
    message = f"{base} (exit {code})."
    return f"{message} {detail}" if detail else message


def _int_field(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _usage_from_frame(frame: Any) -> dict[str, Any] | None:
    """Pull a usage object out of one NDJSON frame.

    Handles both shapes Command Code emits: a top-level ``usage`` (the final
    ``result`` line) and a ``usage`` nested under ``result`` (the ``run_end``
    event frame).
    """
    if not isinstance(frame, dict):
        return None
    usage = frame.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    nested = frame.get("result")
    if isinstance(nested, dict):
        usage = nested.get("usage")
        if isinstance(usage, dict) and usage:
            return usage
    return None


def _extract_token_usage(events: list[Any]) -> dict[str, int]:
    """Token counts from Command Code's NDJSON stream.

    Command Code emits camelCase usage (``inputTokens`` / ``outputTokens`` /
    ``cacheReadTokens`` / ``cacheWriteTokens``), which the SDK's
    ``extract_token_usage`` does not recognize — reusing it silently reported
    zero tokens for every call. Both spellings are read, and the last usage
    object wins so the run totals on the final ``result`` line (or the
    ``run_end`` frame) override the per-turn values.
    """
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        # Event frames wrap the payload in an "event" envelope; the final
        # result line does not.
        usage = _usage_from_frame(event) or _usage_from_frame(event.get("event"))
        if usage is None:
            continue
        tokens = {
            "input_tokens": _int_field(usage, "inputTokens", "input_tokens", "prompt_tokens"),
            "output_tokens": _int_field(usage, "outputTokens", "output_tokens", "completion_tokens"),
            "cache_read_tokens": _int_field(
                usage, "cacheReadTokens", "cache_read_input_tokens", "cached_input_tokens"
            ),
            "cache_creation_tokens": _int_field(
                usage, "cacheWriteTokens", "cache_creation_input_tokens"
            ),
        }
    return tokens


# Permission modes that grant writes and shell. SWE-AF coders need both, and
# the other runtimes' effective defaults allow them, so an unset mode maps here
# rather than to Command Code's headless default (which blocks writes).
_WRITE_MODES = frozenset({"", "auto", "yolo", "bypass", "bypasspermissions"})
_READ_ONLY_MODES = frozenset({"plan", "read-only", "readonly"})


def _resolve_bin(options: dict[str, object]) -> str:
    for key in ("commandcode_bin", "bin_path"):
        candidate = options.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return os.environ.get("SWE_COMMANDCODE_BIN", "").strip() or "cmd"


def _permission_args(options: dict[str, object]) -> list[str]:
    raw = options.get("permission_mode")
    mode = str(raw).strip().lower() if raw is not None else ""
    if mode in _WRITE_MODES:
        return ["--yolo"]
    if mode in _READ_ONLY_MODES:
        return ["--plan"]
    return ["--permission-mode", mode]


class CommandCodeProvider:
    """Invokes the Command Code CLI (``cmd``) as a subprocess."""

    def __init__(self, bin_path: str | None = None):
        self._bin = bin_path or "cmd"

    def _build_command(
        self, prompt: str, options: dict[str, object], *, stdin_prompt: bool
    ) -> list[str]:
        from agentfield.harness._cli import resolve_model_and_variant

        cmd = [self._bin, "-p"]
        if not stdin_prompt:
            cmd.append(prompt)
        cmd.extend(
            [
                "--output-format",
                "json",
                "--skip-onboarding",
                "--no-auto-update",
                "--trust",
            ]
        )
        cmd.extend(_permission_args(options))

        model_value, variant_value = resolve_model_and_variant(options)
        if model_value:
            cmd.extend(["-m", model_value])
        if variant_value:
            cmd.extend(["--effort", variant_value])

        max_turns = options.get("max_turns")
        if isinstance(max_turns, int) and max_turns > 0:
            cmd.extend(["--max-turns", str(max_turns)])

        resume = options.get("resume_session_id")
        if isinstance(resume, str) and resume.strip():
            cmd.extend(["--resume", resume.strip()])

        cwd = options.get("cwd")
        project_dir = options.get("project_dir")
        if (
            isinstance(project_dir, str)
            and project_dir
            and isinstance(cwd, str)
            and cwd
            and project_dir != cwd
        ):
            cmd.extend(["--add-dir", project_dir])
        return cmd

    async def execute(self, prompt: str, options: dict[str, object]) -> Any:
        from agentfield.harness._cli import (
            extract_final_text,
            parse_jsonl,
            run_cli,
            strip_ansi,
        )
        from agentfield.harness._result import FailureType, Metrics, RawResult

        # Command Code reads the prompt from stdin when no positional query is
        # given; on Windows that avoids the ~8k command-line cap on npm .cmd
        # shims (same rationale as the SDK's opencode provider).
        via_stdin = os.name == "nt"

        effective_prompt = prompt
        system_prompt = options.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt.strip():
            effective_prompt = (
                f"SYSTEM INSTRUCTIONS:\n{system_prompt.strip()}\n\n---\n\n"
                f"USER REQUEST:\n{prompt}"
            )

        cmd = self._build_command(effective_prompt, options, stdin_prompt=via_stdin)

        env: dict[str, str] = {}
        env_value = options.get("env")
        if isinstance(env_value, dict):
            env = {
                str(key): str(value)
                for key, value in env_value.items()
                if isinstance(key, str) and isinstance(value, str)
            }

        cwd_raw = options.get("project_dir") or options.get("cwd")
        cwd = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else None

        timeout_value = options.get("timeout_seconds")
        timeout = (
            float(timeout_value)
            if isinstance(timeout_value, (int, float)) and timeout_value > 0
            else None
        )

        start = time.monotonic()
        try:
            stdout, stderr, returncode = await run_cli(
                cmd,
                env=env,
                cwd=cwd,
                timeout=timeout,
                input_text=effective_prompt if via_stdin else None,
            )
        except FileNotFoundError:
            return RawResult(
                result=None,
                messages=[],
                metrics=Metrics(),
                is_error=True,
                error_message=(
                    f"Command Code CLI not found at '{self._bin}'. "
                    "Install it with: npm install -g command-code"
                ),
                failure_type=FailureType.CRASH,
            )
        except TimeoutError as exc:
            return RawResult(
                result=None,
                messages=[],
                metrics=Metrics(
                    duration_api_ms=int((time.monotonic() - start) * 1000)
                ),
                is_error=True,
                error_message=str(exc),
                failure_type=FailureType.TIMEOUT,
            )

        events = parse_jsonl(stdout)

        result_text: str | None = None
        session_id = ""
        duration_ms = 0
        result_error = ""
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            final_text = event.get("finalText")
            if isinstance(final_text, str):
                result_text = final_text
            sid = event.get("sessionId")
            if isinstance(sid, str):
                session_id = sid
            dm = event.get("durationMs")
            if isinstance(dm, (int, float)):
                duration_ms = int(dm)
            if event.get("subtype") == "error":
                err = event.get("error")
                if isinstance(err, str):
                    result_error = err

        if result_text is None:
            fallback = extract_final_text(events)
            if fallback is not None:
                result_text = fallback

        clean_stderr = strip_ansi(stderr).strip() if stderr else ""

        is_error = False
        failure_type = FailureType.NONE
        error_message: str | None = None

        if returncode < 0:
            is_error = True
            failure_type = FailureType.CRASH
            error_message = (
                f"Command Code CLI killed by signal {-returncode}. {clean_stderr}"
            ).strip()
        elif returncode != 0:
            is_error = True
            if returncode in _TRANSIENT_EXIT_CODES:
                failure_type = FailureType.API_ERROR
            elif returncode in _NO_OUTPUT_EXIT_CODES:
                failure_type = FailureType.NO_OUTPUT
            else:
                failure_type = FailureType.CRASH
            error_message = _exit_error_message(returncode, result_error, clean_stderr)
        elif result_error and not result_text:
            is_error = True
            failure_type = FailureType.CRASH
            error_message = result_error

        tokens = _extract_token_usage(events)

        return RawResult(
            result=result_text,
            messages=events,
            metrics=Metrics(
                duration_api_ms=duration_ms
                or int((time.monotonic() - start) * 1000),
                num_turns=1 if result_text else 0,
                total_cost_usd=None,
                session_id=session_id,
                input_tokens=tokens["input_tokens"],
                output_tokens=tokens["output_tokens"],
                cache_read_tokens=tokens["cache_read_tokens"],
                cache_creation_tokens=tokens["cache_creation_tokens"],
            ),
            is_error=is_error,
            error_message=error_message,
            failure_type=failure_type,
            returncode=returncode,
        )


def apply_commandcode_harness_patch() -> None:
    """Register the ``command-code`` provider with AgentField's harness factory."""
    global _PATCHED
    if _PATCHED:
        return
    try:
        from agentfield.harness import _runner
        from agentfield.harness.providers import _factory
    except Exception:
        return

    original_build_provider = _factory.build_provider
    _factory.SUPPORTED_PROVIDERS.add("command-code")

    def build_provider(config: object) -> object:
        if getattr(config, "provider", None) == "command-code":
            return CommandCodeProvider()
        return original_build_provider(config)

    # _runner does `from ..._factory import build_provider`, binding the name at
    # import time, so the wrapper must be installed on _runner (the call site)
    # as well as on the factory for direct callers.
    _factory.build_provider = build_provider
    _runner.build_provider = build_provider
    _PATCHED = True

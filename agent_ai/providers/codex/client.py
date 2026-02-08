"""Codex provider client backed by ``codex exec`` CLI."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Type, TypeVar

from pydantic import BaseModel

from agent_ai.providers.codex.adapter import build_codex_command, parse_codex_jsonl
from agent_ai.types import AgentResponse, Message, Metrics, TextContent, Tool

T = TypeVar("T", bound=BaseModel)

_TRANSIENT_PATTERNS = frozenset(
    {
        "rate limit",
        "rate_limit",
        "overloaded",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "service unavailable",
        "503",
        "502",
        "504",
        "internal server error",
        "500",
    }
)

DEFAULT_TOOLS: list[str] = [
    Tool.READ,
    Tool.WRITE,
    Tool.EDIT,
    Tool.BASH,
    Tool.GLOB,
    Tool.GREP,
]


def _is_transient(error: str) -> bool:
    low = error.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


def _tmp_path(cwd: str, prefix: str) -> str:
    name = f".{prefix}_{uuid.uuid4().hex[:12]}.json"
    return os.path.join(os.path.abspath(cwd), name)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text)
    except Exception:
        return None


def _read_and_parse_json_file(path: str, schema: Type[T]) -> T | None:
    """Read a JSON file and parse against schema. Returns None on failure."""
    data = _read_json(path)
    if data is None:
        return None
    try:
        return schema.model_validate(data)
    except Exception:
        return None


def _cleanup_files(paths: list[str]) -> None:
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _write_log(fh: IO[str], event: str, **data: Any) -> None:
    entry = {"ts": time.time(), "event": event, **data}
    fh.write(json.dumps(entry, default=str) + "\n")
    fh.flush()


def _open_log(log_file: str | Path | None) -> IO[str] | None:
    if log_file is None:
        return None
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8")


@dataclass
class CodexProviderConfig:
    """Configuration for the Codex CLI provider."""

    codex_bin: str = "codex"
    model: str = "sonnet"
    cwd: str | Path = "."
    max_turns: int = 10
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    system_prompt: str | None = None
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    permission_mode: str | None = None
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)


class CodexProviderClient:
    """Async client for invoking Codex via CLI."""

    def __init__(self, config: CodexProviderConfig | None = None) -> None:
        self.config = config or CodexProviderConfig()

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: str | Path | None = None,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        output_schema: Type[T] | None = None,
        max_retries: int | None = None,
        max_budget_usd: float | None = None,
        permission_mode: str | None = None,
        env: dict[str, str] | None = None,
        log_file: str | Path | None = None,
    ) -> AgentResponse[T]:
        """Run a prompt through Codex CLI with optional schema-constrained output."""
        cfg = self.config
        effective_model = model or cfg.model
        effective_cwd = str(cwd or cfg.cwd)
        effective_retries = max_retries if max_retries is not None else cfg.max_retries
        effective_env = {**cfg.env, **(env or {})}
        effective_system = system_prompt or cfg.system_prompt

        output_schema_path: str | None = None
        output_last_message_path = _tmp_path(effective_cwd, "codex_output")
        temp_files: list[str] = [output_last_message_path]

        if output_schema:
            output_schema_path = _tmp_path(effective_cwd, "codex_schema")
            temp_files.append(output_schema_path)
            with open(output_schema_path, "w", encoding="utf-8") as f:
                json.dump(output_schema.model_json_schema(), f, indent=2)

        final_prompt = prompt
        if effective_system:
            final_prompt = f"System Instructions:\n{effective_system}\n\nUser Task:\n{prompt}"

        log_fh = _open_log(log_file)
        try:
            return await self._run_with_retries(
                prompt=prompt,
                final_prompt=final_prompt,
                effective_model=effective_model,
                effective_cwd=effective_cwd,
                effective_env=effective_env,
                output_schema=output_schema,
                output_schema_path=output_schema_path,
                output_last_message_path=output_last_message_path,
                effective_retries=effective_retries,
                log_fh=log_fh,
            )
        finally:
            if log_fh:
                log_fh.close()
            _cleanup_files(temp_files)

    async def _run_with_retries(
        self,
        *,
        prompt: str,
        final_prompt: str,
        effective_model: str,
        effective_cwd: str,
        effective_env: dict[str, str],
        output_schema: Type[T] | None,
        output_schema_path: str | None,
        output_last_message_path: str,
        effective_retries: int,
        log_fh: IO[str] | None,
    ) -> AgentResponse[T]:
        cfg = self.config
        delay = cfg.initial_delay
        last_exc: Exception | None = None

        if log_fh:
            _write_log(log_fh, "start", prompt=prompt, model=effective_model, provider="codex")

        for attempt in range(effective_retries + 1):
            try:
                response = await self._execute(
                    prompt=final_prompt,
                    model=effective_model,
                    cwd=effective_cwd,
                    env=effective_env,
                    output_schema_path=output_schema_path,
                    output_last_message_path=output_last_message_path,
                    log_fh=log_fh,
                )

                if not output_schema:
                    if log_fh:
                        _write_log(
                            log_fh,
                            "end",
                            is_error=response.is_error,
                            num_turns=response.metrics.num_turns,
                            cost_usd=response.metrics.total_cost_usd,
                        )
                    return response

                parsed = _read_and_parse_json_file(output_last_message_path, output_schema)
                if parsed is not None:
                    parsed_response = AgentResponse(
                        result=response.result,
                        parsed=parsed,
                        messages=response.messages,
                        metrics=response.metrics,
                        is_error=False,
                    )
                    if log_fh:
                        _write_log(
                            log_fh,
                            "end",
                            is_error=False,
                            num_turns=parsed_response.metrics.num_turns,
                            cost_usd=parsed_response.metrics.total_cost_usd,
                        )
                    return parsed_response

                if log_fh:
                    _write_log(log_fh, "end", is_error=True, reason="schema parse failed")
                return AgentResponse(
                    result=response.result,
                    parsed=None,
                    messages=response.messages,
                    metrics=response.metrics,
                    is_error=True,
                )

            except Exception as e:
                last_exc = e
                if attempt < effective_retries and _is_transient(str(e)):
                    if log_fh:
                        _write_log(log_fh, "retry", attempt=attempt + 1, error=str(e), delay=delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay)
                    continue
                if log_fh:
                    _write_log(log_fh, "end", is_error=True, error=str(e))
                raise

        raise last_exc  # type: ignore[misc]

    async def _execute(
        self,
        *,
        prompt: str,
        model: str,
        cwd: str,
        env: dict[str, str],
        output_schema_path: str | None,
        output_last_message_path: str,
        log_fh: IO[str] | None,
    ) -> AgentResponse[Any]:
        started = time.time()

        cmd = build_codex_command(
            codex_bin=self.config.codex_bin,
            cwd=cwd,
            prompt=prompt,
            model=model,
            output_schema_path=output_schema_path,
            output_last_message_path=output_last_message_path,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, **env},
        )
        stdout_b, stderr_b = await proc.communicate()

        stdout_text = stdout_b.decode("utf-8", errors="replace")
        stderr_text = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exec failed with exit code {proc.returncode}: {stderr_text[-800:]}"
            )

        final_text, usage, events = parse_codex_jsonl(stdout_text)
        if final_text is None and os.path.exists(output_last_message_path):
            try:
                with open(output_last_message_path, "r", encoding="utf-8") as f:
                    final_text = f.read().strip()
            except OSError:
                final_text = None

        duration_ms = int((time.time() - started) * 1000)
        output_tokens = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0

        metrics = Metrics(
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=1,
            total_cost_usd=None,
            usage=usage,
            session_id="",
        )
        messages = []
        if final_text:
            messages.append(
                Message(
                    role="assistant",
                    content=[TextContent(text=final_text)],
                    model=model,
                )
            )

        if log_fh:
            _write_log(
                log_fh,
                "result",
                provider="codex",
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                stderr_tail=stderr_text[-400:] if stderr_text else None,
                event_count=len(events),
            )

        return AgentResponse(
            result=final_text,
            parsed=None,
            messages=messages,
            metrics=metrics,
            is_error=False,
        )

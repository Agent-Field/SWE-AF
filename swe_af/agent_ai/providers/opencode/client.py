"""OpenCode provider client backed by ``opencode acp`` CLI."""

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

from swe_af.agent_ai.types import (
    AgentResponse,
    Content,
    Message,
    Metrics,
    TextContent,
    Tool,
)

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

_SCHEMA_FILE_TOOLS: list[str] = [Tool.WRITE, Tool.READ]


def _is_transient(error: str) -> bool:
    """Check if an error message indicates a transient failure."""
    low = error.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


def _schema_output_path(cwd: str) -> str:
    """Generate a unique temp file path for structured JSON output."""
    name = f".opencode_output_{uuid.uuid4().hex[:12]}.json"
    return os.path.join(os.path.abspath(cwd), name)


def _build_schema_suffix(output_path: str, schema_json: str) -> str:
    """Prompt suffix instructing the agent to write structured output to a file."""
    return (
        f"\n\n---\n"
        f"IMPORTANT — STRUCTURED OUTPUT REQUIREMENT:\n"
        f"After completing the task, you MUST write your final structured output "
        f"as a single valid JSON object to this file:\n"
        f"  {output_path}\n\n"
        f"The JSON must conform to this schema:\n"
        f"```json\n{schema_json}\n```\n\n"
        f"Write ONLY valid JSON to the file — no markdown fences, no explanation, "
        f"just the raw JSON object. Use the Write tool to create the file."
    )


def _read_and_parse_json_file(path: str, schema: Type[T]) -> T | None:
    """Read a JSON file and parse against schema. Returns None on failure."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        text = raw.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n", 1)
            text = lines[1] if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[: -len("```")]
            text = text.strip()
        data = json.loads(text)
        return schema.model_validate(data)
    except Exception:
        return None


def _cleanup_files(paths: list[str]) -> None:
    """Remove all temp files, silently ignoring missing/errors."""
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _content_to_dict(c: Content) -> dict[str, Any]:
    """Convert a Content dataclass to a JSON-serializable dict."""
    if isinstance(c, TextContent):
        return {"type": "text", "text": c.text[:500]}
    return {"type": "unknown"}


def _write_log(fh: IO[str], event: str, **data: Any) -> None:
    """Append a single JSONL event to the log file handle."""
    entry = {"ts": time.time(), "event": event, **data}
    fh.write(json.dumps(entry, default=str) + "\n")
    fh.flush()


def _open_log(log_file: str | Path | None) -> IO[str] | None:
    """Open a log file for appending. Returns None if no log_file."""
    if log_file is None:
        return None
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8")


@dataclass
class OpenCodeProviderConfig:
    """Configuration for the OpenCode provider client."""

    opencode_bin: str = "opencode"
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


class OpenCodeProviderClient:
    """Async client for invoking OpenCode via CLI with prompt-based structured output."""

    def __init__(self, config: OpenCodeProviderConfig | None = None) -> None:
        self.config = config or OpenCodeProviderConfig()

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
        """Run a prompt through OpenCode ACP."""
        cfg = self.config
        effective_model = model or cfg.model
        effective_cwd = str(cwd or cfg.cwd)
        effective_turns = max_turns or cfg.max_turns
        effective_tools = allowed_tools if allowed_tools is not None else list(cfg.allowed_tools)
        effective_retries = max_retries if max_retries is not None else cfg.max_retries
        effective_env = {**cfg.env, **(env or {})}
        effective_system = system_prompt or cfg.system_prompt

        output_path: str | None = None
        final_prompt = prompt
        if output_schema:
            output_path = _schema_output_path(effective_cwd)
            schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
            final_prompt = prompt + _build_schema_suffix(output_path, schema_json)
            # Ensure Write and Read tools are available for structured output
            for t in _SCHEMA_FILE_TOOLS:
                if t not in effective_tools:
                    effective_tools.append(t)

        temp_files: list[str] = []
        if output_path:
            temp_files.append(output_path)

        log_fh = _open_log(log_file)
        try:
            return await self._run_with_retries(
                prompt=prompt,
                final_prompt=final_prompt,
                output_schema=output_schema,
                output_path=output_path,
                effective_cwd=effective_cwd,
                effective_model=effective_model,
                effective_turns=effective_turns,
                effective_tools=effective_tools,
                effective_system=effective_system,
                effective_env=effective_env,
                effective_retries=effective_retries,
                temp_files=temp_files,
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
        output_schema: Type[T] | None,
        output_path: str | None,
        effective_cwd: str,
        effective_model: str,
        effective_turns: int,
        effective_tools: list[str],
        effective_system: str | None,
        effective_env: dict[str, str],
        effective_retries: int,
        temp_files: list[str],
        log_fh: IO[str] | None = None,
    ) -> AgentResponse[T]:
        """Execute with retry logic for transient errors."""
        cfg = self.config
        delay = cfg.initial_delay
        last_exc: Exception | None = None

        if log_fh:
            _write_log(
                log_fh,
                "start",
                prompt=prompt,
                model=effective_model,
                max_turns=effective_turns,
            )

        for attempt in range(effective_retries + 1):
            try:
                response = await self._execute(
                    prompt=final_prompt,
                    model=effective_model,
                    cwd=effective_cwd,
                    max_turns=effective_turns,
                    tools=effective_tools,
                    system_prompt=effective_system,
                    env=effective_env,
                    log_fh=log_fh,
                )

                # If no output schema, return as-is
                if not output_schema or output_path is None:
                    if log_fh:
                        _write_log(
                            log_fh,
                            "end",
                            is_error=response.is_error,
                            num_turns=response.metrics.num_turns,
                            cost_usd=response.metrics.total_cost_usd,
                        )
                    return response

                # Try to parse structured output from file
                parsed = _read_and_parse_json_file(output_path, output_schema)
                if parsed is not None:
                    resp = AgentResponse(
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
                            num_turns=response.metrics.num_turns,
                            cost_usd=response.metrics.total_cost_usd,
                        )
                    return resp

                # Structured output parsing failed
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
                # Check if error is transient and we have retries left
                if attempt < effective_retries and _is_transient(str(e)):
                    if log_fh:
                        _write_log(
                            log_fh,
                            "retry",
                            attempt=attempt + 1,
                            error=str(e),
                            delay=delay,
                        )
                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay)
                    # Generate new temp file path for retry
                    if output_schema:
                        output_path = _schema_output_path(effective_cwd)
                        temp_files.append(output_path)
                        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
                        final_prompt = prompt + _build_schema_suffix(output_path, schema_json)
                    continue
                # Non-transient error or out of retries
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
        max_turns: int,
        tools: list[str],
        system_prompt: str | None,
        env: dict[str, str],
        log_fh: IO[str] | None = None,
    ) -> AgentResponse[Any]:
        """Execute single OpenCode invocation via subprocess."""
        start_time = time.time()

        # Build command - OpenCode uses -p for non-interactive mode
        cmd = [
            self.config.opencode_bin,
            "-p",
            prompt,
            "-c",
            cwd,
            "-q",  # Quiet mode - hide spinner
        ]

        # Construct full environment (inherit + add user env + disable TTY)
        full_env = {
            **os.environ,
            **env,
            "TERM": "dumb",  # Disable fancy terminal features
            "NO_COLOR": "1",  # Disable colors
        }

        # Execute OpenCode with prompt - it runs in cwd automatically
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=full_env,
        )

        # Wait for completion
        stdout_b, stderr_b = await proc.communicate()
        duration_ms = int((time.time() - start_time) * 1000)

        stdout_text = stdout_b.decode("utf-8", errors="replace")
        stderr_text = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            error_msg = f"opencode failed with exit code {proc.returncode}: {stderr_text}"
            raise RuntimeError(error_msg)

        # Parse output - OpenCode writes response to stdout
        final_text = stdout_text.strip() or None

        # Build metrics
        metrics = Metrics(
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=1,
            total_cost_usd=None,
            usage=None,
            session_id="",
        )

        # Build messages
        messages = [
            Message(
                role="assistant",
                content=[TextContent(text=final_text)] if final_text else [],
                model=model,
                error=None,
                parent_tool_use_id=None,
            )
        ]

        if log_fh:
            _write_log(
                log_fh,
                "result",
                num_turns=1,
                duration_ms=duration_ms,
            )

        return AgentResponse(
            result=final_text,
            parsed=None,
            messages=messages,
            metrics=metrics,
            is_error=False,
        )

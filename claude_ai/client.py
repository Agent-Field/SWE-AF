"""ClaudeAI — typed async wrapper around claude_agent_sdk."""

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

from claude_agent_sdk import (
    AssistantMessage as _AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage as _ResultMessage,
    TextBlock as _TextBlock,
    ThinkingBlock as _ThinkingBlock,
    ToolResultBlock as _ToolResultBlock,
    ToolUseBlock as _ToolUseBlock,
    query as _query,
)

from claude_ai.types import (
    ClaudeResponse,
    Content,
    ErrorKind,
    Message,
    Metrics,
    TextContent,
    ThinkingContent,
    Tool,
    ToolResultContent,
    ToolUseContent,
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
    low = error.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


def _convert_content(block: Any) -> Content:
    if isinstance(block, _TextBlock):
        return TextContent(text=block.text)
    if isinstance(block, _ToolUseBlock):
        return ToolUseContent(id=block.id, name=block.name, input=block.input)
    if isinstance(block, _ToolResultBlock):
        return ToolResultContent(
            tool_use_id=block.tool_use_id,
            content=block.content,
            is_error=block.is_error or False,
        )
    if isinstance(block, _ThinkingBlock):
        return ThinkingContent(thinking=block.thinking, signature=block.signature)
    return TextContent(text=str(block)[:500])


def _schema_output_path(cwd: str) -> str:
    """Generate a unique temp file path for structured JSON output."""
    name = f".claude_output_{uuid.uuid4().hex[:12]}.json"
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
        with open(path, "r") as f:
            raw = f.read()
        text = raw.strip()
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
    if isinstance(c, ToolUseContent):
        return {"type": "tool_use", "name": c.name, "id": c.id}
    if isinstance(c, ToolResultContent):
        return {"type": "tool_result", "tool_use_id": c.tool_use_id,
                "is_error": c.is_error}
    if isinstance(c, ThinkingContent):
        return {"type": "thinking", "length": len(c.thinking)}
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
class ClaudeAIConfig:
    """Configuration for a ClaudeAI client."""

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


class ClaudeAI:
    """
    Async client for invoking Claude Code as an AI agent.

    Usage:
        ai = ClaudeAI(ClaudeAIConfig(model="sonnet", cwd="/path/to/repo"))

        # Plain text response
        resp = await ai.run("Explain the auth module.")
        print(resp.text)

        # Structured output — agent writes JSON to file, we parse it
        resp = await ai.run("List all TODO items.", output_schema=TodoList)
        print(resp.parsed)  # TodoList instance
    """

    def __init__(self, config: ClaudeAIConfig | None = None) -> None:
        self.config = config or ClaudeAIConfig()

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
    ) -> ClaudeResponse[T]:
        """
        Run a prompt through Claude Code.

        When `output_schema` is provided, the agent is instructed to write
        its structured JSON output to a temp file. We read and validate it.
        If that fails, a lightweight backup agent re-reads the work and
        produces just the JSON.
        """
        cfg = self.config
        effective_model = model or cfg.model
        effective_cwd = str(cwd or cfg.cwd)
        effective_turns = max_turns or cfg.max_turns
        effective_tools = allowed_tools if allowed_tools is not None else list(cfg.allowed_tools)
        effective_retries = max_retries if max_retries is not None else cfg.max_retries
        effective_env = {**cfg.env, **(env or {})}
        effective_system = system_prompt or cfg.system_prompt
        effective_budget = max_budget_usd or cfg.max_budget_usd
        effective_perm = permission_mode or cfg.permission_mode

        # If schema requested, prepare file path and augment prompt + tools
        output_path: str | None = None
        final_prompt = prompt
        if output_schema:
            output_path = _schema_output_path(effective_cwd)
            schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
            final_prompt = prompt + _build_schema_suffix(output_path, schema_json)
            # Ensure Write is available so the agent can create the file
            for t in _SCHEMA_FILE_TOOLS:
                if t not in effective_tools:
                    effective_tools.append(t)

        # Build SDK options
        # Capture stderr from the claude subprocess so errors are diagnosable
        _stderr_lines: list[str] = []

        def _stderr_callback(line: str) -> None:
            _stderr_lines.append(line)
            # Keep only last 200 lines to bound memory
            if len(_stderr_lines) > 200:
                _stderr_lines.pop(0)

        opts_kwargs: dict[str, Any] = {
            "model": effective_model,
            "cwd": effective_cwd,
            "max_turns": effective_turns,
            "stderr": _stderr_callback,
        }
        if effective_tools:
            opts_kwargs["allowed_tools"] = effective_tools
        if effective_system:
            opts_kwargs["system_prompt"] = effective_system
        if effective_budget:
            opts_kwargs["max_budget_usd"] = effective_budget
        if effective_perm:
            opts_kwargs["permission_mode"] = effective_perm
        if effective_env:
            opts_kwargs["env"] = effective_env

        options = ClaudeAgentOptions(**opts_kwargs)

        # Track all temp files we create so we can guarantee cleanup
        _temp_files: list[str] = []
        if output_path:
            _temp_files.append(output_path)

        log_fh = _open_log(log_file)
        try:
            return await self._run_with_retries(
                prompt=prompt,
                final_prompt=final_prompt,
                options=options,
                output_schema=output_schema,
                output_path=output_path,
                effective_cwd=effective_cwd,
                effective_model=effective_model,
                effective_env=effective_env,
                effective_perm=effective_perm,
                effective_retries=effective_retries,
                temp_files=_temp_files,
                log_fh=log_fh,
                stderr_lines=_stderr_lines,
            )
        finally:
            if log_fh:
                log_fh.close()
            _cleanup_files(_temp_files)

    async def _run_with_retries(
        self,
        *,
        prompt: str,
        final_prompt: str,
        options: ClaudeAgentOptions,
        output_schema: Type[T] | None,
        output_path: str | None,
        effective_cwd: str,
        effective_model: str,
        effective_env: dict[str, str],
        effective_perm: str | None,
        effective_retries: int,
        temp_files: list[str],
        log_fh: IO[str] | None = None,
        stderr_lines: list[str] | None = None,
    ) -> ClaudeResponse[T]:
        cfg = self.config
        delay = cfg.initial_delay
        last_exc: Exception | None = None

        if log_fh:
            _write_log(log_fh, "start", prompt=prompt, model=options.model,
                       max_turns=options.max_turns)

        for attempt in range(effective_retries + 1):
            try:
                response = await self._execute(final_prompt, options, log_fh=log_fh)

                # No schema requested — return as-is
                if not output_schema or output_path is None:
                    if log_fh:
                        _write_log(log_fh, "end", is_error=response.is_error,
                                   num_turns=response.metrics.num_turns,
                                   cost_usd=response.metrics.total_cost_usd)
                    return response

                # Try reading the JSON file the agent wrote
                parsed = _read_and_parse_json_file(output_path, output_schema)
                if parsed is not None:
                    resp = ClaudeResponse(
                        result=response.result,
                        parsed=parsed,
                        messages=response.messages,
                        metrics=response.metrics,
                        is_error=False,
                    )
                    if log_fh:
                        _write_log(log_fh, "end", is_error=False,
                                   num_turns=response.metrics.num_turns,
                                   cost_usd=response.metrics.total_cost_usd)
                    return resp

                # Primary failed — run backup agent
                if log_fh:
                    _write_log(log_fh, "backup_start",
                               reason="structured output parse failed")

                backup_log_file = f"{log_fh.name}_backup" if log_fh else None
                backup_log_fh = _open_log(backup_log_file)
                try:
                    parsed = await self._backup_schema_agent(
                        original_prompt=prompt,
                        output_schema=output_schema,
                        cwd=effective_cwd,
                        model=effective_model,
                        env=effective_env,
                        perm=effective_perm,
                        temp_files=temp_files,
                        log_fh=backup_log_fh,
                    )
                finally:
                    if backup_log_fh:
                        backup_log_fh.close()

                if parsed is not None:
                    resp = ClaudeResponse(
                        result=response.result,
                        parsed=parsed,
                        messages=response.messages,
                        metrics=response.metrics,
                        is_error=False,
                    )
                    if log_fh:
                        _write_log(log_fh, "end", is_error=False,
                                   backup_used=True,
                                   num_turns=response.metrics.num_turns,
                                   cost_usd=response.metrics.total_cost_usd)
                    return resp

                # Both failed
                if log_fh:
                    _write_log(log_fh, "end", is_error=True,
                               reason="schema parse failed after backup")
                return ClaudeResponse(
                    result=response.result,
                    parsed=None,
                    messages=response.messages,
                    metrics=response.metrics,
                    is_error=True,
                )

            except Exception as e:
                last_exc = e
                _captured_stderr = "\n".join(stderr_lines[-50:]) if stderr_lines else ""
                if attempt < effective_retries and _is_transient(str(e)):
                    if log_fh:
                        _write_log(log_fh, "retry", attempt=attempt + 1,
                                   error=str(e), delay=delay,
                                   stderr=_captured_stderr or None)
                    if stderr_lines:
                        stderr_lines.clear()
                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay)
                    # Fresh output path for next attempt
                    if output_schema:
                        output_path = _schema_output_path(effective_cwd)
                        temp_files.append(output_path)
                        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
                        final_prompt = prompt + _build_schema_suffix(output_path, schema_json)
                    continue
                if log_fh:
                    _write_log(log_fh, "end", is_error=True, error=str(e),
                               stderr=_captured_stderr or None)
                raise

        raise last_exc  # type: ignore[misc]

    async def _backup_schema_agent(
        self,
        original_prompt: str,
        output_schema: Type[T],
        cwd: str,
        model: str,
        env: dict[str, str],
        perm: str | None,
        temp_files: list[str],
        log_fh: IO[str] | None = None,
    ) -> T | None:
        """
        Backup agent: tells a fresh agent that the previous agent already
        completed the work but failed to produce valid JSON. This agent
        inspects the cwd and writes just the JSON file.
        """
        output_path = _schema_output_path(cwd)
        temp_files.append(output_path)
        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)

        backup_prompt = (
            f"A previous agent was given the following task and has ALREADY completed "
            f"the work (files are written, changes are made). However, it failed to "
            f"produce the required structured JSON output.\n\n"
            f"Original task:\n{original_prompt}\n\n"
            f"Your ONLY job is to inspect the current state of the working directory, "
            f"understand what was done, and write a JSON file that accurately summarizes "
            f"the result.\n\n"
            f"Write the JSON to:\n  {output_path}\n\n"
            f"Schema:\n```json\n{schema_json}\n```\n\n"
            f"Write ONLY valid JSON — no markdown fences, no explanation. "
            f"Use the Write tool."
        )

        backup_tools = [Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP]
        opts_kwargs: dict[str, Any] = {
            "model": model,
            "cwd": cwd,
            "max_turns": 5,
            "allowed_tools": backup_tools,
        }
        if env:
            opts_kwargs["env"] = env
        if perm:
            opts_kwargs["permission_mode"] = perm

        options = ClaudeAgentOptions(**opts_kwargs)

        if log_fh:
            _write_log(log_fh, "start", prompt="[backup schema agent]",
                       model=model, max_turns=5)

        try:
            async for msg in _query(prompt=backup_prompt, options=options):
                if log_fh and isinstance(msg, _AssistantMessage):
                    content = [_convert_content(b) for b in (msg.content or [])]
                    _write_log(log_fh, "assistant", turn="backup",
                               content=[_content_to_dict(c) for c in content])
        except Exception:
            pass

        if log_fh:
            _write_log(log_fh, "end")

        return _read_and_parse_json_file(output_path, output_schema)

    async def _execute(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        *,
        log_fh: IO[str] | None = None,
    ) -> ClaudeResponse[Any]:
        """Execute a single query against the SDK and map to ClaudeResponse."""
        messages: list[Message] = []
        result_text: str | None = None
        metrics_data: dict[str, Any] = {}
        turn = 0

        async for msg in _query(prompt=prompt, options=options):
            if isinstance(msg, _AssistantMessage):
                turn += 1
                content = [_convert_content(b) for b in (msg.content or [])]
                error = ErrorKind(msg.error) if msg.error else None
                messages.append(
                    Message(
                        role="assistant",
                        content=content,
                        model=msg.model,
                        error=error,
                        parent_tool_use_id=msg.parent_tool_use_id,
                    )
                )
                if log_fh:
                    _write_log(log_fh, "assistant", turn=turn,
                               model=msg.model,
                               content=[_content_to_dict(c) for c in content])
            elif isinstance(msg, _ResultMessage):
                result_text = msg.result
                metrics_data = {
                    "duration_ms": msg.duration_ms,
                    "duration_api_ms": msg.duration_api_ms,
                    "num_turns": msg.num_turns,
                    "total_cost_usd": msg.total_cost_usd,
                    "usage": msg.usage,
                    "session_id": msg.session_id,
                }
                if log_fh:
                    _write_log(log_fh, "result",
                               num_turns=msg.num_turns,
                               cost_usd=msg.total_cost_usd,
                               duration_ms=msg.duration_ms)

        metrics = Metrics(**metrics_data) if metrics_data else Metrics(
            duration_ms=0, duration_api_ms=0, num_turns=0,
            total_cost_usd=None, usage=None, session_id="",
        )

        is_error = metrics_data.get("is_error", False) if metrics_data else False

        return ClaudeResponse(
            result=result_text,
            parsed=None,
            messages=messages,
            metrics=metrics,
            is_error=is_error,
        )

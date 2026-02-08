"""Typed schema for Claude AI responses and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class Tool(str, Enum):
    """Available Claude Code tools."""

    READ = "Read"
    WRITE = "Write"
    EDIT = "Edit"
    BASH = "Bash"
    GLOB = "Glob"
    GREP = "Grep"
    NOTEBOOK_EDIT = "NotebookEdit"
    TASK = "Task"
    WEB_FETCH = "WebFetch"
    WEB_SEARCH = "WebSearch"


class Model(str, Enum):
    """Available Claude models."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


class ErrorKind(str, Enum):
    """Error categories from the SDK."""

    AUTH = "authentication_failed"
    BILLING = "billing_error"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    SERVER = "server_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextContent:
    """Text block from assistant."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseContent:
    """Tool invocation by assistant."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultContent:
    """Result returned from a tool."""

    tool_use_id: str
    content: str | list[dict[str, Any]] | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ThinkingContent:
    """Extended thinking block."""

    thinking: str
    signature: str


Content = TextContent | ToolUseContent | ToolResultContent | ThinkingContent


@dataclass(frozen=True, slots=True)
class Message:
    """A single message in the conversation."""

    role: Literal["assistant"]
    content: list[Content]
    model: str
    error: ErrorKind | None = None
    parent_tool_use_id: str | None = None


@dataclass(frozen=True, slots=True)
class Metrics:
    """Execution metrics."""

    duration_ms: int
    duration_api_ms: int
    num_turns: int
    total_cost_usd: float | None
    usage: dict[str, Any] | None
    session_id: str


@dataclass(frozen=True, slots=True)
class ClaudeResponse[T]:
    """
    Typed response from a Claude AI invocation.

    Generic over T — when an output_schema is provided, `parsed` holds
    the validated Pydantic model instance. Otherwise it's None.
    """

    result: str | None
    parsed: T | None
    messages: list[Message]
    metrics: Metrics
    is_error: bool

    @property
    def text(self) -> str:
        """Last text content from the conversation, or result."""
        if self.result:
            return self.result
        for msg in reversed(self.messages):
            for block in reversed(msg.content):
                if isinstance(block, TextContent):
                    return block.text
        return ""

    @property
    def tool_uses(self) -> list[ToolUseContent]:
        """All tool invocations across messages."""
        out: list[ToolUseContent] = []
        for msg in self.messages:
            for block in msg.content:
                if isinstance(block, ToolUseContent):
                    out.append(block)
        return out

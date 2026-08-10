"""Shared runtime/provider normalization and mapping utilities."""

from __future__ import annotations

RUNTIME_VALUES = ("aforge", "claude_code", "open_code", "codex")


def normalize_runtime_provider(runtime: str) -> str:
    """Normalize user/runtime aliases to canonical runtime values."""
    value = (runtime or "").strip().lower()
    if value in {"claude_code", "claude", "claude-code"}:
        return "claude_code"
    if value in {"open_code", "opencode"}:
        return "open_code"
    if value in {"aforge", "aforge_v2", "aforge-v2"}:
        return "aforge"
    if value == "codex":
        return "codex"
    raise ValueError(f"Unsupported runtime provider: {runtime}")


def runtime_to_harness_provider(runtime: str) -> str:
    """Map canonical runtime to harness provider value."""
    normalized = normalize_runtime_provider(runtime)
    if normalized == "claude_code":
        return "claude"
    if normalized == "open_code":
        return "opencode"
    if normalized == "aforge":
        return "aforge"
    return "codex"


def runtime_to_harness_adapter(runtime: str) -> str:
    """Map runtime aliases to AgentField harness adapter values."""
    normalized = normalize_runtime_provider(runtime)
    if normalized == "claude_code":
        return "claude-code"
    if normalized == "open_code":
        return "opencode"
    if normalized == "aforge":
        return "aforge"
    return "codex"

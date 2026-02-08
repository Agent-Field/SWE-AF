"""Codex CLI adapter helpers."""

from __future__ import annotations

import json
from typing import Any

CLAUDE_ALIAS_MODELS = {"haiku", "sonnet", "opus"}


def should_pass_model(model: str | None) -> bool:
    """Only pass model to codex when it is not a Claude alias."""
    if not model:
        return False
    return model.lower() not in CLAUDE_ALIAS_MODELS


def build_codex_command(
    *,
    codex_bin: str,
    cwd: str,
    prompt: str,
    model: str | None,
    output_schema_path: str | None,
    output_last_message_path: str,
) -> list[str]:
    """Build the codex CLI argv list."""
    cmd = [
        codex_bin,
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        cwd,
        "--skip-git-repo-check",
        "--output-last-message",
        output_last_message_path,
    ]
    if should_pass_model(model):
        cmd.extend(["-m", model])
    if output_schema_path:
        cmd.extend(["--output-schema", output_schema_path])
    cmd.append(prompt)
    return cmd


def parse_codex_jsonl(stdout_text: str) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """Parse codex JSONL event stream for final message and usage."""
    final_text: str | None = None
    usage: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    final_text = item["text"]
            elif event.get("type") == "turn.completed":
                turn_usage = event.get("usage")
                if isinstance(turn_usage, dict):
                    usage = turn_usage

    return final_text, usage, events

"""Grok structured-output patch (Codex-patch architecture, no silent fallback).

AgentField's default schema path asks the model to Write ``.agentfield_output.json``.
Grok headless runs (and SWE-AF roles without a Write tool / with read-only
permission) cannot create that file, so structured parsing fails.

This module mirrors ``codex_harness_patch``:

1. When ``provider=grok``, replace the Write-tool prompt suffix with a
   "return final JSON as the answer" contract (schema file is written for
   reference only).
2. After a Grok CLI run, persist non-empty final message text to
   ``.agentfield_output.json`` when a schema file is present — same role as
   Codex ``--output-last-message``. Transport only: empty results are left
   alone and the harness fails loudly.

Non-goals:

- No business-level deterministic approve/fix synthesis.
- No automatic re-run with weaker settings when parse fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PATCHED = False

_ORIGINAL_BUILD_PROMPT_SUFFIX: Any = None
_ORIGINAL_GROK_EXECUTE: Any = None


def _grok_build_prompt_suffix(schema: Any, cwd: str) -> str:
    """Codex-style suffix: final JSON answer, no Write-tool requirement."""
    from agentfield.harness import _schema

    json_schema = _schema.schema_to_json_schema(schema)
    schema_json = json.dumps(json_schema, indent=2)
    _schema.write_schema_file(schema_json, cwd)
    schema_path = _schema.get_schema_path(cwd)
    return (
        "\n\n---\n"
        "CRITICAL GROK STRUCTURED OUTPUT REQUIREMENTS:\n"
        f"Return a single final JSON object conforming to the schema at: {schema_path}\n"
        "Do not use markdown fences, comments, or surrounding prose.\n"
        "Do not try to create .agentfield_output.json yourself with a Write tool; "
        "emit the JSON object as your final response text. AgentField will capture "
        "that final response for schema validation.\n"
        f"Required JSON Schema:\n{schema_json}\n"
    )


def apply_grok_harness_patch() -> None:
    """Install Grok structured-output hooks. Idempotent."""
    global _PATCHED, _ORIGINAL_BUILD_PROMPT_SUFFIX, _ORIGINAL_GROK_EXECUTE
    if _PATCHED:
        return

    # Ensure Codex patch is installed first so ``active_provider`` is set on
    # Agent.harness and codex dispatch remains intact for codex calls.
    from swe_af.runtime.codex_harness_patch import (
        active_provider,
        apply_codex_harness_patch,
    )

    apply_codex_harness_patch()

    try:
        from agentfield.harness import _runner, _schema
        from agentfield.harness.providers.grok import GrokProvider
    except Exception:
        return

    _ORIGINAL_BUILD_PROMPT_SUFFIX = _schema.build_prompt_suffix

    def build_prompt_suffix_dispatching(schema: Any, cwd: str) -> str:
        if active_provider.get() == "grok":
            return _grok_build_prompt_suffix(schema, cwd)
        return _ORIGINAL_BUILD_PROMPT_SUFFIX(schema, cwd)

    _ORIGINAL_GROK_EXECUTE = GrokProvider.execute

    async def execute_with_final_message_persist(
        self: Any, prompt: str, options: dict[str, object]
    ) -> Any:
        raw = await _ORIGINAL_GROK_EXECUTE(self, prompt, options)

        cwd = options.get("cwd") or options.get("project_dir")
        if not isinstance(cwd, str) or not cwd.strip():
            return raw

        schema_path = _schema.get_schema_path(cwd)
        if not Path(schema_path).exists():
            return raw

        text = raw.result if isinstance(getattr(raw, "result", None), str) else None
        if not text or not text.strip():
            return raw

        output_path = _schema.get_output_path(cwd)
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(text, encoding="utf-8")
        except OSError:
            return raw

        return raw

    _schema.build_prompt_suffix = build_prompt_suffix_dispatching
    _runner.build_prompt_suffix = build_prompt_suffix_dispatching
    GrokProvider.execute = execute_with_final_message_persist  # type: ignore[method-assign]
    _PATCHED = True

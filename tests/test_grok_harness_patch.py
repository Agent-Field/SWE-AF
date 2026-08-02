"""Tests for Grok structured-output patch (Codex-style, no silent fallback)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from swe_af.runtime.codex_harness_patch import active_provider
from swe_af.runtime.grok_harness_patch import apply_grok_harness_patch


class _TinySchema(BaseModel):
    action: str
    summary: str = ""


def test_grok_prompt_suffix_rejects_write_tool_instruction(tmp_path: Path) -> None:
    from agentfield.harness import _schema

    apply_grok_harness_patch()
    token = active_provider.set("grok")
    try:
        suffix = _schema.build_prompt_suffix(_TinySchema, str(tmp_path))
    finally:
        active_provider.reset(token)

    assert "CRITICAL GROK STRUCTURED OUTPUT REQUIREMENTS" in suffix
    assert "Do not try to create .agentfield_output.json yourself" in suffix
    assert "Write tool" in suffix
    # Must not require Write-to-file as the only path (AF default language).
    assert "You MUST use your Write tool to create this file" not in suffix
    assert (tmp_path / ".agentfield_schema.json").exists()


def test_non_grok_provider_keeps_original_or_codex_suffix(tmp_path: Path) -> None:
    from agentfield.harness import _schema

    apply_grok_harness_patch()
    token = active_provider.set("claude-code")
    try:
        suffix = _schema.build_prompt_suffix(_TinySchema, str(tmp_path))
    finally:
        active_provider.reset(token)

    assert "CRITICAL GROK STRUCTURED OUTPUT REQUIREMENTS" not in suffix


def test_grok_execute_persists_final_message_when_schema_present(tmp_path: Path) -> None:
    from agentfield.harness import _schema
    from agentfield.harness._result import FailureType, Metrics, RawResult
    from agentfield.harness.providers.grok import GrokProvider
    from swe_af.runtime import grok_harness_patch as patch_mod

    apply_grok_harness_patch()
    # Seed schema file (as build_prompt_suffix would).
    _schema.write_schema_file(
        '{"type":"object","properties":{"action":{"type":"string"}}}',
        str(tmp_path),
    )

    final_json = '{"action":"approve","summary":"ok"}'
    original = AsyncMock(
        return_value=RawResult(
            result=final_json,
            messages=[],
            metrics=Metrics(),
            is_error=False,
            failure_type=FailureType.NONE,
            returncode=0,
        )
    )
    # Replace the stored original so our wrap calls this mock.
    patch_mod._ORIGINAL_GROK_EXECUTE = original

    provider = GrokProvider()
    raw = asyncio.run(
        provider.execute(
            "task",
            {"cwd": str(tmp_path), "model": "grok-4.5"},
        )
    )

    assert raw.result == final_json
    out = tmp_path / ".agentfield_output.json"
    assert out.exists()
    assert out.read_text(encoding="utf-8") == final_json
    original.assert_awaited_once()


def test_grok_execute_does_not_invent_output_when_result_empty(tmp_path: Path) -> None:
    from agentfield.harness import _schema
    from agentfield.harness._result import FailureType, Metrics, RawResult
    from agentfield.harness.providers.grok import GrokProvider
    from swe_af.runtime import grok_harness_patch as patch_mod

    apply_grok_harness_patch()
    _schema.write_schema_file(
        '{"type":"object","properties":{"action":{"type":"string"}}}',
        str(tmp_path),
    )

    original = AsyncMock(
        return_value=RawResult(
            result=None,
            messages=[],
            metrics=Metrics(),
            is_error=True,
            error_message="no output",
            failure_type=FailureType.CRASH,
            returncode=1,
        )
    )
    patch_mod._ORIGINAL_GROK_EXECUTE = original

    provider = GrokProvider()
    raw = asyncio.run(
        provider.execute("task", {"cwd": str(tmp_path), "model": "grok-4.5"})
    )

    assert raw.is_error is True
    assert not (tmp_path / ".agentfield_output.json").exists()

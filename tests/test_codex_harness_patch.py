from __future__ import annotations

from swe_af.runtime.codex_harness_patch import (
    _augment_codex_error_message,
    _codex_strict_json_schema,
    active_provider,
    apply_codex_harness_patch,
)


def test_codex_strict_json_schema_requires_all_object_properties() -> None:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "default": ""},
            "files_changed": {"type": "array", "items": {"type": "string"}},
        },
    }

    strict = _codex_strict_json_schema(schema)

    assert strict["required"] == ["summary", "files_changed"]
    assert strict["additionalProperties"] is False
    assert "default" not in strict["properties"]["summary"]


def test_codex_strict_json_schema_recurses_into_defs() -> None:
    schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "default": 1},
                },
                "required": ["name"],
            }
        },
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
        },
    }

    strict = _codex_strict_json_schema(schema)

    item = strict["$defs"]["Item"]
    assert item["required"] == ["name", "count"]
    assert item["additionalProperties"] is False
    assert "default" not in item["properties"]["count"]


def test_codex_git_metadata_error_gets_actionable_hint() -> None:
    message = _augment_codex_error_message(
        "fatal: cannot create .git/index.lock",
        "fatal: cannot create .git/index.lock",
    )

    assert "Codex tried to mutate git metadata under workspace-write" in message
    assert "git must be host-managed" in message


def test_codex_unrelated_error_is_unchanged() -> None:
    assert _augment_codex_error_message("plain error", "plain error") == "plain error"


def test_codex_prompt_suffix_uses_final_json_not_write_tool(tmp_path) -> None:
    from agentfield.harness import _schema

    apply_codex_harness_patch()

    token = active_provider.set("codex")
    try:
        suffix = _schema.build_prompt_suffix(
            {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            str(tmp_path),
        )
    finally:
        active_provider.reset(token)

    assert "Return a single final JSON object" in suffix
    assert "Write tool" not in suffix
    assert (tmp_path / ".agentfield_schema.json").exists()


def test_non_codex_prompt_suffix_keeps_agentfield_write_tool_default(tmp_path) -> None:
    """For claude_code / open_code calls, build_prompt_suffix must return the
    original AgentField suffix that instructs the agent to use its Write tool.

    Without this gate the codex-native suffix would leak into every harness
    call, forcing claude/opencode runs onto the slower stdout-parse fallback.
    """
    from agentfield.harness import _schema

    apply_codex_harness_patch()

    # No active provider set ⇒ default suffix.
    suffix = _schema.build_prompt_suffix(
        {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        },
        str(tmp_path),
    )

    assert "Write tool" in suffix
    assert "Codex CLI" not in suffix

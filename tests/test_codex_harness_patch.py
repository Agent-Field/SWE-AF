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


def test_run_codex_cli_spawns_via_resolved_command(monkeypatch) -> None:
    """The spawn must route cmd[0] through the SDK's resolve_cli_command.

    On Windows, npm installs the codex CLI only as a .cmd shim, and
    CreateProcess does no PATHEXT resolution — spawning the bare name fails
    with FileNotFoundError ([WinError 2]) on every codex harness call.
    resolve_cli_command resolves the shim's real path (no-op on POSIX).
    """
    import asyncio

    import agentfield.harness._cli as sdk_cli

    from swe_af.runtime.codex_harness_patch import _run_codex_cli_with_stdin

    spawned: dict = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
            spawned["stdin"] = data
            return b"", b""

    async def fake_exec(program: str, *args: str, **kwargs: object) -> FakeProc:
        spawned["program"] = program
        spawned["args"] = args
        return FakeProc()

    monkeypatch.setattr(sdk_cli, "resolve_cli_command", lambda name: f"resolved::{name}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    stdout, stderr, returncode = asyncio.run(
        _run_codex_cli_with_stdin(
            ["codex", "exec", "--json"], "prompt", env=None, cwd=None
        )
    )

    assert spawned["program"] == "resolved::codex"
    assert spawned["args"][:2] == ("exec", "--json")
    assert spawned["stdin"] == b"prompt"
    assert (stdout, stderr, returncode) == ("", "", 0)


# The real error event codex exec --json emits when the server's strict
# validator refuses --output-schema (captured live on codex-cli 0.144.1).
_REJECTION_STDOUT = (
    '{"type":"error","message":"{\\n  \\"type\\": \\"error\\",\\n  \\"error\\": {\\n'
    '    \\"type\\": \\"invalid_request_error\\",\\n'
    '    \\"code\\": \\"invalid_json_schema\\",\\n'
    '    \\"message\\": \\"Invalid schema for response_format '
    "'codex_output_schema'. Please ensure it is a valid JSON Schema.\\\",\\n"
    '    \\"param\\": \\"text.format.schema\\"\\n  },\\n  \\"status\\": 400\\n}"}'
)

_SUCCESS_STDOUT = (
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    '"text":"{\\"ok\\":true}"}}'
)


def _strict(schema: dict) -> dict:
    from swe_af.runtime.codex_harness_patch import _codex_strict_json_schema

    return _codex_strict_json_schema(schema)


def test_strict_expressible_accepts_plain_and_nested_strict_objects() -> None:
    from swe_af.runtime.codex_harness_patch import _codex_schema_strict_expressible

    assert _codex_schema_strict_expressible(
        _strict(
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "when": {"type": ["string", "null"]},
                },
                "$defs": {
                    "Sub": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                    }
                },
            }
        )
    )


def test_strict_expressible_rejects_free_form_and_typed_maps_and_any() -> None:
    from swe_af.runtime.codex_harness_patch import _codex_schema_strict_expressible

    base = {"type": "object", "properties": {"status": {"type": "string"}}}

    free_form = _strict({**base, "properties": {"meta": {"type": "object"}}})
    typed_map = _strict(
        {
            **base,
            "properties": {
                "meta": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                }
            },
        }
    )
    bare_any = _strict({**base, "properties": {"value": {}}})
    boolean_subschema = _strict({**base, "properties": {"value": True}})
    poisoned_defs = _strict(
        {
            **base,
            "properties": {"sub": {"$ref": "#/$defs/Sub"}},
            "$defs": {"Sub": {"type": "object"}},
        }
    )
    object_in_type_list = _strict(
        {**base, "properties": {"v": {"type": ["object", "null"]}}}
    )

    for schema in (
        free_form,
        typed_map,
        bare_any,
        boolean_subschema,
        poisoned_defs,
        object_in_type_list,
    ):
        assert not _codex_schema_strict_expressible(schema)


def _run_patched_execute(tmp_path, monkeypatch, source_schema, run_results):
    """Drive the patched CodexProvider.execute with a fake CLI runner.

    Writes the strict schema file the way the codex-native prompt suffix does,
    then records every spawned command and pops results from ``run_results``.
    Returns (RawResult, recorded_cmds).
    """
    import asyncio

    from agentfield.harness import _schema
    from agentfield.harness.providers.codex import CodexProvider

    import swe_af.runtime.codex_harness_patch as patch_mod

    apply_codex_harness_patch()

    token = active_provider.set("codex")
    try:
        _schema.build_prompt_suffix(source_schema, str(tmp_path))
    finally:
        active_provider.reset(token)

    cmds: list[list[str]] = []

    async def fake_run(cmd, prompt, *, env, cwd):
        cmds.append(list(cmd))
        return run_results[min(len(cmds) - 1, len(run_results) - 1)]

    monkeypatch.setattr(patch_mod, "_run_codex_cli_with_stdin", fake_run)

    provider = CodexProvider.__new__(CodexProvider)
    provider._bin = "codex"
    raw = asyncio.run(provider.execute("prompt", {"cwd": str(tmp_path)}))
    return raw, cmds


def test_execute_reruns_without_output_schema_on_server_rejection(
    tmp_path, monkeypatch
) -> None:
    """A schema the strict validator refuses server-side triggers exactly one
    rerun without --output-schema, keeping --output-last-message, and the
    retry's output becomes the result."""
    raw, cmds = _run_patched_execute(
        tmp_path,
        monkeypatch,
        {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        [(_REJECTION_STDOUT, "", 1), (_SUCCESS_STDOUT, "", 0)],
    )

    assert len(cmds) == 2
    assert "--output-schema" in cmds[0]
    assert "--output-schema" not in cmds[1]
    assert "--output-last-message" in cmds[1]
    assert raw.is_error is False
    assert raw.result == '{"ok":true}'


def test_execute_skips_output_schema_for_inexpressible_schema(
    tmp_path, monkeypatch
) -> None:
    """A schema strict mode cannot express (free-form dict field) must never be
    sent through --output-schema — one invocation, last-message only."""
    raw, cmds = _run_patched_execute(
        tmp_path,
        monkeypatch,
        {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "meta": {"type": "object"},
            },
        },
        [(_SUCCESS_STDOUT, "", 0)],
    )

    assert len(cmds) == 1
    assert "--output-schema" not in cmds[0]
    assert "--output-last-message" in cmds[0]
    assert raw.is_error is False


def test_execute_does_not_rerun_on_unrelated_failure(tmp_path, monkeypatch) -> None:
    raw, cmds = _run_patched_execute(
        tmp_path,
        monkeypatch,
        {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        [("", "stream error: something unrelated", 1)],
    )

    assert len(cmds) == 1
    assert "--output-schema" in cmds[0]
    assert raw.is_error is True

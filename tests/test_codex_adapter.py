import unittest

from agent_ai.providers.codex.adapter import (
    build_codex_command,
    normalize_schema_for_codex,
    parse_codex_jsonl,
)


class CodexAdapterTests(unittest.TestCase):
    def test_build_command_omits_model_for_claude_alias(self) -> None:
        cmd = build_codex_command(
            codex_bin="codex",
            cwd=".",
            prompt="hello",
            model="sonnet",
            output_schema_path=None,
            output_last_message_path="/tmp/out.json",
        )
        self.assertNotIn("-m", cmd)

    def test_build_command_includes_model_and_schema(self) -> None:
        cmd = build_codex_command(
            codex_bin="codex",
            cwd=".",
            prompt="hello",
            model="gpt-5.3-codex",
            output_schema_path="/tmp/schema.json",
            output_last_message_path="/tmp/out.json",
        )
        self.assertIn("-m", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertEqual(cmd[0:3], ["codex", "exec", "--json"])

    def test_parse_jsonl_extracts_last_message_and_usage(self) -> None:
        stream = "\n".join(
            [
                '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"ok\\":true}"}}',
                '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
            ]
        )
        final_text, usage, events = parse_codex_jsonl(stream)
        self.assertEqual(final_text, '{"ok":true}')
        self.assertEqual(usage["output_tokens"], 2)
        self.assertEqual(len(events), 2)

    def test_normalize_schema_sets_additional_properties_false(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "item": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            },
        }
        normalized = normalize_schema_for_codex(schema)
        self.assertIs(normalized["additionalProperties"], False)
        self.assertIs(
            normalized["properties"]["item"]["additionalProperties"],
            False,
        )
        self.assertEqual(normalized["required"], ["item"])
        self.assertEqual(
            normalized["properties"]["item"]["required"],
            ["name"],
        )


if __name__ == "__main__":
    unittest.main()

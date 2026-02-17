"""Tests for V2 runtime + flat model configuration."""

from __future__ import annotations

import unittest

from swe_af.execution.schemas import (
    ALL_MODEL_FIELDS,
    BuildConfig,
    ExecutionConfig,
    ROLE_TO_MODEL_FIELD,
    resolve_runtime_models,
)


class TestResolveRuntimeModels(unittest.TestCase):
    def test_claude_code_defaults(self) -> None:
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        for field in ALL_MODEL_FIELDS:
            if field == "qa_synthesizer_model":
                continue
            self.assertEqual(resolved[field], "sonnet")
        self.assertEqual(resolved["qa_synthesizer_model"], "haiku")

    def test_open_code_defaults(self) -> None:
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for field in ALL_MODEL_FIELDS:
            self.assertEqual(resolved[field], "minimax/minimax-m2.5")

    def test_models_default_applies_to_all(self) -> None:
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"default": "opus"},
        )
        for field in ALL_MODEL_FIELDS:
            self.assertEqual(resolved[field], "opus")

    def test_role_override_beats_default(self) -> None:
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"default": "sonnet", "coder": "opus"},
        )
        self.assertEqual(resolved["coder_model"], "opus")
        self.assertEqual(resolved["qa_model"], "sonnet")

    def test_invalid_runtime_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runtime_models(runtime="bad_runtime", models=None)

    def test_invalid_model_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runtime_models(runtime="claude_code", models={"bad": "opus"})


class TestBuildConfig(unittest.TestCase):
    def test_default_runtime_and_provider(self) -> None:
        cfg = BuildConfig()
        self.assertEqual(cfg.runtime, "claude_code")
        self.assertEqual(cfg.ai_provider, "claude")

    def test_open_code_runtime_provider(self) -> None:
        cfg = BuildConfig(runtime="open_code")
        self.assertEqual(cfg.ai_provider, "opencode")
        resolved = cfg.resolved_models()
        self.assertEqual(resolved["coder_model"], "minimax/minimax-m2.5")

    def test_to_execution_config_dict_roundtrips(self) -> None:
        cfg = BuildConfig(runtime="open_code", models={"coder": "deepseek/deepseek-chat"})
        d = cfg.to_execution_config_dict()
        self.assertEqual(d["runtime"], "open_code")
        self.assertEqual(d["models"]["coder"], "deepseek/deepseek-chat")
        exec_cfg = ExecutionConfig(**d)
        self.assertEqual(exec_cfg.coder_model, "deepseek/deepseek-chat")
        self.assertEqual(exec_cfg.qa_model, "minimax/minimax-m2.5")

    def test_legacy_top_level_keys_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BuildConfig(**{"ai_provider": "claude"})
        self.assertIn("ai_provider", str(ctx.exception))
        self.assertIn("runtime", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            BuildConfig(**{"coder_model": "opus"})
        self.assertIn("coder_model", str(ctx.exception))
        self.assertIn("models.coder", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            BuildConfig(**{"preset": "fast"})
        self.assertIn("preset", str(ctx.exception))
        self.assertIn("runtime + models", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            BuildConfig(**{"model": "opus"})
        self.assertIn("model", str(ctx.exception))
        self.assertIn("models.default", str(ctx.exception))

    def test_legacy_model_group_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BuildConfig(models={"planning": "opus"})
        self.assertIn("planning", str(ctx.exception))
        self.assertIn("models.pm", str(ctx.exception))


class TestExecutionConfig(unittest.TestCase):
    def test_default_resolution(self) -> None:
        cfg = ExecutionConfig()
        self.assertEqual(cfg.runtime, "claude_code")
        self.assertEqual(cfg.ai_provider, "claude")
        self.assertEqual(cfg.coder_model, "sonnet")
        self.assertEqual(cfg.qa_synthesizer_model, "haiku")

    def test_open_code_resolution(self) -> None:
        cfg = ExecutionConfig(runtime="open_code")
        self.assertEqual(cfg.ai_provider, "opencode")
        self.assertEqual(cfg.coder_model, "minimax/minimax-m2.5")
        self.assertEqual(cfg.qa_synthesizer_model, "minimax/minimax-m2.5")

    def test_models_override(self) -> None:
        cfg = ExecutionConfig(runtime="claude_code", models={"default": "sonnet", "qa": "opus"})
        self.assertEqual(cfg.qa_model, "opus")
        self.assertEqual(cfg.coder_model, "sonnet")

    def test_all_role_keys_resolve(self) -> None:
        models = {role: f"model-{role}" for role in ROLE_TO_MODEL_FIELD}
        cfg = ExecutionConfig(runtime="open_code", models=models)
        for role, field in ROLE_TO_MODEL_FIELD.items():
            self.assertEqual(getattr(cfg, field), f"model-{role}")

    def test_legacy_keys_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ExecutionConfig(**{"ai_provider": "claude"})
        self.assertIn("ai_provider", str(ctx.exception))
        self.assertIn("runtime", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            ExecutionConfig(**{"replan_model": "sonnet"})
        self.assertIn("replan_model", str(ctx.exception))
        self.assertIn("models.replan", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            ExecutionConfig(models={"coding": "opus"})
        self.assertIn("coding", str(ctx.exception))
        self.assertIn("models.coder", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

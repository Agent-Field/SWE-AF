"""Tests for V2 runtime + flat model configuration."""

from __future__ import annotations

import os
import unittest
from unittest import mock

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
            self.assertEqual(resolved[field], "openrouter/minimax/minimax-m2.5")

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
        self.assertEqual(resolved["coder_model"], "openrouter/minimax/minimax-m2.5")

    def test_to_execution_config_dict_roundtrips(self) -> None:
        cfg = BuildConfig(runtime="open_code", models={"coder": "deepseek/deepseek-chat"})
        d = cfg.to_execution_config_dict()
        self.assertEqual(d["runtime"], "open_code")
        self.assertEqual(d["models"]["coder"], "deepseek/deepseek-chat")
        exec_cfg = ExecutionConfig(**d)
        self.assertEqual(exec_cfg.coder_model, "deepseek/deepseek-chat")
        self.assertEqual(exec_cfg.qa_model, "openrouter/minimax/minimax-m2.5")

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


class TestDefaultRuntimeFromEnv(unittest.TestCase):
    """`SWE_DEFAULT_RUNTIME` lets the deployer pick runtime without callers
    threading a config through. Callers that DO pass `runtime=...` win."""

    def test_env_open_code_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_RUNTIME": "open_code"}):
            self.assertEqual(BuildConfig().runtime, "open_code")
            self.assertEqual(ExecutionConfig().runtime, "open_code")

    def test_env_claude_code_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_RUNTIME": "claude_code"}):
            self.assertEqual(BuildConfig().runtime, "claude_code")
            self.assertEqual(ExecutionConfig().runtime, "claude_code")

    def test_explicit_runtime_beats_env(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_RUNTIME": "open_code"}):
            self.assertEqual(BuildConfig(runtime="claude_code").runtime, "claude_code")
            self.assertEqual(ExecutionConfig(runtime="claude_code").runtime, "claude_code")

    def test_invalid_env_falls_back_to_claude_code(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_RUNTIME": "bogus_runtime"}):
            self.assertEqual(BuildConfig().runtime, "claude_code")

    def test_unset_env_uses_claude_code(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "SWE_DEFAULT_RUNTIME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(BuildConfig().runtime, "claude_code")

    def test_empty_env_uses_claude_code(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_RUNTIME": ""}):
            self.assertEqual(BuildConfig().runtime, "claude_code")


class TestDefaultModelFromEnv(unittest.TestCase):
    """`SWE_DEFAULT_MODEL` lets the deployer pin a single model id without
    code changes or threading config through every caller. Caller-supplied
    models still win at higher precedence layers."""

    def test_env_overrides_runtime_base_default(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "openrouter/minimax/minimax-m2.6"},
        ):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/minimax/minimax-m2.6"
                )

    def test_env_overrides_claude_code_runtime_too(self) -> None:
        with mock.patch.dict(os.environ, {"SWE_DEFAULT_MODEL": "opus"}):
            resolved = resolve_runtime_models(runtime="claude_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(resolved[field], "opus")

    def test_caller_models_default_beats_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "openrouter/minimax/minimax-m2.6"},
        ):
            resolved = resolve_runtime_models(
                runtime="open_code",
                models={"default": "openrouter/qwen/qwen-3-coder"},
            )
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/qwen/qwen-3-coder"
                )

    def test_caller_per_role_beats_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "openrouter/minimax/minimax-m2.6"},
        ):
            resolved = resolve_runtime_models(
                runtime="open_code",
                models={"coder": "openrouter/deepseek/deepseek-v3"},
            )
            self.assertEqual(
                resolved["coder_model"], "openrouter/deepseek/deepseek-v3"
            )
            # Other roles still pick up the env default
            self.assertEqual(
                resolved["pm_model"], "openrouter/minimax/minimax-m2.6"
            )

    def test_empty_env_value_treated_as_unset(self) -> None:
        # All three cascade vars empty/whitespace → falls through to runtime base
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "   ", "AI_MODEL": "", "HARNESS_MODEL": "   "},
        ):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/minimax/minimax-m2.5"
                )

    def test_unset_env_uses_runtime_base(self) -> None:
        cascade_vars = {"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"}
        env = {k: v for k, v in os.environ.items() if k not in cascade_vars}
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/minimax/minimax-m2.5"
                )

    def test_ai_model_env_used_when_swe_default_unset(self) -> None:
        # AI_MODEL is the env var the rest of the stack (pr-af, github-buddy)
        # uses — SWE-AF must respect it too without requiring a SWE-specific
        # var, otherwise the deployer has to set the same model in two places.
        cascade_vars = {"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"}
        env = {k: v for k, v in os.environ.items() if k not in cascade_vars}
        env["AI_MODEL"] = "openrouter/moonshotai/kimi-k2.6"
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/moonshotai/kimi-k2.6"
                )

    def test_harness_model_env_used_when_others_unset(self) -> None:
        cascade_vars = {"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"}
        env = {k: v for k, v in os.environ.items() if k not in cascade_vars}
        env["HARNESS_MODEL"] = "openrouter/moonshotai/kimi-k2.6"
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/moonshotai/kimi-k2.6"
                )

    def test_swe_default_model_beats_ai_model_in_cascade(self) -> None:
        # When both are set, the SWE-specific name wins so deployers can
        # override the global AI_MODEL just for this service.
        cascade_vars = {"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"}
        env = {k: v for k, v in os.environ.items() if k not in cascade_vars}
        env["SWE_DEFAULT_MODEL"] = "openrouter/qwen/qwen-3-coder"
        env["AI_MODEL"] = "openrouter/moonshotai/kimi-k2.6"
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = resolve_runtime_models(runtime="open_code", models=None)
            for field in ALL_MODEL_FIELDS:
                self.assertEqual(
                    resolved[field], "openrouter/qwen/qwen-3-coder"
                )

    def test_env_flows_through_build_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "openrouter/minimax/minimax-m2.6"},
        ):
            cfg = BuildConfig(runtime="open_code")
            resolved = cfg.resolved_models()
            self.assertEqual(
                resolved["coder_model"], "openrouter/minimax/minimax-m2.6"
            )

    def test_env_flows_through_execution_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SWE_DEFAULT_MODEL": "openrouter/minimax/minimax-m2.6"},
        ):
            cfg = ExecutionConfig(runtime="open_code")
            self.assertEqual(
                cfg.coder_model, "openrouter/minimax/minimax-m2.6"
            )
            self.assertEqual(
                cfg.qa_synthesizer_model, "openrouter/minimax/minimax-m2.6"
            )


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
        self.assertEqual(cfg.coder_model, "openrouter/minimax/minimax-m2.5")
        self.assertEqual(cfg.qa_synthesizer_model, "openrouter/minimax/minimax-m2.5")

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

    def test_ci_fixer_role_resolves(self) -> None:
        """ci_fixer is a real role with its own model field, defaulting to the
        runtime base. It can be overridden per-role like any other."""
        cfg = ExecutionConfig(runtime="claude_code")
        self.assertEqual(cfg.ci_fixer_model, "sonnet")

        cfg = ExecutionConfig(runtime="open_code")
        self.assertEqual(cfg.ci_fixer_model, "openrouter/minimax/minimax-m2.5")

        cfg = ExecutionConfig(
            runtime="claude_code", models={"ci_fixer": "opus"}
        )
        self.assertEqual(cfg.ci_fixer_model, "opus")
        # other roles untouched
        self.assertEqual(cfg.coder_model, "sonnet")


class TestCIGateConfig(unittest.TestCase):
    """check_ci defaults to True and its caps round-trip from BuildConfig
    into ExecutionConfig so the post-PR gate sees consistent settings."""

    def test_check_ci_defaults_true(self) -> None:
        self.assertTrue(BuildConfig().check_ci)
        self.assertTrue(ExecutionConfig().check_ci)

    def test_ci_gate_caps_have_sensible_defaults(self) -> None:
        cfg = BuildConfig()
        self.assertEqual(cfg.max_ci_fix_cycles, 2)
        self.assertEqual(cfg.ci_wait_seconds, 1500)
        self.assertEqual(cfg.ci_poll_seconds, 30)

    def test_ci_gate_caps_round_trip(self) -> None:
        cfg = BuildConfig(
            check_ci=False,
            max_ci_fix_cycles=5,
            ci_wait_seconds=600,
            ci_poll_seconds=15,
        )
        d = cfg.to_execution_config_dict()
        self.assertEqual(d["check_ci"], False)
        self.assertEqual(d["max_ci_fix_cycles"], 5)
        self.assertEqual(d["ci_wait_seconds"], 600)
        self.assertEqual(d["ci_poll_seconds"], 15)

        exec_cfg = ExecutionConfig(**d)
        self.assertFalse(exec_cfg.check_ci)
        self.assertEqual(exec_cfg.max_ci_fix_cycles, 5)
        self.assertEqual(exec_cfg.ci_wait_seconds, 600)
        self.assertEqual(exec_cfg.ci_poll_seconds, 15)


if __name__ == "__main__":
    unittest.main()

"""Tests for the per-tier model env vars (SWE_MODEL_LOW / _MED / _HIGH).

Validation contract:
- No tier envs set → resolution is unchanged for every runtime.
- A tier var applies to exactly the roles in its tier (see ROLE_TO_TIER).
- Tier vars beat the SWE_DEFAULT_MODEL → AI_MODEL → HARNESS_MODEL cascade,
  and lose to caller config (models["default"], models["<role>"]).
- SWE_MODEL_HIGH also wins the _default_planning_model cascade (the planning
  reasoners are high-tier roles).
"""

from __future__ import annotations

import pytest

from swe_af.execution.schemas import (
    ALL_MODEL_FIELDS,
    MODEL_TIERS,
    ROLE_TO_MODEL_FIELD,
    ROLE_TO_TIER,
    TIER_MODEL_ENV_VARS,
    _default_planning_model,
    resolve_runtime_models,
)

_HIGH_FIELDS = {"pm_model", "architect_model", "tech_lead_model", "replan_model"}
_LOW_FIELDS = {"qa_synthesizer_model", "git_model"}

_OPEN_CODE_BASE = "openrouter/minimax/minimax-m2.5"

# Env vars that steer provider/runtime/model selection. Cleared before every
# test so assertions never depend on the developer's ambient shell.
_STEERING_ENV_KEYS = (
    "SWE_MODEL_LOW",
    "SWE_MODEL_MED",
    "SWE_MODEL_HIGH",
    "SWE_DEFAULT_MODEL",
    "AI_MODEL",
    "HARNESS_MODEL",
    "SWE_DEFAULT_RUNTIME",
    "SWE_CODEX_AUTH_MODE",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _STEERING_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestNoTierEnvsUnchanged:
    """Contract: no tier envs set → resolution unchanged for all runtimes."""

    def test_claude_code_base_defaults(self) -> None:
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        for field in ALL_MODEL_FIELDS:
            expected = "haiku" if field == "qa_synthesizer_model" else "sonnet"
            assert resolved[field] == expected

    def test_open_code_base_defaults(self) -> None:
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for field in ALL_MODEL_FIELDS:
            assert resolved[field] == _OPEN_CODE_BASE

    def test_codex_base_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "api_key")
        resolved = resolve_runtime_models(runtime="codex", models=None)
        for field in ALL_MODEL_FIELDS:
            assert resolved[field] == "gpt-5.3-codex"


class TestTierEnvApplication:
    """Contract: a tier var applies to exactly the roles in its tier."""

    def test_high_only_changes_exactly_the_high_tier_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_MODEL_HIGH", "openrouter/z-ai/glm-5.2")
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for field in ALL_MODEL_FIELDS:
            if field in _HIGH_FIELDS:
                assert resolved[field] == "openrouter/z-ai/glm-5.2"
            else:
                assert resolved[field] == _OPEN_CODE_BASE

    def test_all_three_tiers_resolve_every_field_by_role_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tier_models = {
            "high": "openrouter/z-ai/glm-5.2",
            "med": "openrouter/deepseek/deepseek-v4-pro",
            "low": "openrouter/deepseek/deepseek-v4-flash",
        }
        for tier, model in tier_models.items():
            monkeypatch.setenv(TIER_MODEL_ENV_VARS[tier], model)
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for role, field in ROLE_TO_MODEL_FIELD.items():
            assert resolved[field] == tier_models[ROLE_TO_TIER[role]]

    def test_empty_tier_value_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_MODEL_HIGH", "   ")
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for field in ALL_MODEL_FIELDS:
            assert resolved[field] == _OPEN_CODE_BASE


class TestTierPrecedence:
    """Contract: tier vars beat the default-model env cascade and lose to
    caller config."""

    def test_models_default_beats_all_tier_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_MODEL_HIGH", "tier-high")
        monkeypatch.setenv("SWE_MODEL_MED", "tier-med")
        monkeypatch.setenv("SWE_MODEL_LOW", "tier-low")
        resolved = resolve_runtime_models(
            runtime="open_code", models={"default": "caller-default"}
        )
        for field in ALL_MODEL_FIELDS:
            assert resolved[field] == "caller-default"

    def test_models_role_beats_everything_for_that_role_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_MODEL_MED", "tier-med")
        resolved = resolve_runtime_models(
            runtime="open_code", models={"coder": "caller-coder"}
        )
        assert resolved["coder_model"] == "caller-coder"
        # Other med-tier roles still pick up the tier env value.
        assert resolved["qa_model"] == "tier-med"

    def test_tier_var_beats_default_env_cascade_for_its_roles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_DEFAULT_MODEL", "env-default")
        monkeypatch.setenv("AI_MODEL", "env-ai-model")
        monkeypatch.setenv("SWE_MODEL_HIGH", "tier-high")
        resolved = resolve_runtime_models(runtime="open_code", models=None)
        for field in ALL_MODEL_FIELDS:
            if field in _HIGH_FIELDS:
                assert resolved[field] == "tier-high"
            else:
                # Unset tiers still get the cascade winner (SWE_DEFAULT_MODEL).
                assert resolved[field] == "env-default"


class TestDefaultPlanningModelHighTier:
    """Contract: SWE_MODEL_HIGH wins the planning-model cascade; without it
    the prior behavior is unchanged."""

    def test_high_tier_var_wins_over_default_model_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_DEFAULT_MODEL", "env-default")
        monkeypatch.setenv("SWE_MODEL_HIGH", "tier-high")
        assert _default_planning_model() == "tier-high"

    def test_unset_high_tier_falls_back_to_env_cascade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_DEFAULT_MODEL", "env-default")
        assert _default_planning_model() == "env-default"

    def test_whitespace_high_tier_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SWE_MODEL_HIGH", "   ")
        assert _default_planning_model() == "sonnet"

    def test_no_env_at_all_defaults_to_sonnet(self) -> None:
        assert _default_planning_model() == "sonnet"


class TestTierMappingCompleteness:
    """Contract: every role has a tier, and every tier is a known tier."""

    def test_every_role_has_a_tier(self) -> None:
        assert set(ROLE_TO_TIER) == set(ROLE_TO_MODEL_FIELD)

    def test_every_tier_value_is_known(self) -> None:
        assert set(ROLE_TO_TIER.values()) <= set(MODEL_TIERS)
        assert set(TIER_MODEL_ENV_VARS) == set(MODEL_TIERS)

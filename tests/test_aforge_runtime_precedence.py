"""AForge defaults coexist with the gateways and overrides on main."""

import pytest

from swe_af.execution.schemas import BuildConfig, ExecutionConfig, resolve_runtime_models
from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models
from swe_af.issue.schemas import IssueBuildConfig


@pytest.fixture(autouse=True)
def clean_runtime_env(monkeypatch):
    for key in (
        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "INFRON_API_KEY",
        "SWE_DEFAULT_RUNTIME", "SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL",
        "SWE_MODEL_HIGH", "SWE_MODEL_MED", "SWE_MODEL_LOW",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("config_type", [BuildConfig, ExecutionConfig, IssueBuildConfig, FastBuildConfig])
@pytest.mark.parametrize("other_keys", [(), ("ANTHROPIC_API_KEY",), ("INFRON_API_KEY",),
                                      ("ANTHROPIC_API_KEY", "INFRON_API_KEY")])
def test_openrouter_default_with_other_gateways(config_type, other_keys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    monkeypatch.setenv("SWE_DEFAULT_RUNTIME", " \t")
    for key in other_keys:
        monkeypatch.setenv(key, "test-key")
    config = config_type()
    assert config.runtime == "aforge"
    if isinstance(config, FastBuildConfig):
        models = fast_resolve_models(config)
    else:
        models = resolve_runtime_models(runtime=config.runtime, models=config.models)
    assert set(models.values()) == {"openrouter/deepseek/deepseek-v4-flash-0731"}


@pytest.mark.parametrize("config_type", [BuildConfig, ExecutionConfig, IssueBuildConfig, FastBuildConfig])
@pytest.mark.parametrize("runtime", ["aforge", "claude_code", "open_code", "codex"])
def test_request_and_deployment_runtime_overrides(config_type, runtime, monkeypatch):
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "INFRON_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    monkeypatch.setenv("SWE_DEFAULT_RUNTIME", runtime)
    assert config_type().runtime == runtime
    # A request can still override a deployment configured for AForge.
    monkeypatch.setenv("SWE_DEFAULT_RUNTIME", "aforge")
    assert config_type(runtime=runtime).runtime == runtime


def test_aforge_model_overrides_and_harness_isolation(monkeypatch):
    monkeypatch.setenv("HARNESS_MODEL", "infron/opencode-only")
    monkeypatch.setenv("INFRON_API_KEY", "test-infron")
    assert set(resolve_runtime_models(runtime="aforge", models=None).values()) == {
        "openrouter/deepseek/deepseek-v4-flash-0731"
    }
    assert set(fast_resolve_models(FastBuildConfig(runtime="aforge")).values()) == {
        "openrouter/deepseek/deepseek-v4-flash-0731"
    }
    monkeypatch.setenv("SWE_DEFAULT_MODEL", "deployment-model")
    monkeypatch.setenv("SWE_MODEL_HIGH", "planning-model")
    models = resolve_runtime_models(runtime="aforge", models={"coder": "caller-model"})
    assert models["pm_model"] == "planning-model"
    assert models["coder_model"] == "caller-model"
    assert models["qa_model"] == "deployment-model"
    fast_models = fast_resolve_models(FastBuildConfig(
        runtime="aforge", models={"default": "caller-default", "coder": "caller-model"}
    ))
    assert fast_models["pm_model"] == "caller-default"
    assert fast_models["coder_model"] == "caller-model"

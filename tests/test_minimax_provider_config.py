from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG = ROOT / "opencode.json"
DOCKERFILES = (ROOT / "Dockerfile", ROOT / "go" / "Dockerfile")

MODEL_IDS = ["MiniMax-M3.1-Flash", "MiniMax-M3"]
OPENAI_PROVIDERS = {
    "minimax-global-openai": "https://api.minimax.io/v1",
    "minimax-cn-openai": "https://api.minimaxi.com/v1",
}
ANTHROPIC_PROVIDER = "minimax-anthropic"
MODEL_COSTS = {
    "MiniMax-M3.1-Flash": {"input": 0.15, "output": 0.6, "cache_read": 0.015},
    "MiniMax-M3": {"input": 0.3, "output": 1.2, "cache_read": 0.06},
}


@pytest.fixture(scope="module")
def opencode_config() -> dict[str, object]:
    return json.loads(OPENCODE_CONFIG.read_text())


def test_both_images_install_the_shared_opencode_config() -> None:
    expected = "COPY opencode.json /root/.config/opencode/opencode.json"
    for dockerfile in DOCKERFILES:
        assert expected in dockerfile.read_text()


@pytest.mark.parametrize(("provider_id", "base_url"), OPENAI_PROVIDERS.items())
def test_minimax_openai_provider_registry(
    opencode_config: dict[str, object], provider_id: str, base_url: str
) -> None:
    providers = opencode_config["provider"]
    assert isinstance(providers, dict)
    provider = providers[provider_id]

    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["env"] == ["MINIMAX_API_KEY"]
    assert provider["options"] == {
        "apiKey": "{env:MINIMAX_API_KEY}",
        "baseURL": base_url,
    }
    assert list(provider["models"]) == MODEL_IDS


def test_minimax_anthropic_provider_registry(
    opencode_config: dict[str, object],
) -> None:
    providers = opencode_config["provider"]
    assert isinstance(providers, dict)
    provider = providers[ANTHROPIC_PROVIDER]

    assert provider["npm"] == "@ai-sdk/anthropic"
    assert provider["env"] == ["MINIMAX_API_KEY"]
    assert provider["options"] == {
        "apiKey": "{env:MINIMAX_API_KEY}",
        "baseURL": "{env:ANTHROPIC_BASE_URL}/v1",
    }
    assert list(provider["models"]) == MODEL_IDS


def test_minimax_model_metadata_matches_target_config(
    opencode_config: dict[str, object],
) -> None:
    providers = opencode_config["provider"]
    assert isinstance(providers, dict)

    for provider_id in (*OPENAI_PROVIDERS, ANTHROPIC_PROVIDER):
        models = providers[provider_id]["models"]
        for model_id in MODEL_IDS:
            assert models[model_id] == {
                "id": model_id,
                "name": model_id,
                "attachment": True,
                "reasoning": True,
                "limit": {"context": 1000000, "output": 524288},
                "cost": MODEL_COSTS[model_id],
                "modalities": {
                    "input": ["text", "image", "video"],
                    "output": ["text"],
                },
            }


def test_minimax_anthropic_endpoints_and_model_ids_are_documented() -> None:
    docs = (ROOT / "README.md").read_text() + (ROOT / ".env.example").read_text()

    for value in (
        "https://api.minimax.io/anthropic",
        "https://api.minimaxi.com/anthropic",
        "minimax-anthropic/MiniMax-M3.1-Flash",
        "minimax-anthropic/MiniMax-M3",
        "claude_code",
        "1,000,000",
        "524,288",
        "adaptive or disabled",
        "always adaptive",
        "$0.15 / $0.60 / $0.015",
        "$0.30 / $1.20 / $0.06",
        "over 512K input tokens are billed at $0.60 / $2.40 / $0.12",
        "Priority pricing is 1.5× Standard",
        "Cache-write pricing is unspecified and omitted",
    ):
        assert value in docs

    for model_id in MODEL_IDS:
        assert f"minimax/{model_id}" not in docs
        assert f"minimax-cn/{model_id}" not in docs
    assert "MiniMax-M2.7" not in docs
    assert "https://api.minimax.io/anthropic/v1" not in docs
    assert "https://api.minimaxi.com/anthropic/v1" not in docs


def test_minimax_examples_select_flash_without_replacing_harness_overrides(
    opencode_config: dict[str, object],
) -> None:
    assert opencode_config["model"] == "{env:HARNESS_MODEL}"
    assert opencode_config["small_model"] == "{env:HARNESS_MODEL}"
    env_example = (ROOT / ".env.example").read_text()
    for provider_id in (*OPENAI_PROVIDERS, ANTHROPIC_PROVIDER):
        assert f"SWE_DEFAULT_MODEL={provider_id}/MiniMax-M3.1-Flash" in env_example
    assert "HARNESS_MODEL=minimax-global-openai/MiniMax-M3.1-Flash" in env_example
    assert "SWE_DEFAULT_MODEL=MiniMax-M3.1-Flash" in env_example

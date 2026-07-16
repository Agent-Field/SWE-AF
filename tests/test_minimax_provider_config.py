from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG = ROOT / "opencode.json"
DOCKERFILES = (ROOT / "Dockerfile", ROOT / "go" / "Dockerfile")

MODEL_IDS = {"MiniMax-M3", "MiniMax-M2.7"}
OPENAI_PROVIDERS = {
    "minimax-global-openai": "https://api.minimax.io/v1",
    "minimax-cn-openai": "https://api.minimaxi.com/v1",
}
ANTHROPIC_PROVIDER = "minimax-anthropic"
MODEL_LIMITS = {
    "MiniMax-M3": {"context": 1000000, "output": 524288},
    "MiniMax-M2.7": {"context": 204800, "output": 204800},
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
    assert set(provider["models"]) == MODEL_IDS


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
    assert set(provider["models"]) == MODEL_IDS


def test_minimax_model_metadata_matches_target_config(
    opencode_config: dict[str, object],
) -> None:
    providers = opencode_config["provider"]
    assert isinstance(providers, dict)

    for provider_id in (*OPENAI_PROVIDERS, ANTHROPIC_PROVIDER):
        models = providers[provider_id]["models"]
        assert {
            model_id: models[model_id]["limit"] for model_id in MODEL_IDS
        } == MODEL_LIMITS
        assert models["MiniMax-M3"]["cost"] == {
            "input": 0.3,
            "output": 1.2,
            "cache_read": 0.06,
        }
        assert models["MiniMax-M3"]["modalities"]["input"] == [
            "text",
            "image",
            "video",
        ]
        assert models["MiniMax-M3"]["reasoning"] is True

        assert models["MiniMax-M2.7"]["cost"] == {
            "input": 0.3,
            "output": 1.2,
            "cache_read": 0.06,
            "cache_write": 0.375,
        }
        assert models["MiniMax-M2.7"]["modalities"]["input"] == ["text"]
        assert models["MiniMax-M2.7"]["reasoning"] is True


def test_minimax_anthropic_endpoints_and_model_ids_are_documented() -> None:
    docs = (ROOT / "README.md").read_text() + (ROOT / ".env.example").read_text()

    for value in (
        "https://api.minimax.io/anthropic",
        "https://api.minimaxi.com/anthropic",
        "minimax-anthropic/MiniMax-M3",
        "minimax-anthropic/MiniMax-M2.7",
        "claude_code",
        "1,000,000",
        "204,800",
        "adaptive or disabled",
        "always on",
        "$0.30 / $1.20 / $0.06 / not charged",
        "$0.60 / $2.40 / $0.12 / not charged",
        "$0.45 / $1.80 / $0.09 / not charged",
        "$0.90 / $3.60 / $0.18 / not charged",
        "$0.30 / $1.20 / $0.06 / $0.375",
    ):
        assert value in docs

    assert "minimax/MiniMax-M3" not in docs
    assert "minimax/MiniMax-M2.7" not in docs
    assert "minimax-cn/MiniMax-M3" not in docs
    assert "minimax-cn/MiniMax-M2.7" not in docs
    assert "https://api.minimax.io/anthropic/v1" not in docs
    assert "https://api.minimaxi.com/anthropic/v1" not in docs

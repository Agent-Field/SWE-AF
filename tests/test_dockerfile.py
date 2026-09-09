"""Tests for Dockerfile correctness.

Validates that the Dockerfile contains required directives for correct
container behavior — particularly around directory pre-creation that
prevents read-only filesystem errors with named volume mounts.

Ref: https://github.com/Agent-Field/SWE-AF/issues/46
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
GO_DOCKERFILE = REPO_ROOT / "go" / "Dockerfile"
OPENCODE_CONFIG = REPO_ROOT / "opencode.json"
REQUIREMENTS_DOCKER = REPO_ROOT / "requirements-docker.txt"


@pytest.fixture(scope="module")
def dockerfile_content() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def opencode_config() -> dict:
    return json.loads(OPENCODE_CONFIG.read_text())


class TestWorkspacesDirectory:
    """Issue #46: /workspaces must be pre-created with write permissions."""

    def test_mkdir_workspaces_exists(self, dockerfile_content: str) -> None:
        """Dockerfile must create /workspaces before any volume mount."""
        assert re.search(
            r"mkdir\s+-p\s+/workspaces", dockerfile_content
        ), (
            "Dockerfile must contain 'mkdir -p /workspaces' to pre-create the "
            "directory before named volume mounts (see issue #46)"
        )

    def test_chmod_workspaces(self, dockerfile_content: str) -> None:
        """Dockerfile must set write permissions on /workspaces."""
        assert re.search(
            r"chmod\s+\d*7\d*\s+/workspaces", dockerfile_content
        ), (
            "Dockerfile must chmod /workspaces with world-writable permissions "
            "so the running process can write to it (see issue #46)"
        )

    def test_workspaces_created_before_expose(self, dockerfile_content: str) -> None:
        """mkdir /workspaces must appear before EXPOSE (i.e. in the build stage)."""
        mkdir_match = re.search(r"mkdir\s+-p\s+/workspaces", dockerfile_content)
        expose_match = re.search(r"^EXPOSE\s+", dockerfile_content, re.MULTILINE)
        assert mkdir_match is not None and expose_match is not None, (
            "Both 'mkdir -p /workspaces' and 'EXPOSE' must exist in Dockerfile"
        )
        assert mkdir_match.start() < expose_match.start(), (
            "/workspaces must be created before EXPOSE to ensure it's part of "
            "the image layer before any volume mount"
        )


def test_dockerfile_installs_codex_cli(dockerfile_content: str) -> None:
    assert "npm install -g @openai/codex" in dockerfile_content
    assert "SWE_CODEX_AUTH_MODE" in dockerfile_content
    assert "codex-real" in dockerfile_content


def test_dockerfile_preserves_opencode_install(dockerfile_content: str) -> None:
    assert "https://opencode.ai/install" in dockerfile_content
    expected_copy = "COPY opencode.json /root/.config/opencode/opencode.json"
    assert expected_copy in dockerfile_content
    assert expected_copy in GO_DOCKERFILE.read_text()
    assert "OPENROUTER_API_KEY" in OPENCODE_CONFIG.read_text()


class TestOpenCodeProviders:
    """The root opencode.json wires the harness providers for both images."""

    def test_model_follows_harness_model_env(self, opencode_config: dict) -> None:
        """Both model and small_model must honor HARNESS_MODEL.

        small_model is the one that falls through to config; if it does not
        interpolate the same env var, OpenCode silently auto-selects a model
        from whatever provider keys it finds.
        """
        assert opencode_config["model"] == "{env:HARNESS_MODEL}"
        assert opencode_config["small_model"] == "{env:HARNESS_MODEL}"

    def test_existing_provider_preserved(self, opencode_config: dict) -> None:
        existing = opencode_config["provider"]["openrouter"]
        assert existing["options"]["apiKey"] == "{env:OPENROUTER_API_KEY}"

    def test_infron_provider_is_openai_compatible(self, opencode_config: dict) -> None:
        infron = opencode_config["provider"]["infron"]
        assert infron["npm"] == "@ai-sdk/openai-compatible"
        assert infron["options"]["baseURL"] == "https://llm.onerouter.pro/v1"
        assert infron["options"]["apiKey"] == "{env:INFRON_API_KEY}"

    def test_infron_declares_models(self, opencode_config: dict) -> None:
        """openai-compatible providers are not in models.dev, so models must
        be listed explicitly or OpenCode cannot resolve `-m infron/<id>`."""
        models = opencode_config["provider"]["infron"]["models"]
        assert models, "infron provider must declare at least one model"
        # The ids are the vendors' own, unchanged across gateways, which is
        # what makes this a base-URL change rather than a model-mapping
        # exercise.
        assert "moonshotai/kimi-k2.6" in models
        assert "deepseek/deepseek-v4-flash-0731" in models


def test_docker_requirements_pin_cryptography_below_sigill_version() -> None:
    """Docker image should avoid cryptography 48 SIGILL on Linux/aarch64."""
    content = REQUIREMENTS_DOCKER.read_text()
    assert "cryptography<46" in content

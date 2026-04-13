"""Tests for deployment documentation completeness.

Validates that docs/deployment.md exists and contains required sections
covering prerequisites, known issues, and quick-start instructions.

Ref: https://github.com/Agent-Field/SWE-AF/issues/48
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEPLOYMENT_DOC = Path(__file__).resolve().parent.parent / "docs" / "deployment.md"


@pytest.fixture(scope="module")
def doc_content() -> str:
    assert DEPLOYMENT_DOC.exists(), (
        f"docs/deployment.md not found at {DEPLOYMENT_DOC}. "
        "This file is required for deployment guidance (see issue #48)."
    )
    return DEPLOYMENT_DOC.read_text()


class TestDeploymentDocStructure:
    """Issue #48: Deployment docs must cover key sections."""

    def test_prerequisites_section(self, doc_content: str) -> None:
        assert "## Prerequisites" in doc_content

    def test_environment_variables_documented(self, doc_content: str) -> None:
        for var in ("ANTHROPIC_API_KEY", "GH_TOKEN", "AGENTFIELD_SERVER"):
            assert var in doc_content, f"Missing env var documentation: {var}"

    def test_quick_start_section(self, doc_content: str) -> None:
        assert "## Quick Start" in doc_content

    def test_quick_start_has_docker_compose(self, doc_content: str) -> None:
        assert "docker compose" in doc_content

    def test_quick_start_has_env_setup(self, doc_content: str) -> None:
        assert ".env.example" in doc_content

    def test_known_issues_section(self, doc_content: str) -> None:
        assert "## Known Issues" in doc_content

    def test_known_issue_workspaces(self, doc_content: str) -> None:
        """Issue #46 must be documented."""
        assert "/workspaces" in doc_content
        assert "read-only" in doc_content.lower() or "Read-only" in doc_content

    def test_known_issue_opencode(self, doc_content: str) -> None:
        """Issue #45 must be documented."""
        assert "opencode" in doc_content.lower() or "open_code" in doc_content

    def test_known_issue_fatal_errors(self, doc_content: str) -> None:
        """Issue #49 must be documented."""
        assert "fatal" in doc_content.lower() or "credit" in doc_content.lower()

    def test_known_issue_parallel_builds(self, doc_content: str) -> None:
        """Issue #43 must be documented."""
        assert "parallel" in doc_content.lower() or "concurrent" in doc_content.lower()

    def test_troubleshooting_section(self, doc_content: str) -> None:
        assert "## Troubleshooting" in doc_content or "## Scaling" in doc_content

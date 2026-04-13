"""Tests for Dockerfile correctness.

Validates that the Dockerfile contains required directives for correct
container behavior — particularly around directory pre-creation that
prevents read-only filesystem errors with named volume mounts.

Ref: https://github.com/Agent-Field/SWE-AF/issues/46
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


@pytest.fixture(scope="module")
def dockerfile_content() -> str:
    return DOCKERFILE.read_text()


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

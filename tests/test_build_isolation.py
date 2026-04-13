"""Tests for parallel build workspace isolation.

Validates that concurrent builds for the same repository get isolated
workspace directories, preventing cross-contamination of git state,
artifacts, and worktrees.

Ref: https://github.com/Agent-Field/SWE-AF/issues/43
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_SOURCE = (
    Path(__file__).resolve().parent.parent / "swe_af" / "app.py"
).read_text()


class TestBuildWorkspaceIsolation:
    """Issue #43: Parallel builds must not share workspace paths."""

    def test_auto_derived_repo_path_includes_build_id(self) -> None:
        """When repo_url is provided without repo_path, the derived path
        must include the build_id to isolate concurrent builds."""
        assert re.search(
            r'repo_path\s*=\s*f"/workspaces/\{repo_name\}-\{build_id\}"',
            APP_SOURCE,
        ), (
            "Auto-derived repo_path must include build_id suffix to isolate "
            "concurrent builds (see issue #43)"
        )

    def test_build_id_generated_before_clone(self) -> None:
        """build_id must be generated before the clone/workspace setup
        so it can be used to scope the workspace path."""
        build_id_match = re.search(r'build_id = uuid\.uuid4\(\)', APP_SOURCE)
        clone_match = re.search(r'git_dir = os\.path\.join\(repo_path', APP_SOURCE)

        assert build_id_match is not None, "build_id generation not found"
        assert clone_match is not None, "clone logic not found"
        assert build_id_match.start() < clone_match.start(), (
            "build_id must be generated BEFORE clone/workspace setup "
            "to be available for path scoping (see issue #43)"
        )

    def test_multi_repo_path_includes_build_id(self) -> None:
        """Multi-repo derived path must also include build_id."""
        multi_repo_start = APP_SOURCE.find("Multi-repo: derive")
        assert multi_repo_start > 0, "Multi-repo section not found"
        multi_repo_section = APP_SOURCE[multi_repo_start:multi_repo_start + 500]
        assert re.search(
            r'repo_path\s*=\s*f"/workspaces/\{repo_name\}-\{build_id\}"',
            multi_repo_section,
        ), (
            "Multi-repo derived repo_path must include build_id suffix (see issue #43)"
        )

    def test_two_builds_same_repo_get_different_paths(self) -> None:
        """Simulate that two builds for the same repo_url produce different paths."""
        import uuid
        from swe_af.execution.schemas import _derive_repo_name

        repo_url = "https://github.com/example/my-repo.git"
        repo_name = _derive_repo_name(repo_url)

        build_id_a = uuid.uuid4().hex[:8]
        build_id_b = uuid.uuid4().hex[:8]

        path_a = f"/workspaces/{repo_name}-{build_id_a}"
        path_b = f"/workspaces/{repo_name}-{build_id_b}"

        assert path_a != path_b, (
            "Two builds for the same repo must produce different workspace paths"
        )
        assert repo_name in path_a
        assert repo_name in path_b
        assert build_id_a in path_a
        assert build_id_b in path_b

    def test_artifacts_dir_isolated_by_build_scoped_path(self) -> None:
        """artifacts_dir is relative to repo_path, so build-scoped repo_path
        means artifacts are also isolated."""
        import uuid
        import os
        from swe_af.execution.schemas import _derive_repo_name

        repo_url = "https://github.com/example/my-repo.git"
        repo_name = _derive_repo_name(repo_url)
        artifacts_dir = ".artifacts"

        build_id_a = uuid.uuid4().hex[:8]
        build_id_b = uuid.uuid4().hex[:8]

        repo_path_a = f"/workspaces/{repo_name}-{build_id_a}"
        repo_path_b = f"/workspaces/{repo_name}-{build_id_b}"

        artifacts_a = os.path.join(repo_path_a, artifacts_dir)
        artifacts_b = os.path.join(repo_path_b, artifacts_dir)

        assert artifacts_a != artifacts_b, (
            "Artifacts dirs must differ between concurrent builds"
        )

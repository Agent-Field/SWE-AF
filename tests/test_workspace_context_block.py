"""Tests for swe_af.prompts._utils.workspace_context_block."""

from __future__ import annotations

import pytest

from swe_af.execution.schemas import WorkspaceManifest, WorkspaceRepo
from swe_af.prompts._utils import workspace_context_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(
    repo_name: str,
    role: str = "primary",
    absolute_path: str = "/tmp/repo",
    repo_url: str = "https://github.com/org/repo.git",
    branch: str = "main",
    create_pr: bool = True,
) -> WorkspaceRepo:
    return WorkspaceRepo(
        repo_name=repo_name,
        repo_url=repo_url,
        role=role,
        absolute_path=absolute_path,
        branch=branch,
        sparse_paths=[],
        create_pr=create_pr,
    )


def _make_manifest(repos: list[WorkspaceRepo], primary_repo_name: str = "") -> WorkspaceManifest:
    if not primary_repo_name and repos:
        primary_repo_name = repos[0].repo_name
    return WorkspaceManifest(
        workspace_root="/tmp",
        repos=repos,
        primary_repo_name=primary_repo_name,
    )


# ---------------------------------------------------------------------------
# AC-14: None input returns empty string (no raise)
# ---------------------------------------------------------------------------


def test_none_manifest_returns_empty_string() -> None:
    """workspace_context_block(None) returns '' without raising."""
    result = workspace_context_block(None)
    assert result == ""


# ---------------------------------------------------------------------------
# Single-repo: returns empty string (AC-14)
# ---------------------------------------------------------------------------


def test_single_repo_manifest_returns_empty_string() -> None:
    """Single-repo manifest returns '' to avoid prompt pollution."""
    repo = _make_repo("a", role="primary", absolute_path="/tmp/a")
    manifest = _make_manifest([repo], primary_repo_name="a")
    result = workspace_context_block(manifest)
    assert result == ""


# ---------------------------------------------------------------------------
# Zero repos: returns empty string (edge case)
# ---------------------------------------------------------------------------


def test_zero_repos_manifest_returns_empty_string() -> None:
    """Manifest with no repos returns ''."""
    manifest = WorkspaceManifest(workspace_root="/tmp", repos=[], primary_repo_name="")
    result = workspace_context_block(manifest)
    assert result == ""


# ---------------------------------------------------------------------------
# AC-15: Multi-repo manifest returns non-empty string with repo info
# ---------------------------------------------------------------------------


def test_two_repo_manifest_returns_non_empty_string() -> None:
    """Two-repo manifest returns a non-empty string."""
    api_repo = _make_repo(
        "api",
        role="primary",
        absolute_path="/tmp/api",
        repo_url="https://github.com/org/api.git",
    )
    lib_repo = _make_repo(
        "lib",
        role="dependency",
        absolute_path="/tmp/lib",
        repo_url="https://github.com/org/lib.git",
        create_pr=False,
    )
    manifest = _make_manifest([api_repo, lib_repo], primary_repo_name="api")
    result = workspace_context_block(manifest)
    assert result != ""


def test_two_repo_manifest_contains_repo_names() -> None:
    """Two-repo manifest output contains both repo names."""
    api_repo = _make_repo("api", role="primary", absolute_path="/tmp/api")
    lib_repo = _make_repo("lib", role="dependency", absolute_path="/tmp/lib", create_pr=False)
    manifest = _make_manifest([api_repo, lib_repo], primary_repo_name="api")
    result = workspace_context_block(manifest)
    assert "api" in result
    assert "lib" in result


def test_two_repo_manifest_contains_absolute_paths() -> None:
    """Two-repo manifest output contains both absolute paths."""
    api_repo = _make_repo("api", role="primary", absolute_path="/tmp/api")
    lib_repo = _make_repo("lib", role="dependency", absolute_path="/tmp/lib", create_pr=False)
    manifest = _make_manifest([api_repo, lib_repo], primary_repo_name="api")
    result = workspace_context_block(manifest)
    assert "/tmp/api" in result
    assert "/tmp/lib" in result


def test_multi_repo_output_includes_role_labels() -> None:
    """Multi-repo output includes role labels for each repo."""
    api_repo = _make_repo("api", role="primary", absolute_path="/tmp/api")
    lib_repo = _make_repo("lib", role="dependency", absolute_path="/tmp/lib", create_pr=False)
    manifest = _make_manifest([api_repo, lib_repo], primary_repo_name="api")
    result = workspace_context_block(manifest)
    assert "primary" in result
    assert "dependency" in result


def test_ac14_exact_single_repo_pass() -> None:
    """AC-14 verbatim check: single-repo returns exact empty string."""
    m = WorkspaceManifest(
        workspace_root="/tmp",
        repos=[
            WorkspaceRepo(
                repo_name="a",
                repo_url="https://github.com/org/a.git",
                role="primary",
                absolute_path="/tmp/a",
                branch="main",
                sparse_paths=[],
                create_pr=True,
            )
        ],
        primary_repo_name="a",
    )
    result = workspace_context_block(m)
    assert result == "", f"Expected empty string, got: {repr(result)}"


def test_ac15_two_repo_pass() -> None:
    """AC-15 verbatim check: two-repo manifest has both names and primary path."""
    m = WorkspaceManifest(
        workspace_root="/tmp",
        repos=[
            WorkspaceRepo(
                repo_name="api",
                repo_url="https://github.com/org/api.git",
                role="primary",
                absolute_path="/tmp/api",
                branch="main",
                sparse_paths=[],
                create_pr=True,
            ),
            WorkspaceRepo(
                repo_name="lib",
                repo_url="https://github.com/org/lib.git",
                role="dependency",
                absolute_path="/tmp/lib",
                branch="main",
                sparse_paths=[],
                create_pr=False,
            ),
        ],
        primary_repo_name="api",
    )
    result = workspace_context_block(m)
    assert "api" in result
    assert "lib" in result
    assert "/tmp/api" in result

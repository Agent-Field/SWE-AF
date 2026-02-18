"""Smoke tests verifying AC-01 through AC-15 from the Multi-Repo PRD.

Each test function directly runs the assertions from the corresponding acceptance
criterion as inline Python code (no subprocess calls).

Run with:
    python -m pytest tests/test_multi_repo_smoke.py -v
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from swe_af.execution.schemas import (
    BuildConfig,
    BuildResult,
    CoderResult,
    DAGState,
    GitInitResult,
    MergeResult,
    RepoPRResult,
    RepoSpec,
    WorkspaceManifest,
    WorkspaceRepo,
)
from swe_af.prompts._utils import workspace_context_block
from swe_af.reasoners.schemas import PlannedIssue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace_repo(
    repo_name: str,
    role: str = "primary",
    absolute_path: str = "/tmp/repo",
    repo_url: str = "https://github.com/org/repo.git",
    branch: str = "main",
    sparse_paths: list[str] | None = None,
    create_pr: bool = True,
) -> WorkspaceRepo:
    return WorkspaceRepo(
        repo_name=repo_name,
        repo_url=repo_url,
        role=role,
        absolute_path=absolute_path,
        branch=branch,
        sparse_paths=sparse_paths or [],
        create_pr=create_pr,
    )


def _make_manifest(
    repos: list[WorkspaceRepo],
    workspace_root: str = "/tmp/ws",
    primary_repo_name: str = "",
) -> WorkspaceManifest:
    if not primary_repo_name and repos:
        primary_repo_name = repos[0].repo_name
    return WorkspaceManifest(
        workspace_root=workspace_root,
        repos=repos,
        primary_repo_name=primary_repo_name,
    )


# ---------------------------------------------------------------------------
# AC-01: RepoSpec model validation
# ---------------------------------------------------------------------------


def test_smoke_ac01() -> None:
    """AC-01: RepoSpec with URL + role='primary' validates correctly; bad URL raises."""
    r = RepoSpec(repo_url="https://github.com/org/repo.git", role="primary")
    assert r.role == "primary"
    assert r.create_pr is True
    assert r.sparse_paths == []
    assert r.branch == ""
    assert r.mount_point == ""

    with pytest.raises((ValidationError, ValueError)):
        RepoSpec(repo_url="not-a-url", role="primary")


# ---------------------------------------------------------------------------
# AC-02: BuildConfig normalizes legacy repo_url to repos
# ---------------------------------------------------------------------------


def test_smoke_ac02() -> None:
    """AC-02: BuildConfig with legacy repo_url synthesizes a repos list."""
    cfg = BuildConfig(repo_url="https://github.com/org/repo.git")
    assert len(cfg.repos) == 1
    assert cfg.repos[0].repo_url == "https://github.com/org/repo.git"
    assert cfg.repos[0].role == "primary"
    assert cfg.primary_repo is not None
    assert cfg.primary_repo.repo_url == "https://github.com/org/repo.git"


# ---------------------------------------------------------------------------
# AC-03: BuildConfig rejects multiple primary repos
# ---------------------------------------------------------------------------


def test_smoke_ac03() -> None:
    """AC-03: Two RepoSpecs with role='primary' raises an error."""
    with pytest.raises((ValidationError, ValueError)):
        BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/a.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/b.git", role="primary"),
            ]
        )


# ---------------------------------------------------------------------------
# AC-04: BuildConfig rejects both repo_url and repos simultaneously
# ---------------------------------------------------------------------------


def test_smoke_ac04() -> None:
    """AC-04: Providing both repo_url and repos raises an error."""
    with pytest.raises((ValidationError, ValueError)):
        BuildConfig(
            repo_url="https://github.com/org/a.git",
            repos=[RepoSpec(repo_url="https://github.com/org/b.git", role="primary")],
        )


# ---------------------------------------------------------------------------
# AC-05: BuildConfig sets repo_url from primary in multi-repo mode
# ---------------------------------------------------------------------------


def test_smoke_ac05() -> None:
    """AC-05: Multi-repo BuildConfig backfills repo_url from primary."""
    cfg = BuildConfig(
        repos=[
            RepoSpec(repo_url="https://github.com/org/api.git", role="primary"),
            RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
        ]
    )
    assert cfg.repo_url == "https://github.com/org/api.git", f"Got: {cfg.repo_url}"


# ---------------------------------------------------------------------------
# AC-06: WorkspaceManifest model construction and JSON serialization
# ---------------------------------------------------------------------------


def test_smoke_ac06() -> None:
    """AC-06: WorkspaceManifest constructs and round-trips through JSON."""
    m = WorkspaceManifest(
        workspace_root="/tmp/ws",
        repos=[
            WorkspaceRepo(
                repo_name="myrepo",
                repo_url="https://github.com/org/myrepo.git",
                role="primary",
                absolute_path="/tmp/ws/myrepo",
                branch="main",
                sparse_paths=[],
                create_pr=True,
            )
        ],
        primary_repo_name="myrepo",
    )
    assert m.primary_repo_name == "myrepo"
    assert m.repos[0].absolute_path == "/tmp/ws/myrepo"
    j = m.model_dump_json(indent=2)
    parsed = json.loads(j)
    assert parsed["primary_repo_name"] == "myrepo"


# ---------------------------------------------------------------------------
# AC-07: RepoPRResult model construction
# ---------------------------------------------------------------------------


def test_smoke_ac07() -> None:
    """AC-07: RepoPRResult constructs with correct field values."""
    r = RepoPRResult(
        repo_name="myrepo",
        repo_url="https://github.com/org/myrepo.git",
        success=True,
        pr_url="https://github.com/org/myrepo/pull/1",
        pr_number=1,
    )
    assert r.repo_name == "myrepo"
    assert r.success is True
    assert r.pr_number == 1


# ---------------------------------------------------------------------------
# AC-08: BuildResult.pr_url backward-compat property
# ---------------------------------------------------------------------------


def test_smoke_ac08() -> None:
    """AC-08: BuildResult.pr_url property returns first successful PR URL."""
    br = BuildResult(
        plan_result={},
        dag_state={},
        verification=None,
        success=True,
        summary="",
        pr_results=[
            RepoPRResult(
                repo_name="r",
                repo_url="https://github.com/org/r.git",
                success=True,
                pr_url="https://github.com/org/r/pull/1",
                pr_number=1,
            )
        ],
    )
    assert br.pr_url == "https://github.com/org/r/pull/1"

    br2 = BuildResult(
        plan_result={},
        dag_state={},
        verification=None,
        success=True,
        summary="",
        pr_results=[],
    )
    assert br2.pr_url == ""


# ---------------------------------------------------------------------------
# AC-09: DAGState has workspace_manifest field defaulting to None
# ---------------------------------------------------------------------------


def test_smoke_ac09() -> None:
    """AC-09: DAGState.workspace_manifest exists and defaults to None."""
    ds = DAGState(repo_path="/tmp/repo", artifacts_dir="/tmp/artifacts")
    assert hasattr(ds, "workspace_manifest")
    assert ds.workspace_manifest is None


# ---------------------------------------------------------------------------
# AC-10: PlannedIssue has target_repo field defaulting to empty string
# ---------------------------------------------------------------------------


def test_smoke_ac10() -> None:
    """AC-10: PlannedIssue.target_repo defaults to '' and can be set."""
    pi = PlannedIssue(
        name="test-issue",
        title="Test",
        description="desc",
        acceptance_criteria=["AC1"],
        depends_on=[],
        provides=[],
        files_to_create=[],
        files_to_modify=[],
        testing_strategy="pytest",
        sequence_number=1,
    )
    assert hasattr(pi, "target_repo")
    assert pi.target_repo == ""

    pi2 = PlannedIssue(
        name="test-issue",
        title="Test",
        description="desc",
        acceptance_criteria=["AC1"],
        depends_on=[],
        provides=[],
        files_to_create=[],
        files_to_modify=[],
        testing_strategy="pytest",
        sequence_number=1,
        target_repo="myrepo",
    )
    assert pi2.target_repo == "myrepo"


# ---------------------------------------------------------------------------
# AC-11: CoderResult has repo_name field defaulting to empty string
# ---------------------------------------------------------------------------


def test_smoke_ac11() -> None:
    """AC-11: CoderResult.repo_name exists and defaults to ''."""
    cr = CoderResult(
        files_changed=[],
        summary="done",
        complete=True,
        tests_passed=True,
        test_summary="all pass",
    )
    assert hasattr(cr, "repo_name")
    assert cr.repo_name == ""


# ---------------------------------------------------------------------------
# AC-12: GitInitResult has repo_name field defaulting to empty string
# ---------------------------------------------------------------------------


def test_smoke_ac12() -> None:
    """AC-12: GitInitResult.repo_name exists and defaults to ''."""
    gir = GitInitResult(
        mode="fresh",
        integration_branch="main",
        original_branch="main",
        initial_commit_sha="abc123",
        success=True,
    )
    assert hasattr(gir, "repo_name")
    assert gir.repo_name == ""


# ---------------------------------------------------------------------------
# AC-13: MergeResult has repo_name field defaulting to empty string
# ---------------------------------------------------------------------------


def test_smoke_ac13() -> None:
    """AC-13: MergeResult.repo_name exists and defaults to ''."""
    mr = MergeResult(
        success=True,
        merged_branches=[],
        failed_branches=[],
        conflict_resolutions=[],
        merge_commit_sha="abc",
        pre_merge_sha="def",
        needs_integration_test=False,
        integration_test_rationale="",
        summary="",
    )
    assert hasattr(mr, "repo_name")
    assert mr.repo_name == ""


# ---------------------------------------------------------------------------
# AC-14: workspace_context_block returns empty string for single repo
# ---------------------------------------------------------------------------


def test_smoke_ac14() -> None:
    """AC-14: workspace_context_block returns '' for a single-repo manifest."""
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


# ---------------------------------------------------------------------------
# AC-15: workspace_context_block returns block with all repos for multi-repo
# ---------------------------------------------------------------------------


def test_smoke_ac15() -> None:
    """AC-15: workspace_context_block returns formatted block containing repo names and paths."""
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
    assert "/tmp/lib" in result
    assert len(result) > 0

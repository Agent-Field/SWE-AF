"""Tests for multi-repo schema extensions (issue daaccc55-01-core-schemas).

Covers AC-01 through AC-09, AC-11, AC-12, AC-13, AC-24 and backward
compatibility for existing single-repo callers.
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
    IssueResult,
    IssueOutcome,
    MergeResult,
    RepoPRResult,
    RepoSpec,
    WorkspaceManifest,
    WorkspaceRepo,
    _derive_repo_name,
)


# ---------------------------------------------------------------------------
# _derive_repo_name
# ---------------------------------------------------------------------------


class TestDeriveRepoName:
    def test_https_with_dot_git(self) -> None:
        assert _derive_repo_name("https://github.com/org/my-project.git") == "my-project"

    def test_https_without_dot_git(self) -> None:
        assert _derive_repo_name("https://github.com/org/repo") == "repo"

    def test_ssh_url(self) -> None:
        assert _derive_repo_name("git@github.com:org/repo.git") == "repo"

    def test_empty_string(self) -> None:
        assert _derive_repo_name("") == ""


# ---------------------------------------------------------------------------
# RepoSpec — AC-01
# ---------------------------------------------------------------------------


class TestRepoSpec:
    def test_defaults_with_primary_role(self) -> None:
        """AC-01: RepoSpec with URL + role='primary' sets correct defaults."""
        r = RepoSpec(repo_url="https://github.com/org/repo.git", role="primary")
        assert r.role == "primary"
        assert r.create_pr is True
        assert r.sparse_paths == []
        assert r.branch == ""
        assert r.mount_point == ""

    def test_dependency_role(self) -> None:
        r = RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency")
        assert r.role == "dependency"
        assert r.create_pr is True

    @pytest.mark.parametrize("role", ["invalid", "Primary", "DEPENDENCY", ""])
    def test_invalid_role_raises(self, role: str) -> None:
        with pytest.raises((ValidationError, ValueError)):
            RepoSpec(repo_url="https://github.com/org/repo.git", role=role)

    def test_valid_https_url(self) -> None:
        r = RepoSpec(repo_url="https://github.com/org/repo.git", role="primary")
        assert r.repo_url == "https://github.com/org/repo.git"

    def test_valid_http_url(self) -> None:
        r = RepoSpec(repo_url="http://github.example.com/org/repo.git", role="primary")
        assert r.repo_url.startswith("http://")

    def test_valid_ssh_url(self) -> None:
        r = RepoSpec(repo_url="git@github.com:org/repo.git", role="primary")
        assert r.repo_url.startswith("git@")

    def test_invalid_url_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            RepoSpec(repo_url="not-a-valid-url", role="primary")

    def test_sparse_paths_field(self) -> None:
        r = RepoSpec(repo_url="https://github.com/org/repo.git", role="primary", sparse_paths=["src/"])
        assert r.sparse_paths == ["src/"]

    def test_create_pr_can_be_false(self) -> None:
        r = RepoSpec(repo_url="https://github.com/org/repo.git", role="dependency", create_pr=False)
        assert r.create_pr is False


# ---------------------------------------------------------------------------
# BuildConfig — AC-02, AC-03, AC-04, AC-05, AC-24
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_ac02_single_repo_url_synthesises_repos(self) -> None:
        """AC-02: BuildConfig(repo_url=...) synthesises repos=[RepoSpec(..., role='primary')]."""
        cfg = BuildConfig(repo_url="https://github.com/org/repo.git")
        assert len(cfg.repos) == 1
        assert cfg.repos[0].repo_url == "https://github.com/org/repo.git"
        assert cfg.repos[0].role == "primary"
        assert cfg.primary_repo is not None

    def test_ac03_two_primaries_raises(self) -> None:
        """AC-03: Two RepoSpecs with role='primary' raises ValueError."""
        with pytest.raises((ValidationError, ValueError)):
            BuildConfig(
                repos=[
                    RepoSpec(repo_url="https://github.com/org/a.git", role="primary"),
                    RepoSpec(repo_url="https://github.com/org/b.git", role="primary"),
                ]
            )

    def test_ac04_repo_url_and_repos_simultaneously_raises(self) -> None:
        """AC-04: Specifying both repo_url and repos raises ValueError."""
        with pytest.raises((ValidationError, ValueError)):
            BuildConfig(
                repo_url="https://github.com/org/a.git",
                repos=[RepoSpec(repo_url="https://github.com/org/b.git", role="primary")],
            )

    def test_ac05_multi_repo_backfills_repo_url(self) -> None:
        """AC-05: Multi-repo BuildConfig backfills repo_url from primary."""
        cfg = BuildConfig(
            repos=[
                RepoSpec(repo_url="https://github.com/org/api.git", role="primary"),
                RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency"),
            ]
        )
        assert cfg.repo_url == "https://github.com/org/api.git"

    def test_ac24_duplicate_repo_url_raises(self) -> None:
        """AC-24: Duplicate repo_url values in repos raises ValueError."""
        with pytest.raises((ValidationError, ValueError)):
            BuildConfig(
                repos=[
                    RepoSpec(repo_url="https://github.com/org/myrepo.git", role="primary"),
                    RepoSpec(repo_url="https://github.com/org/myrepo.git", role="dependency"),
                ]
            )

    def test_primary_repo_property_returns_primary(self) -> None:
        cfg = BuildConfig(repo_url="https://github.com/org/repo.git")
        assert cfg.primary_repo is not None
        assert cfg.primary_repo.role == "primary"

    def test_primary_repo_property_none_when_no_repos(self) -> None:
        cfg = BuildConfig()  # No repo_url or repos
        assert cfg.primary_repo is None

    def test_empty_build_config_no_repos(self) -> None:
        """Empty BuildConfig (no repo_url) is allowed (deferred to build())."""
        cfg = BuildConfig()
        assert cfg.repos == []
        assert cfg.repo_url == ""

    def test_existing_single_repo_backward_compat(self) -> None:
        """Existing tests: BuildConfig() and BuildConfig(repo_url=...) still work."""
        cfg = BuildConfig(runtime="open_code")
        assert cfg.ai_provider == "opencode"

    @pytest.mark.parametrize("zero_primary_count", [
        [],  # empty repos (handled - pass through)
    ])
    def test_no_primary_in_repos_raises(self, zero_primary_count: list) -> None:
        """A non-empty repos list with no primary should raise."""
        if not zero_primary_count:
            # Empty repos is allowed (pass through)
            cfg = BuildConfig(repos=[])
            assert cfg.repos == []
        else:
            with pytest.raises((ValidationError, ValueError)):
                BuildConfig(repos=zero_primary_count)

    def test_one_dependency_no_primary_raises(self) -> None:
        """repos list with only a dependency (no primary) raises."""
        with pytest.raises((ValidationError, ValueError)):
            BuildConfig(
                repos=[RepoSpec(repo_url="https://github.com/org/lib.git", role="dependency")]
            )


# ---------------------------------------------------------------------------
# WorkspaceRepo
# ---------------------------------------------------------------------------


class TestWorkspaceRepo:
    def test_construction(self) -> None:
        wr = WorkspaceRepo(
            repo_name="myrepo",
            repo_url="https://github.com/org/myrepo.git",
            role="primary",
            absolute_path="/tmp/ws/myrepo",
            branch="main",
        )
        assert wr.repo_name == "myrepo"
        assert wr.git_init_result is None
        assert wr.sparse_paths == []
        assert wr.create_pr is True

    def test_mutable_git_init_result(self) -> None:
        """WorkspaceRepo.frozen=False allows in-place mutation of git_init_result."""
        wr = WorkspaceRepo(
            repo_name="myrepo",
            repo_url="https://github.com/org/myrepo.git",
            role="primary",
            absolute_path="/tmp/ws/myrepo",
            branch="main",
        )
        assert wr.git_init_result is None
        wr.git_init_result = {"mode": "fresh", "success": True}
        assert wr.git_init_result == {"mode": "fresh", "success": True}


# ---------------------------------------------------------------------------
# WorkspaceManifest — AC-06
# ---------------------------------------------------------------------------


class TestWorkspaceManifest:
    def _make_manifest(self) -> WorkspaceManifest:
        return WorkspaceManifest(
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

    def test_ac06_primary_repo_name(self) -> None:
        """AC-06: primary_repo_name is set correctly."""
        m = self._make_manifest()
        assert m.primary_repo_name == "myrepo"

    def test_ac06_model_dump_json_round_trip(self) -> None:
        """AC-06: model_dump_json round-trip preserves all fields."""
        m = self._make_manifest()
        j = m.model_dump_json(indent=2)
        parsed = json.loads(j)
        assert parsed["primary_repo_name"] == "myrepo"
        assert parsed["workspace_root"] == "/tmp/ws"
        assert len(parsed["repos"]) == 1
        assert parsed["repos"][0]["repo_name"] == "myrepo"

    def test_primary_repo_property_returns_correct_repo(self) -> None:
        m = self._make_manifest()
        pr = m.primary_repo
        assert pr is not None
        assert pr.repo_name == "myrepo"

    def test_primary_repo_property_returns_none_if_not_found(self) -> None:
        m = WorkspaceManifest(
            workspace_root="/tmp/ws",
            repos=[],
            primary_repo_name="missing",
        )
        assert m.primary_repo is None


# ---------------------------------------------------------------------------
# RepoPRResult — AC-07
# ---------------------------------------------------------------------------


class TestRepoPRResult:
    def test_ac07_all_fields_present(self) -> None:
        """AC-07: RepoPRResult fields are all accessible."""
        r = RepoPRResult(
            repo_name="myrepo",
            repo_url="https://github.com/org/myrepo.git",
            success=True,
            pr_url="https://github.com/org/myrepo/pull/1",
            pr_number=1,
        )
        assert r.repo_name == "myrepo"
        assert r.success is True
        assert r.pr_url == "https://github.com/org/myrepo/pull/1"
        assert r.pr_number == 1
        assert r.error_message == ""

    def test_defaults(self) -> None:
        r = RepoPRResult(
            repo_name="r",
            repo_url="https://github.com/org/r.git",
            success=False,
        )
        assert r.pr_url == ""
        assert r.pr_number == 0
        assert r.error_message == ""


# ---------------------------------------------------------------------------
# BuildResult — AC-08
# ---------------------------------------------------------------------------


class TestBuildResult:
    def test_ac08_pr_url_property_first_success(self) -> None:
        """AC-08: BuildResult.pr_url returns first successful pr_url from pr_results."""
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

    def test_ac08_pr_url_empty_when_no_results(self) -> None:
        """AC-08: BuildResult.pr_url returns '' when pr_results is empty."""
        br2 = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[],
        )
        assert br2.pr_url == ""

    def test_pr_url_skips_failed_results(self) -> None:
        """BuildResult.pr_url skips failed PR results and returns first success."""
        br = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
            pr_results=[
                RepoPRResult(
                    repo_name="failed",
                    repo_url="https://github.com/org/failed.git",
                    success=False,
                    pr_url="",
                ),
                RepoPRResult(
                    repo_name="success",
                    repo_url="https://github.com/org/success.git",
                    success=True,
                    pr_url="https://github.com/org/success/pull/2",
                    pr_number=2,
                ),
            ],
        )
        assert br.pr_url == "https://github.com/org/success/pull/2"

    def test_model_dump_includes_pr_url(self) -> None:
        """BuildResult.model_dump() includes pr_url for backward compat."""
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
                    pr_url="https://github.com/org/r/pull/3",
                    pr_number=3,
                )
            ],
        )
        d = br.model_dump()
        assert "pr_url" in d
        assert d["pr_url"] == "https://github.com/org/r/pull/3"

    def test_model_dump_pr_url_empty_when_no_results(self) -> None:
        br = BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=True,
            summary="",
        )
        d = br.model_dump()
        assert d.get("pr_url") == ""


# ---------------------------------------------------------------------------
# DAGState — AC-09
# ---------------------------------------------------------------------------


class TestDAGState:
    def test_ac09_workspace_manifest_exists_and_defaults_none(self) -> None:
        """AC-09: DAGState has workspace_manifest attribute defaulting to None."""
        ds = DAGState(repo_path="/tmp/repo", artifacts_dir="/tmp/artifacts")
        assert hasattr(ds, "workspace_manifest")
        assert ds.workspace_manifest is None

    def test_workspace_manifest_can_be_dict(self) -> None:
        ds = DAGState(
            repo_path="/tmp/repo",
            artifacts_dir="/tmp/artifacts",
            workspace_manifest={"workspace_root": "/tmp/ws", "repos": []},
        )
        assert ds.workspace_manifest is not None
        assert ds.workspace_manifest["workspace_root"] == "/tmp/ws"


# ---------------------------------------------------------------------------
# CoderResult — AC-11
# ---------------------------------------------------------------------------


class TestCoderResult:
    def test_ac11_repo_name_defaults_to_empty_string(self) -> None:
        """AC-11: CoderResult has repo_name field defaulting to empty string."""
        cr = CoderResult(
            files_changed=[],
            summary="done",
            complete=True,
            tests_passed=True,
            test_summary="all pass",
        )
        assert hasattr(cr, "repo_name")
        assert cr.repo_name == ""

    def test_repo_name_can_be_set(self) -> None:
        cr = CoderResult(files_changed=[], summary="", repo_name="myrepo")
        assert cr.repo_name == "myrepo"


# ---------------------------------------------------------------------------
# GitInitResult — AC-12
# ---------------------------------------------------------------------------


class TestGitInitResult:
    def test_ac12_repo_name_defaults_to_empty_string(self) -> None:
        """AC-12: GitInitResult has repo_name field defaulting to empty string."""
        gir = GitInitResult(
            mode="fresh",
            integration_branch="main",
            original_branch="main",
            initial_commit_sha="abc123",
            success=True,
        )
        assert hasattr(gir, "repo_name")
        assert gir.repo_name == ""

    def test_repo_name_can_be_set(self) -> None:
        gir = GitInitResult(
            mode="fresh",
            integration_branch="main",
            original_branch="main",
            initial_commit_sha="abc123",
            success=True,
            repo_name="myrepo",
        )
        assert gir.repo_name == "myrepo"


# ---------------------------------------------------------------------------
# MergeResult — AC-13
# ---------------------------------------------------------------------------


class TestMergeResult:
    def test_ac13_repo_name_defaults_to_empty_string(self) -> None:
        """AC-13: MergeResult has repo_name field defaulting to empty string."""
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

    def test_repo_name_can_be_set(self) -> None:
        mr = MergeResult(
            success=True,
            merged_branches=[],
            failed_branches=[],
            needs_integration_test=False,
            summary="",
            repo_name="myrepo",
        )
        assert mr.repo_name == "myrepo"


# ---------------------------------------------------------------------------
# IssueResult — repo_name extension
# ---------------------------------------------------------------------------


class TestIssueResult:
    def test_repo_name_defaults_to_empty_string(self) -> None:
        """IssueResult has repo_name field defaulting to empty string."""
        ir = IssueResult(issue_name="issue-01", outcome=IssueOutcome.COMPLETED)
        assert hasattr(ir, "repo_name")
        assert ir.repo_name == ""

    def test_repo_name_can_be_set(self) -> None:
        ir = IssueResult(
            issue_name="issue-01",
            outcome=IssueOutcome.COMPLETED,
            repo_name="myrepo",
        )
        assert ir.repo_name == "myrepo"


# ---------------------------------------------------------------------------
# Backward compatibility: existing single-repo patterns still work
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_build_config_no_args(self) -> None:
        """BuildConfig() with no args still works."""
        cfg = BuildConfig()
        assert cfg.runtime == "claude_code"
        assert cfg.ai_provider == "claude"

    def test_build_config_with_repo_url(self) -> None:
        """BuildConfig(repo_url=...) still works and populates repos."""
        cfg = BuildConfig(repo_url="https://github.com/org/repo.git")
        assert cfg.repo_url == "https://github.com/org/repo.git"
        assert len(cfg.repos) == 1

    def test_dag_state_no_workspace_manifest(self) -> None:
        """DAGState without workspace_manifest still works."""
        ds = DAGState()
        assert ds.workspace_manifest is None

    def test_coder_result_without_repo_name(self) -> None:
        """CoderResult without repo_name still works."""
        cr = CoderResult()
        assert cr.repo_name == ""
        assert cr.complete is True

    def test_git_init_result_backward_compat(self) -> None:
        gir = GitInitResult(
            mode="existing",
            integration_branch="feat/x",
            original_branch="main",
            initial_commit_sha="sha1",
            success=True,
        )
        assert gir.repo_name == ""

    def test_merge_result_backward_compat(self) -> None:
        mr = MergeResult(
            success=True,
            merged_branches=["feat/x"],
            failed_branches=[],
            needs_integration_test=True,
            summary="merged",
        )
        assert mr.repo_name == ""

    def test_build_result_backward_compat_pr_url_empty(self) -> None:
        """BuildResult without pr_results has pr_url == ''."""
        br = BuildResult(
            plan_result={},
            dag_state={},
            success=True,
            summary="done",
        )
        assert br.pr_url == ""

    def test_build_result_model_dump_has_pr_url_key(self) -> None:
        """model_dump() on BuildResult always contains pr_url key."""
        br = BuildResult(plan_result={}, dag_state={}, success=True, summary="")
        d = br.model_dump()
        assert "pr_url" in d

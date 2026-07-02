"""Functional tests for the swe_af/app.py execute() reasoner.

Tests verify behavioral correctness of the execute() routing logic without
making real API calls.  All external I/O is mocked via ``mock_agent_ai``
(which patches ``swe_af.app.app.call``) and a DAG-level patch on
``swe_af.execution.dag_executor.run_dag``.

AC-9: both test_execute_single_issue and test_execute_with_external must be
discoverable and pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_plan_result(issues: list[dict] | None = None) -> dict:
    """Build a minimal PlanResult dict accepted by _init_dag_state."""
    if issues is None:
        issues = [
            {
                "name": "implement-feature",
                "title": "Implement the feature",
                "description": "Write the code.",
                "acceptance_criteria": ["Feature works correctly"],
                "depends_on": [],
                "files_to_create": ["src/feature.py"],
                "files_to_modify": [],
            }
        ]
    return {
        "issues": issues,
        "levels": [[i["name"] for i in issues]],
        "rationale": "Test plan",
        "artifacts_dir": "",
        "prd": {"validated_description": "Test PRD", "acceptance_criteria": []},
        "architecture": {"summary": "Test architecture"},
        "file_conflicts": [],
    }


def _write_checkpoint(repo_path: Path, checkpoint: dict) -> None:
    checkpoint_path = repo_path / ".artifacts" / "execution" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")


def _make_dag_state(completed: list[str], failed: list[str]) -> DAGState:
    """Build a DAGState with given completed / failed issue names."""
    return DAGState(
        repo_path="/tmp/test-repo",
        completed_issues=[
            IssueResult(
                issue_name=name,
                outcome=IssueOutcome.COMPLETED,
                result_summary="Done",
            )
            for name in completed
        ],
        failed_issues=[
            IssueResult(
                issue_name=name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message="Failed",
            )
            for name in failed
        ],
    )


def test_resume_incomplete_summary_empty_for_finished_dag():
    import swe_af.app as app_module

    result = {
        "all_issues": [{"name": "one"}],
        "completed_issues": [{"issue_name": "one"}],
        "failed_issues": [],
        "skipped_issues": [],
    }

    assert app_module._resume_incomplete_summary(result) == ""


def test_resume_incomplete_summary_flags_failed_and_skipped_dag():
    import swe_af.app as app_module

    result = {
        "all_issues": [{"name": "one"}, {"name": "two"}, {"name": "three"}],
        "completed_issues": [{"issue_name": "one"}],
        "failed_issues": [{"issue_name": "two"}],
        "skipped_issues": ["three"],
    }

    summary = app_module._resume_incomplete_summary(result)

    assert "Resume incomplete: 1/3 issues completed" in summary
    assert "failed=['two']" in summary
    assert "skipped=['three']" in summary


def test_build_config_saved_state_round_trips_normalized_repo_url():
    import swe_af.app as app_module
    from swe_af.execution.schemas import BuildConfig

    saved_config = BuildConfig(
        repo_url="https://github.com/example/repo.git",
    ).model_dump()

    cfg = app_module._build_config_from_saved_state(saved_config)

    assert cfg.repo_url == "https://github.com/example/repo.git"
    assert len(cfg.repos) == 1


@pytest.mark.asyncio
async def test_resume_execute_only_resumes_dag(tmp_path: Path):
    import swe_af.app as app_module

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    plan_result = _make_plan_result()
    completed_result = {
        "all_issues": plan_result["issues"],
        "completed_issues": [{"issue_name": "implement-feature"}],
        "failed_issues": [],
        "skipped_issues": [],
        "merged_branches": ["feature-branch"],
    }
    _write_checkpoint(
        repo_path,
        {
            "all_issues": plan_result["issues"],
            "levels": plan_result["levels"],
            "artifacts_dir": str(repo_path / ".artifacts"),
            "git_integration_branch": "integration",
        },
    )

    async def fake_call(target: str, **kwargs):
        assert target == f"{app_module.NODE_ID}.execute"
        assert kwargs["resume"] is True
        return completed_result

    with patch.object(app_module.app, "call", new=AsyncMock(side_effect=fake_call)):
        result = await app_module.resume_execute(repo_path=str(repo_path))

    assert result == completed_result


@pytest.mark.asyncio
async def test_resume_build_continues_full_tail(tmp_path: Path):
    import swe_af.app as app_module

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    plan_result = _make_plan_result()
    dag_result = {
        "all_issues": plan_result["issues"],
        "completed_issues": [{"issue_name": "implement-feature"}],
        "failed_issues": [],
        "skipped_issues": [],
        "merged_branches": ["feature-branch"],
        "accumulated_debt": [],
    }
    git_config = {
        "integration_branch": "integration",
        "original_branch": "main",
        "initial_commit_sha": "abc123",
        "mode": "existing",
        "remote_url": "https://github.com/example/repo.git",
        "remote_default_branch": "main",
    }
    _write_checkpoint(
        repo_path,
        {
            "all_issues": plan_result["issues"],
            "levels": plan_result["levels"],
            "artifacts_dir": str(repo_path / ".artifacts"),
            "git_integration_branch": "integration",
        },
    )
    app_module._save_build_state(
        str(repo_path),
        ".artifacts",
        {
            "goal": "ship feature",
            "repo_path": str(repo_path),
            "repo_url": "https://github.com/example/repo.git",
            "artifacts_dir": ".artifacts",
            "config": {
                "runtime": "codex",
                "models": {"default": "gpt-5.5"},
                "enable_github_pr": True,
                "check_ci": False,
            },
            "plan_result": plan_result,
            "git_config": git_config,
        },
    )

    calls: list[str] = []

    async def fake_call(target: str, **kwargs):
        calls.append(target)
        if target == f"{app_module.NODE_ID}.execute":
            assert kwargs["resume"] is True
            return dag_result
        if target == f"{app_module.NODE_ID}.run_verifier":
            return {"passed": True, "summary": "ok", "criteria_results": []}
        if target == f"{app_module.NODE_ID}.run_repo_finalize":
            return {"success": True, "summary": "clean"}
        if target == f"{app_module.NODE_ID}.run_github_pr":
            return {
                "success": True,
                "pr_url": "https://github.com/example/repo/pull/1",
                "pr_number": 1,
            }
        raise AssertionError(f"unexpected call: {target}")

    with (
        patch.object(app_module.app, "call", new=AsyncMock(side_effect=fake_call)),
        patch.object(app_module, "_existing_pr_for_branch", return_value={}),
        patch.object(app_module, "_append_plan_docs_to_pr"),
    ):
        result = await app_module.resume_build(repo_path=str(repo_path))

    assert result["success"] is True
    assert calls == [
        f"{app_module.NODE_ID}.execute",
        f"{app_module.NODE_ID}.run_verifier",
        f"{app_module.NODE_ID}.run_repo_finalize",
        f"{app_module.NODE_ID}.run_github_pr",
    ]


# ---------------------------------------------------------------------------
# test_execute_single_issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_single_issue(mock_agent_ai):
    """execute() with a single no-dependency issue returns a dict with
    completed/failed issue counts derived from the DAGState returned by run_dag.

    Verifies:
    - The returned value is a dict (model_dump of DAGState).
    - completed_issues contains the single issue result.
    - failed_issues is empty.
    - No real app.call invocations occur (mock_agent_ai is never called).
    """
    plan_result = _make_plan_result()
    expected_state = _make_dag_state(completed=["implement-feature"], failed=[])

    with patch(
        "swe_af.execution.dag_executor.run_dag",
        new=AsyncMock(return_value=expected_state),
    ) as mock_run_dag:
        # Import the actual execute function (the raw async function, not the
        # reasoner-wrapped version) so we can call it directly in the test.
        import swe_af.app as app_module

        result = await app_module.execute(
            plan_result=plan_result,
            repo_path="/tmp/test-repo",
        )

    # The result must be a dict (DAGState.model_dump())
    assert isinstance(result, dict), "execute() must return a dict"

    # run_dag was called exactly once
    mock_run_dag.assert_called_once()

    # Verify completed / failed counts in the returned dict
    assert len(result["completed_issues"]) == 1, "Expected 1 completed issue"
    assert result["completed_issues"][0]["issue_name"] == "implement-feature"
    assert len(result["failed_issues"]) == 0, "Expected 0 failed issues"

    # The mock app.call must not have been invoked (no real API calls)
    mock_agent_ai.assert_not_called()


# ---------------------------------------------------------------------------
# test_execute_with_external
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_with_external(mock_agent_ai):
    """execute() with execute_fn_target set passes the target through correctly.

    When execute_fn_target is non-empty the execute() reasoner constructs a
    closure that calls app.call(execute_fn_target, ...).  This test verifies:
    - run_dag is called with a non-None execute_fn (the external path).
    - When execute_fn is invoked, it calls app.call with the expected target.
    - The returned dict is a valid DAGState dump.
    """
    plan_result = _make_plan_result()
    expected_state = _make_dag_state(completed=["implement-feature"], failed=[])
    external_target = "coder-agent.code_issue"

    # Capture execute_fn passed to run_dag for later inspection
    captured: dict = {}

    async def fake_run_dag(
        plan_result,
        repo_path,
        execute_fn=None,
        **kwargs,
    ):
        captured["execute_fn"] = execute_fn
        return expected_state

    with patch(
        "swe_af.execution.dag_executor.run_dag",
        new=fake_run_dag,
    ):
        import swe_af.app as app_module

        result = await app_module.execute(
            plan_result=plan_result,
            repo_path="/tmp/test-repo",
            execute_fn_target=external_target,
        )

    # execute_fn must have been passed (not None) when execute_fn_target is set
    assert captured.get("execute_fn") is not None, (
        "execute() must pass a non-None execute_fn to run_dag when "
        "execute_fn_target is non-empty"
    )

    # Invoke the captured execute_fn to trigger app.call and verify the target
    mock_issue = {"name": "implement-feature"}
    mock_dag_state = MagicMock()
    mock_dag_state.repo_path = "/tmp/test-repo"

    # mock_agent_ai already patches app.call; configure its return value
    mock_agent_ai.return_value = {
        "outcome": "completed",
        "result_summary": "Done",
        "files_changed": [],
        "branch_name": "",
        "error_message": "",
    }

    await captured["execute_fn"](mock_issue, mock_dag_state)

    # Verify app.call was called with the external target
    assert mock_agent_ai.call_count >= 1, "execute_fn must call app.call"
    first_call = mock_agent_ai.call_args_list[0]
    assert first_call.args[0] == external_target, (
        f"execute_fn must call app.call('{external_target}', ...), "
        f"got {first_call.args[0]!r}"
    )

    # Result must still be a valid dict
    assert isinstance(result, dict), "execute() must return a dict"

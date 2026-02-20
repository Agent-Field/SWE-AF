"""Tests for CoderResult.repo_name propagation to IssueResult in run_coding_loop().

Covers:
- AC: CoderResult.repo_name is propagated to IssueResult.repo_name in success path
- Edge case: empty repo_name passes through unchanged
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from swe_af.execution.coding_loop import run_coding_loop
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome


def _make_dag_state(tmp_path) -> DAGState:
    """Create a minimal DAGState pointing to a temp directory."""
    artifacts_dir = str(tmp_path / "artifacts")
    return DAGState(
        repo_path=str(tmp_path),
        artifacts_dir=artifacts_dir,
    )


def _make_call_fn(coder_result: dict, reviewer_result: dict):
    """Return a call_fn that dispatches coder/reviewer results by node suffix."""

    async def _coro(*args, **kwargs):
        return coder_result if "run_coder" in args[0] else reviewer_result

    def call_fn(node_role: str, **kwargs):
        return _coro(node_role, **kwargs)

    return call_fn


class TestRepoNamePropagation:
    def test_repo_name_propagated_on_approve(self, tmp_path):
        """CoderResult.repo_name='api' is propagated to IssueResult.repo_name on approval."""
        coder_result = {
            "files_changed": ["src/main.py"],
            "summary": "done",
            "complete": True,
            "repo_name": "api",
        }
        reviewer_result = {
            "approved": True,
            "blocking": False,
            "summary": "looks good",
        }

        call_fn = _make_call_fn(coder_result, reviewer_result)
        dag_state = _make_dag_state(tmp_path)
        config = ExecutionConfig(max_coding_iterations=1)
        issue = {"name": "test-issue", "branch_name": "feat/test"}

        result = asyncio.run(
            run_coding_loop(
                issue=issue,
                dag_state=dag_state,
                call_fn=call_fn,
                node_id="node-1",
                config=config,
            )
        )

        assert result.outcome == IssueOutcome.COMPLETED
        assert result.repo_name == "api"

    def test_empty_repo_name_passes_through(self, tmp_path):
        """CoderResult.repo_name='' results in IssueResult.repo_name=='' (backfill in dag_executor)."""
        coder_result = {
            "files_changed": [],
            "summary": "done",
            "complete": True,
            "repo_name": "",
        }
        reviewer_result = {
            "approved": True,
            "blocking": False,
            "summary": "ok",
        }

        call_fn = _make_call_fn(coder_result, reviewer_result)
        dag_state = _make_dag_state(tmp_path)
        config = ExecutionConfig(max_coding_iterations=1)
        issue = {"name": "test-issue-empty", "branch_name": "feat/test-empty"}

        result = asyncio.run(
            run_coding_loop(
                issue=issue,
                dag_state=dag_state,
                call_fn=call_fn,
                node_id="node-2",
                config=config,
            )
        )

        assert result.outcome == IssueOutcome.COMPLETED
        assert result.repo_name == ""

    def test_repo_name_absent_defaults_to_empty(self, tmp_path):
        """CoderResult without repo_name key defaults IssueResult.repo_name to ''."""
        coder_result = {
            "files_changed": [],
            "summary": "done",
            "complete": True,
            # repo_name key intentionally absent
        }
        reviewer_result = {
            "approved": True,
            "blocking": False,
            "summary": "all good",
        }

        call_fn = _make_call_fn(coder_result, reviewer_result)
        dag_state = _make_dag_state(tmp_path)
        config = ExecutionConfig(max_coding_iterations=1)
        issue = {"name": "test-issue-absent", "branch_name": "feat/test-absent"}

        result = asyncio.run(
            run_coding_loop(
                issue=issue,
                dag_state=dag_state,
                call_fn=call_fn,
                node_id="node-3",
                config=config,
            )
        )

        assert result.outcome == IssueOutcome.COMPLETED
        assert result.repo_name == ""

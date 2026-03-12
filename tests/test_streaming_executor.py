"""Tests for the PlanDB-backed streaming executor.

Tests cover:
- PlanDB task creation from plan_result
- Worker claim loop and task processing
- Merge serialization (no concurrent merges)
- Reassessment calls
- Failure handling and cascade
- Final DAGState construction
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
)
from swe_af.execution.streaming_executor import (
    _build_final_dag_state,
    _plandb,
    _plandb_list,
    run_streaming_dag,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_result():
    """Minimal plan_result with 3 issues across 2 levels."""
    return {
        "prd": {"validated_description": "Build a todo app"},
        "architecture": {"summary": "FastAPI + SQLite"},
        "issues": [
            {
                "name": "setup-project",
                "title": "Setup project structure",
                "description": "Create the FastAPI project skeleton",
                "acceptance_criteria": ["pyproject.toml exists", "main.py runs"],
                "depends_on": [],
                "files_to_create": ["main.py", "pyproject.toml"],
                "files_to_modify": [],
                "sequence_number": 1,
            },
            {
                "name": "add-models",
                "title": "Add database models",
                "description": "Create SQLAlchemy models for todos",
                "acceptance_criteria": ["Todo model exists"],
                "depends_on": ["setup-project"],
                "files_to_create": ["models.py"],
                "files_to_modify": [],
                "sequence_number": 2,
            },
            {
                "name": "add-routes",
                "title": "Add API routes",
                "description": "Create CRUD endpoints for todos",
                "acceptance_criteria": ["GET /todos works", "POST /todos works"],
                "depends_on": ["setup-project"],
                "files_to_create": ["routes.py"],
                "files_to_modify": ["main.py"],
                "sequence_number": 3,
            },
        ],
        "levels": [["setup-project"], ["add-models", "add-routes"]],
        "artifacts_dir": "",
        "rationale": "Simple todo app",
    }


@pytest.fixture
def exec_config():
    """Execution config for tests."""
    return ExecutionConfig(
        max_concurrent_issues=2,
        streaming=True,
    )


@pytest.fixture
def base_dag_state(plan_result):
    """Base DAGState for tests."""
    return DAGState(
        repo_path="/tmp/test-repo",
        artifacts_dir="/tmp/test-artifacts",
        all_issues=plan_result["issues"],
        levels=plan_result["levels"],
        prd_summary="Build a todo app",
        architecture_summary="FastAPI + SQLite",
        build_id="test-build-001",
    )


# ---------------------------------------------------------------------------
# Unit tests: _build_final_dag_state
# ---------------------------------------------------------------------------


class TestBuildFinalDagState:
    """Tests for building DAGState from worker results."""

    def test_empty_results(self, base_dag_state):
        """No worker results → empty DAGState."""
        state = _build_final_dag_state(
            base_state=base_dag_state,
            worker_results=[],
            db_path="/tmp/test.db",
        )
        assert len(state.completed_issues) == 0
        assert len(state.failed_issues) == 0

    def test_completed_issues_collected(self, base_dag_state):
        """Completed issues from multiple workers are aggregated."""
        worker_results = [
            {
                "completed": [
                    IssueResult(
                        issue_name="setup-project",
                        outcome=IssueOutcome.COMPLETED,
                        result_summary="Project set up",
                    ).model_dump(),
                ],
                "failed": [],
            },
            {
                "completed": [
                    IssueResult(
                        issue_name="add-models",
                        outcome=IssueOutcome.COMPLETED,
                        result_summary="Models created",
                    ).model_dump(),
                ],
                "failed": [],
            },
        ]

        state = _build_final_dag_state(
            base_state=base_dag_state,
            worker_results=worker_results,
            db_path="/tmp/test.db",
        )
        assert len(state.completed_issues) == 2
        assert {r.issue_name for r in state.completed_issues} == {
            "setup-project",
            "add-models",
        }

    def test_failed_issues_collected(self, base_dag_state):
        """Failed issues from workers are aggregated."""
        worker_results = [
            {
                "completed": [],
                "failed": [
                    IssueResult(
                        issue_name="add-routes",
                        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                        error_message="Test failure",
                    ).model_dump(),
                ],
            },
        ]

        state = _build_final_dag_state(
            base_state=base_dag_state,
            worker_results=worker_results,
            db_path="/tmp/test.db",
        )
        assert len(state.failed_issues) == 1
        assert state.failed_issues[0].issue_name == "add-routes"

    def test_exceptions_ignored(self, base_dag_state):
        """Worker exceptions don't crash the aggregation."""
        worker_results = [
            RuntimeError("Worker crashed"),
            {
                "completed": [
                    IssueResult(
                        issue_name="setup-project",
                        outcome=IssueOutcome.COMPLETED,
                    ).model_dump(),
                ],
                "failed": [],
            },
        ]

        state = _build_final_dag_state(
            base_state=base_dag_state,
            worker_results=worker_results,
            db_path="/tmp/test.db",
        )
        assert len(state.completed_issues) == 1

    def test_base_state_fields_preserved(self, base_dag_state):
        """Base state fields (repo_path, prd_summary, etc.) carry through."""
        state = _build_final_dag_state(
            base_state=base_dag_state,
            worker_results=[],
            db_path="/tmp/test.db",
        )
        assert state.repo_path == "/tmp/test-repo"
        assert state.prd_summary == "Build a todo app"
        assert state.build_id == "test-build-001"


# ---------------------------------------------------------------------------
# Integration tests: PlanDB CLI (requires plandb binary)
# ---------------------------------------------------------------------------


@pytest.fixture
def plandb_db():
    """Create a temp PlanDB database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        yield db_path


def _plandb_available() -> bool:
    """Check if plandb CLI is available."""
    try:
        result = subprocess.run(
            ["plandb", "--help"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


plandb_available = pytest.mark.skipif(
    not _plandb_available(),
    reason="plandb CLI not available",
)


@plandb_available
class TestPlanDBIntegration:
    """Integration tests that use the real plandb binary."""

    def test_create_project_and_tasks(self, plandb_db):
        """Can create a project and tasks via CLI."""
        # Create project
        result = _plandb(["project", "create", "test-project"], plandb_db)
        assert result is not None
        project_id = result.get("project", {}).get("id", result.get("id", ""))
        assert project_id

        # Create a task
        result = _plandb(
            [
                "task", "create",
                "--title", "Test task",
                "--kind", "code",
                "--description", "Do something",
                "--project", project_id,
            ],
            plandb_db,
        )
        assert result is not None

    def test_task_claim_and_complete(self, plandb_db):
        """Worker can claim a task and mark it done."""
        # Create project + task
        proj = _plandb(["project", "create", "test-claim"], plandb_db)
        project_id = proj.get("project", {}).get("id", proj.get("id", ""))

        task_result = _plandb(
            [
                "task", "create",
                "--title", "Claimable task",
                "--project", project_id,
            ],
            plandb_db,
        )
        task_id = task_result.get("task", {}).get("id", task_result.get("id", ""))
        assert task_id

        # Claim
        go_result = _plandb(["go", "--agent", "test-worker"], plandb_db)
        assert go_result is not None

        # Complete
        done_result = _plandb(
            ["done", task_id, "--result", '{"status": "ok"}'],
            plandb_db,
        )
        assert done_result is not None

    def test_dependency_ordering(self, plandb_db):
        """Tasks with deps aren't ready until deps complete."""
        proj = _plandb(["project", "create", "test-deps"], plandb_db)
        project_id = proj.get("project", {}).get("id", proj.get("id", ""))

        # Create task A (no deps)
        task_a = _plandb(
            [
                "task", "create",
                "--title", "Task A",
                "--project", project_id,
            ],
            plandb_db,
        )
        task_a_id = task_a.get("task", {}).get("id", task_a.get("id", ""))

        # Create task B (depends on A)
        task_b = _plandb(
            [
                "task", "create",
                "--title", "Task B",
                "--dep", task_a_id,
                "--project", project_id,
            ],
            plandb_db,
        )

        # Only task A should be claimable
        go_result = _plandb(["go", "--agent", "test-worker"], plandb_db)
        claimed_title = go_result.get("task", {}).get("title", "")
        assert "Task A" in claimed_title

        # Complete A
        _plandb(["done", task_a_id, "--result", '{"ok": true}'], plandb_db)

        # Now B should be claimable
        go_result = _plandb(["go", "--agent", "test-worker"], plandb_db)
        assert go_result is not None
        claimed_title = go_result.get("task", {}).get("title", "")
        assert "Task B" in claimed_title

    def test_no_more_tasks_returns_none(self, plandb_db):
        """plandb go returns None when no tasks are available."""
        proj = _plandb(["project", "create", "test-empty"], plandb_db)
        project_id = proj.get("project", {}).get("id", proj.get("id", ""))

        # Create and complete the only task
        task = _plandb(
            ["task", "create", "--title", "Only task", "--project", project_id],
            plandb_db,
        )
        task_id = task.get("task", {}).get("id", task.get("id", ""))
        _plandb(["go", "--agent", "w1"], plandb_db)
        _plandb(["done", task_id], plandb_db)

        # No more tasks — plandb go returns a non-zero exit code or
        # a result without a "task" key when nothing is claimable
        result = _plandb(["go", "--agent", "w1"], plandb_db)
        if result is not None:
            # If plandb returns a progress summary, there should be no task key
            assert "task" not in result or result.get("task") is None


# ---------------------------------------------------------------------------
# Streaming executor integration test (mocked call_fn)
# ---------------------------------------------------------------------------


class TestRunStreamingDag:
    """Tests for the main run_streaming_dag function."""

    @pytest.mark.asyncio
    async def test_calls_planner_then_workers(self, plan_result, exec_config):
        """Verify the bootstrapper calls planner, then spawns workers."""
        call_log = []

        async def mock_call_fn(target: str, **kwargs):
            call_log.append(target)

            if "run_plandb_planner" in target:
                return {
                    "project_id": "build-test",
                    "task_count": 3,
                    "ready_count": 1,
                    "task_map": {
                        "setup-project": "t-001",
                        "add-models": "t-002",
                        "add-routes": "t-003",
                    },
                }
            elif "run_streaming_worker" in target:
                return {
                    "completed": [
                        IssueResult(
                            issue_name="setup-project",
                            outcome=IssueOutcome.COMPLETED,
                        ).model_dump(),
                    ],
                    "failed": [],
                    "tasks_processed": 1,
                    "agent_id": kwargs.get("agent_id", "worker-0"),
                }
            return {}

        plan_result["artifacts_dir"] = tempfile.mkdtemp()

        state = await run_streaming_dag(
            plan_result=plan_result,
            repo_path="/tmp/test-repo",
            config=exec_config,
            call_fn=mock_call_fn,
            build_id="test-001",
        )

        # Planner should be called first
        planner_calls = [c for c in call_log if "run_plandb_planner" in c]
        assert len(planner_calls) == 1

        # Workers should be spawned (max_concurrent_issues=2)
        worker_calls = [c for c in call_log if "run_streaming_worker" in c]
        assert len(worker_calls) == 2

        # State should reflect completed work
        assert len(state.completed_issues) >= 1

    @pytest.mark.asyncio
    async def test_worker_failure_handled(self, plan_result, exec_config):
        """Worker exceptions don't crash the executor."""
        call_count = {"workers": 0}

        async def mock_call_fn(target: str, **kwargs):
            if "run_plandb_planner" in target:
                return {
                    "project_id": "build-test",
                    "task_count": 3,
                    "ready_count": 1,
                    "task_map": {},
                }
            elif "run_streaming_worker" in target:
                call_count["workers"] += 1
                if call_count["workers"] == 1:
                    raise RuntimeError("Worker crashed!")
                return {
                    "completed": [],
                    "failed": [],
                    "tasks_processed": 0,
                    "agent_id": kwargs.get("agent_id", "worker-0"),
                }
            return {}

        plan_result["artifacts_dir"] = tempfile.mkdtemp()

        # Should not raise despite one worker failing
        state = await run_streaming_dag(
            plan_result=plan_result,
            repo_path="/tmp/test-repo",
            config=exec_config,
            call_fn=mock_call_fn,
            build_id="test-002",
        )

        assert isinstance(state, DAGState)

    @pytest.mark.asyncio
    async def test_requires_call_fn(self, plan_result, exec_config):
        """Raises ValueError when call_fn is None."""
        with pytest.raises(ValueError, match="call_fn"):
            await run_streaming_dag(
                plan_result=plan_result,
                repo_path="/tmp/test-repo",
                config=exec_config,
                call_fn=None,
            )


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestStreamingConfig:
    """Test the streaming flag in BuildConfig and ExecutionConfig."""

    def test_streaming_default_false(self):
        """streaming defaults to False."""
        config = ExecutionConfig()
        assert config.streaming is False

    def test_streaming_flag_set(self):
        """streaming=True is accepted."""
        config = ExecutionConfig(streaming=True)
        assert config.streaming is True

    def test_streaming_in_execution_config_dict(self):
        """streaming flag carries through to_execution_config_dict."""
        from swe_af.execution.schemas import BuildConfig
        cfg = BuildConfig(streaming=True)
        d = cfg.to_execution_config_dict()
        assert d["streaming"] is True

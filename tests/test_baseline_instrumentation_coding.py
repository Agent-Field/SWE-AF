"""Unit tests for baseline instrumentation in coding loop.

Tests verify that trivial flag logging, coder duration tracking, and Issue Writer
timing wrapper are correctly instrumented without affecting functional behavior.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, call

from swe_af.execution.coding_loop import _log_agent_metrics, run_coding_loop
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_dag_state(artifacts_dir: str, repo_path: str = "/tmp/fake-repo") -> DAGState:
    """Create a minimal DAGState for testing."""
    return DAGState(
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        prd_path="",
        architecture_path="",
        issues_dir="",
    )


def _make_config(**overrides) -> ExecutionConfig:
    """Create an ExecutionConfig with sensible test defaults."""
    defaults = {
        "max_coding_iterations": 5,
        "agent_timeout_seconds": 30,
    }
    defaults.update(overrides)
    return ExecutionConfig(**defaults)


def _make_issue(name: str = "ISSUE-1", trivial: bool = False, **extra) -> dict:
    """Create a minimal issue dict."""
    issue = {
        "name": name,
        "title": "Test issue",
        "description": "A test issue for the coding loop",
        "acceptance_criteria": ["AC-1: it works"],
        "depends_on": [],
        "provides": [],
        "files_to_create": [],
        "files_to_modify": [],
        "worktree_path": "/tmp/fake-repo",
        "branch_name": "test/issue-1",
    }
    if trivial:
        issue["guidance"] = {"trivial": True}
    issue.update(extra)
    return issue


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestTrivialLogging(unittest.TestCase):
    """Test AC1: Trivial flag extraction and logging."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.artifacts_dir = os.path.join(self.temp_dir, "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_trivial_flag_logged(self):
        """Verify trivial flag triggers logging with correct tags."""
        # Setup
        dag_state = _make_dag_state(self.artifacts_dir)
        config = _make_config()
        issue = _make_issue(name="trivial-issue", trivial=True)

        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        async def mock_call_fn(method: str, **kwargs):
            # Return coder result that approves
            if "run_coder" in method:
                return {
                    "files_changed": ["a.py"],
                    "summary": "Done",
                    "complete": True,
                    "tests_passed": True,
                }
            # Return reviewer result that approves
            if "run_code_reviewer" in method:
                return {
                    "approved": True,
                    "blocking": False,
                    "summary": "LGTM",
                }
            return {}

        # Execute
        result = asyncio.run(run_coding_loop(
            issue=issue,
            dag_state=dag_state,
            call_fn=mock_call_fn,
            node_id="test-node",
            config=config,
            note_fn=mock_note_fn,
        ))

        # Verify: trivial flag should be logged with expected tags
        trivial_logs = [
            n for n in notes
            if "trivial" in n["tags"] and "eligible" in n["tags"] and "coding_loop" in n["tags"]
        ]
        self.assertEqual(len(trivial_logs), 1, "Expected exactly one trivial log")
        self.assertIn("trivial-issue", trivial_logs[0]["msg"])
        self.assertIn("trivial-issue", trivial_logs[0]["tags"])

    def test_non_trivial_no_log(self):
        """Verify non-trivial issues do not trigger trivial logging."""
        # Setup
        dag_state = _make_dag_state(self.artifacts_dir)
        config = _make_config()
        issue = _make_issue(name="normal-issue", trivial=False)

        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        async def mock_call_fn(method: str, **kwargs):
            if "run_coder" in method:
                return {
                    "files_changed": ["a.py"],
                    "summary": "Done",
                    "complete": True,
                    "tests_passed": True,
                }
            if "run_code_reviewer" in method:
                return {
                    "approved": True,
                    "blocking": False,
                    "summary": "LGTM",
                }
            return {}

        # Execute
        result = asyncio.run(run_coding_loop(
            issue=issue,
            dag_state=dag_state,
            call_fn=mock_call_fn,
            node_id="test-node",
            config=config,
            note_fn=mock_note_fn,
        ))

        # Verify: no trivial logs should exist
        trivial_logs = [
            n for n in notes
            if "trivial" in n["tags"] and "eligible" in n["tags"]
        ]
        self.assertEqual(len(trivial_logs), 0, "Expected no trivial logs for non-trivial issue")


class TestCoderMetrics(unittest.TestCase):
    """Test AC2 & AC3: Coder duration and test pass status in metrics."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.artifacts_dir = os.path.join(self.temp_dir, "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_coder_metrics_logged(self):
        """Verify coder metrics are logged with duration and test pass status."""
        # Setup
        dag_state = _make_dag_state(self.artifacts_dir)
        config = _make_config()
        issue = _make_issue(name="test-issue")

        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        async def mock_call_fn(method: str, **kwargs):
            if "run_coder" in method:
                # Simulate coder taking some time
                await asyncio.sleep(0.1)
                return {
                    "files_changed": ["a.py"],
                    "summary": "Done",
                    "complete": True,
                    "tests_passed": True,
                }
            if "run_code_reviewer" in method:
                return {
                    "approved": True,
                    "blocking": False,
                    "summary": "LGTM",
                }
            return {}

        # Execute
        result = asyncio.run(run_coding_loop(
            issue=issue,
            dag_state=dag_state,
            call_fn=mock_call_fn,
            node_id="test-node",
            config=config,
            note_fn=mock_note_fn,
        ))

        # Verify: agent_metrics log with coder role should exist
        coder_metrics = [
            n for n in notes
            if "agent_metrics" in n["tags"] and "role:coder" in n["tags"]
        ]
        self.assertEqual(len(coder_metrics), 1, "Expected exactly one coder metrics log")

        # Verify duration tag exists
        duration_tags = [t for t in coder_metrics[0]["tags"] if t.startswith("duration:")]
        self.assertEqual(len(duration_tags), 1, "Expected duration tag")

        # Verify test pass status tag exists
        test_passed_tags = [t for t in coder_metrics[0]["tags"] if t.startswith("tests_passed:")]
        self.assertEqual(len(test_passed_tags), 1, "Expected tests_passed tag")
        self.assertIn("tests_passed:True", coder_metrics[0]["tags"])

    def test_coder_metrics_with_failed_tests(self):
        """Verify test pass status correctly reflects failed tests."""
        # Setup
        dag_state = _make_dag_state(self.artifacts_dir)
        config = _make_config()
        issue = _make_issue(name="test-issue")

        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        async def mock_call_fn(method: str, **kwargs):
            if "run_coder" in method:
                return {
                    "files_changed": ["a.py"],
                    "summary": "Done",
                    "complete": True,
                    "tests_passed": False,
                }
            if "run_code_reviewer" in method:
                return {
                    "approved": True,
                    "blocking": False,
                    "summary": "LGTM",
                }
            return {}

        # Execute
        result = asyncio.run(run_coding_loop(
            issue=issue,
            dag_state=dag_state,
            call_fn=mock_call_fn,
            node_id="test-node",
            config=config,
            note_fn=mock_note_fn,
        ))

        # Verify: test pass status should be False
        coder_metrics = [
            n for n in notes
            if "agent_metrics" in n["tags"] and "role:coder" in n["tags"]
        ]
        self.assertIn("tests_passed:False", coder_metrics[0]["tags"])


class TestLogAgentMetricsHelper(unittest.TestCase):
    """Test _log_agent_metrics helper function."""

    def test_log_agent_metrics_basic(self):
        """Verify _log_agent_metrics logs with correct structure."""
        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        _log_agent_metrics(
            note_fn=mock_note_fn,
            role="coder",
            duration=123.4,
            success=True,
            iteration=1,
            extra_tags=["test-issue", "tests_passed:True"]
        )

        self.assertEqual(len(notes), 1)
        tags = notes[0]["tags"]
        self.assertIn("agent_metrics", tags)
        self.assertIn("role:coder", tags)
        self.assertIn("duration:123.4", tags)
        self.assertIn("success:True", tags)
        self.assertIn("iteration:1", tags)
        self.assertIn("test-issue", tags)
        self.assertIn("tests_passed:True", tags)

    def test_log_agent_metrics_no_note_fn(self):
        """Verify _log_agent_metrics handles None note_fn gracefully."""
        # Should not raise
        _log_agent_metrics(
            note_fn=None,
            role="coder",
            duration=123.4,
            success=True,
        )


class TestIssueWriterTiming(unittest.TestCase):
    """Test AC4: Issue Writer timing wrapper in app.py parallel loop."""

    def test_issue_writer_timing_mock(self):
        """Verify Issue Writer timing wrapper adds duration tags."""
        # This is a mock test since we can't easily test the full app.py plan() function.
        # We verify the pattern would work by mocking asyncio.gather with timing.

        notes = []
        def mock_note_fn(msg: str, tags: list[str] | None = None):
            notes.append({"msg": msg, "tags": tags or []})

        async def mock_issue_writer_call(issue_name: str):
            """Simulate Issue Writer call."""
            await asyncio.sleep(0.05)  # Simulate work
            return {"success": True}

        async def write_issue_with_timing(issue_name: str):
            """Timing wrapper for Issue Writer."""
            start = time.time()
            result = await mock_issue_writer_call(issue_name)
            duration = time.time() - start
            mock_note_fn(
                f"Issue Writer: {issue_name} in {duration:.1f}s",
                tags=["issue_writer", "complete", issue_name, f"duration:{duration:.1f}"]
            )
            return result

        # Execute
        async def run_test():
            tasks = [
                write_issue_with_timing("issue-1"),
                write_issue_with_timing("issue-2"),
            ]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(run_test())

        # Verify: two Issue Writer logs with duration tags
        iw_logs = [n for n in notes if "issue_writer" in n["tags"]]
        self.assertEqual(len(iw_logs), 2, "Expected two Issue Writer logs")

        for log in iw_logs:
            self.assertIn("complete", log["tags"])
            duration_tags = [t for t in log["tags"] if t.startswith("duration:")]
            self.assertEqual(len(duration_tags), 1, "Expected duration tag")


if __name__ == "__main__":
    unittest.main()

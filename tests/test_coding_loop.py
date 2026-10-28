"""Integration tests for the coding loop stuck-detection and graceful degradation.

These tests exercise ``run_coding_loop`` end-to-end with **real** DAGState,
ExecutionConfig, artifact I/O, and iteration bookkeeping.  The **only** mock is
``call_fn`` — the async callable that dispatches to AI agents (coder, reviewer,
QA, synthesizer).  Each test wires up a ``call_fn`` that returns scripted
responses simulating realistic agent behaviour, then asserts on the IssueResult
outcome, iteration count, files_changed, and iteration_history.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

from swe_af.execution.coding_loop import _detect_stuck_loop, run_coding_loop
from swe_af.execution.schemas import DAGState, ExecutionConfig, IssueOutcome


# ---------------------------------------------------------------------------
# Helpers
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


def _make_issue(name: str = "ISSUE-1", needs_deeper_qa: bool = False, **extra) -> dict:
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
    if needs_deeper_qa:
        issue["guidance"] = {"needs_deeper_qa": True}
    issue.update(extra)
    return issue


class _CallFnBuilder:
    """Build a scripted ``call_fn`` that returns predetermined responses.

    Usage::

        builder = _CallFnBuilder()
        builder.on_coder(iteration=1, files_changed=["a.py"])
        builder.on_reviewer(iteration=1, approved=False, blocking=False)
        ...
        call_fn = builder.build()
    """

    def __init__(self):
        self._coder_responses: dict[int, dict] = {}
        self._reviewer_responses: dict[int, dict] = {}
        self._qa_responses: dict[int, dict] = {}
        self._synth_responses: dict[int, dict] = {}
        self._call_count = 0
        # Track iteration by counting coder calls
        self._coder_calls = 0

    # -- coder --
    def on_coder(
        self,
        iteration: int,
        files_changed: list[str] | None = None,
        summary: str = "Implemented changes",
    ) -> "_CallFnBuilder":
        self._coder_responses[iteration] = {
            "files_changed": files_changed or [],
            "summary": summary,
            "complete": True,
        }
        return self

    # -- reviewer --
    def on_reviewer(
        self,
        iteration: int,
        approved: bool = False,
        blocking: bool = False,
        summary: str = "Minor style issues",
        debt_items: list[dict] | None = None,
    ) -> "_CallFnBuilder":
        self._reviewer_responses[iteration] = {
            "approved": approved,
            "blocking": blocking,
            "summary": summary,
            "debt_items": debt_items or [],
        }
        return self

    # -- QA --
    def on_qa(
        self,
        iteration: int,
        passed: bool = True,
        summary: str = "Tests pass",
    ) -> "_CallFnBuilder":
        self._qa_responses[iteration] = {
            "passed": passed,
            "summary": summary,
            "test_failures": [],
        }
        return self

    # -- synthesizer --
    def on_synth(
        self,
        iteration: int,
        action: str = "fix",
        summary: str = "Continue fixing",
        stuck: bool = False,
    ) -> "_CallFnBuilder":
        self._synth_responses[iteration] = {
            "action": action,
            "summary": summary,
            "stuck": stuck,
        }
        return self

    def build(self) -> callable:
        """Return an async call_fn that dispatches based on agent name."""
        builder = self

        def call_fn(agent_name: str, **kwargs):
            """Return a coroutine that resolves to the scripted response."""

            async def _invoke():
                # Determine which iteration we're on based on coder call count
                if "run_coder" in agent_name:
                    builder._coder_calls += 1
                    iteration = builder._coder_calls
                    default = {"files_changed": [], "summary": "No changes", "complete": True}
                    return builder._coder_responses.get(iteration, default)

                iteration = builder._coder_calls  # same iteration as last coder call

                if "run_code_reviewer" in agent_name:
                    default = {"approved": False, "blocking": False, "summary": "Needs work"}
                    return builder._reviewer_responses.get(iteration, default)

                # Check synthesizer BEFORE qa — "run_qa" is a substring of
                # "run_qa_synthesizer" so order matters.
                if "run_qa_synthesizer" in agent_name:
                    default = {"action": "fix", "summary": "Continue fixing", "stuck": False}
                    return builder._synth_responses.get(iteration, default)

                if "run_qa" in agent_name:
                    default = {"passed": True, "summary": "Tests pass", "test_failures": []}
                    return builder._qa_responses.get(iteration, default)

                return {}

            return _invoke()

        return call_fn


def _run(coro):
    """Run an async coroutine synchronously for tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Unit tests: _detect_stuck_loop
# ---------------------------------------------------------------------------


class TestDetectStuckLoop(unittest.TestCase):
    """Unit tests for the stuck-loop detection helper."""

    def test_too_few_iterations(self):
        history = [
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertFalse(_detect_stuck_loop(history, window=3))

    def test_all_non_blocking_fix_triggers(self):
        history = [
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertTrue(_detect_stuck_loop(history, window=3))

    def test_blocking_iteration_prevents_trigger(self):
        history = [
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": True},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertFalse(_detect_stuck_loop(history, window=3))

    def test_approve_in_window_prevents_trigger(self):
        history = [
            {"action": "approve", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertFalse(_detect_stuck_loop(history, window=3))

    def test_only_looks_at_recent_window(self):
        history = [
            {"action": "approve", "review_blocking": False},  # outside window
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertTrue(_detect_stuck_loop(history, window=3))

    def test_custom_window_size(self):
        history = [
            {"action": "fix", "review_blocking": False},
            {"action": "fix", "review_blocking": False},
        ]
        self.assertTrue(_detect_stuck_loop(history, window=2))

    def test_empty_history(self):
        self.assertFalse(_detect_stuck_loop([], window=3))


# ---------------------------------------------------------------------------
# Integration tests: run_coding_loop
# ---------------------------------------------------------------------------


class TestCodingLoopIntegration(unittest.TestCase):
    """End-to-end tests for run_coding_loop with scripted call_fn."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="swe-af-test-")
        self.artifacts_dir = os.path.join(self.tmpdir, "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self.notes: list[str] = []
        self.note_tags: list[list[str]] = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _note_fn(self, msg: str, tags: list[str] | None = None):
        self.notes.append(msg)
        self.note_tags.append(tags or [])

    # -- Scenario 1: Happy path — approved on first iteration --

    def test_approved_first_iteration(self):
        """Coder produces changes, reviewer approves → COMPLETED."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/app.py"])
        builder.on_reviewer(1, approved=True, blocking=False, summary="LGTM")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        self.assertEqual(result.attempts, 1)
        self.assertIn("src/app.py", result.files_changed)
        self.assertEqual(len(result.iteration_history), 1)
        self.assertEqual(result.iteration_history[0]["action"], "approve")

    # -- Scenario 2: Approved after a fix cycle --

    def test_approved_after_fix_cycle(self):
        """Reviewer rejects first, approves second → COMPLETED on iteration 2."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/app.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Fix imports")
        builder.on_coder(2, files_changed=["src/app.py"])
        builder.on_reviewer(2, approved=True, blocking=False, summary="LGTM now")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.iteration_history), 2)
        self.assertEqual(result.iteration_history[0]["action"], "fix")
        self.assertEqual(result.iteration_history[1]["action"], "approve")

    # -- Scenario 3: Non-blocking stuck loop → COMPLETED_WITH_DEBT --

    def test_stuck_loop_non_blocking_accepts_with_debt(self):
        """Reviewer keeps saying fix (non-blocking) with code changes.

        After 3 consecutive non-blocking fix cycles, the stuck detector fires
        and the loop returns COMPLETED_WITH_DEBT instead of burning all 5
        iterations.
        """
        builder = _CallFnBuilder()
        for i in range(1, 6):
            builder.on_coder(i, files_changed=["src/app.py"])
            builder.on_reviewer(i, approved=False, blocking=False,
                                summary=f"Minor style issue iter {i}")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(max_coding_iterations=5),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED_WITH_DEBT)
        # Should stop at iteration 3 (window=3 consecutive non-blocking fix)
        self.assertEqual(result.attempts, 3)
        self.assertIn("src/app.py", result.files_changed)
        # Verify stuck detection note was emitted
        stuck_notes = [n for n in self.notes if "STUCK" in n]
        self.assertTrue(len(stuck_notes) > 0, f"Expected stuck note, got: {self.notes}")

    # -- Scenario 4: Blocking review → fix iteration, then approval --

    def test_blocking_review_gets_fix_iteration(self):
        """Reviewer marks blocking=True → next iteration is a fix pass, not a stop.

        "blocking" means "must be fixed before merge", so the default path must
        give the coder a chance to fix it (as the flagged path does).
        """
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/app.py"])
        builder.on_reviewer(1, approved=False, blocking=True,
                            summary="Security vulnerability: SQL injection")
        builder.on_coder(2, files_changed=["src/app.py"])
        builder.on_reviewer(2, approved=True, blocking=False, summary="Fixed")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.iteration_history[0]["action"], "fix")
        self.assertTrue(result.iteration_history[0]["review_blocking"])
        self.assertEqual(result.iteration_history[1]["action"], "approve")

    # -- Scenario 5: Stagnation (no files changed) → FAILED_UNRECOVERABLE --

    def test_stagnation_no_files_changed(self):
        """Coder produces no files across all iterations.

        Even though reviewer is non-blocking, having no code changes means
        we can't accept-with-debt — it should fail as unrecoverable.
        """
        builder = _CallFnBuilder()
        for i in range(1, 6):
            builder.on_coder(i, files_changed=[])  # No changes!
            builder.on_reviewer(i, approved=False, blocking=False,
                                summary="Nothing to review")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(max_coding_iterations=5),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.FAILED_UNRECOVERABLE)
        self.assertEqual(result.files_changed, [])

    # -- Scenario 6: Loop exhaustion (non-blocking, has changes) → COMPLETED_WITH_DEBT --

    def test_exhaustion_non_blocking_accepts_debt(self):
        """Loop exhausts all iterations but reviewer was never blocking and
        code changes exist. This should accept-with-debt rather than fail.

        Uses window > max_iterations so stuck detection doesn't fire first.
        """
        config = _make_config(max_coding_iterations=2)
        builder = _CallFnBuilder()
        # 2 iterations: first fix cycle doesn't trigger stuck (window=3 > 2)
        builder.on_coder(1, files_changed=["src/main.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Needs work")
        builder.on_coder(2, files_changed=["src/main.py", "src/util.py"])
        builder.on_reviewer(2, approved=False, blocking=False, summary="Still needs work")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=config,
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED_WITH_DEBT)
        self.assertEqual(result.attempts, 2)
        self.assertIn("src/main.py", result.files_changed)
        self.assertIn("src/util.py", result.files_changed)

    # -- Scenario 7: Loop exhaustion (blocking) → FAILED_UNRECOVERABLE --

    def test_exhaustion_blocking_fails_unrecoverable(self):
        """Loop exhausts with a blocking last review → FAILED_UNRECOVERABLE.

        A blocking review is now a fix iteration, so a blocking finding that is
        still unresolved when the iteration budget runs out must fail the issue
        instead of being recorded as accepted-with-debt (the production bug that
        merged reviewer-blocked code as "reviewer non-blocking").
        """
        config = _make_config(max_coding_iterations=2)
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/app.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Needs refactor")
        builder.on_coder(2, files_changed=["src/app.py"])
        builder.on_reviewer(2, approved=False, blocking=True, summary="Critical bug")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=config,
            note_fn=self._note_fn,
        ))

        # blocking=True on the last iteration → fix action → exhausted while
        # still blocking → FAILED_UNRECOVERABLE (never COMPLETED_WITH_DEBT).
        self.assertEqual(result.outcome, IssueOutcome.FAILED_UNRECOVERABLE)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.iteration_history[-1]["action"], "fix")
        self.assertTrue(result.iteration_history[-1]["review_blocking"])
        self.assertEqual(result.debt_items, [])

    # -- Scenario 8: Coder exception → FAILED_UNRECOVERABLE --

    def test_coder_exception_fails_unrecoverable(self):
        """Coder raises an exception → immediate FAILED_UNRECOVERABLE."""

        def call_fn(agent_name: str, **kwargs):
            async def _invoke():
                if "run_coder" in agent_name:
                    raise RuntimeError("Model API timeout")
                return {}
            return _invoke()

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=call_fn,
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.FAILED_UNRECOVERABLE)
        self.assertIn("Coder agent failed", result.error_message)
        self.assertEqual(result.attempts, 1)

    # -- Scenario 9: Flagged path — synthesizer detects stuck --

    def test_flagged_path_synthesizer_stuck(self):
        """Flagged path: synthesizer sets stuck=True with non-blocking review.

        Should return COMPLETED_WITH_DEBT from the stuck handler (since
        files_changed and not blocking).
        """
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/module.py"])
        builder.on_qa(1, passed=True, summary="Tests pass")
        builder.on_reviewer(1, approved=False, blocking=False, summary="Minor issues")
        builder.on_synth(1, action="fix", summary="Continue fixing", stuck=True)

        result = _run(run_coding_loop(
            issue=_make_issue(needs_deeper_qa=True),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED_WITH_DEBT)
        self.assertEqual(result.attempts, 1)
        self.assertIn("src/module.py", result.files_changed)

    # -- Scenario 10: Flagged path — synthesizer stuck + blocking → FAILED --

    def test_flagged_path_stuck_blocking_fails(self):
        """Flagged path: synthesizer stuck + reviewer blocking → FAILED_UNRECOVERABLE."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/module.py"])
        builder.on_qa(1, passed=False, summary="Security test failed")
        builder.on_reviewer(1, approved=False, blocking=True, summary="Security vulnerability")
        builder.on_synth(1, action="fix", summary="Critical issue", stuck=True)

        result = _run(run_coding_loop(
            issue=_make_issue(needs_deeper_qa=True),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.FAILED_UNRECOVERABLE)
        self.assertEqual(result.attempts, 1)

    # -- Scenario 11: Flagged path happy path --

    def test_flagged_path_approved(self):
        """Flagged path: QA passes, reviewer approves, synthesizer approves."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["src/feature.py", "tests/test_feature.py"])
        builder.on_qa(1, passed=True, summary="All 5 tests pass")
        builder.on_reviewer(1, approved=True, blocking=False, summary="Clean code")
        builder.on_synth(1, action="approve", summary="All checks pass")

        result = _run(run_coding_loop(
            issue=_make_issue(needs_deeper_qa=True),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.files_changed), 2)

    # -- Scenario 12: Iteration history is accumulated correctly --

    def test_iteration_history_accumulated(self):
        """Verify iteration_history contains full detail from each iteration."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["a.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Fix A")
        builder.on_coder(2, files_changed=["b.py"])
        builder.on_reviewer(2, approved=True, blocking=False, summary="LGTM")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(len(result.iteration_history), 2)

        h1 = result.iteration_history[0]
        self.assertEqual(h1["iteration"], 1)
        self.assertEqual(h1["action"], "fix")
        self.assertEqual(h1["path"], "default")
        self.assertFalse(h1["review_blocking"])

        h2 = result.iteration_history[1]
        self.assertEqual(h2["iteration"], 2)
        self.assertEqual(h2["action"], "approve")
        self.assertTrue(h2["review_approved"])

    # -- Scenario 13: Artifacts are saved to disk --

    def test_artifacts_saved(self):
        """Verify iteration artifacts (coder, review) are written to disk."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["x.py"])
        builder.on_reviewer(1, approved=True, summary="Good")

        _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        # Check that coding-loop artifacts directory was created
        coding_loop_dir = os.path.join(self.artifacts_dir, "coding-loop")
        self.assertTrue(os.path.isdir(coding_loop_dir),
                        f"Expected artifacts dir at {coding_loop_dir}")

        # Should have at least one iteration subdirectory with coder.json and review.json
        subdirs = os.listdir(coding_loop_dir)
        self.assertTrue(len(subdirs) >= 1)
        first_iter = os.path.join(coding_loop_dir, subdirs[0])
        self.assertTrue(os.path.exists(os.path.join(first_iter, "coder.json")))
        self.assertTrue(os.path.exists(os.path.join(first_iter, "review.json")))

    # -- Scenario 14: Iteration checkpoint saved --

    def test_iteration_checkpoint_saved(self):
        """Verify per-issue iteration checkpoint JSON is written."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["x.py"])
        builder.on_reviewer(1, approved=True, summary="Good")

        _run(run_coding_loop(
            issue=_make_issue("CHECKPOINT-1"),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        checkpoint_path = os.path.join(
            self.artifacts_dir, "execution", "iterations", "CHECKPOINT-1.json"
        )
        self.assertTrue(os.path.exists(checkpoint_path))

    # -- Scenario 15: Files accumulate across iterations --

    def test_files_accumulate_across_iterations(self):
        """Files changed in different iterations should all appear in result."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["a.py", "b.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Fix")
        builder.on_coder(2, files_changed=["b.py", "c.py"])  # b.py again + new c.py
        builder.on_reviewer(2, approved=True, summary="LGTM")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        # a.py, b.py from iter 1, c.py from iter 2 — b.py not duplicated
        self.assertEqual(sorted(result.files_changed), ["a.py", "b.py", "c.py"])

    # -- Scenario 16: Stuck detection fires exactly at window boundary --

    def test_stuck_fires_at_window_boundary(self):
        """With window=3, stuck fires on iteration 3 (not before, not after)."""
        # 3 iterations, all non-blocking fix → stuck on iter 3
        builder = _CallFnBuilder()
        for i in range(1, 4):
            builder.on_coder(i, files_changed=["x.py"])
            builder.on_reviewer(i, approved=False, blocking=False,
                                summary=f"Polish iter {i}")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(max_coding_iterations=10),  # high limit, stuck should fire first
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED_WITH_DEBT)
        self.assertEqual(result.attempts, 3)

    # -- Scenario 17: Mixed blocking/non-blocking doesn't trigger stuck --

    def test_mixed_blocking_prevents_stuck(self):
        """If one of the recent iterations was blocking (action=block returns
        immediately), stuck detection can't reach window=3 of consecutive
        non-blocking fix cycles.

        Here: iter 1 non-blocking fix, iter 2 approved (approved breaks the fix streak).
        Then iter 3 can't happen because iter 2 was approved.
        """
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["x.py"])
        builder.on_reviewer(1, approved=False, blocking=False, summary="Fix")
        builder.on_coder(2, files_changed=["x.py"])
        builder.on_reviewer(2, approved=True, summary="LGTM")

        result = _run(run_coding_loop(
            issue=_make_issue(),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        self.assertEqual(result.outcome, IssueOutcome.COMPLETED)
        self.assertEqual(result.attempts, 2)

    # -- Scenario 18: note_fn receives meaningful tags --

    def test_note_fn_receives_tags(self):
        """Verify note_fn is called with structured tags for observability."""
        builder = _CallFnBuilder()
        builder.on_coder(1, files_changed=["x.py"])
        builder.on_reviewer(1, approved=True, summary="Good")

        _run(run_coding_loop(
            issue=_make_issue("TAG-TEST"),
            dag_state=_make_dag_state(self.artifacts_dir),
            call_fn=builder.build(),
            node_id="test-node",
            config=_make_config(),
            note_fn=self._note_fn,
        ))

        # Should have tags with issue name and lifecycle events
        all_tags = [tag for tags in self.note_tags for tag in tags]
        self.assertIn("TAG-TEST", all_tags)
        self.assertIn("coding_loop", all_tags)
        self.assertIn("start", all_tags)
        self.assertIn("complete", all_tags)


if __name__ == "__main__":
    unittest.main()

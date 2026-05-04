"""Tests for the post-PR CI gate (watcher + promote-to-ready).

The watcher polls `gh pr checks` until conclusive. Polling is wired through
injectable `runner`, `sleep`, and `now` callables so these tests run in
microseconds without invoking gh, sleeping, or hitting GitHub.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from swe_af.execution.ci_gate import (
    _classify,
    _extract_run_id,
    _is_conclusive,
    _parse_checks,
    _tail,
    mark_pr_ready,
    watch_pr_checks,
)
from swe_af.execution.schemas import CIWatchResult


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _mk_check(bucket: str, name: str = "Tests", state: str = "", workflow: str = "CI"):
    return {
        "bucket": bucket,
        "state": state or bucket.upper(),
        "name": name,
        "workflow": workflow,
        "link": f"https://github.com/o/r/actions/runs/12345/job/{hash(name) & 0xFFFF}",
    }


class _ScriptedRunner:
    """Returns pre-baked CompletedProcess values keyed by command shape.

    Treats the first 3 args (`gh pr checks` or `gh run view`, etc.) as the
    "kind" of call and pops the next scripted reply from a per-kind queue.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.checks_queue: list[subprocess.CompletedProcess] = []
        self.run_view_queue: list[subprocess.CompletedProcess] = []
        self.ready_queue: list[subprocess.CompletedProcess] = []

    def __call__(self, cmd, cwd):  # type: ignore[no-untyped-def]
        self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "checks"]:
            assert self.checks_queue, "ran out of scripted gh pr checks replies"
            return self.checks_queue.pop(0)
        if cmd[:3] == ["gh", "run", "view"]:
            if self.run_view_queue:
                return self.run_view_queue.pop(0)
            return _completed(stdout="(no log captured)\n")
        if cmd[:3] == ["gh", "pr", "ready"]:
            if self.ready_queue:
                return self.ready_queue.pop(0)
            return _completed(returncode=0)
        raise AssertionError(f"unexpected command in test: {cmd}")


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


async def _no_sleep(_seconds: float) -> None:
    return None


class TestParseAndClassify(unittest.TestCase):
    def test_parse_empty_payload(self) -> None:
        self.assertEqual(_parse_checks(""), [])
        self.assertEqual(_parse_checks("   \n"), [])

    def test_parse_array(self) -> None:
        payload = json.dumps([{"bucket": "pass", "name": "a"}])
        self.assertEqual(_parse_checks(payload), [{"bucket": "pass", "name": "a"}])

    def test_parse_non_array_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_checks(json.dumps({"not": "array"}))

    def test_is_conclusive(self) -> None:
        self.assertTrue(_is_conclusive([_mk_check("pass"), _mk_check("fail")]))
        self.assertFalse(_is_conclusive([_mk_check("pass"), _mk_check("pending")]))
        self.assertFalse(_is_conclusive([_mk_check("queued")]))
        self.assertTrue(_is_conclusive([_mk_check("skip")]))

    def test_classify_passes_when_only_pass_or_skip(self) -> None:
        self.assertEqual(_classify([_mk_check("pass"), _mk_check("skip")]), "passed")

    def test_classify_fails_on_any_failure(self) -> None:
        self.assertEqual(_classify([_mk_check("pass"), _mk_check("fail")]), "failed")
        self.assertEqual(_classify([_mk_check("cancel")]), "failed")

    def test_extract_run_id(self) -> None:
        self.assertEqual(
            _extract_run_id("https://github.com/o/r/actions/runs/12345/job/678"),
            "12345",
        )
        self.assertEqual(_extract_run_id(""), "")
        self.assertEqual(_extract_run_id("not a url"), "")

    def test_tail_truncates_long_strings(self) -> None:
        s = "x" * 5000
        out = _tail(s, max_chars=100)
        self.assertTrue(out.startswith("…[truncated]…"))
        self.assertEqual(len(out.rstrip()), len("…[truncated]…\n") + 100)

    def test_tail_passes_short_strings_through(self) -> None:
        self.assertEqual(_tail("short"), "short")


class TestWatchPRChecks(unittest.IsolatedAsyncioTestCase):
    async def test_passes_when_first_poll_is_all_green(self) -> None:
        runner = _ScriptedRunner()
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check("pass", "Tests"),
            _mk_check("pass", "Lint"),
        ])))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=600, poll_seconds=10,
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.pr_number, 42)
        self.assertEqual(len(result.failed_checks), 0)
        self.assertEqual(len(runner.calls), 1)  # one poll, then conclusive

    async def test_fails_and_collects_failed_logs(self) -> None:
        runner = _ScriptedRunner()
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check("pass", "Lint"),
            _mk_check("fail", "Tests"),
        ])))
        runner.run_view_queue.append(_completed(stdout="E   AssertionError: foo != bar\n"))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=7,
            wait_seconds=600, poll_seconds=10,
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.failed_checks), 1)
        fc = result.failed_checks[0]
        self.assertEqual(fc.name, "Tests")
        self.assertIn("AssertionError", fc.logs_excerpt)
        # We did one `gh pr checks` and one `gh run view` to fetch logs
        kinds = [c[:3] for c in runner.calls]
        self.assertEqual(kinds.count(["gh", "pr", "checks"]), 1)
        self.assertEqual(kinds.count(["gh", "run", "view"]), 1)

    async def test_polls_until_conclusive(self) -> None:
        runner = _ScriptedRunner()
        # First two polls: still pending. Third: green.
        runner.checks_queue.extend([
            _completed(stdout=json.dumps([_mk_check("pending", "Tests")])),
            _completed(stdout=json.dumps([_mk_check("pending", "Tests")])),
            _completed(stdout=json.dumps([_mk_check("pass", "Tests")])),
        ])

        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        clock = _FakeClock()

        async def advancing_now() -> float:  # not used; just for clarity
            return clock.t

        # Bump the fake clock between polls so we exercise elapsed accounting.
        original_runner = runner

        def runner_with_advance(cmd: Any, cwd: str) -> Any:
            clock.t += 5.0
            return original_runner(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=1,
            wait_seconds=600, poll_seconds=10,
            runner=runner_with_advance, sleep=record_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(sleeps, [10, 10])  # slept twice between three polls

    async def test_times_out_when_checks_never_settle(self) -> None:
        runner = _ScriptedRunner()
        # Always pending.
        for _ in range(20):
            runner.checks_queue.append(
                _completed(stdout=json.dumps([_mk_check("pending", "Tests")]))
            )
        clock = _FakeClock()

        # Simulate 100s of wall time advancing every poll.
        original_runner = runner

        def advance(cmd: Any, cwd: str) -> Any:
            clock.t += 100.0
            return original_runner(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=99,
            wait_seconds=300, poll_seconds=50,
            runner=advance, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "timed_out")
        self.assertGreaterEqual(result.elapsed_seconds, 300)

    async def test_no_checks_when_pr_has_no_ci(self) -> None:
        runner = _ScriptedRunner()
        # gh returns an empty array repeatedly.
        for _ in range(5):
            runner.checks_queue.append(_completed(stdout="[]"))
        clock = _FakeClock()

        original_runner = runner

        def advance(cmd: Any, cwd: str) -> Any:
            clock.t += 200.0
            return original_runner(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=5,
            wait_seconds=300, poll_seconds=50,
            runner=advance, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "no_checks")

    async def test_failed_checks_with_nonzero_exit_still_parsed(self) -> None:
        """`gh pr checks` exits non-zero when ANY check failed even though it
        also prints valid JSON. Treat the body, not the exit code, as truth."""
        runner = _ScriptedRunner()
        runner.checks_queue.append(_completed(
            stdout=json.dumps([
                _mk_check("pass", "Lint"),
                _mk_check("fail", "Tests"),
            ]),
            stderr="some checks failing",
            returncode=8,  # gh exit code for "checks failing"
        ))
        runner.run_view_queue.append(_completed(stdout="boom"))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=11,
            wait_seconds=600, poll_seconds=10,
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.failed_checks), 1)

    async def test_real_error_when_gh_fails_with_no_payload(self) -> None:
        runner = _ScriptedRunner()
        runner.checks_queue.append(_completed(
            stdout="", stderr="gh: not authenticated", returncode=1,
        ))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=3,
            wait_seconds=600, poll_seconds=10,
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "error")
        self.assertIn("not authenticated", result.summary)


class TestMarkPRReady(unittest.TestCase):
    def test_promotes_on_success(self) -> None:
        runner = _ScriptedRunner()
        runner.ready_queue.append(_completed(returncode=0))
        ok, msg = mark_pr_ready(repo_path="/tmp/r", pr_number=42, runner=runner)
        self.assertTrue(ok)
        self.assertIn("#42", msg)
        self.assertEqual(runner.calls[-1], ["gh", "pr", "ready", "42"])

    def test_reports_failure(self) -> None:
        runner = _ScriptedRunner()
        runner.ready_queue.append(_completed(stderr="not a draft", returncode=1))
        ok, msg = mark_pr_ready(repo_path="/tmp/r", pr_number=42, runner=runner)
        self.assertFalse(ok)
        self.assertIn("not a draft", msg)


class TestSchemas(unittest.TestCase):
    def test_ci_watch_result_serialises_with_failures(self) -> None:
        result = CIWatchResult(
            status="failed", pr_number=1, elapsed_seconds=42,
        )
        self.assertEqual(result.model_dump()["status"], "failed")


if __name__ == "__main__":
    unittest.main()

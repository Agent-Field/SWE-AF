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


def _mk_check_with_sha(
    bucket: str,
    name: str,
    head_sha: str,
    workflow: str = "CI",
):
    """Helper for SHA-anchored tests — populates the headSha field."""
    return {
        "bucket": bucket,
        "state": bucket.upper(),
        "name": name,
        "workflow": workflow,
        "link": f"https://github.com/o/r/actions/runs/12345/job/{hash(name) & 0xFFFF}",
        "headSha": head_sha,
    }


class TestWatchPRChecksHeadShaAnchor(unittest.IsolatedAsyncioTestCase):
    """When ``head_sha`` is supplied, the watcher must not declare a verdict
    based on checks that belong to a previous commit. This prevents the
    stale-state race: just after a push, ``gh pr checks`` can briefly
    return the previous HEAD's already-conclusive checks before the new
    workflow run is registered, which would short-circuit the verdict.
    """

    async def test_stale_passed_checks_are_ignored_until_new_sha_appears(self) -> None:
        """Previous HEAD's passed checks must not let the watcher return
        ``passed`` before a check for the new SHA has even shown up."""
        runner = _ScriptedRunner()
        # Poll 1: only the OLD SHA's check, conclusive (would otherwise
        # short-circuit to passed).
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("pass", "Old Lint", head_sha="oldsha111"),
        ])))
        # Poll 2: still only the old check.
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("pass", "Old Lint", head_sha="oldsha111"),
        ])))
        # Poll 3: new SHA's checks now visible AND conclusive (passed).
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("pass", "Old Lint", head_sha="oldsha111"),
            _mk_check_with_sha("pass", "Tests", head_sha="newsha222"),
        ])))
        clock = _FakeClock()
        original = runner

        def advance(cmd: Any, cwd: str) -> Any:
            clock.t += 5.0
            return original(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=600, poll_seconds=10,
            head_sha="newsha222",
            runner=advance, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "passed")
        # Three polls, not one — the SHA anchor blocked the early return on
        # poll 1's stale-but-conclusive snapshot.
        self.assertEqual(len(runner.calls), 3)

    async def test_stale_failed_checks_dont_short_circuit_to_failed(self) -> None:
        """Critical regression: the previous HEAD's FAILED checks must not
        be reported as the verdict for the new SHA. This was the observed
        bug — `_run_ci_gate` saw stale failures from the previous commit and
        either acted on them or got into an inconsistent state."""
        runner = _ScriptedRunner()
        # Poll 1: only the OLD SHA's failed check.
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("fail", "Old Tests", head_sha="oldsha111"),
        ])))
        # Poll 2: new SHA's checks settle as passed.
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("fail", "Old Tests", head_sha="oldsha111"),
            _mk_check_with_sha("pass", "Tests", head_sha="newsha222"),
        ])))
        clock = _FakeClock()
        original = runner

        def advance(cmd: Any, cwd: str) -> Any:
            clock.t += 5.0
            return original(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=600, poll_seconds=10,
            head_sha="newsha222",
            runner=advance, sleep=_no_sleep, now=clock.now,
        )

        # The verdict is computed ONLY over checks for newsha222; the old
        # failure must not poison it.
        self.assertEqual(result.status, "passed")

    async def test_no_checks_emitted_when_only_other_sha_checks_seen(self) -> None:
        """If the wait cap fires and we never saw a check for the requested
        SHA (e.g. CI is broken / paths-filter excluded the new commit),
        return ``no_checks`` with a SHA-specific summary so the caller can
        diagnose."""
        runner = _ScriptedRunner()
        for _ in range(5):
            runner.checks_queue.append(_completed(stdout=json.dumps([
                _mk_check_with_sha("pass", "Old", head_sha="oldsha111"),
            ])))
        clock = _FakeClock()
        original = runner

        def advance(cmd: Any, cwd: str) -> Any:
            clock.t += 100.0
            return original(cmd, cwd)

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=300, poll_seconds=50,
            head_sha="newsha222",
            runner=advance, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "no_checks")
        self.assertIn("newsha222"[:10], result.summary)

    async def test_missing_head_sha_field_does_not_block_verdict(self) -> None:
        """Older `gh` versions may not populate `headSha`. When the field is
        absent (empty string), the watcher should treat the check as
        "unknown — could be ours" and let it count toward the verdict, so
        we degrade gracefully on outdated CLIs rather than hanging."""
        runner = _ScriptedRunner()
        # Check has no headSha at all (older gh CLI).
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check("pass", "Tests"),  # no headSha
        ])))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=600, poll_seconds=10,
            head_sha="newsha222",
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        # With no headSha to compare, we accept the check and pass.
        self.assertEqual(result.status, "passed")

    async def test_no_anchor_preserves_existing_behavior(self) -> None:
        """Without ``head_sha``, the watcher must behave exactly like before
        (no filtering). Pin so the build() path's call site stays unaffected."""
        runner = _ScriptedRunner()
        # Conclusive on first poll — must short-circuit just like before.
        runner.checks_queue.append(_completed(stdout=json.dumps([
            _mk_check_with_sha("pass", "Tests", head_sha="anysha"),
        ])))
        clock = _FakeClock()

        result = await watch_pr_checks(
            repo_path="/tmp/repo", pr_number=42,
            wait_seconds=600, poll_seconds=10,
            # head_sha intentionally omitted
            runner=runner, sleep=_no_sleep, now=clock.now,
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(len(runner.calls), 1)


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

"""Tests for the post-PR CI gate (watcher + promote-to-ready).

The watcher polls exact-Commit GraphQL rollups until conclusive. Polling is wired through
injectable `runner`, `sleep`, and `now` callables so these tests run in
microseconds without invoking gh, sleeping, or hitting GitHub.
"""

from __future__ import annotations

import asyncio
import json
import copy
import importlib
import os
from pathlib import Path
import subprocess
import unittest
from typing import Any
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from agentfield import Agent
from agentfield.exceptions import (
    AgentFieldClientError,
    ExecutionCancelledError,
    ExecutionFailedError,
    ExecutionTimeoutError,
)

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
from swe_af.execution.fatal_error import FatalHarnessError, is_fatal_error


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
        result, runner, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row("tests"), _check_run_row("lint", name="Lint")])])
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.pr_number, 42)
        self.assertEqual(len(result.failed_checks), 0)
        self.assertEqual(len(runner.calls), 2)  # discovery, one poll, then conclusive

    async def test_fails_and_collects_failed_logs(self) -> None:
        page = _commit_rollup_page([_check_run_row("lint", name="Lint"), _check_run_row("tests", conclusion="FAILURE")])
        result, runner, _ = await _watch_commit_pages([page, page])
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.failed_checks), 1)
        fc = result.failed_checks[0]
        self.assertEqual(fc.name, "Tests")
        self.assertIn("AssertionError", fc.logs_excerpt)
        # We acquired checks, fetched one log, and guarded provenance again.
        kinds = [c[:3] for c in runner.calls]
        self.assertEqual(kinds.count(["gh", "api", "graphql"]), 2)
        self.assertEqual(kinds.count(["gh", "run", "view"]), 1)

    async def test_polls_until_conclusive(self) -> None:
        # First two polls: still pending. Third: green.
        pending = _commit_rollup_page([_check_run_row(status="IN_PROGRESS", conclusion=None)])
        clock = _PollingClock()
        runner = _CIHeadCommandRunner([pending, pending, _commit_rollup_page([_check_run_row()])])
        self.assertEqual(clock.now(), 0)  # initial time, before polling
        # Bump the fake clock between polls so we exercise elapsed accounting.
        result, _, _ = await _watch_commit_pages([], runner=runner, sleep=clock.sleep, now=clock.now)
        self.assertEqual(result.status, "passed")
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(clock.sleeps, [1, 1])  # slept twice between three polls

    async def test_times_out_when_checks_never_settle(self) -> None:
        # Always pending.
        pending = _commit_rollup_page([_check_run_row(status="IN_PROGRESS", conclusion=None)])
        # Simulate wall time advancing between polls within one deadline.
        result, _, _ = await _watch_commit_pages([pending], repeat=True)
        self.assertEqual(result.status, "timed_out")
        self.assertGreaterEqual(result.elapsed_seconds, 4)

    async def test_no_checks_when_pr_has_no_ci(self) -> None:
        # gh returns a complete empty connection repeatedly.
        result, _, _ = await _watch_commit_pages([_commit_rollup_page()], repeat=True)
        self.assertEqual(result.status, "no_checks")

    async def test_nonzero_graphql_exit_rejects_partial_stdout(self) -> None:
        """GraphQL acquisition requires exit zero even with valid-looking JSON."""
        proc = _completed(stdout=json.dumps(_commit_rollup_page([_check_run_row(conclusion="FAILURE")])),
                          stderr="command failed", returncode=8)  # command failure is authoritative
        result, _, _ = await _watch_commit_pages([proc])
        self.assertEqual(result.status, "error")
        self.assertEqual(len(result.failed_checks), 0)

    async def test_real_error_when_gh_fails_with_no_payload(self) -> None:
        result, _, _ = await _watch_commit_pages([_completed(stderr="gh: not authenticated", returncode=1)])
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
    based on checks that belong to a previous commit. Exact Commit identity
    and the separately observed PR head must both match the selected target.
    """

    async def test_previous_commit_passed_rows_are_rejected(self) -> None:
        """Previous HEAD's passed checks cannot supply the selected verdict."""
        # Poll 1: the Commit object belongs to the previous target.
        # Its successful checks cannot short-circuit to passed.
        old = _commit_rollup_page([_check_run_row()], sha=COMMIT_B, head=COMMIT_A)
        # Poll 2 would repeat the old object, but must never be requested.
        repeated = copy.deepcopy(old)
        # Poll 3 would pass at the target, but cannot erase observed mismatch.
        result, runner, _ = await _watch_commit_pages([old, repeated, _commit_rollup_page([_check_run_row()])])
        self.assertEqual(result.status, "error")
        # One acquisition fails immediately; later data cannot repair its
        # already-observed provenance mismatch.
        self.assertEqual(len(runner.pages), 2)

    async def test_stale_failed_checks_dont_short_circuit_to_failed(self) -> None:
        """Critical regression: the previous HEAD's FAILED checks must not
        be reported as the verdict for the new SHA. This was the observed
        bug — `_run_ci_gate` saw stale failures from the previous commit and
        either acted on them or got into an inconsistent state."""
        # Poll 1: the previous Commit object's failed check is not target data.
        old = _commit_rollup_page([_check_run_row(conclusion="FAILURE")], sha=COMMIT_B, head=COMMIT_A)
        # Poll 2's target success cannot erase the earlier provenance error.
        result, runner, _ = await _watch_commit_pages([old, _commit_rollup_page([_check_run_row()])])
        # Only the exact selected Commit can supply a verdict; a different
        # object must fail provenance validation before fetching failure logs.
        self.assertEqual(result.status, "error")
        self.assertFalse(any(cmd[:3] == ["gh", "run", "view"] for cmd in runner.calls))

    async def test_other_head_is_error_instead_of_no_checks(self) -> None:
        """A moved PR head is an identity error, never an empty observation."""
        result, _, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row()], head=COMMIT_B)], repeat=True)
        self.assertEqual(result.status, "error")

    async def test_missing_commit_oid_blocks_verdict(self) -> None:
        """Missing selected Commit provenance cannot degrade to PR-level data."""
        # The response omits the selected immutable Commit OID.
        page = _commit_rollup_page([_check_run_row()])
        del page["data"]["repository"]["object"]["oid"]  # required provenance
        result, _, _ = await _watch_commit_pages([page])
        # Without the Commit OID, a passing row cannot establish its target.
        self.assertEqual(result.status, "error")

    async def test_omitted_anchor_selects_local_committed_head(self) -> None:
        """Omitted targets resolve local HEAD before exact-Commit acquisition."""
        # Conclusive on the first poll after local target selection.
        result, runner, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row()])],
                                         head_sha="")  # head_sha intentionally omitted
        self.assertEqual(result.status, "passed")
        self.assertEqual(runner.calls.count(["git", "rev-parse", "--verify", "HEAD^{commit}"]), 1)


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


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40
REPOSITORY_PATH = str(Path(__file__).resolve().parents[1])
REPOSITORY_IDENTITY = {
    "id": "R_contract", "nameWithOwner": "Agent-Field/SWE-AF",
    "url": "https://github.com/Agent-Field/SWE-AF",
}
CI_HEAD_QUERY = """query CIHeadRollup(
  $owner: String!
  $name: String!
  $number: Int!
  $oid: GitObjectID!
  $after: String
) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
    pullRequest(number: $number) {
      number
      headRefOid
    }
    object(oid: $oid) {
      __typename
      ... on Commit {
        oid
        statusCheckRollup {
          contexts(first: 100, after: $after) {
            checkRunCount
            statusContextCount
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              __typename
              ... on CheckRun {
                id
                name
                status
                conclusion
                detailsUrl
                startedAt
                checkSuite {
                  workflowRun {
                    event
                    workflow {
                      name
                    }
                  }
                }
              }
              ... on StatusContext {
                id
                context
                state
                targetUrl
                createdAt
              }
            }
          }
        }
      }
    }
  }
}
"""


def _check_run_row(ident="run-1", name="Tests", conclusion="SUCCESS", **changes):
    row = {
        "__typename": "CheckRun", "id": ident, "name": name,
        "status": "COMPLETED", "conclusion": conclusion,
        "detailsUrl": "https://github.com/Agent-Field/SWE-AF/actions/runs/12345/job/1",
        "startedAt": "2026-10-23T00:00:00Z",
        "checkSuite": {"workflowRun": {"event": "pull_request", "workflow": {"name": "CI"}}},
    }
    row.update(changes)
    return row


def _status_context_row(ident="status-1", state="SUCCESS", **changes):
    row = {
        "__typename": "StatusContext", "id": ident, "context": "external",
        "state": state, "targetUrl": "https://ci.example.test/build/1",
        "createdAt": "2026-10-23T00:00:00Z",
    }
    row.update(changes)
    return row


def _commit_rollup_page(rows=None, *, sha=COMMIT_A, head=None, counts=None, more=False, cursor=None, null=False):
    nodes = [] if rows is None else rows
    check_runs = sum(row.get("__typename") == "CheckRun" for row in nodes)
    statuses = sum(row.get("__typename") == "StatusContext" for row in nodes)
    totals = (check_runs, statuses) if counts is None else counts
    end = ("cursor-1" if nodes else None) if cursor is None else cursor
    contexts = {
        "checkRunCount": totals[0], "statusContextCount": totals[1],
        "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": end},
    }
    return {"data": {"repository": {
        "id": REPOSITORY_IDENTITY["id"], "nameWithOwner": REPOSITORY_IDENTITY["nameWithOwner"],
        "pullRequest": {"number": 42, "headRefOid": sha if head is None else head},
        "object": {"__typename": "Commit", "oid": sha,
                   "statusCheckRollup": None if null else {"contexts": contexts}},
    }}}


def _rollup_contexts(page):
    return page["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]


def _ci_head_command(sha=COMMIT_A, after=None):
    command = [
        "gh", "api", "graphql", "--hostname", "github.com",
        "--raw-field", "query=" + CI_HEAD_QUERY,
        "--raw-field", "owner=Agent-Field", "--raw-field", "name=SWE-AF",
        "--field", "number=42", "--raw-field", "oid=" + sha,
    ]
    if after is None:
        return command + ["--field", "after=null"]
    return command + ["--raw-field", "after=" + after]


def _canonical_command(command):
    result = list(command)
    for index, arg in enumerate(result):
        if arg.startswith("query="):
            result[index] = "query=" + " ".join(arg[6:].split())
    return result


class _PollingClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.t += seconds


class _CIHeadCommandRunner:
    def __init__(self, pages=(), *, repeat=False, discovery=None, clock=None, delay=0, logs=None):
        self.pages = list(pages)
        self.repeat = repeat
        self.discovery = REPOSITORY_IDENTITY if discovery is None else discovery
        self.clock = clock
        self.delay = delay
        self.calls = []
        self.expected_after = None
        self.last_page = None
        self.unsupported_calls = []
        self.logs = _completed(stdout="E AssertionError: contract failure\n") if logs is None else logs

    def __call__(self, cmd, cwd, **kwargs):
        command = list(cmd)
        assert str(Path(cwd).resolve()) == REPOSITORY_PATH
        self.calls.append(command)
        if self.clock is not None:
            self.clock.t += self.delay
        if command[:3] == ["gh", "pr", "checks"]:
            assert command == ["gh", "pr", "checks", "42", "--json", "bucket,state,name,workflow,link,headSha"]
            self.unsupported_calls.append(command)
            return _completed(stderr='Unknown JSON field: "headSha"', returncode=1)
        return self.selected_command(command)

    def selected_command(self, command):
        if command == ["gh", "repo", "view", "--json", "id,nameWithOwner,url"]:
            return _completed(stdout=json.dumps(self.discovery))
        if command == ["git", "rev-parse", "--verify", "HEAD^{commit}"]:
            return _completed(stdout=COMMIT_A + "\n")
        if command[:3] == ["gh", "run", "view"]:
            assert command == ["gh", "run", "view", "12345", "--repo", "github.com/Agent-Field/SWE-AF", "--log-failed"]
            return self.logs
        return self.graphql(command)

    def graphql(self, command):
        requested = command[command.index("--raw-field", 13) + 1]
        assert requested.startswith("oid=")
        expected = _ci_head_command(requested[4:], self.expected_after)
        assert _canonical_command(command) == _canonical_command(expected)
        if self.pages:
            self.last_page = self.pages.pop(0)
        else:
            assert self.repeat and self.last_page is not None, "fixture queue exhausted"
        page = copy.deepcopy(self.last_page)
        self.update_cursor(page)
        if isinstance(page, BaseException):
            raise page
        if isinstance(page, subprocess.CompletedProcess):
            return page
        return _completed(stdout=json.dumps(page))

    def update_cursor(self, page):
        self.expected_after = None
        if not isinstance(page, dict):
            return
        try:
            contexts = _rollup_contexts(page)
            info = contexts["pageInfo"]
        except (KeyError, TypeError):
            return
        if isinstance(info, dict) and info.get("hasNextPage") is True:
            self.expected_after = info.get("endCursor")


async def _watch_commit_pages(pages, *, repeat=False, wait=4, **options):
    clock = _PollingClock()
    runner = _CIHeadCommandRunner(pages, repeat=repeat)
    arguments = dict(repo_path=REPOSITORY_PATH, pr_number=42, wait_seconds=wait,
                     poll_seconds=1, head_sha=COMMIT_A, runner=runner,
                     sleep=clock.sleep, now=clock.now)
    arguments.update(options)
    with patch.dict(os.environ, {"GH_REPO": "", "GH_HOST": ""}):
        result = await watch_pr_checks(**arguments)
    return result, runner, clock


def _watch_result_payload(status="failed", **changes):
    result = CIWatchResult(status=status, pr_number=42, summary="contract watcher").model_dump()
    if status == "failed":
        result["failed_checks"] = [{"name": "Tests", "workflow": "CI", "conclusion": "FAILURE",
                                    "details_url": _check_run_row()["detailsUrl"], "logs_excerpt": "contract failure"}]
    result.update(changes)
    return result


def _successful_execution_envelope(result):
    return {"status": "success", "execution_id": "contract-execution", "result": result}


class _CIGateDispatch:
    def __init__(self, watches, fixes):
        self.watches = list(watches)
        self.fixes = list(fixes)
        self.targets = []
        self.watch_arguments = []
        self.fixer_arguments = []

    async def call(self, target, **arguments):
        if target.endswith(".run_ci_watcher"):
            self.targets.append(arguments["head_sha"])
            self.watch_arguments.append(arguments)
            assert self.watches, "watch fixture queue exhausted"
            result = self.watches.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        assert target.endswith(".run_ci_fixer"), target
        self.fixer_arguments.append(arguments)
        assert self.fixes, "fix fixture queue exhausted"
        result = self.fixes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def note(self, *args, **kwargs):
        return None


async def _run_gate_fixture(watches, fixes=(), *, cycles=1, pages=None, head_sha=COMMIT_A):
    module = importlib.import_module("swe_af.app")
    dispatch = _CIGateDispatch(watches, fixes)
    guards = [_commit_rollup_page([_check_run_row()]), _commit_rollup_page([_check_run_row()], sha=COMMIT_B)] if pages is None else pages
    clock = _PollingClock()
    runner = _CIHeadCommandRunner(guards, repeat=True, clock=clock)
    cfg = module.BuildConfig(max_ci_fix_cycles=cycles, ci_wait_seconds=5, ci_poll_seconds=1)
    with patch.object(module.app, "call", dispatch.call), patch.object(module.app, "note", dispatch.note):
        with patch("swe_af.execution.ci_gate._default_runner", runner), patch.object(module, "time", SimpleNamespace(monotonic=clock.now), create=True):
            with patch("swe_af.execution.ci_gate.time", SimpleNamespace(monotonic=clock.now)), patch("swe_af.execution.ci_gate.asyncio.sleep", clock.sleep):
                return await _invoke_gate_fixture(module, cfg, head_sha, dispatch, runner)


async def _invoke_gate_fixture(module, cfg, head_sha, dispatch, runner):
    with patch.dict(os.environ, {"GH_REPO": "", "GH_HOST": ""}):
        result = await module._run_ci_gate(
            repo_path=REPOSITORY_PATH, pr_number=42, pr_url="https://github.com/Agent-Field/SWE-AF/pull/42",
            integration_branch="contract", base_branch="main", cfg=cfg,
            resolved_models={}, goal="contract", completed_issues=[], head_sha=head_sha,
        )
    return result, dispatch, runner


class TestCIHeadControls(unittest.IsolatedAsyncioTestCase):
    def test_existing_diagnostics_and_schema_controls(self):
        self.assertEqual(_extract_run_id(_check_run_row()["detailsUrl"]), "12345")
        self.assertEqual(_tail("short"), "short")
        self.assertTrue(_tail("x" * 4000).endswith("x" * 3000))
        self.assertEqual(CIWatchResult(status="no_checks", pr_number=42).model_dump()["status"], "no_checks")

    def test_selected_fixture_and_exact_command_contract(self):
        row = _check_run_row(status="IN_PROGRESS", conclusion=None)
        page = _commit_rollup_page([row])
        runner = _CIHeadCommandRunner([page])
        self.assertEqual(json.loads(runner(_ci_head_command(), REPOSITORY_PATH).stdout), page)
        self.assertEqual(runner.pages, [])
        self.assertEqual(_rollup_contexts(page)["checkRunCount"], 1)
        self.assertNotIn("state", page["data"]["repository"]["object"]["statusCheckRollup"])
        self.assertNotEqual(_commit_rollup_page(null=True), _commit_rollup_page())

    def test_captured_unsupported_field_rejection(self):
        runner = _CIHeadCommandRunner()
        proc = runner(["gh", "pr", "checks", "42", "--json", "bucket,state,name,workflow,link,headSha"], REPOSITORY_PATH)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, 'Unknown JSON field: "headSha"')

    async def test_success_envelope_failed_watch_exhausts_without_fixer(self):
        result, dispatch, _ = await _run_gate_fixture([_successful_execution_envelope(_watch_result_payload())], cycles=0)
        self.assertEqual(result["final_status"], "failed_exhausted")
        self.assertEqual(dispatch.targets, [COMMIT_A])
        self.assertEqual(dispatch.fixer_arguments, [])


class TestCIHeadRegression(unittest.IsolatedAsyncioTestCase):
    async def test_exact_commit_query_pending_then_passes(self):
        pages = [_commit_rollup_page([_check_run_row(status="IN_PROGRESS", conclusion=None)]), _commit_rollup_page([_check_run_row()])]
        result, runner, clock = await _watch_commit_pages(pages)
        self.assertEqual(result.status, "passed", result.summary)
        self.assertEqual(runner.unsupported_calls, [])
        self.assertEqual(runner.pages, [])
        self.assertEqual(clock.sleeps, [1])

    async def test_gate_advances_watcher_target_to_fixer_commit(self):
        watches = [_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))]
        result, dispatch, _ = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}])
        self.assertEqual(result["final_status"], "passed")
        self.assertEqual(dispatch.targets, [COMMIT_A, COMMIT_B])
        self.assertEqual(len(dispatch.fixer_arguments), 1)
        self.assertEqual(dispatch.fixer_arguments[0]["failed_checks"], _watch_result_payload()["failed_checks"])

    async def test_gate_accepts_raw_failed_watch_result(self):
        watches = [_watch_result_payload(), _watch_result_payload("passed")]
        result, dispatch, _ = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}])
        self.assertEqual(result["final_status"], "passed")
        self.assertEqual(len(dispatch.fixer_arguments), 1)
        self.assertEqual(dispatch.targets, [COMMIT_A, COMMIT_B])


@pytest.mark.parametrize("null", [False, True])
async def test_ci_head_empty_only_remains_distinct(null):
    result, _, clock = await _watch_commit_pages([_commit_rollup_page(null=null)], repeat=True)
    assert result.status == "no_checks"
    assert clock.t == 4


async def test_ci_head_null_to_pending_to_success():
    pending = _check_run_row(status="QUEUED", conclusion=None)
    result, runner, _ = await _watch_commit_pages([_commit_rollup_page(null=True), _commit_rollup_page([pending]), _commit_rollup_page([_check_run_row()])])
    assert result.status == "passed"
    assert runner.pages == []


async def test_ci_head_nonempty_then_null_times_out():
    result, _, clock = await _watch_commit_pages([_commit_rollup_page([_check_run_row(status="WAITING", conclusion=None)]), _commit_rollup_page(null=True)], repeat=True)
    assert result.status == "timed_out"
    assert clock.t == 4


@pytest.mark.parametrize("size", [100, 101])
async def test_ci_head_complete_manual_pagination_includes_late_failure(size):
    rows = [_check_run_row(str(i), name=str(i)) for i in range(size)]
    rows[-1]["conclusion"] = "FAILURE"
    pages = [_commit_rollup_page(rows[:100], counts=(size, 0), more=size > 100)]
    if size > 100:
        pages.append(_commit_rollup_page(rows[100:], counts=(size, 0), cursor="cursor-2"))
    pages.append(_commit_rollup_page(rows[:100], counts=(size, 0), more=size > 100))
    result, runner, _ = await _watch_commit_pages(pages)
    assert result.status == "failed"
    assert len(result.failed_checks) == 1
    assert result.failed_checks[0].name == str(size - 1)
    assert result.failed_checks[0].details_url == rows[-1]["detailsUrl"]
    assert "contract failure" in result.failed_checks[0].logs_excerpt
    assert runner.pages == []


@pytest.mark.parametrize("movement", ["count", "null"])
async def test_ci_head_moving_pagination_restarts_at_null(movement):
    first = _commit_rollup_page([_check_run_row("old")], counts=(2, 0), more=True)
    second = _commit_rollup_page([_check_run_row("new")], counts=(3, 0), cursor="cursor-2")
    if movement == "null":
        second = _commit_rollup_page(null=True)
    result, runner, clock = await _watch_commit_pages([first, second, _commit_rollup_page([_check_run_row()])])
    assert result.status == "passed"
    queries = [cmd for cmd in runner.calls if cmd[:3] == ["gh", "api", "graphql"]]
    assert [cmd[-2:] for cmd in queries] == [["--field", "after=null"], ["--raw-field", "after=cursor-1"], ["--field", "after=null"]]
    assert clock.sleeps == [1]


async def test_ci_head_repeated_instability_keeps_original_deadline():
    pages = []
    for index in range(4):
        pages.extend([_commit_rollup_page([_check_run_row(str(index))], counts=(2, 0), more=True), _commit_rollup_page(null=True)])
    result, runner, clock = await _watch_commit_pages(pages)
    assert result.status == "timed_out"
    assert clock.t == 4
    assert runner.pages == []


@pytest.mark.parametrize("kind", ["CheckRun", "StatusContext"])
@pytest.mark.parametrize("latest_fails", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_ci_head_distinct_node_ids_retain_failure_after_raw_count_validation(kind, latest_fails, reverse):
    """Adversarial same-name contract double: distinct node IDs cannot supersede."""
    make = {"CheckRun": _check_run_row, "StatusContext": _status_context_row}[kind]
    old = make("old")
    new = make("new")
    field = {"CheckRun": "conclusion", "StatusContext": "state"}[kind]
    stamp = {"CheckRun": "startedAt", "StatusContext": "createdAt"}[kind]
    link = {"CheckRun": "detailsUrl", "StatusContext": "targetUrl"}[kind]
    base = {"CheckRun": "https://github.com/Agent-Field/SWE-AF/actions/runs/12345/job/", "StatusContext": "https://ci.example.test/build/"}[kind]
    old[link], new[link] = base + "1", base + "2"
    old[field] = "SUCCESS" if latest_fails else "FAILURE"
    new[field] = "FAILURE" if latest_fails else "SUCCESS"
    new[stamp] = "2026-10-23T00:01:00Z"
    rows = [new, old] if reverse else [old, new]
    page = _commit_rollup_page(rows)
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"
    failed = new if latest_fails else old
    assert len(result.failed_checks) == 1
    assert result.failed_checks[0].details_url == failed[link]
    assert result.failed_checks[0].logs_excerpt


@pytest.mark.parametrize("separation", ["workflow", "event", "typed_collision"])
async def test_ci_head_other_workflow_event_and_type_remain_distinct(separation):
    old = _check_run_row("old", conclusion="FAILURE")
    new = _check_run_row("new", startedAt="2026-10-23T00:01:00Z")
    if separation == "typed_collision":
        new = _status_context_row("new", context="Tests")
    elif separation == "workflow":
        new["checkSuite"]["workflowRun"]["workflow"]["name"] = "other"
    else:
        new["checkSuite"]["workflowRun"]["event"] = "push"
    page = _commit_rollup_page([new, old])
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"


@pytest.mark.parametrize("kind,stamp", [("CheckRun", None), ("CheckRun", "2026-10-23T04:00:00+04:00"), ("StatusContext", "2026-10-23T04:00:00+04:00")])
async def test_ci_head_independent_successful_nodes_need_no_recency_election(kind, stamp):
    """Adversarial equal/null-time successes need no cross-ID recency election."""
    make, field = {"CheckRun": (_check_run_row, "startedAt"), "StatusContext": (_status_context_row, "createdAt")}[kind]
    rows = [make("old"), make("new", **{field: stamp})]
    result, _, clock = await _watch_commit_pages([_commit_rollup_page(rows)], repeat=True)
    assert result.status == "passed"
    assert result.summary == "All 2 selected checks passed"
    assert clock.sleeps == []


async def test_ci_head_singleton_null_start_is_valid():
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row(startedAt=None, checkSuite=None)])])
    assert result.status == "passed"


INVALID_CHECK_RUN_FIELDS = [
    ("__typename", "Unknown"), ("id", ""), ("name", ""), ("name", None),
    ("detailsUrl", 7), ("startedAt", "yesterday"), ("startedAt", "2026-10-23T00:00:00"),
    ("status", "SURPRISE"), ("conclusion", "SURPRISE"), ("conclusion", None),
    ("checkSuite", []), ("checkSuite", {"workflowRun": {}}),
]


@pytest.mark.parametrize("field,value", INVALID_CHECK_RUN_FIELDS)
async def test_ci_head_strict_checkrun_rows(field, value):
    row = _check_run_row()
    row[field] = value
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([row])])
    assert result.status == "error"


@pytest.mark.parametrize("field", ["startedAt", "conclusion", "detailsUrl", "checkSuite"])
async def test_ci_head_missing_selected_checkrun_fields_are_errors(field):
    row = _check_run_row()
    del row[field]
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([row])])
    assert result.status == "error"


@pytest.mark.parametrize("field,value", [("createdAt", None), ("createdAt", "bad"), ("targetUrl", 1), ("state", "UNKNOWN"), ("context", "")])
async def test_ci_head_strict_status_rows(field, value):
    row = _status_context_row()
    row[field] = value
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([row])])
    assert result.status == "error"


@pytest.mark.parametrize("conclusion,expected", [("SUCCESS", "passed"), ("SKIPPED", "passed"), ("NEUTRAL", "passed"), ("FAILURE", "failed"), ("TIMED_OUT", "failed"), ("ACTION_REQUIRED", "failed"), ("CANCELLED", "failed"), ("STALE", "timed_out")])
async def test_ci_head_checkrun_conclusion_whitelist(conclusion, expected):
    page = _commit_rollup_page([_check_run_row(conclusion=conclusion)])
    result, _, _ = await _watch_commit_pages([page, page], repeat=True)
    assert result.status == expected


@pytest.mark.parametrize("state,expected", [("SUCCESS", "passed"), ("FAILURE", "failed"), ("ERROR", "failed"), ("PENDING", "timed_out"), ("EXPECTED", "timed_out")])
async def test_ci_head_status_whitelist(state, expected):
    page = _commit_rollup_page([_status_context_row(state=state)])
    result, _, _ = await _watch_commit_pages([page, page], repeat=True)
    assert result.status == expected


@pytest.mark.parametrize("part,value", [("repository", None), ("id", "other"), ("nameWithOwner", "wrong/repo"), ("pullRequest", None), ("number", True), ("number", 43), ("headRefOid", COMMIT_B), ("object", None), ("__typename", "Tree"), ("oid", COMMIT_B), ("rollup_missing", None)])
async def test_ci_head_exact_provenance_is_required_on_every_page(part, value):
    bad = _commit_rollup_page([_check_run_row("second")], counts=(2, 0), cursor="cursor-2")
    repo = bad["data"]["repository"]
    target = {"number": repo["pullRequest"], "headRefOid": repo["pullRequest"], "__typename": repo["object"], "oid": repo["object"]}.get(part, repo)
    if part == "repository":
        bad["data"]["repository"] = value
    elif part == "rollup_missing":
        del repo["object"]["statusCheckRollup"]
    else:
        target[part] = value
    first = _commit_rollup_page([_check_run_row("first")], counts=(2, 0), more=True)
    result, _, _ = await _watch_commit_pages([first, bad])
    assert result.status == "error"


@pytest.mark.parametrize("defect", ["duplicate", "mismatch", "bool_count", "missing_info", "bad_flag", "missing_cursor", "empty_continuation", "too_many", "cyclic_cursor"])
async def test_ci_head_pagination_fails_closed(defect):
    page = _commit_rollup_page([_check_run_row()])
    contexts = _rollup_contexts(page)
    mutations = {
        "duplicate": {"nodes": [_check_run_row(), _check_run_row()], "checkRunCount": 2},
        "mismatch": {"checkRunCount": 2}, "bool_count": {"checkRunCount": True},
        "missing_info": {"pageInfo": None}, "bad_flag": {"pageInfo": {"hasNextPage": 1, "endCursor": "x"}},
        "missing_cursor": {"pageInfo": {"hasNextPage": True, "endCursor": None}},
        "empty_continuation": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "x"}},
        "too_many": {"nodes": [_check_run_row(str(i)) for i in range(101)], "checkRunCount": 101},
        "cyclic_cursor": {"checkRunCount": 3, "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}},
    }
    contexts.update(mutations[defect])
    second = _commit_rollup_page([_check_run_row("second")], counts=(3, 0), more=True)
    result, _, _ = await _watch_commit_pages([page, second])
    assert result.status == "error"


async def test_ci_head_duplicate_id_still_errors_when_counts_move():
    first = _commit_rollup_page([_check_run_row()], counts=(2, 0), more=True)
    second = _commit_rollup_page([_check_run_row()], counts=(3, 0), cursor="cursor-2")
    result, _, _ = await _watch_commit_pages([first, second])
    assert result.status == "error"


@pytest.mark.parametrize("payload", [_completed(stdout=" "), _completed(stdout="{"), _completed(stdout="[]"), _completed(stdout=json.dumps(_commit_rollup_page([_check_run_row()])), stderr="authentication failed", returncode=1), {"errors": [{"message": "denied"}], "data": _commit_rollup_page()["data"]}, OSError("launch failure"), subprocess.TimeoutExpired("gh", 1)])
async def test_ci_head_acquisition_errors_never_accept_partial_data(payload):
    result, _, _ = await _watch_commit_pages([payload])
    assert result.status == "error"


@pytest.mark.parametrize("target", [None, False, " ", "abc", "0" * 40, "g" * 40])
async def test_ci_head_invalid_explicit_target_never_resolves_local_head(target):
    result, runner, _ = await _watch_commit_pages([], head_sha=target)
    assert result.status == "error"
    assert not any(cmd[0] == "git" for cmd in runner.calls)


async def test_ci_head_omitted_target_resolves_local_commit_once():
    result, runner, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row()])], head_sha="")
    assert result.status == "passed"
    assert runner.calls.count(["git", "rev-parse", "--verify", "HEAD^{commit}"]) == 1


async def test_ci_head_deadline_crossed_during_acquisition_is_error():
    clock = _PollingClock()
    runner = _CIHeadCommandRunner([_commit_rollup_page([_check_run_row()])], clock=clock, delay=5)
    result, _, _ = await _watch_commit_pages([], runner=runner, now=clock.now, sleep=clock.sleep)
    assert result.status == "error"


async def test_ci_head_deadline_before_first_observation_is_error():
    readings = iter([0, 0, 3.999, 4, 4])
    result, runner, clock = await _watch_commit_pages([], now=readings.__next__)
    assert runner.calls == [["gh", "repo", "view", "--json", "id,nameWithOwner,url"]]
    assert clock.sleeps == []
    assert result.elapsed_seconds == 4
    assert result.pr_number == 42
    assert result.failed_checks == []
    assert list(readings) == []
    assert result.status == "error"


@pytest.mark.parametrize("status", ["no_checks", "timed_out", "error"])
async def test_ci_gate_preserves_terminal_nonfailure_status_without_fixer(status):
    result, dispatch, _ = await _run_gate_fixture([_successful_execution_envelope(_watch_result_payload(status))])
    assert result["final_status"] == status
    assert dispatch.fixer_arguments == []


@pytest.mark.parametrize("fix", [{}, {"pushed": "yes", "commit_sha": COMMIT_B}, {"pushed": True}, {"pushed": True, "commit_sha": "short"}, {"pushed": True, "commit_sha": COMMIT_A}])
async def test_ci_gate_invalid_fixer_commit_never_starts_second_watch(fix):
    result, dispatch, _ = await _run_gate_fixture([_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))], [fix])
    assert result["final_status"] == "error"
    assert dispatch.targets == [COMMIT_A]


async def test_ci_gate_fixer_explicit_false_gives_up():
    result, dispatch, _ = await _run_gate_fixture([_successful_execution_envelope(_watch_result_payload())], [{"pushed": False}])
    assert result["final_status"] == "fixer_gave_up"
    assert dispatch.targets == [COMMIT_A]


@pytest.mark.parametrize("bad", [_watch_result_payload("passed", pr_number=43), _watch_result_payload("passed", elapsed_seconds=True), _watch_result_payload("passed", failed_checks="bad"), {"status": "success", "result": {"status": "unknown"}}, {"status": "failed", "execution_id": "failed", "result": _watch_result_payload()},
    pytest.param({"status": "failed", "execution_id": "watch-failure", "result": None, "error_message": "worker stopped"}, id="failed-sync-none"),
    pytest.param({"status": "error", "execution_id": "watch-error", "result": None, "error_message": "worker stopped"}, id="error-sync-none")])
async def test_ci_gate_invalid_watch_and_failed_envelopes_never_reach_fixer(bad):
    result, dispatch, _ = await _run_gate_fixture([bad])
    assert result["final_status"] == "error"
    assert result["watch"]["status"] == "error"
    assert result["fix_attempts"] == []
    assert dispatch.fixer_arguments == []
    assert dispatch.targets == [COMMIT_A]


@pytest.mark.parametrize("head", [COMMIT_C, COMMIT_A])
async def test_ci_gate_fixer_convergence_never_accepts_third_head_or_exhaustion(head):
    pages = [_commit_rollup_page([_check_run_row()]), _commit_rollup_page([_check_run_row()], sha=COMMIT_B, head=head)]
    watches = [_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))]
    result, dispatch, _ = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}], pages=pages)
    assert result["final_status"] == "error"
    assert dispatch.targets == [COMMIT_A]


async def test_ci_gate_fixer_convergence_spends_next_watch_budget():
    pages = [_commit_rollup_page([_check_run_row()]), _commit_rollup_page([_check_run_row()], sha=COMMIT_B, head=COMMIT_A),
             _commit_rollup_page([_check_run_row()], sha=COMMIT_B, head=COMMIT_A), _commit_rollup_page([_check_run_row()], sha=COMMIT_B)]
    watches = [_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))]
    result, dispatch, runner = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}], pages=pages)
    assert result["final_status"] == "passed"
    assert dispatch.targets == [COMMIT_A, COMMIT_B]
    assert runner.clock.sleeps == [1, 1]
    assert 0 < dispatch.watch_arguments[1]["wait_seconds"] <= 3


async def test_ci_gate_missing_fixer_commit_object_is_error():
    second = _commit_rollup_page(sha=COMMIT_B)
    second["data"]["repository"]["object"] = None
    watches = [_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))]
    result, dispatch, _ = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}], pages=[_commit_rollup_page(), second])
    assert result["final_status"] == "error"
    assert dispatch.targets == [COMMIT_A]


async def test_ci_gate_head_movement_before_fixer_stops_dispatch():
    watches = [_successful_execution_envelope(_watch_result_payload()), _successful_execution_envelope(_watch_result_payload("passed"))]
    result, dispatch, _ = await _run_gate_fixture(watches, [{"pushed": True, "commit_sha": COMMIT_B}], pages=[_commit_rollup_page(head=COMMIT_B)])
    assert result["final_status"] == "error"
    assert dispatch.fixer_arguments == []


async def test_ci_gate_omitted_target_is_resolved_once_and_forwarded():
    result, dispatch, runner = await _run_gate_fixture([_successful_execution_envelope(_watch_result_payload("passed"))], head_sha="")
    assert result["final_status"] == "passed"
    assert dispatch.targets == [COMMIT_A]
    assert runner.calls.count(["git", "rev-parse", "--verify", "HEAD^{commit}"]) == 1


@pytest.mark.parametrize("field,value", [("id", ""), ("nameWithOwner", "Agent-Field/SWE-AF/extra"), ("nameWithOwner", "../SWE-AF"), ("url", "https://user@github.com/Agent-Field/SWE-AF"), ("url", "https://github.com/Agent-Field/SWE-AF?x=1")])
async def test_ci_head_repository_discovery_rejects_bad_authority(field, value):
    discovery = dict(REPOSITORY_IDENTITY)
    discovery[field] = value
    runner = _CIHeadCommandRunner([_commit_rollup_page([_check_run_row()])], discovery=discovery)
    result, _, _ = await _watch_commit_pages([], runner=runner)
    assert result.status == "error"


@pytest.mark.parametrize("key,value", [("GH_REPO", "other/repo"), ("GH_HOST", "other.example")])
async def test_ci_head_ambient_repository_overrides_cannot_change_target(key, value):
    clock = _PollingClock()
    runner = _CIHeadCommandRunner([_commit_rollup_page([_check_run_row()])])
    with patch.dict(os.environ, {"GH_REPO": "", "GH_HOST": "", key: value}):
        result = await watch_pr_checks(repo_path=REPOSITORY_PATH, pr_number=42, head_sha=COMMIT_A,
                                       wait_seconds=4, poll_seconds=1, runner=runner, now=clock.now, sleep=clock.sleep)
    assert result.status == "error"


@pytest.mark.parametrize("field,value", [("pr_number", True), ("pr_number", 0), ("wait_seconds", 0), ("poll_seconds", 0)])
async def test_ci_head_invalid_watch_parameters_error(field, value):
    result, _, _ = await _watch_commit_pages([], **{field: value})
    assert result.status == "error"


async def test_ci_head_failure_then_head_movement_overrides_diagnostics():
    failure = _commit_rollup_page([_check_run_row(conclusion="FAILURE")])
    result, _, _ = await _watch_commit_pages([failure, _commit_rollup_page([_check_run_row()], head=COMMIT_B)])
    assert result.status == "error"


async def test_ci_head_shared_run_logs_are_fetched_once_and_urls_retained():
    rows = [_check_run_row("one", name="one", conclusion="FAILURE"), _check_run_row("two", name="two", conclusion="FAILURE")]
    result, runner, _ = await _watch_commit_pages([_commit_rollup_page(rows), _commit_rollup_page(rows)])
    assert result.status == "failed"
    assert len(result.failed_checks) == 2
    calls = [cmd for cmd in runner.calls if cmd[:3] == ["gh", "run", "view"]]
    assert len(calls) == 1
    assert [item.details_url for item in result.failed_checks] == [row["detailsUrl"] for row in rows]


async def test_ci_head_foreign_log_url_is_not_fetched():
    row = _check_run_row(conclusion="FAILURE", detailsUrl="https://other.example/actions/runs/12345/job/1")
    result, runner, _ = await _watch_commit_pages([_commit_rollup_page([row]), _commit_rollup_page([row])])
    assert result.status == "failed"
    assert result.failed_checks[0].details_url == row["detailsUrl"]
    assert result.failed_checks[0].logs_excerpt
    assert not any(cmd[:3] == ["gh", "run", "view"] for cmd in runner.calls)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["CheckRun", "StatusContext"])
async def test_ci_head_distinct_node_failure_retained_after_all_pages_regardless_of_order(kind, reverse):
    """Adversarial collisions span complete pages, followed by the provenance guard."""
    make = {"CheckRun": _check_run_row, "StatusContext": _status_context_row}[kind]
    field, stamp = {"CheckRun": ("conclusion", "startedAt"), "StatusContext": ("state", "createdAt")}[kind]
    old, new = make("old", **{field: "FAILURE"}), make("new", **{stamp: "2026-10-23T00:01:00Z"})
    rows = [new, old] if reverse else [old, new]
    counts = {"CheckRun": (2, 0), "StatusContext": (0, 2)}[kind]
    pages = [_commit_rollup_page(rows[:1], counts=counts, more=True), _commit_rollup_page(rows[1:], counts=counts, cursor="cursor-2")]
    result, runner, _ = await _watch_commit_pages(pages + [pages[0]])
    assert result.status == "failed"
    assert runner.pages == []
    assert len([cmd for cmd in runner.calls if cmd[:3] == ["gh", "api", "graphql"]]) == 3


async def test_ci_head_group_tuple_components_cannot_collide_by_concatenation():
    old = _check_run_row("old", name="a_b", conclusion="FAILURE")
    old["checkSuite"]["workflowRun"]["workflow"]["name"] = "c"
    new = _check_run_row("new", name="a", startedAt="2026-10-23T00:01:00Z")
    new["checkSuite"]["workflowRun"]["workflow"]["name"] = "b_c"
    page = _commit_rollup_page([new, old])
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"


@pytest.mark.parametrize("nesting", [{"workflowRun": None}, {"workflowRun": {"event": "push", "workflow": None}}, None])
async def test_ci_head_nullable_workflow_nesting_preserves_singleton_node(nesting):
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row(checkSuite=nesting)])])
    assert result.status == "passed"


@pytest.mark.parametrize("workflow_run", [{"workflow": {"name": "CI"}}, {"event": None, "workflow": {"name": "CI"}}, {"event": 1, "workflow": {"name": "CI"}}, {"event": "push", "workflow": {}}, {"event": "push", "workflow": {"name": None}}])
async def test_ci_head_present_workflow_objects_require_selected_typed_fields(workflow_run):
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([_check_run_row(checkSuite={"workflowRun": workflow_run})])])
    assert result.status == "error"


async def test_ci_head_malformed_later_page_cannot_hide_behind_count_instability():
    first = _commit_rollup_page([_check_run_row("first")], counts=(2, 0), more=True)
    second = _commit_rollup_page([_check_run_row("second", startedAt="invalid")], counts=(3, 0), cursor="cursor-2")
    result, _, _ = await _watch_commit_pages([first, second])
    assert result.status == "error"


async def test_ci_head_log_failure_preserves_failed_verdict_and_original_url():
    row = _check_run_row(conclusion="FAILURE")
    page = _commit_rollup_page([row])
    runner = _CIHeadCommandRunner([page, page], logs=_completed(stderr="log access denied", returncode=1))
    result, _, _ = await _watch_commit_pages([], runner=runner)
    assert result.status == "failed"
    assert result.failed_checks[0].details_url == row["detailsUrl"]
    assert result.failed_checks[0].logs_excerpt


class _CIHeadSubprocess:
    def __init__(self):
        self.runner = _CIHeadCommandRunner([_commit_rollup_page([_check_run_row()])])
        self.timeouts = []

    def __call__(self, command, **options):
        assert options["check"] is False
        assert options["capture_output"] is True
        assert options["text"] is True
        self.timeouts.append(options["timeout"])
        return self.runner(command, options["cwd"])


async def test_ci_head_default_subprocess_commands_receive_remaining_deadline_timeout():
    adapter = _CIHeadSubprocess()
    with patch("swe_af.execution.ci_gate.subprocess.run", adapter):
        result, _, _ = await _watch_commit_pages([], runner=None)
    assert result.status == "passed"
    assert adapter.timeouts
    assert all(0 < timeout <= 4 for timeout in adapter.timeouts)


@pytest.mark.parametrize("producer_ids,selected_run", [
    pytest.param(({"app": "app-a", "workflow": "workflow-a", "run": "run-a", "job": "job-a"}, {"app": "app-b", "workflow": "workflow-b", "run": "run-b", "job": "job-b"}), {"event": "pull_request", "workflow": {"name": "CI"}}, id="adversarial-independent-apps"),
    pytest.param(({"app": "app-a", "workflow": "workflow-a", "run": "run-a", "job": "job-a"}, {"app": "app-a", "workflow": "workflow-b", "run": "run-b", "job": "job-b"}), {"event": "pull_request", "workflow": {"name": "CI"}}, id="adversarial-different-workflows-same-display-name"),
    pytest.param(({"app": "app-a", "workflow": "workflow-a", "run": "run-a", "job": "job-a"}, {"app": "app-a", "workflow": "workflow-a", "run": "run-b", "job": "job-b"}), {"event": "pull_request", "workflow": {"name": "CI"}}, id="adversarial-separate-runs-one-workflow"),
    pytest.param(({"app": "app-a", "workflow": "workflow-a", "run": "run-a", "job": "job-a"}, {"app": "app-a", "workflow": "workflow-a", "run": "run-a", "job": "job-b"}), {"event": "pull_request", "workflow": {"name": "CI"}}, id="adversarial-same-app-workflow-run-independent-jobs"),
    pytest.param(({"app": None, "workflow": None, "run": None, "job": "job-a"}, {"app": None, "workflow": None, "run": None, "job": "job-b"}), None, id="adversarial-null-app-external-checks"),
    pytest.param(({"job": "job-a"}, {"job": "job-b"}), None, id="adversarial-absent-app-external-checks"),
    pytest.param(({"app": "unknown", "job": "job-a"}, {"app": "unknown", "job": "job-b"}), None, id="adversarial-unknown-app-external-checks"),
])
async def test_ci_head_independent_producer_projections_retain_failure(producer_ids, selected_run):
    """Adversarial producer IDs are metadata; only selected_run reaches the query projection."""
    old = _check_run_row("producer-failure", conclusion="FAILURE", checkSuite={"workflowRun": selected_run})
    new = _check_run_row("producer-success", startedAt="2026-10-23T00:01:00Z", checkSuite={"workflowRun": selected_run})
    page = _commit_rollup_page([old, new])
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"
    assert result.failed_checks[0].details_url == old["detailsUrl"]
    assert "contract failure" in result.failed_checks[0].logs_excerpt


@pytest.mark.parametrize("kind", ["CheckRun", "StatusContext"])
@pytest.mark.parametrize("pending_is_later", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_ci_head_distinct_pending_node_cannot_be_hidden_by_success(kind, pending_is_later, reverse):
    """Adversarial same-name pending and successful nodes remain independent."""
    make, stamp = {"CheckRun": (_check_run_row, "startedAt"), "StatusContext": (_status_context_row, "createdAt")}[kind]
    old, new = make("old"), make("new", **{stamp: "2026-10-23T00:01:00Z"})
    pending = new if pending_is_later else old
    pending.update({"CheckRun": {"status": "IN_PROGRESS", "conclusion": None}, "StatusContext": {"state": "PENDING"}}[kind])
    rows = [new, old] if reverse else [old, new]
    result, _, clock = await _watch_commit_pages([_commit_rollup_page(rows)], repeat=True)
    assert result.status == "timed_out"
    assert clock.t == 4
    assert clock.sleeps == [1, 1, 1, 1]


@pytest.mark.parametrize("kind", ["CheckRun", "StatusContext"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_ci_head_both_same_name_failure_records_are_retained(kind, reverse):
    """Adversarial same-name nodes retain both independent failure diagnostics."""
    make, field, stamp, link = {"CheckRun": (_check_run_row, "conclusion", "startedAt", "detailsUrl"), "StatusContext": (_status_context_row, "state", "createdAt", "targetUrl")}[kind]
    old, new = make("old", **{field: "FAILURE"}), make("new", **{field: "FAILURE", stamp: "2026-10-23T00:01:00Z"})
    base = {"CheckRun": "https://github.com/Agent-Field/SWE-AF/actions/runs/12345/job/", "StatusContext": "https://ci.example.test/build/"}[kind]
    old[link], new[link] = base + "1", base + "2"
    rows = [new, old] if reverse else [old, new]
    page = _commit_rollup_page(rows)
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"
    assert len(result.failed_checks) == 2
    assert sorted(item.details_url for item in result.failed_checks) == sorted(row[link] for row in rows)


@pytest.mark.parametrize("kind", ["CheckRun", "StatusContext"])
@pytest.mark.parametrize("node_id", ["missing", None, "", " ", False, 7])
@pytest.mark.parametrize("later_page", [False, True])
async def test_ci_head_malformed_node_ids_are_errors_before_verdict_or_logs(kind, node_id, later_page):
    make = {"CheckRun": _check_run_row, "StatusContext": _status_context_row}[kind]
    row = make("invalid")
    if node_id == "missing":
        del row["id"]
    else:
        row["id"] = node_id
    pages = [_commit_rollup_page([row])]
    if later_page:
        counts = {"CheckRun": (2, 0), "StatusContext": (0, 2)}[kind]
        pages = [_commit_rollup_page([make("first")], counts=counts, more=True), _commit_rollup_page([row], counts=counts, cursor="cursor-2")]
    result, runner, _ = await _watch_commit_pages(pages)
    assert result.status == "error"
    assert not any(cmd[:3] == ["gh", "run", "view"] for cmd in runner.calls)


async def test_ci_head_status_duplicate_id_errors_despite_changing_counts():
    first = _commit_rollup_page([_status_context_row()], counts=(0, 2), more=True)
    second = _commit_rollup_page([_status_context_row()], counts=(0, 3), cursor="cursor-2")
    result, _, _ = await _watch_commit_pages([first, second])
    assert result.status == "error"


async def test_ci_head_uv_current_attempt_actions_only_projection_passes():
    """Reduced UV run 39934800263 attempt 2 job 120363374413 projection.

    Attempt 1 job 120358827249 is absent. This Actions-only subset is not
    the captured 65-node rollup, which fails because of CodSpeed.
    detailsUrl null is a schema-supported fixture value, not a captured field.
    """
    current = _check_run_row("CR_kwDOKbIFZc8AAAAcBjdXTQ", name="all required jobs passed", startedAt="2026-10-23T10:20:12Z", detailsUrl=None)
    result, _, _ = await _watch_commit_pages([_commit_rollup_page([current])])
    assert result.status == "passed"
    assert result.summary == "All 1 selected checks passed"


async def test_ci_head_uv_historical_failure_co_present_is_not_superseded():
    """Adversarial UV projection adds attempt 1 job 120358827249 to current attempt 2.

    The historical GraphQL node ID is synthetic; REST only bound its job ID.
    detailsUrl null is schema-supported and was not selected by the capture.
    """
    old = _check_run_row("contract-projection-job-120358827249", name="all required jobs passed", conclusion="FAILURE", startedAt="2026-10-23T10:08:42Z", detailsUrl=None)
    current = _check_run_row("CR_kwDOKbIFZc8AAAAcBjdXTQ", name="all required jobs passed", startedAt="2026-10-23T10:20:12Z", detailsUrl=None)
    page = _commit_rollup_page([old, current])
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"
    assert result.failed_checks[0].conclusion == "FAILURE"
    assert result.failed_checks[0].details_url == ""


async def test_ci_head_uv_projection_retains_external_codspeed_failure():
    """Reduced two-node projection preserves the captured 65-node rollup's failure.

    detailsUrl null is a schema-supported fixture value, not a captured field.
    """
    current = _check_run_row("CR_kwDOKbIFZc8AAAAcBjdXTQ", name="all required jobs passed", startedAt="2026-10-23T10:20:12Z", detailsUrl=None)
    codspeed = _check_run_row("CR_kwDOKbIFZc8AAAAcBfHJ5g", name="CodSpeed Performance Analysis", conclusion="FAILURE", startedAt="2026-10-23T10:08:37Z", checkSuite={"workflowRun": None}, detailsUrl=None)
    page = _commit_rollup_page([current, codspeed])
    result, _, _ = await _watch_commit_pages([page, page])
    assert result.status == "failed"
    assert result.failed_checks[0].name == "CodSpeed Performance Analysis"


async def test_ci_head_runner_cancellation_propagates_and_restores_environment():
    with patch.dict(os.environ, {"GH_REPO": "outer/repo", "GH_HOST": "outer.example"}):
        with pytest.raises(asyncio.CancelledError):
            await _watch_commit_pages([asyncio.CancelledError()])
        assert os.environ["GH_REPO"] == "outer/repo"
        assert os.environ["GH_HOST"] == "outer.example"


@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "success-envelope"])
async def test_ci_registered_watcher_consumes_valid_results(wrapped):
    module = importlib.import_module("swe_af.reasoners.execution_agents")
    arguments = {"repo_path": REPOSITORY_PATH, "pr_number": 42, "head_sha": COMMIT_A,
                 "wait_seconds": 5, "poll_seconds": 1}
    watch = CIWatchResult(status="passed", pr_number=42, summary="registered watcher")
    body = AsyncMock(return_value=watch)
    with patch.object(module, "watch_pr_checks", body), patch.object(module.router, "note"):
        payload = await module.run_ci_watcher._original_func(**arguments)
    body.assert_awaited_once_with(**arguments)
    assert CIWatchResult.model_validate(payload, strict=True).model_dump() == watch.model_dump()
    value = _successful_execution_envelope(payload) if wrapped else payload
    result, dispatch, _ = await _run_gate_fixture([value])
    assert result["final_status"] == "passed"
    assert result["watch"] == payload
    assert (result["fix_attempts"], dispatch.fixer_arguments, dispatch.targets) == ([], [], [COMMIT_A])
    assert dispatch.watch_arguments == [arguments]


async def test_ci_registered_watcher_converts_exception_to_error():
    module = importlib.import_module("swe_af.reasoners.execution_agents")
    arguments = {"repo_path": REPOSITORY_PATH, "pr_number": 42, "head_sha": COMMIT_A,
                 "wait_seconds": 5, "poll_seconds": 1}
    body = AsyncMock(side_effect=RuntimeError("watch body stopped"))
    with patch.object(module, "watch_pr_checks", body), patch.object(module.router, "note"):
        payload = await module.run_ci_watcher._original_func(**arguments)
    body.assert_awaited_once_with(**arguments)
    assert CIWatchResult.model_validate(payload, strict=True).status == "error"
    assert "watch body stopped" in payload["summary"]
    result, dispatch, _ = await _run_gate_fixture([payload])
    assert (result["final_status"], result["watch"], result["fix_attempts"]) == ("error", payload, [])
    assert (dispatch.targets, dispatch.fixer_arguments) == ([COMMIT_A], [])


async def test_ci_registered_watcher_cancellation_propagates():
    module = importlib.import_module("swe_af.reasoners.execution_agents")
    arguments = {"repo_path": REPOSITORY_PATH, "pr_number": 42, "head_sha": COMMIT_A,
                 "wait_seconds": 5, "poll_seconds": 1}
    failure = asyncio.CancelledError("watch body cancelled")
    body = AsyncMock(side_effect=failure)
    with patch.object(module, "watch_pr_checks", body), patch.object(module.router, "note"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await module.run_ci_watcher._original_func(**arguments)
    body.assert_awaited_once_with(**arguments)
    assert caught.value is failure


@pytest.mark.parametrize("status,inner,expected_status", [
    pytest.param("success", _watch_result_payload("passed"), "passed", id="success-extraction"),
    pytest.param("failed", None, "error", id="failed-sync-none"),
    pytest.param("error", None, "error", id="error-sync-none"),
])
async def test_ci_sdk_sync_envelope_contract(status, inner, expected_status):
    module = importlib.import_module("swe_af.app")
    arguments = {"repo_path": REPOSITORY_PATH, "pr_number": 42, "head_sha": COMMIT_A,
                 "wait_seconds": 5, "poll_seconds": 1}
    response = {"status": status, "execution_id": "sdk-watch", "result": inner,
                "error_message": "worker stopped"}
    expected = response if inner is None else inner
    execute = AsyncMock(return_value=response)
    execute_async = AsyncMock(side_effect=AssertionError("async SDK execution forbidden"))
    original = (module.app.agentfield_connected, module.app.async_config.enable_async_execution,
                module.app.client.execute, module.app.client.execute_async)
    with patch.object(module.app.client, "execute", execute), patch.object(module.app.client, "execute_async", execute_async):
        with patch.object(module.app, "agentfield_connected", True), patch.object(module.app.async_config, "enable_async_execution", False):
            value = await Agent.call(module.app, f"{module.NODE_ID}.run_ci_watcher", **arguments)
    execute.assert_awaited_once_with(target=f"{module.NODE_ID}.run_ci_watcher", input_data=arguments, headers=ANY)
    execute_async.assert_not_awaited()
    assert value is expected
    assert (module.app.agentfield_connected, module.app.async_config.enable_async_execution,
            module.app.client.execute, module.app.client.execute_async) == original
    result, dispatch, _ = await _run_gate_fixture([value])
    assert (result["final_status"], result["watch"]["status"], result["fix_attempts"],
            dispatch.fixer_arguments, dispatch.targets) == (expected_status, expected_status, [], [], [COMMIT_A])


@pytest.mark.parametrize("response,error_type", [
    pytest.param(asyncio.CancelledError("boundary control"), asyncio.CancelledError, id="direct-cancellation"),
    pytest.param(ExecutionCancelledError("boundary control"), ExecutionCancelledError, id="sdk-cancellation"),
    pytest.param(ExecutionFailedError("boundary control"), ExecutionFailedError, id="sdk-execution-failure"),
    pytest.param(ExecutionTimeoutError("boundary control"), ExecutionTimeoutError, id="sdk-timeout"),
    pytest.param(AgentFieldClientError("403 Forbidden: boundary control"), AgentFieldClientError, id="authorization"),
    pytest.param(FatalHarnessError("boundary control"), FatalHarnessError, id="direct-fatal"),
    pytest.param({"status": "cancelled", "execution_id": "cancelled-watch", "result": None, "error_message": "boundary control"}, RuntimeError, id="cancelled-envelope"),
    pytest.param({"status": "timeout", "execution_id": "timed-out-watch", "result": None, "error_message": "boundary control"}, RuntimeError, id="timeout-envelope"),
])
async def test_ci_gate_boundary_propagates_control_failures(response, error_type):
    dispatch = _CIGateDispatch([response], [])
    with patch(__name__ + "._CIGateDispatch", return_value=dispatch), pytest.raises(error_type) as caught:
        await _run_gate_fixture([])
    assert type(caught.value) is error_type
    assert isinstance(response, dict) or caught.value is response
    assert str(caught.value).endswith("boundary control")
    assert dispatch.targets == [COMMIT_A]
    assert dispatch.fixer_arguments == []


@pytest.mark.parametrize("status", ["failed", "error"])
@pytest.mark.parametrize("diagnostic,regex_match", [
    pytest.param("invalid api key", True, id="fatal-pattern"),
    pytest.param("Fatal API error (non-retryable): worker stopped", False, id="serialized-marker"),
])
async def test_ci_gate_boundary_propagates_fatal_envelopes(status, diagnostic, regex_match):
    response = {"status": status, "execution_id": "fatal-watch", "result": None, "error_message": diagnostic}
    dispatch = _CIGateDispatch([response], [])
    with patch(__name__ + "._CIGateDispatch", return_value=dispatch), pytest.raises(FatalHarnessError) as caught:
        await _run_gate_fixture([])
    assert is_fatal_error(diagnostic) is regex_match
    assert caught.value.original_message == diagnostic
    assert dispatch.targets == [COMMIT_A]
    assert dispatch.fixer_arguments == []


@pytest.mark.parametrize("response,error_type,diagnostic", [
    pytest.param(FatalHarnessError("fixer stopped"), FatalHarnessError, "fixer stopped", id="direct-fatal"),
    pytest.param({"status": "failed", "execution_id": "fatal-fix", "result": None, "error_message": "invalid api key"}, FatalHarnessError, "invalid api key", id="fatal-pattern"),
    pytest.param({"status": "error", "execution_id": "fatal-fix", "result": None, "error_message": "Fatal API error (non-retryable): worker stopped"}, FatalHarnessError, "Fatal API error (non-retryable): worker stopped", id="serialized-marker"),
    pytest.param({"status": "failed", "execution_id": "failed-fix", "result": None, "error_message": "worker stopped"}, RuntimeError, "worker stopped", id="ordinary-failed-envelope"),
])
async def test_ci_gate_boundary_preserves_fixer_failures(response, error_type, diagnostic):
    dispatch = _CIGateDispatch([_watch_result_payload()], [response])
    with patch(__name__ + "._CIGateDispatch", return_value=dispatch), pytest.raises(error_type) as caught:
        await _run_gate_fixture([])
    assert type(caught.value) is error_type
    assert isinstance(response, dict) or caught.value is response
    assert str(caught.value).endswith(diagnostic)
    assert dispatch.targets == [COMMIT_A]
    assert len(dispatch.fixer_arguments) == 1
    assert dispatch.fixer_arguments[0]["failed_checks"] == _watch_result_payload()["failed_checks"]


@pytest.mark.parametrize("origin,failure", [
    pytest.param("call", RuntimeError("run_ci_watcher failed (status=failed): programming sentinel"), id="call-runtime"),
    pytest.param("call", NotImplementedError("call subclass sentinel"), id="call-subclass"),
    pytest.param("validation", RuntimeError("validation programming sentinel"), id="validation-runtime"),
    pytest.param("validation", NotImplementedError("validation subclass sentinel"), id="validation-subclass"),
    pytest.param("unwrap", NotImplementedError("decoder subclass sentinel"), id="decoder-subclass"),
])
async def test_ci_gate_boundary_runtime_error_outside_unwrap_propagates(origin, failure):
    payload = failure if origin == "call" else _successful_execution_envelope(_watch_result_payload("passed"))
    if origin == "unwrap":
        payload["status"] = "failed"
    dispatch = _CIGateDispatch([payload], [])
    with patch(__name__ + "._CIGateDispatch", return_value=dispatch):
        if origin == "call":
            with pytest.raises(type(failure)) as caught:
                await _run_gate_fixture([])
        else:
            target = "_unwrap" if origin == "unwrap" else "CIWatchResult.model_validate"
            with patch("swe_af.app." + target, side_effect=failure), pytest.raises(type(failure)) as caught:
                await _run_gate_fixture([])
    assert caught.value is failure
    assert dispatch.targets == [COMMIT_A]
    assert dispatch.fixer_arguments == []


if __name__ == "__main__":
    unittest.main()

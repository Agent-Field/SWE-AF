"""Deterministic helpers for the post-PR CI gate.

The CI gate watches GitHub Actions checks on a PR after SWE-AF pushes its
work, using the ``gh`` CLI. Polling and log retrieval live here so they can
be unit-tested without a GitHub remote or an LLM in the loop.

PRs are opened ready for review (no draft phase). ``mark_pr_ready`` remains
in this module as a backwards-compatible no-op for callers that still
invoke it, but the build pipeline no longer calls it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from swe_af.execution.schemas import CIFailedCheck, CIWatchResult

# Normalized context buckets that mean "still running". Anything
# outside this set is conclusive (pass/fail/cancel/skip).
_PENDING_BUCKETS: frozenset[str] = frozenset({"pending", "queued"})
_FAILURE_BUCKETS: frozenset[str] = frozenset({"fail", "cancel"})

# Per-failure log tail size. Big enough to surface the actual error, small
# enough to keep the CI-fixer prompt under control across multi-failure runs.
_LOG_TAIL_CHARS: int = 3000

# Regex to pull the run id out of a check's details_url.
# Expected shape: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")


CommandRunner = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]


def _default_runner(
    cmd: Sequence[str], cwd: str, *, timeout: float | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(cmd),
        cwd=cwd or None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _parse_checks(payload: str) -> list[dict[str, Any]]:
    """Parse `gh pr checks --json` output into a list of dicts.

    Returns an empty list when no checks are configured for the PR yet.
    Raises ``ValueError`` if the payload is non-empty but not parseable JSON.
    """
    text = (payload or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array from gh pr checks, got {type(data).__name__}")
    return data


def _is_conclusive(checks: Sequence[dict[str, Any]]) -> bool:
    """All listed checks have settled (no pending/queued)."""
    return all(c.get("bucket", "") not in _PENDING_BUCKETS for c in checks)


def _classify(checks: Sequence[dict[str, Any]]) -> str:
    """Classify a conclusive set of checks.

    Returns ``"failed"`` if any required check failed/was cancelled,
    ``"passed"`` otherwise. ``"skip"`` and ``"pass"`` buckets count as passing.
    """
    for c in checks:
        if c.get("bucket", "") in _FAILURE_BUCKETS:
            return "failed"
    return "passed"


def _extract_run_id(details_url: str) -> str:
    if not details_url:
        return ""
    match = _RUN_ID_RE.search(details_url)
    return match.group(1) if match else ""


def _tail(text: str, max_chars: int = _LOG_TAIL_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return "…[truncated]…\n" + text[-max_chars:]


# Field set selected so we can both classify checks AND identify which
# commit they belong to. Query the exact Commit object and independently
# validate the current PR head on every page. Missing selected identity
# fields never fall back to PR-level filtering.
_CI_HEAD_QUERY = """query CIHeadRollup(
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


_COMPLETED_BUCKETS = {
    "SUCCESS": "pass", "SKIPPED": "skipping", "NEUTRAL": "skipping",
    "FAILURE": "fail", "TIMED_OUT": "fail", "ACTION_REQUIRED": "fail",
    "CANCELLED": "cancel", "STALE": "pending",
}
_STATUS_BUCKETS = {
    "SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail",
    "PENDING": "pending", "EXPECTED": "pending",
}
_RUNNING_STATES = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING"})


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def _text(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    if not allow_empty and not value.strip():
        raise ValueError("Expected a nonempty string")
    return value


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("Expected a full 40-character commit OID")
    if value == "0" * 40:
        raise ValueError("Zero commit OID")
    return value.lower()


def _positive_number(value: Any) -> None:
    if type(value) not in (int, float) or not 0 < value < float("inf"):
        raise ValueError("Wait and poll intervals must be positive finite numbers")


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative integer count")
    return value


def _repository_url(name: str, url: Any) -> str:
    parsed = urlsplit(_text(url))
    host = parsed.hostname
    if not host or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("Invalid repository host")
    if any(not label or label.startswith("-") or label.endswith("-") for label in host.split(".")):
        raise ValueError("Invalid repository host labels")
    if url != f"https://{host}/{name}":
        raise ValueError("Expected a canonical HTTPS repository URL")
    if os.getenv("GH_HOST", "").lower() not in ("", host):
        raise ValueError("GH_HOST disagrees with repository discovery")
    return host


def _repository_identity(payload: dict) -> tuple[str, str, str, str]:
    ident = _text(payload["id"])
    name = _text(payload["nameWithOwner"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository name")
    owner, repository = name.split("/")
    if owner in (".", "..") or repository in (".", ".."):
        raise ValueError("Invalid repository components")
    return _repository_url(name, payload["url"]), ident, owner, repository


def _query_command(identity: tuple, number: int, sha: str, after: str | None) -> list[str]:
    host, _, owner, name = identity
    command = [
        "gh", "api", "graphql", "--hostname", host,
        "--raw-field", "query=" + _CI_HEAD_QUERY,
        "--raw-field", "owner=" + owner, "--raw-field", "name=" + name,
        "--field", "number=" + str(number), "--raw-field", "oid=" + sha,
    ]
    if after is None:
        return command + ["--field", "after=null"]
    return command + ["--raw-field", "after=" + after]


def _provenance(payload: dict, identity: tuple, number: int, sha: str, allowed: set[str]) -> tuple:
    # When a head_sha is supplied, restrict conclusive-verdict logic to
    # data queried from that exact Commit. This prevents the stale-state
    # race where the previous HEAD's already-conclusive checks bypass
    # the new push's workflow registration. Every page must identify
    # the pinned repository, requested PR and exact Commit object.
    # A missing OID or a different observed PR head is an error;
    # no row may be treated as "unknown — could be ours".
    host, ident, owner, name = identity
    repository = _object(_object(payload["data"])["repository"])
    if (repository["id"], repository["nameWithOwner"]) != (ident, owner + "/" + name):
        raise ValueError("Repository identity changed")
    pr = _object(repository["pullRequest"])
    if type(pr["number"]) is not int or pr["number"] != number:
        raise ValueError("PR identity mismatch")
    head = _oid(pr["headRefOid"])
    if head not in allowed:
        raise ValueError("PR head moved away from the selected commit")
    commit = _object(repository["object"])
    if commit["__typename"] != "Commit" or commit["oid"] != sha:
        raise ValueError("Exact Commit identity mismatch")
    return head, commit["statusCheckRollup"]


def _timestamp(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    value = _text(value)
    pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)"
    if not re.fullmatch(pattern, value):
        raise ValueError("Expected an aware ISO timestamp")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("Timestamp has no timezone")
    return instant


def _workflow(row: dict) -> tuple[str, str]:
    suite = row["checkSuite"]
    if suite is None:
        return "", ""
    run = _object(suite)["workflowRun"]
    if run is None:
        return "", ""
    run = _object(run)
    event = _text(run["event"], allow_empty=True)
    workflow = run["workflow"]
    if workflow is None:
        return "", event
    return _text(_object(workflow)["name"], allow_empty=True), event


def _check_bucket(row: dict) -> tuple[str, str]:
    status, conclusion = row["status"], row["conclusion"]
    if status == "COMPLETED":
        return _COMPLETED_BUCKETS[_text(conclusion)], conclusion
    if status not in _RUNNING_STATES or conclusion is not None:
        raise ValueError("Unknown or inconsistent CheckRun state")
    return "pending", status


def _check_run(row: dict) -> dict:
    name = _text(row["name"])
    workflow, event = _workflow(row)
    bucket, state = _check_bucket(row)
    return {"name": name, "workflow": workflow, "bucket": bucket, "state": state,
            "identity": ("CheckRun", name, workflow, event),
            "timestamp": _timestamp(row["startedAt"], nullable=True), "link": row["detailsUrl"]}


def _status_context(row: dict) -> dict:
    name = _text(row["context"])
    state = _text(row["state"])
    return {"name": name, "workflow": "", "bucket": _STATUS_BUCKETS[state], "state": state,
            "identity": ("StatusContext", name), "timestamp": _timestamp(row["createdAt"]),
            "link": row["targetUrl"]}


def _normalize_row(value: Any) -> dict:
    # No per-row headSha is requested or inferred. Validate every raw
    # row before grouping reruns, including nullable workflow nesting.
    # Distinct workflow/event identities remain separate; an absent
    # selected field cannot silently supply a successful default.
    row = _object(value)
    ident = _text(row["id"])
    normalizer = {"CheckRun": _check_run, "StatusContext": _status_context}[row["__typename"]]
    normalized = normalizer(row)
    link = normalized["link"]
    normalized["link"] = "" if link is None else _text(link, allow_empty=True)
    normalized["id"] = ident
    return normalized


def _page_info(contexts: dict, rows: list, after: str | None) -> tuple[bool, str | None]:
    info = _object(contexts["pageInfo"])
    more, cursor = info["hasNextPage"], info["endCursor"]
    if type(more) is not bool:
        raise ValueError("Invalid hasNextPage")
    if rows:
        _text(cursor)
    elif after is not None or more or cursor is not None:
        raise ValueError("Empty or malformed pagination continuation")
    return more, cursor


def _row_counts(rows: list[dict]) -> tuple[int, int]:
    runs = sum(row["identity"][0] == "CheckRun" for row in rows)
    statuses = sum(row["identity"][0] == "StatusContext" for row in rows)
    return runs, statuses


def _reconcile_counts(rows: list, counts: tuple, *, complete: bool) -> None:
    observed = _row_counts(rows)
    if any(actual > total for actual, total in zip(observed, counts)):
        raise ValueError("Context counts exceeded")
    if complete and observed != counts:
        raise ValueError("Incomplete context pagination")


def _context_page(rollup: Any, after: str | None) -> dict | None:
    if rollup is None:
        return None
    contexts = _object(_object(rollup)["contexts"])
    counts = (_count(contexts["checkRunCount"]), _count(contexts["statusContextCount"]))
    nodes = contexts["nodes"]
    if not isinstance(nodes, list) or len(nodes) > 100:
        raise ValueError("Invalid context page size")
    rows = [_normalize_row(row) for row in nodes]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Repeated context ID")
    if not rows and any(counts):
        raise ValueError("Empty page advertises nonempty counts")
    more, cursor = _page_info(contexts, rows, after)
    _reconcile_counts(rows, counts, complete=False)
    return {"rows": rows, "counts": counts, "more": more, "cursor": cursor}


def _append_page(rows: list, seen: set, cursors: set, page: dict) -> None:
    for row in page["rows"]:
        if row["id"] in seen:
            raise ValueError("Repeated context ID or overlapping pages")
        seen.add(row["id"])
        rows.append(row)
    cursor = page["cursor"]
    if cursor is not None:
        if cursor in cursors:
            raise ValueError("Repeated pagination cursor")
        cursors.add(cursor)


class _CommitChecks:
    def __init__(self, repo_path: str, pr_number: int, deadline: float, runner=None, now=None):
        self.path = str(Path(repo_path).resolve())
        if not Path(self.path).is_dir() or os.getenv("GH_REPO"):
            raise ValueError("Invalid repository directory or GH_REPO override")
        if type(pr_number) is not int or pr_number <= 0:
            raise ValueError("Expected a positive integer PR number")
        self.number = pr_number
        self.deadline = deadline
        self.runner = runner
        self.clock = now or time.monotonic
        self.identity = None
        self.empty_only = True  # only validated empty history permits no_checks

    def command(self, command: list[str], *, deadline: float | None = None):
        limit = self.deadline if deadline is None else min(deadline, self.deadline)
        remaining = limit - self.clock()
        if remaining <= 0:
            raise ValueError("Acquisition deadline exhausted")
        if self.runner is None:
            proc = _default_runner(command, self.path, timeout=min(30, remaining))
        else:
            proc = self.runner(command, self.path)
        if self.clock() >= limit:
            raise ValueError("Deadline crossed during acquisition")
        # GraphQL commands require exit zero even with partial stdout.
        # A usable-looking JSON body cannot override command/auth errors.
        # Failed checks are represented by validated context conclusions,
        # while nonzero command exits always terminate acquisition.
        if proc.returncode != 0:
            raise ValueError((proc.stderr or "gh command failed").strip()[:300])
        return proc

    def json(self, command: list[str]) -> dict:
        payload = _object(json.loads(self.command(command).stdout))
        if "errors" in payload and payload["errors"] not in (None, []):
            raise ValueError("GraphQL errors: " + str(payload["errors"])[:300])
        return payload

    def discover(self) -> None:
        payload = self.json(["gh", "repo", "view", "--json", "id,nameWithOwner,url"])
        self.identity = _repository_identity(payload)

    def page(self, sha: str, after=None, *, allowed=None) -> tuple:
        payload = self.json(_query_command(self.identity, self.number, sha, after))
        head, rollup = _provenance(payload, self.identity, self.number, sha, {sha} if allowed is None else allowed)
        page = _context_page(rollup, after)
        if self.clock() >= self.deadline:
            raise ValueError("Deadline crossed while validating acquisition")
        if page is not None and (page["rows"] or any(page["counts"])):
            self.empty_only = False
        return head, page

    def collect(self, sha: str) -> list | None:
        rows, seen, cursors = [], set(), set()
        counts, after = None, None
        while True:
            _, page = self.page(sha, after)
            if page is None:
                return [] if after is None else None
            _append_page(rows, seen, cursors, page)
            if counts is None:
                counts = page["counts"]
            if counts != page["counts"]:
                self.empty_only = False
                return None
            _reconcile_counts(rows, counts, complete=not page["more"])
            if not page["more"]:
                return rows
            after = page["cursor"]


def _resolve_target(head_sha: str, reader: _CommitChecks) -> str:
    if head_sha != "":
        return _oid(head_sha)
    output = reader.command(["git", "rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    return _oid(output)


def _latest_group(rows: list[dict]) -> dict | None:
    if len(rows) == 1:
        return rows[0]
    if any(row["timestamp"] is None for row in rows):
        return None
    latest = max(row["timestamp"] for row in rows)
    winners = [row for row in rows if row["timestamp"] == latest]
    return winners[0] if len(winners) == 1 else None


def _select_latest(rows: list[dict]) -> list | None:
    groups = {}
    for row in rows:
        groups.setdefault(row["identity"], []).append(row)
    selected = []
    for group in groups.values():
        latest = _latest_group(group)
        if latest is None:
            return None
        selected.append(latest)
    return sorted(selected, key=lambda row: (row["identity"], row["id"]))


def _settled_checks(reader: _CommitChecks, sha: str) -> tuple[str, list]:
    rows = reader.collect(sha)
    if not rows:
        return "", []
    selected = _select_latest(rows)
    if selected is None or not _is_conclusive(selected):
        return "", []
    return _classify(selected), selected


def _actions_run_id(link: str, reader: _CommitChecks) -> str:
    host, _, owner, name = reader.identity
    try:
        parsed = urlsplit(link)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.netloc != host:
        return ""
    prefix = "/" + owner + "/" + name + "/actions/runs/"
    if not parsed.path.startswith(prefix):
        return ""
    match = re.fullmatch(r"(\d+)(?:/job/\d+)?/?", parsed.path[len(prefix):])
    return match.group(1) if match else ""


def _fetch_failed_logs(reader: _CommitChecks, run_id: str, deadline: float) -> str:
    """Fetch a bounded diagnostic tail without weakening a CI verdict."""
    host, _, owner, name = reader.identity
    command = ["gh", "run", "view", run_id, "--repo", host + "/" + owner + "/" + name, "--log-failed"]
    try:
        body = reader.command(command, deadline=deadline).stdout
        return _tail(body) if body else "Failed log unavailable: empty response"
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        return "Failed log unavailable: " + str(error)[:300]


def _failure_logs(check: dict, reader: _CommitChecks, cache: dict, deadline: float) -> str:
    run_id = _actions_run_id(check["link"], reader)
    if not run_id:
        return "Failed log unavailable: no matching Actions run URL"
    if run_id not in cache:
        cache[run_id] = _fetch_failed_logs(reader, run_id, deadline)
    return cache[run_id]


def _build_failed_checks(raw_checks: Sequence[dict[str, Any]], reader: _CommitChecks) -> list[CIFailedCheck]:
    failures, cache = [], {}
    remaining = reader.deadline - reader.clock()
    deadline = min(reader.clock() + 30, reader.deadline - min(30, remaining / 2))
    for check in raw_checks:
        if check["bucket"] not in _FAILURE_BUCKETS:
            continue
        failures.append(CIFailedCheck(
            name=check["name"], workflow=check["workflow"], conclusion=check["state"],
            details_url=check["link"], logs_excerpt=_failure_logs(check, reader, cache, deadline),
        ))
    return failures


def _watch_result(status: str, number: int, start: float, clock, **fields) -> CIWatchResult:
    return CIWatchResult(status=status, pr_number=number, elapsed_seconds=max(0, int(clock() - start)), **fields)


async def _watch_loop(reader: _CommitChecks, sha: str, poll: float, sleeper, start: float) -> CIWatchResult:
    # When anchored to a SHA, require validated exact-Commit observations
    # before declaring a verdict. Every complete raw page is validated,
    # then reruns are grouped by typed workflow/event identity and time.
    # Unresolved recency and well-formed pagination movement continue
    # within the original deadline; neither can become no_checks.
    # Observed nonempty rows or positive counts permanently rule out an
    # empty-only outcome for this watcher target.
    while reader.clock() < reader.deadline:
        status, checks = _settled_checks(reader, sha)
        if status == "passed":
            return _watch_result(status, reader.number, start, reader.clock, summary=f"All {len(checks)} selected checks passed")
        if status == "failed":
            failures = _build_failed_checks(checks, reader)
            reader.page(sha)
            return _watch_result(status, reader.number, start, reader.clock, failed_checks=failures, summary=f"{len(failures)} checks failing")
        remaining = reader.deadline - reader.clock()
        if remaining > 0:
            await sleeper(min(poll, remaining))
    status = "no_checks" if reader.empty_only else "timed_out"
    return _watch_result(status, reader.number, start, reader.clock, summary=f"CI observation window exhausted for {sha}")


async def watch_pr_checks(
    *,
    repo_path: str,
    pr_number: int,
    wait_seconds: int = 1500,
    poll_seconds: int = 30,
    head_sha: str = "",
    runner: CommandRunner | None = None,
    sleep: Callable[[float], Any] | None = None,
    now: Callable[[], float] | None = None,
) -> CIWatchResult:
    """Watch complete exact-Commit rollups within one monotonic deadline.

    The current PR head and immutable Commit OID are validated on every page.
    An omitted target selects local committed HEAD once. Nullable rollups
    count as empty observations; no_checks remains distinct from passed.
    Runner, clock and sleep seams permit deterministic offline tests.
    """
    clock = now or time.monotonic
    start = clock()
    try:
        _positive_number(wait_seconds)
        _positive_number(poll_seconds)
        reader = _CommitChecks(repo_path, pr_number, start + wait_seconds, runner, clock)
        sha = _resolve_target(head_sha, reader)
        reader.discover()
        return await _watch_loop(reader, sha, poll_seconds, sleep or asyncio.sleep, start)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        number = pr_number if type(pr_number) is int else 0
        return _watch_result("error", number, start, clock, summary=str(error)[:500])


def _resolve_ci_target(repo_path: str, number: int, head_sha: str, wait: float) -> str:
    _positive_number(wait)
    reader = _CommitChecks(repo_path, number, time.monotonic() + wait)
    return _resolve_target(head_sha, reader)


def _guard_ci_head(repo_path: str, number: int, sha: str, wait: float) -> None:
    reader = _CommitChecks(repo_path, number, time.monotonic() + min(30, wait))
    reader.discover()
    reader.page(sha)


async def _wait_for_fixer_head(repo_path: str, number: int, previous: str, target: str, wait: float, poll: float) -> int:
    _positive_number(wait)
    _positive_number(poll)
    reader = _CommitChecks(repo_path, number, time.monotonic() + wait)
    reader.discover()
    while reader.clock() < reader.deadline:
        head, _ = reader.page(target, allowed={previous, target})
        remaining = reader.deadline - reader.clock()
        if head == target:
            if int(remaining) <= 0:
                raise ValueError("No watcher budget remains after convergence")
            return int(remaining)
        if remaining > 0:
            await asyncio.sleep(min(poll, remaining))
    raise ValueError("Fixer commit did not become the PR head within the next watch budget")


def mark_pr_ready(
    *,
    repo_path: str,
    pr_number: int,
    runner: CommandRunner | None = None,
) -> tuple[bool, str]:
    """Promote a draft PR to ready-for-review via `gh pr ready <num>`.

    Kept for backwards compatibility and as a manually-callable utility.
    The build pipeline no longer invokes this — PRs are now opened ready
    for review from the start (no draft phase) so promotion is unnecessary.

    Returns ``(success, message)``. ``message`` carries the gh stderr on
    failure (truncated), or a short confirmation on success.
    """
    cmd_runner: CommandRunner = runner or _default_runner
    proc = cmd_runner(["gh", "pr", "ready", str(pr_number)], repo_path)
    if proc.returncode == 0:
        return True, f"PR #{pr_number} marked ready for review"
    return False, (proc.stderr or "").strip()[:300] or "gh pr ready failed"

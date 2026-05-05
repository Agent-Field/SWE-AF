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
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from swe_af.execution.schemas import CIFailedCheck, CIWatchResult

# Buckets (`gh pr checks --json bucket`) that mean "still running". Anything
# outside this set is conclusive (pass/fail/cancel/skip).
_PENDING_BUCKETS: frozenset[str] = frozenset({"pending", "queued"})
_FAILURE_BUCKETS: frozenset[str] = frozenset({"fail", "cancel"})

# Per-failure log tail size. Big enough to surface the actual error, small
# enough to keep the CI-fixer prompt under control across multi-failure runs.
_LOG_TAIL_CHARS: int = 3000

# Regex to pull the run id out of the details_url returned by `gh pr checks`.
# Expected shape: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")


CommandRunner = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]


def _default_runner(
    cmd: Sequence[str], cwd: str
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(cmd),
        cwd=cwd or None,
        capture_output=True,
        text=True,
        check=False,
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


def _fetch_failed_logs(
    repo_path: str,
    run_id: str,
    runner: CommandRunner,
) -> str:
    """Run `gh run view <id> --log-failed` and return a tail of the output."""
    if not run_id:
        return ""
    proc = runner(["gh", "run", "view", run_id, "--log-failed"], repo_path)
    body = proc.stdout or ""
    if proc.returncode != 0 and not body:
        body = proc.stderr or ""
    return _tail(body)


def _build_failed_checks(
    raw_checks: Sequence[dict[str, Any]],
    repo_path: str,
    runner: CommandRunner,
) -> list[CIFailedCheck]:
    failures: list[CIFailedCheck] = []
    for c in raw_checks:
        if c.get("bucket", "") not in _FAILURE_BUCKETS:
            continue
        details_url = c.get("link", "") or c.get("detailsUrl", "")
        run_id = _extract_run_id(details_url)
        logs_excerpt = _fetch_failed_logs(repo_path, run_id, runner)
        failures.append(
            CIFailedCheck(
                name=c.get("name", "?"),
                workflow=c.get("workflow", ""),
                conclusion=c.get("state", c.get("bucket", "")),
                details_url=details_url,
                logs_excerpt=logs_excerpt,
            )
        )
    return failures


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
    """Poll `gh pr checks <pr>` until conclusive, the wait cap is hit, or no checks exist.

    Returns a ``CIWatchResult`` describing the outcome. Failed checks include a
    truncated log tail fetched via ``gh run view --log-failed`` so downstream
    callers (the CI fixer) get actionable context without re-querying.

    When ``head_sha`` is provided, the watcher refuses to declare a verdict
    until ``gh pr checks`` reports a HEAD SHA matching ``head_sha`` (it
    requeries with ``--json bucket,state,name,workflow,link,headSha`` and
    filters). Without that anchor, immediately after a push the watcher
    can briefly see the PREVIOUS HEAD's still-cached check states, hit
    ``_is_conclusive``, and return ``passed`` / ``failed`` verdicts that
    don't actually reflect the new commit. With it, mismatched checks are
    treated as "not yet seen for this commit" and polling continues.

    Parameters are dependency-injected so the polling loop is unit-testable
    without invoking ``gh`` or sleeping in real wall-clock time.
    """
    cmd_runner: CommandRunner = runner or _default_runner
    sleeper = sleep or asyncio.sleep
    clock = now or time.monotonic

    start = clock()
    expected_sha = (head_sha or "").strip().lower()

    def elapsed() -> int:
        return int(clock() - start)

    last_checks: list[dict[str, Any]] = []
    saw_any_check = False
    saw_any_for_sha = False  # only meaningful when expected_sha != ""

    # Field set selected so we can both classify checks AND identify which
    # commit they belong to. Older `gh` versions don't expose `headSha` for
    # PR-level checks; the loop falls back to PR-level filtering when the
    # field is missing on every row.
    fields = "bucket,state,name,workflow,link,headSha"

    while True:
        proc = cmd_runner(
            [
                "gh", "pr", "checks", str(pr_number),
                "--json", fields,
            ],
            repo_path,
        )

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            # `gh pr checks` exits non-zero when there ARE failed checks but
            # also when the call itself errors out. Distinguish via stdout
            # presence: a parseable JSON body means we got real check data
            # alongside the non-zero exit, so keep going.
            try:
                last_checks = _parse_checks(proc.stdout)
            except ValueError:
                last_checks = []
            if not last_checks:
                return CIWatchResult(
                    status="error",
                    pr_number=pr_number,
                    elapsed_seconds=elapsed(),
                    summary=f"`gh pr checks` failed: {stderr[:300]}",
                )
        else:
            try:
                last_checks = _parse_checks(proc.stdout)
            except ValueError as e:
                return CIWatchResult(
                    status="error",
                    pr_number=pr_number,
                    elapsed_seconds=elapsed(),
                    summary=f"Could not parse gh pr checks output: {e}",
                )

        # When a head_sha is supplied, restrict the conclusive-verdict logic
        # to checks that actually belong to that commit. This prevents the
        # stale-state race where the previous HEAD's already-conclusive
        # checks make `_is_conclusive` true and short-circuit the verdict
        # before the new push's workflow run has registered. Checks that
        # report a non-empty `headSha` are filtered; checks that report no
        # headSha at all are kept (treated as "unknown — could be ours").
        sha_unsupported = False
        if expected_sha:
            sha_matched = [
                c for c in last_checks
                if not str(c.get("headSha", "") or "").strip()
                or str(c.get("headSha", "") or "").strip().lower() == expected_sha
            ]
            checks_for_verdict = sha_matched
            if any(
                str(c.get("headSha", "") or "").strip().lower() == expected_sha
                for c in last_checks
            ):
                saw_any_for_sha = True
            # Older `gh` versions don't populate `headSha` at all. If we
            # have checks but none of them carry a headSha, we can't anchor
            # — fall back to the old PR-level behavior so we don't hang
            # waiting for a field that will never appear.
            if last_checks and not any(
                str(c.get("headSha", "") or "").strip()
                for c in last_checks
            ):
                sha_unsupported = True
        else:
            checks_for_verdict = last_checks

        if last_checks:
            saw_any_check = True

        # When anchored to a SHA, require that we've actually seen at least
        # one check for that SHA before declaring a verdict. Exception:
        # when the `gh` CLI clearly doesn't expose `headSha` (no field on
        # any returned check), degrade to the old behavior — every check
        # is verdict-eligible. Otherwise the first poll's lingering
        # previous-HEAD checks could short-circuit `_is_conclusive` and
        # lock in a stale "passed"/"failed".
        if expected_sha and sha_unsupported:
            verdict_eligible = bool(checks_for_verdict)
        else:
            verdict_eligible = checks_for_verdict and (
                saw_any_for_sha if expected_sha else True
            )

        if verdict_eligible and _is_conclusive(checks_for_verdict):
            verdict = _classify(checks_for_verdict)
            if verdict == "passed":
                return CIWatchResult(
                    status="passed",
                    pr_number=pr_number,
                    elapsed_seconds=elapsed(),
                    summary=f"All {len(checks_for_verdict)} check(s) passed",
                )
            failures = _build_failed_checks(checks_for_verdict, repo_path, cmd_runner)
            return CIWatchResult(
                status="failed",
                pr_number=pr_number,
                elapsed_seconds=elapsed(),
                failed_checks=failures,
                summary=f"{len(failures)} of {len(checks_for_verdict)} check(s) failing",
            )

        if elapsed() >= wait_seconds:
            if not saw_any_check or (expected_sha and not saw_any_for_sha):
                return CIWatchResult(
                    status="no_checks",
                    pr_number=pr_number,
                    elapsed_seconds=elapsed(),
                    summary=(
                        f"No checks reported in {wait_seconds}s — "
                        "PR has no CI configured or checks not yet started"
                        + (
                            f" for {expected_sha[:10]}"
                            if expected_sha and not saw_any_for_sha
                            else ""
                        )
                    ),
                )
            return CIWatchResult(
                status="timed_out",
                pr_number=pr_number,
                elapsed_seconds=elapsed(),
                summary=(
                    f"Checks still pending after {wait_seconds}s "
                    f"({len(checks_for_verdict)} reporting)"
                ),
            )

        await sleeper(poll_seconds)


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

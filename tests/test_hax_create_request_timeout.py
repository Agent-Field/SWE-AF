"""Pin: ``_create_hax_request_with_timeout`` bounds a hung hax-sdk call.

Reproduces and pins the fix for production run ``run_1778512783034_f4985c96``:
github-buddy.implement_from_issue tripped its 7200s active-time watchdog
because swe-planner.build's revision-iteration ``hax_client.create_request``
hung for 76 minutes (16:29:45 → 17:45:36). The hax-sdk Python client is
synchronous and has no client-side timeout; without an explicit wrap, a
wedged hax-sdk surfaces as a multi-hour silent stall up the call chain.

Each test runs 10–20 iterations to catch scheduler/timing flakes. The
parametrize ID is the iteration number so failures point at the specific
attempt that broke.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from swe_af.app import _create_hax_request_with_timeout


# ---------------------------------------------------------------------------
# Common test plumbing
# ---------------------------------------------------------------------------


def _make_hung_hax_client(*, hang_seconds: float = 1.5):
    """A hax client whose ``create_request`` blocks the calling thread.

    ``hang_seconds`` defaults to 1.5s — long enough to outlast the test's
    0.3s timeout with a clean margin, short enough that orphaned threads
    don't pile up in the default executor and starve later iterations.
    ``asyncio.to_thread`` doesn't cancel the underlying thread when the
    awaiting coroutine is cancelled by ``wait_for``, so the thread keeps
    sleeping until ``hang_seconds`` elapses regardless of test outcome.
    """

    client = MagicMock()
    client.create_request = MagicMock(
        side_effect=lambda **_kwargs: (time.sleep(hang_seconds), MagicMock())[1]
    )
    return client


def _make_responsive_hax_client(*, request_id: str = "req-test", url: str = "https://hax/r/test"):
    """A hax client that returns immediately with the given ids."""

    client = MagicMock()
    response = MagicMock(id=request_id, url=url)
    client.create_request = MagicMock(return_value=response)
    return client


@pytest.fixture(autouse=True)
def _silence_app_note():
    """Patch ``app.note`` so tests don't make real HTTP calls.

    The helper writes timeline notes at entry/success/error. In tests we
    only care about the timeout semantics, not the notes themselves.
    """

    with patch("swe_af.app.app.note") as note_mock:
        yield note_mock


# ---------------------------------------------------------------------------
# 1. Hang case — the actual production bug we're fixing.
#
# A synchronously-hung ``create_request`` must be cancelled by
# ``asyncio.wait_for`` within a small margin of the configured timeout,
# and the helper must surface a clear ``RuntimeError`` (not silently
# return None or hang the event loop).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", list(range(20)))
async def test_hung_create_request_raises_runtime_error_within_timeout_bound(iteration):
    """20× repeat: under hang, the helper raises RuntimeError within
    [timeout, timeout * 4] seconds. The lower bound rules out spurious
    early failures; the upper bound rules out the bug (unbounded wait).
    """

    hax_client = _make_hung_hax_client(hang_seconds=1.5)

    timeout_seconds = 0.3
    start = time.monotonic()
    with pytest.raises(RuntimeError, match=r"hax-sdk create_request timed out"):
        await _create_hax_request_with_timeout(
            hax_client=hax_client,
            hax_create_kwargs={"type": "plan-review-v2"},
            revision_iter=1,
            timeout_seconds=timeout_seconds,
        )
    elapsed = time.monotonic() - start

    # Must not fire BEFORE the configured timeout — that would mean the
    # helper is firing for the wrong reason.
    assert elapsed >= timeout_seconds * 0.9, (
        f"iteration {iteration}: timeout fired at {elapsed:.3f}s, "
        f"before configured {timeout_seconds}s"
    )
    # Must not take much LONGER than the timeout — the bug we're pinning is
    # exactly "wait longer than configured." A 4× margin allows for thread
    # cleanup + asyncio scheduling jitter; the real production failure was
    # 6000× over budget.
    assert elapsed <= timeout_seconds * 4, (
        f"iteration {iteration}: timeout did NOT fire within "
        f"4*{timeout_seconds}s; elapsed={elapsed:.3f}s. This means a hung "
        f"hax-sdk could still chew through the parent's active-time budget."
    )


# ---------------------------------------------------------------------------
# 2. Success case — happy path must not be broken by the wrap.
#
# A fast ``create_request`` (the normal production case) must return its
# response, complete well under the configured timeout, and call
# ``app.note(... 'submitted' ...)`` so the run timeline reflects success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", list(range(20)))
async def test_responsive_create_request_returns_response(iteration, _silence_app_note):
    """20× repeat: under no-hang, the helper returns the hax response
    and the timeout doesn't fire."""

    hax_client = _make_responsive_hax_client(
        request_id=f"req-{iteration:02d}",
        url=f"https://hax/r/{iteration:02d}",
    )

    start = time.monotonic()
    result = await _create_hax_request_with_timeout(
        hax_client=hax_client,
        hax_create_kwargs={"type": "plan-review-v2", "title": "test"},
        revision_iter=iteration,
        timeout_seconds=0.5,
    )
    elapsed = time.monotonic() - start

    assert result.id == f"req-{iteration:02d}", f"iteration {iteration}: wrong response id"
    assert elapsed < 0.4, (
        f"iteration {iteration}: responsive call took {elapsed:.3f}s "
        f"(unexpected slowness)"
    )
    # The submitted-note must fire so the run timeline shows success.
    submitted_calls = [
        call for call in _silence_app_note.call_args_list
        if "submitted" in str(call)
    ]
    assert len(submitted_calls) >= 1, (
        f"iteration {iteration}: 'submitted' app.note never fired"
    )


# ---------------------------------------------------------------------------
# 3. Non-timeout exception — must surface unchanged.
#
# A ConnectionError from hax-sdk (network-level failure) must propagate
# verbatim. We do NOT want to swallow real errors or convert them into
# our timeout RuntimeError — those are different failure modes and
# operators need to tell them apart.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", list(range(10)))
async def test_non_timeout_exception_propagates(iteration, _silence_app_note):
    """10× repeat: a ConnectionError (or any non-TimeoutError) must surface
    unchanged so operators can distinguish network failure from hang."""

    hax_client = MagicMock()
    hax_client.create_request = MagicMock(
        side_effect=ConnectionError(f"hax-sdk unreachable (iter {iteration})")
    )

    with pytest.raises(ConnectionError, match=rf"hax-sdk unreachable \(iter {iteration}\)"):
        await _create_hax_request_with_timeout(
            hax_client=hax_client,
            hax_create_kwargs={"type": "plan-review-v2"},
            revision_iter=iteration,
            timeout_seconds=0.5,
        )

    # The error-note must fire so the run timeline shows the failure mode.
    error_calls = [
        call for call in _silence_app_note.call_args_list
        if "raised" in str(call) and "ConnectionError" in str(call)
    ]
    assert len(error_calls) >= 1, (
        f"iteration {iteration}: 'raised ConnectionError' app.note never fired"
    )


# ---------------------------------------------------------------------------
# 4. Regression pin: the helper must use the configured timeout, not the
# module default. This is a property of the wrapper that we want to keep
# stable as the helper evolves.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configurable_timeout_is_honored():
    """The ``timeout_seconds`` kwarg must shorten the wait correctly.

    Without this, callers that want to tighten the budget (e.g. tests, or
    a future caller that knows hax-sdk should never take > 30s) couldn't.
    """

    hax_client = _make_hung_hax_client(hang_seconds=1.5)

    # 0.2s timeout → must fire in ~0.2s, NOT the module default of 120s.
    start = time.monotonic()
    with pytest.raises(RuntimeError):
        await _create_hax_request_with_timeout(
            hax_client=hax_client,
            hax_create_kwargs={"type": "x"},
            revision_iter=0,
            timeout_seconds=0.2,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, (
        f"timeout_seconds=0.2 took {elapsed:.3f}s — module default is leaking through"
    )


# ---------------------------------------------------------------------------
# 5. Module default sanity. Pin so the production timeout doesn't drift
# accidentally (e.g. someone bumps it to 1s thinking it's a test value).
# ---------------------------------------------------------------------------


def test_module_default_timeout_is_120_seconds():
    """120s is the production budget. Drift would either hide hangs
    (too high) or false-positive on legitimate cold-starts (too low)."""

    from swe_af.app import HAX_CREATE_REQUEST_TIMEOUT_SECONDS

    assert HAX_CREATE_REQUEST_TIMEOUT_SECONDS == 120.0, (
        f"Unexpected module default {HAX_CREATE_REQUEST_TIMEOUT_SECONDS}. "
        f"If you intentionally changed it, update this assertion + add a "
        f"comment in the helper explaining the new value."
    )

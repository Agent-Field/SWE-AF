"""Keep execution activity current while a harness child process is running.

The AgentField SDK already exposes ``Agent.note`` as the node's fire-and-forget
execution activity channel.  This module only decides *when* that existing
channel should be used; it does not create a second status-delivery path.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

# A ninety-second interval stays comfortably below the control-plane inactivity
# fuse and its sweep interval without adding noticeable note traffic.
ACTIVITY_HEARTBEAT_INTERVAL_SECONDS = 90.0
ACTIVITY_NOTE_TIMEOUT_SECONDS = 5.0
ACTIVITY_HEARTBEAT_MESSAGE = "Harness child tool is still running"
ACTIVITY_HEARTBEAT_TAGS = ["harness", "heartbeat"]


NoteFn = Callable[..., Any]


@dataclass
class ChildToolActivity:
    """Liveness signals collected for children launched by one harness call.

    A pending harness coroutine is not enough to justify a heartbeat: the
    subprocess may already have died while its parent is unwinding.  The
    subprocess hooks below attach the actual child process, and the heartbeat
    checks its return code before every note.  ``last_output_at`` is retained as
    an additional observable signal for diagnostics and tests; output is not
    required for every interval because coding tools can legitimately spend a
    long interval in a silent tool call while their process remains alive.
    """

    _processes: list[Any] = field(default_factory=list)
    last_output_at: float | None = None

    def attach_process(self, process: Any) -> None:
        """Record a subprocess created by the active harness call."""
        self._processes.append(process)

    def record_output(self) -> None:
        """Record fresh output from the active harness child."""
        self.last_output_at = time.monotonic()

    def is_alive(self) -> bool:
        """Return whether at least one observed child still has no exit code."""
        for process in self._processes:
            try:
                if process.returncode is None:
                    return True
            except (AttributeError, RuntimeError):
                continue
        return False


_current_child_activity: contextvars.ContextVar[ChildToolActivity | None] = (
    contextvars.ContextVar("swe_af_current_child_activity", default=None)
)

_subprocess_hooks_installed = False


def current_child_activity() -> ChildToolActivity | None:
    """Return the activity monitor for the current harness task, if any."""
    return _current_child_activity.get()


def install_subprocess_activity_hooks() -> None:
    """Observe AgentField harness subprocesses without changing their delivery.

    The harness SDK owns process creation and stream draining.  Its public
    result API intentionally does not expose a child handle, so the SWE-AF
    runtime adds a context-local observer at the two existing SDK seams.  The
    wrappers return the SDK's original process/results unchanged; they only
    record process handles and output timestamps for the heartbeat gate.

    This is deliberately best-effort.  If a future SDK removes either private
    seam, the harness still works and simply emits no heartbeat for that path.
    """
    global _subprocess_hooks_installed
    if _subprocess_hooks_installed:
        return

    try:
        from agentfield.harness import _cli as sdk_cli
    except ImportError:
        return

    original_create_subprocess_exec = asyncio.create_subprocess_exec

    @functools.wraps(original_create_subprocess_exec)
    async def create_subprocess_exec_with_activity(*args: Any, **kwargs: Any) -> Any:
        process = await original_create_subprocess_exec(*args, **kwargs)
        activity = current_child_activity()
        if activity is not None:
            activity.attach_process(process)
        return process

    # AgentField's run_cli resolves create_subprocess_exec through asyncio at
    # call time.  Keep the observer context-local so unrelated subprocesses in
    # the node are not treated as harness activity.
    asyncio.create_subprocess_exec = create_subprocess_exec_with_activity  # type: ignore[assignment]

    original_drain = getattr(sdk_cli, "_drain", None)
    if original_drain is not None:

        @functools.wraps(original_drain)
        async def drain_with_activity(
            stream: Any, chunks: Any, last_activity: list[float]
        ) -> None:
            # Keep the SDK's bounded chunking and idle-clock updates identical,
            # adding only a context-local output timestamp per received chunk.
            if stream is None:
                return
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                last_activity[0] = asyncio.get_event_loop().time()
                activity = current_child_activity()
                if activity is not None:
                    activity.record_output()

        sdk_cli._drain = drain_with_activity

    _subprocess_hooks_installed = True


async def _send_note(note_fn: NoteFn) -> None:
    """Use the existing note channel without letting delivery stall the child."""
    try:
        result = note_fn(
            ACTIVITY_HEARTBEAT_MESSAGE,
            tags=list(ACTIVITY_HEARTBEAT_TAGS),
        )
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=ACTIVITY_NOTE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        # Activity is advisory.  A control-plane/network failure must not alter
        # the harness result or turn a successful coding step into a failure.
        return


async def _heartbeat_loop(
    child_task: asyncio.Task[Any],
    activity: ChildToolActivity,
    note_fn: NoteFn,
    interval_seconds: float,
) -> None:
    """Send notes only while the awaited child is live."""
    while not child_task.done():
        await asyncio.sleep(interval_seconds)
        if child_task.done():
            return
        if activity.is_alive():
            await _send_note(note_fn)


async def run_with_activity_heartbeat(
    awaitable: Awaitable[Any],
    *,
    note_fn: NoteFn,
    activity: ChildToolActivity,
    interval_seconds: float = ACTIVITY_HEARTBEAT_INTERVAL_SECONDS,
) -> Any:
    """Await a harness result while refreshing activity for a live child.

    The heartbeat task is owned by this wait and is cancelled in ``finally``
    before the function returns or raises.  Thus a successful, failed, or
    cancelled/terminal harness call cannot leave a periodic task behind.
    """
    interval = float(interval_seconds)
    if interval <= 0:
        raise ValueError("interval_seconds must be positive")

    token = _current_child_activity.set(activity)
    try:
        child_task = asyncio.ensure_future(awaitable)
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(child_task, activity, note_fn, interval)
        )
    finally:
        _current_child_activity.reset(token)

    try:
        return await child_task
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

        # If the caller cancels this wrapper, do not leave the harness process
        # running detached from the reasoner that owns it.
        if not child_task.done():
            child_task.cancel()
            with suppress(asyncio.CancelledError):
                await child_task

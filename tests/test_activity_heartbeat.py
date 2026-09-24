from __future__ import annotations

import asyncio
import subprocess
import sys

import anyio
import pytest

from swe_af.runtime.activity_heartbeat import (
    ChildToolActivity,
    install_subprocess_activity_hooks,
    run_with_activity_heartbeat,
)


class _FakeChild:
    def __init__(self) -> None:
        self.returncode: int | None = None


async def _leave_stale_cancellation() -> None:
    """Precondition helper: receive a cancellation, catch it, and keep going.

    ``Task.cancelling()`` stays nonzero after the matching CancelledError has
    been caught and handled; ``uncancel()`` is deliberately not called so the
    rest of the call runs with a stale cancellation count.
    """
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass
    assert task.cancelling() == 1


@pytest.mark.asyncio
async def test_activity_advances_during_a_live_long_tool_wait() -> None:
    child = _FakeChild()
    activity = ChildToolActivity()
    activity.attach_process(child)
    notes: list[tuple[str, list[str]]] = []

    async def tool_wait() -> str:
        await asyncio.sleep(0.7)
        await asyncio.sleep(0.7)
        return "done"

    result = await run_with_activity_heartbeat(
        tool_wait(),
        note_fn=lambda message, *, tags: notes.append((message, tags)),
        activity=activity,
        interval_seconds=0.2,
    )

    assert result == "done"
    assert len(notes) >= 3
    assert all(tags == ["harness", "heartbeat"] for _, tags in notes)


@pytest.mark.asyncio
async def test_anyio_spawned_child_drives_activity_heartbeat() -> None:
    """The default claude_code SDK path uses anyio.open_process."""
    install_subprocess_activity_hooks()
    activity = ChildToolActivity()
    notes: list[str] = []

    async def run_short_lived_child() -> int:
        process = await anyio.open_process(
            [sys.executable, "-c", "import time; time.sleep(0.7)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    result = await run_with_activity_heartbeat(
        run_short_lived_child(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.1,
    )

    assert result == 0
    assert notes


@pytest.mark.asyncio
async def test_asyncio_spawned_child_drives_activity_heartbeat() -> None:
    """The CLI providers (codex/open_code/gemini/aforge) go through
    agentfield.harness._cli.run_cli, which resolves
    asyncio.create_subprocess_exec at call time."""
    install_subprocess_activity_hooks()
    activity = ChildToolActivity()
    notes: list[str] = []

    async def run_short_lived_child() -> int:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(0.7)",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    result = await run_with_activity_heartbeat(
        run_short_lived_child(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.1,
    )

    assert result == 0
    assert notes


@pytest.mark.asyncio
async def test_heartbeat_task_is_cancelled_on_terminal_resolution(monkeypatch) -> None:
    created_tasks = []
    real_create_task = asyncio.create_task

    class _TrackedTask:
        def __init__(self, task: asyncio.Task[object]) -> None:
            self.task = task
            self.cancel_calls = 0

        def cancel(self, *args: object, **kwargs: object) -> bool:
            self.cancel_calls += 1
            return self.task.cancel(*args, **kwargs)

        def cancelled(self) -> bool:
            return self.task.cancelled()

        def __await__(self):
            return self.task.__await__()

    def create_tracked_task(coro, *args, **kwargs):
        tracked = _TrackedTask(real_create_task(coro, *args, **kwargs))
        created_tasks.append(tracked)
        return tracked

    monkeypatch.setattr(asyncio, "create_task", create_tracked_task)

    async def short_wait() -> str:
        await asyncio.sleep(0.1)
        return "done"

    await run_with_activity_heartbeat(
        short_wait(),
        note_fn=lambda *_args, **_kwargs: None,
        activity=ChildToolActivity(),
        interval_seconds=0.2,
    )

    assert len(created_tasks) == 1
    assert created_tasks[0].cancel_calls == 1
    assert created_tasks[0].cancelled()


@pytest.mark.asyncio
async def test_cancellation_during_heartbeat_teardown_is_not_swallowed(
    monkeypatch,
) -> None:
    """A cancel that lands while the heartbeat task is torn down must still
    cancel the wrapper, even though the child wait already resolved."""
    activity = ChildToolActivity()
    wrapper_task: asyncio.Task[str] | None = None
    real_create_task = asyncio.create_task

    def create_task_with_teardown_cancel(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)

        def _cancel_wrapper_when_heartbeat_finishes(_done: asyncio.Task) -> None:
            # The heartbeat only finishes because the wrapper cancels it in
            # the finally block, i.e. after the child already returned.
            if wrapper_task is not None:
                wrapper_task.cancel()

        task.add_done_callback(_cancel_wrapper_when_heartbeat_finishes)
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task_with_teardown_cancel)

    async def child_wait() -> str:
        await asyncio.sleep(0.02)
        return "child-result"

    async def run_wrapper() -> str:
        return await run_with_activity_heartbeat(
            child_wait(),
            note_fn=lambda *_args, **_kwargs: None,
            activity=activity,
            interval_seconds=30.0,
        )

    wrapper_task = real_create_task(run_wrapper())
    with pytest.raises(asyncio.CancelledError):
        await wrapper_task

    assert wrapper_task.cancelled()


@pytest.mark.asyncio
async def test_prior_caught_cancellation_returns_child_result() -> None:
    """A cancellation caught before the wait must not turn a completed child
    result into a cancellation during heartbeat teardown.

    Cancelling the heartbeat task makes its await raise CancelledError even
    though *this* task was never cancelled.  A stale ``cancelling()`` count
    from an earlier, already-handled cancellation must not be mistaken for a
    fresh cancellation and abort the completed wait.
    """
    activity = ChildToolActivity()
    notes: list[str] = []

    async def child_wait() -> str:
        await asyncio.sleep(0.02)
        return "child-result"

    async def run_wrapper() -> str:
        await _leave_stale_cancellation()
        return await run_with_activity_heartbeat(
            child_wait(),
            note_fn=lambda message, **_: notes.append(message),
            activity=activity,
            interval_seconds=30.0,
        )

    wrapper_task = asyncio.ensure_future(run_wrapper())
    result = await wrapper_task

    assert result == "child-result"
    assert not wrapper_task.cancelled()


@pytest.mark.asyncio
async def test_prior_caught_cancellation_still_propagates_new_teardown_cancel(
    monkeypatch,
) -> None:
    """Even with a stale cancellation count, a *new* cancel that lands while
    the heartbeat is torn down must still cancel the wrapper."""
    activity = ChildToolActivity()
    wrapper_task: asyncio.Task[str] | None = None
    real_create_task = asyncio.create_task

    def create_task_with_teardown_cancel(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)

        def _cancel_wrapper_when_heartbeat_finishes(_done: asyncio.Task) -> None:
            # The heartbeat only finishes because the wrapper cancels it in
            # the finally block, i.e. after the child already returned.
            if wrapper_task is not None:
                wrapper_task.cancel()

        task.add_done_callback(_cancel_wrapper_when_heartbeat_finishes)
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task_with_teardown_cancel)

    async def child_wait() -> str:
        await asyncio.sleep(0.02)
        return "child-result"

    async def run_wrapper() -> str:
        await _leave_stale_cancellation()
        return await run_with_activity_heartbeat(
            child_wait(),
            note_fn=lambda *_args, **_kwargs: None,
            activity=activity,
            interval_seconds=30.0,
        )

    wrapper_task = real_create_task(run_wrapper())
    with pytest.raises(asyncio.CancelledError):
        await wrapper_task

    assert wrapper_task.cancelled()


@pytest.mark.asyncio
async def test_outer_cancellation_while_child_running_propagates() -> None:
    """A cancel landing while the child is still running must cancel the wait
    and the child task; neither may be swallowed by heartbeat teardown."""
    activity = ChildToolActivity()
    child_started = asyncio.Event()

    async def long_child() -> str:
        child_started.set()
        await asyncio.sleep(30)
        return "unreachable"

    child_task = asyncio.ensure_future(long_child())
    await child_started.wait()

    async def run_wrapper() -> str:
        return await run_with_activity_heartbeat(
            child_task,
            note_fn=lambda *_args, **_kwargs: None,
            activity=activity,
            interval_seconds=30.0,
        )

    wrapper_task = asyncio.ensure_future(run_wrapper())
    await asyncio.sleep(0)
    wrapper_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await wrapper_task

    assert wrapper_task.cancelled()
    assert child_task.cancelled()


@pytest.mark.asyncio
async def test_prior_caught_cancellation_failing_child_still_raises_child_error() -> (
    None
):
    """With a stale cancellation count, a child that *fails* must still raise
    its own error; heartbeat teardown must not replace it with a
    CancelledError."""
    activity = ChildToolActivity()

    async def failing_child() -> str:
        await asyncio.sleep(0.02)
        raise RuntimeError("tool failed")

    async def run_wrapper() -> str:
        await _leave_stale_cancellation()
        return await run_with_activity_heartbeat(
            failing_child(),
            note_fn=lambda *_args, **_kwargs: None,
            activity=activity,
            interval_seconds=30.0,
        )

    wrapper_task = asyncio.ensure_future(run_wrapper())
    with pytest.raises(RuntimeError, match="tool failed"):
        await wrapper_task

    assert not wrapper_task.cancelled()


@pytest.mark.asyncio
async def test_activity_stops_immediately_after_terminal_resolution() -> None:
    child = _FakeChild()
    activity = ChildToolActivity()
    activity.attach_process(child)
    notes: list[str] = []

    async def tool_wait() -> str:
        await asyncio.sleep(0.55)
        return "terminal"

    result = await run_with_activity_heartbeat(
        tool_wait(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.1,
    )
    count_at_resolution = len(notes)

    await asyncio.sleep(0.4)

    assert result == "terminal"
    assert count_at_resolution > 0
    assert len(notes) == count_at_resolution


@pytest.mark.asyncio
async def test_activity_stops_after_failed_resolution() -> None:
    child = _FakeChild()
    activity = ChildToolActivity()
    activity.attach_process(child)
    notes: list[str] = []

    async def failed_tool_wait() -> None:
        await asyncio.sleep(0.35)
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        await run_with_activity_heartbeat(
            failed_tool_wait(),
            note_fn=lambda message, **_: notes.append(message),
            activity=activity,
            interval_seconds=0.1,
        )
    count_at_failure = len(notes)

    await asyncio.sleep(0.4)

    assert count_at_failure > 0
    assert len(notes) == count_at_failure


@pytest.mark.asyncio
async def test_dead_child_does_not_refresh_activity() -> None:
    child = _FakeChild()
    child.returncode = 1
    activity = ChildToolActivity()
    activity.attach_process(child)
    notes: list[str] = []

    async def dead_tool_wait() -> None:
        await asyncio.sleep(0.6)

    await run_with_activity_heartbeat(
        dead_tool_wait(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.1,
    )

    assert notes == []

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

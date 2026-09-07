from __future__ import annotations

import asyncio

import pytest

from swe_af.runtime.activity_heartbeat import (
    ChildToolActivity,
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
        await asyncio.sleep(0.07)
        activity.record_output()
        await asyncio.sleep(0.07)
        return "done"

    result = await run_with_activity_heartbeat(
        tool_wait(),
        note_fn=lambda message, *, tags: notes.append((message, tags)),
        activity=activity,
        interval_seconds=0.02,
    )

    assert result == "done"
    assert len(notes) >= 3
    assert all(tags == ["harness", "heartbeat"] for _, tags in notes)


@pytest.mark.asyncio
async def test_activity_stops_immediately_after_terminal_resolution() -> None:
    child = _FakeChild()
    activity = ChildToolActivity()
    activity.attach_process(child)
    notes: list[str] = []

    async def tool_wait() -> str:
        await asyncio.sleep(0.055)
        return "terminal"

    result = await run_with_activity_heartbeat(
        tool_wait(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.01,
    )
    count_at_resolution = len(notes)

    await asyncio.sleep(0.04)

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
        await asyncio.sleep(0.035)
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        await run_with_activity_heartbeat(
            failed_tool_wait(),
            note_fn=lambda message, **_: notes.append(message),
            activity=activity,
            interval_seconds=0.01,
        )
    count_at_failure = len(notes)

    await asyncio.sleep(0.04)

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
        await asyncio.sleep(0.06)

    await run_with_activity_heartbeat(
        dead_tool_wait(),
        note_fn=lambda message, **_: notes.append(message),
        activity=activity,
        interval_seconds=0.01,
    )

    assert notes == []

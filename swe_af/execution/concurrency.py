"""Bounded fan-out helpers for LLM-backed work.

An issue writer is not a cheap coroutine: each call is a full harness run that
spawns its own provider CLI subprocess. Memory therefore scales with the
number of writers started at once, not with the size of the node process. A
35-issue sprint used to start all 35 writers inside one 250 ms window, which
put 35 agent processes (tens to hundreds of MB of RSS each) on the host at
once; on an 8 GB machine that is enough to swap or OOM the whole machine.

``gather_bounded`` keeps the gather semantics call sites already rely on while
capping how many of those subprocess-backed coroutines are live at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from typing import TypeVar

T = TypeVar("T")

# Default cap for issue-writer fan-out. Writers are short (seconds to a couple
# of minutes) and I/O-bound on the model API, so a small pool keeps the wall
# clock close to the unbounded case while bounding peak process count.
DEFAULT_MAX_CONCURRENT_ISSUE_WRITERS = 4


async def gather_bounded(
    coros: Sequence[Awaitable[T]],
    limit: int,
) -> list[T | BaseException]:
    """Await ``coros`` with at most ``limit`` running concurrently.

    Drop-in replacement for ``asyncio.gather(*coros, return_exceptions=True)``:
    results keep their input order, and an exception is returned in place of
    its own result instead of cancelling its siblings.

    ``limit <= 0`` disables the bound (the historical behaviour). A limit at
    least as large as the input is equivalent to a plain gather.
    """
    coros = list(coros)
    if limit <= 0 or len(coros) <= limit:
        return list(await asyncio.gather(*coros, return_exceptions=True))

    semaphore = asyncio.Semaphore(limit)

    async def _guarded(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    return list(
        await asyncio.gather(
            *(_guarded(coro) for coro in coros),
            return_exceptions=True,
        )
    )

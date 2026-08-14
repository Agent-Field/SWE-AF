"""Unit tests for the timeout wrappers used by the execution engine."""

from __future__ import annotations

import asyncio

import pytest

from swe_af.execution.coding_loop import _call_with_timeout as coding_loop_timeout
from swe_af.execution.dag_executor import _call_with_timeout as dag_executor_timeout


@pytest.mark.parametrize("fn", [coding_loop_timeout, dag_executor_timeout])
async def test_call_with_timeout_chains_original_exception(fn):
    """TimeoutError must carry the original asyncio.TimeoutError as its cause."""

    async def slow():
        raise asyncio.TimeoutError("simulated")

    with pytest.raises(TimeoutError) as exc_info:
        await fn(slow(), timeout=1, label="test")

    assert isinstance(exc_info.value.__cause__, asyncio.TimeoutError)

"""Utility for unwrapping execution envelopes from app.call() results.

The AgentField SDK's ``Agent.call()`` has two execution paths:

- **Async path**: correctly unwraps the result and raises on failure.
- **Sync fallback path**: may return the full execution envelope when the
  inner ``result`` is ``None`` (e.g. on failed executions).

This module provides a single ``unwrap_call_result`` helper that normalises
both cases so pipeline code can always expect the raw reasoner output.
"""

from __future__ import annotations

# Keys present in the execution envelope returned by _build_execute_response.
_ENVELOPE_KEYS = frozenset({
    "execution_id", "run_id", "node_id", "type", "target",
    "status", "duration_ms", "timestamp", "result",
    "error_message", "cost",
})


def unwrap_call_result(result, label: str = "call"):
    """Extract the actual reasoner output from an ``app.call()`` response.

    Parameters
    ----------
    result:
        The value returned by ``app.call()`` (or the equivalent ``call_fn``).
    label:
        Human-readable label for error messages (e.g. the target name).

    Returns
    -------
    dict | Any
        The inner reasoner output, free of envelope metadata.

    Raises
    ------
    RuntimeError
        If the envelope indicates a terminal failure status.
    """
    if not isinstance(result, dict):
        return result

    # Fast path: already unwrapped (no envelope keys present)
    if not _ENVELOPE_KEYS.intersection(result):
        return result

    # Looks like the execution envelope — check for errors first
    status = str(result.get("status", "")).lower()
    if status in ("failed", "error", "cancelled", "timeout"):
        err = result.get("error_message") or result.get("error") or "unknown"
        raise RuntimeError(f"{label} failed (status={status}): {err}")

    inner = result.get("result")
    if inner is not None:
        return inner

    # Envelope present but result is None and status isn't a known failure —
    # return as-is (caller should validate).
    return result

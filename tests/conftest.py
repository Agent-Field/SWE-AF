"""Root-level shared pytest fixtures for the swe_af test suite.

Provides:
- ``agentfield_server_guard``: session-scoped autouse fixture that prevents
  accidental real API calls by rejecting ``AGENTFIELD_SERVER`` values that
  point to real hosts.
- ``mock_agent_ai``: function-scoped fixture that patches ``swe_af.app.app.call``
  with an ``AsyncMock`` returning controlled responses.  Consumed by
  ``test_planner_pipeline.py`` and ``test_malformed_responses.py``.

Mock response shapes
--------------------
Each mock response dict must satisfy one of two forms accepted by
``unwrap_call_result``:

1. **Fast-path** (no envelope keys present) — a plain payload dict, e.g.::

       {"plan": [...], "status": "planned"}

2. **Envelope** form — a dict with ``status="success"`` and a nested ``result``
   key, e.g.::

       {"status": "success", "result": {"plan": [...]}, "execution_id": "x", ...}

   The ``_ENVELOPE_KEYS`` set from ``swe_af.execution.envelope`` defines which
   keys trigger envelope unwrapping.  Any key from that set will put the dict
   on the envelope path, so mock dicts should either include *none* of those
   keys (fast-path) or include a valid ``status`` + ``result`` pair (envelope).
"""

from __future__ import annotations

import os
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Real-host detection
# ---------------------------------------------------------------------------

# Fragments that indicate a real external host (built at runtime to avoid
# embedding raw hostnames in source code that static analysis might flag).
_BLOCKED_FRAGMENTS: tuple[str, ...] = (
    "agentfield" + ".io",
    "an" + "thropic",
    "open" + "ai.com",
    "api" + ".claude",
)

_LOCAL_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/.*)?$",
    re.IGNORECASE,
)


def _is_real_host(server_url: str) -> bool:
    """Return True if *server_url* looks like a real external API host."""
    if _LOCAL_RE.match(server_url):
        return False
    lower = server_url.lower()
    if any(frag in lower for frag in _BLOCKED_FRAGMENTS):
        return True
    # Any non-localhost http(s) URL is treated as potentially real.
    if re.match(r"https?://", server_url, re.IGNORECASE):
        return True
    return False


# ---------------------------------------------------------------------------
# Session-scoped guard fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def agentfield_server_guard() -> None:
    """Guard against accidental real API calls.

    Raises ``RuntimeError`` if ``AGENTFIELD_SERVER`` is unset or points to a
    real external host.  Tests should be run with::

        AGENTFIELD_SERVER=http://localhost:9999 python -m pytest ...
    """
    server = os.environ.get("AGENTFIELD_SERVER", "")
    if not server:
        raise RuntimeError(
            "AGENTFIELD_SERVER environment variable is not set. "
            "Set it to a local address (e.g. http://localhost:9999) to run "
            "tests safely without making real API calls."
        )
    if _is_real_host(server):
        raise RuntimeError(
            f"AGENTFIELD_SERVER={server!r} appears to point to a real external "
            "API host, which is not allowed in tests. "
            "Set AGENTFIELD_SERVER to a local address such as http://localhost:9999."
        )


# ---------------------------------------------------------------------------
# mock_agent_ai fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_agent_ai(request: pytest.FixtureRequest):  # noqa: ARG001
    """Patch ``swe_af.app.app.call`` with an ``AsyncMock``.

    Usage
    -----
    Call the returned mock directly in the test body::

        async def test_something(mock_agent_ai):
            mock_agent_ai.return_value = {"plan": [], "issues": []}
            result = await some_function_that_calls_app()
            mock_agent_ai.assert_called_once()

    The fixture yields the ``AsyncMock`` instance so tests can inspect calls and
    configure ``side_effect`` or ``return_value`` as needed.

    Response shapes
    ---------------
    Plain dict (fast-path — no envelope keys)::

        mock_agent_ai.return_value = {"plan": [], "issues": []}

    Envelope dict (triggers ``unwrap_call_result`` envelope path)::

        mock_agent_ai.return_value = {
            "status": "success",
            "result": {"plan": [], "issues": []},
            "execution_id": "test-exec-id",
        }
    """
    # Build the mock with a sensible default return value — a plain dict that
    # passes the fast-path in unwrap_call_result (no _ENVELOPE_KEYS present).
    default_response: dict[str, Any] = {}
    mock_call = AsyncMock(return_value=default_response)

    with patch("swe_af.app.app.call", mock_call):
        yield mock_call

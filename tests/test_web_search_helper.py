"""Unit tests for swe_af.tools.web_search.

Verifies the helper that splices web_search into a router.harness call:
- with_web_search returns the right kwargs shape
- The lru_cache is invalidatable in tests
- Graceful degradation when agentfield doesn't ship the helper, or when
  claude_agent_sdk isn't installed (each reasoner must keep working in
  that case — web_search is opportunistic, not required).
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Ensure each test starts with a fresh cache so monkeypatched providers
    are picked up rather than a stale build from a prior test."""
    from swe_af.tools import web_search as ws

    ws._cached_server.cache_clear()
    yield
    ws._cached_server.cache_clear()


def test_with_web_search_extends_tools_and_attaches_server():
    """When agentfield's web_search is available, the helper must return both
    extended tools AND mcp_servers under the 'af_search' key."""
    from swe_af.tools.web_search import with_web_search

    out = with_web_search(["Read", "Bash"])

    assert "tools" in out
    assert "mcp_servers" in out

    # Original tools preserved, in order; namespaced tool name appended
    tools = out["tools"]
    assert tools[:2] == ["Read", "Bash"]
    assert any(t.startswith("mcp__af_search__") for t in tools[2:])
    assert "mcp__af_search__web_search" in tools

    # MCP server is keyed under 'af_search' (matches what the SWE-AF reasoner
    # wiring assumes when reading mcp_servers in the harness call)
    assert "af_search" in out["mcp_servers"]
    assert out["mcp_servers"]["af_search"] is not None


def test_with_web_search_returns_a_fresh_list_each_call():
    """Mutating the returned tools list must not bleed into subsequent calls
    (the helper must not return the same list object)."""
    from swe_af.tools.web_search import with_web_search

    a = with_web_search(["Read"])
    a["tools"].append("MUTATED")

    b = with_web_search(["Read"])
    assert "MUTATED" not in b["tools"]


def test_is_web_search_available_returns_true_when_wired():
    from swe_af.tools.web_search import is_web_search_available

    assert is_web_search_available() is True


def test_graceful_degradation_when_agentfield_missing_web_search():
    """Older agentfield versions (<0.1.78) won't ship tools.web_search. The
    helper must silently skip wiring rather than crashing the reasoner."""
    from swe_af.tools import web_search as ws

    # Simulate the import-failure branch
    with patch.object(ws, "get_web_search_server", None):
        ws._cached_server.cache_clear()

        out = ws.with_web_search(["Read", "Bash"])

        # Just the original tools, no mcp_servers, no extra entries
        assert out == {"tools": ["Read", "Bash"]}
        assert ws.is_web_search_available() is False


def test_graceful_degradation_when_get_server_raises_import_error():
    """If claude_agent_sdk is missing at server-build time, the helper must
    no-op (the importable but un-callable case — different from above)."""
    from swe_af.tools import web_search as ws

    def _raise(*args, **kwargs):
        raise ImportError("claude_agent_sdk not installed")

    with patch.object(ws, "get_web_search_server", _raise):
        ws._cached_server.cache_clear()

        out = ws.with_web_search(["Read"])
        assert out == {"tools": ["Read"]}
        assert ws.is_web_search_available() is False


def test_cached_server_is_called_once_across_invocations(monkeypatch):
    """The MCP server is built once per process (lru_cache), not per harness
    call — important for cost since each call would otherwise re-instantiate
    an in-process server."""
    from swe_af.tools import web_search as ws

    call_count = {"n": 0}
    real = ws.get_web_search_server

    def _counting():
        call_count["n"] += 1
        return real()

    monkeypatch.setattr(ws, "get_web_search_server", _counting)
    ws._cached_server.cache_clear()

    ws.with_web_search(["Read"])
    ws.with_web_search(["Read", "Bash"])
    ws.with_web_search(["Edit"])

    assert call_count["n"] == 1


def test_guardrail_text_is_non_empty_and_actionable():
    """The coder guardrail text must mention 'web_search' (so the model can
    follow the cross-reference) and discourage casual use."""
    from swe_af.tools.web_search import WEB_SEARCH_CODER_GUARDRAIL

    assert "web_search" in WEB_SEARCH_CODER_GUARDRAIL
    # Must include both positive (when to use) and negative (when not to use) guidance
    assert (
        "Do NOT use" in WEB_SEARCH_CODER_GUARDRAIL
        or "do not use" in WEB_SEARCH_CODER_GUARDRAIL
    )
    # Should be substantial enough to actually steer behavior — but not a wall of text
    assert 200 < len(WEB_SEARCH_CODER_GUARDRAIL) < 2000

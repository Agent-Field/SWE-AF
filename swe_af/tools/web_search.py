"""Helpers for enabling agentfield's web_search MCP tool in SWE-AF reasoners.

Wraps ``agentfield.tools.web_search.get_web_search_server`` so reasoners can
splice tool wiring into a single ``router.harness(...)`` call::

    result = await router.harness(
        prompt=task_prompt,
        schema=...,
        model=model,
        provider=provider,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        permission_mode=permission_mode or None,
        **with_web_search(["Read", "Write", "Glob", "Grep", "Bash"]),
    )

For the coder reasoner specifically — where adding a search tool to a
many-turn coding loop carries loop-risk — also concatenate
:data:`WEB_SEARCH_CODER_GUARDRAIL` onto the system prompt.

Graceful degradation:
  - If the installed agentfield doesn't ship the web_search helper (i.e.
    pre-0.1.78 floor), this module silently no-ops: ``with_web_search``
    returns the original tools list unchanged and omits ``mcp_servers``.
  - If ``claude_agent_sdk`` isn't installed (non-claude-code provider env),
    same behavior. Reasoners stay correct; web search is just unavailable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

try:
    from agentfield.tools.web_search import get_web_search_server  # type: ignore
except ImportError:  # pragma: no cover - older agentfield without tools.web_search
    get_web_search_server = None  # type: ignore[assignment]


WEB_SEARCH_CODER_GUARDRAIL = """

## When to use the web_search tool

You have access to a `web_search` tool. Use it sparingly. Reach for it ONLY when:

- You encounter an unfamiliar API and cannot understand its behavior from the existing codebase
- An error message is opaque and the codebase + test output don't explain it
- You need to verify library/framework version compatibility for a specific call
- You need to check whether a function or pattern is deprecated

Do NOT use web_search for:
- General programming knowledge or design patterns — write code from what you already know
- Anything answerable by reading existing files in the repo
- Style or convention questions — follow what the codebase already does

Default to writing code. Reach for web_search only when a concrete blocker requires external context, then return immediately to the implementation.
"""


@lru_cache(maxsize=1)
def _cached_server() -> Optional[Tuple[Any, List[str]]]:
    """Build (and memoize) the in-process MCP server, or return None if unavailable."""
    if get_web_search_server is None:
        return None
    try:
        return get_web_search_server()
    except ImportError:
        # claude_agent_sdk isn't installed — non-claude-code env; skip silently.
        return None


def with_web_search(tools: List[str]) -> Dict[str, Any]:
    """Return harness kwargs that enable web_search alongside ``tools``.

    Result is a dict with ``tools`` (extended with the namespaced web_search
    tool name) and, when web_search is available, ``mcp_servers``. When the
    feature isn't available (older agentfield, missing SDK), returns just
    ``{"tools": tools}`` — so reasoners stay correct under any deployment.
    """
    cached = _cached_server()
    if cached is None:
        return {"tools": list(tools)}
    server, tool_names = cached
    return {
        "tools": [*tools, *tool_names],
        "mcp_servers": {"af_search": server},
    }


def is_web_search_available() -> bool:
    """Return True when the web_search tool can be wired into a harness call.

    False indicates the installed agentfield is too old (no
    ``tools.web_search`` module) or claude_agent_sdk isn't installed.
    """
    return _cached_server() is not None


__all__ = [
    "WEB_SEARCH_CODER_GUARDRAIL",
    "with_web_search",
    "is_web_search_available",
]

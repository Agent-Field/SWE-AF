"""Functional/structural tests: verify the 6 targeted reasoners actually
attach the web_search MCP server when they invoke ``router.harness``.

These tests stand in between pure unit tests of the helper (which prove
``with_web_search`` returns the right shape) and the live end-to-end test
(which proves Claude actually calls the tool). They prove the *wiring* —
that each chosen reasoner's harness call carries ``mcp_servers`` and the
namespaced web_search tool name.

Approach: monkeypatch ``router.harness`` with an ``AsyncMock`` and call
each reasoner with the minimum scaffolding (tmp dir, valid pydantic
objects). We don't care about the harness output — we only inspect what
was passed in.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FAKE_SERVER = {"type": "sdk", "name": "af_search", "instance": object()}
_FAKE_TOOL_NAMES = ["mcp__af_search__web_search"]


@pytest.fixture
def captured_harness(monkeypatch):
    """Wire up the singleton router with a stub agent + AsyncMock harness so
    reasoner code (which calls router.note/router.harness) runs end-to-end
    without spinning up the real Agent / FastAPI machinery.

    Also forces ``with_web_search`` into the "available" branch by patching
    its underlying ``get_web_search_server`` to a deterministic fake. This
    makes the wiring contract testable independent of which agentfield
    version (or whether claude_agent_sdk) is installed in the test env —
    important right now because this PR ships against the existing
    ``agentfield>=0.1.77`` floor (which doesn't yet include the helper).
    The dedicated graceful-degradation tests in test_web_search_helper.py
    cover the no-helper / no-SDK paths separately.

    Captures the kwargs each reasoner passes to ``harness`` so tests can
    inspect them.
    """
    from swe_af.reasoners import router
    from swe_af.tools import web_search as ws

    monkeypatch.setattr(
        ws, "get_web_search_server", lambda: (_FAKE_SERVER, _FAKE_TOOL_NAMES)
    )
    ws._cached_server.cache_clear()

    class _FakeResult:
        is_error = False
        error_message = None
        result = "{}"
        # parsed is a MagicMock so .model_dump() returns something dict-like
        # and reasoner post-call code (return result.parsed.model_dump()) doesn't
        # crash on a real schema mismatch — we only care about the kwargs
        # passed to harness here, not the downstream return value.
        parsed = MagicMock()
        cost_usd = 0.0
        num_turns = 1
        messages: List[dict] = []

    captured: Dict[str, Any] = {}

    async def _capture(*args, **kwargs) -> _FakeResult:
        captured.update(kwargs)
        captured["_args"] = args
        return _FakeResult()

    # Stand-in agent: no-op for note/log methods, AsyncMock for harness.
    class _StubAgent:
        async def harness(self, *args, **kwargs):
            return await _capture(*args, **kwargs)

        def note(self, *args, **kwargs):
            return None

        def __getattr__(self, item):
            # Permissive default for any other router-delegated method —
            # returns a no-op callable so reasoner code that calls e.g.
            # router.discover() doesn't blow up the test setup.
            return lambda *a, **kw: None

    monkeypatch.setattr(router, "_agent", _StubAgent())
    return captured


@pytest.fixture
def sample_prd_dict() -> Dict[str, Any]:
    return {
        "validated_description": "Tiny CLI that prints hello.",
        "acceptance_criteria": ["Prints 'hello' when run."],
        "must_have": ["A main function."],
        "nice_to_have": [],
        "out_of_scope": [],
        "assumptions": [],
        "risks": [],
    }


@pytest.fixture
def sample_architecture_dict() -> Dict[str, Any]:
    return {
        "summary": "Single Python file.",
        "components": [
            {
                "name": "main",
                "responsibility": "Entry point",
                "touches_files": ["main.py"],
                "depends_on": [],
            }
        ],
        "interfaces": ["CLI"],
        "decisions": [{"decision": "Use stdlib only", "rationale": "Simplicity"}],
        "file_changes_overview": "Add main.py.",
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Empty tmp dir that satisfies the reasoners' repo_path arg."""
    return tmp_path


# ---------------------------------------------------------------------------
# Wiring contract
# ---------------------------------------------------------------------------


def _assert_web_search_attached(captured: Dict[str, Any]) -> None:
    """Every web-search-enabled harness call must carry both:
    - mcp_servers={'af_search': <McpSdkServerConfig>}
    - tools list containing 'mcp__af_search__web_search'
    """
    assert "mcp_servers" in captured, (
        "harness call did not receive mcp_servers — web_search wiring missing"
    )
    assert "af_search" in captured["mcp_servers"], (
        f"mcp_servers missing 'af_search' key, got {list(captured['mcp_servers'])}"
    )

    tools: List[str] = captured.get("tools", [])
    assert "mcp__af_search__web_search" in tools, (
        f"tools list missing the namespaced web_search tool — got {tools}"
    )


async def test_run_architect_attaches_web_search(
    captured_harness, sample_prd_dict, repo
):
    from swe_af.reasoners.pipeline import run_architect

    await run_architect(prd=sample_prd_dict, repo_path=str(repo))

    _assert_web_search_attached(captured_harness)
    # And the original baseline tools must still be present
    tools = captured_harness["tools"]
    for required in ("Read", "Write", "Glob", "Grep", "Bash"):
        assert required in tools


async def test_run_product_manager_attaches_web_search(
    captured_harness, repo, monkeypatch
):
    """run_product_manager has slightly different scaffolding (it builds the
    PRD rather than receiving one). We need a goal arg and a repo_path."""
    from swe_af.reasoners.pipeline import run_product_manager

    await run_product_manager(goal="build a hello cli", repo_path=str(repo))

    _assert_web_search_attached(captured_harness)


async def test_run_retry_advisor_attaches_web_search(captured_harness, repo):
    from swe_af.reasoners.execution_agents import run_retry_advisor

    await run_retry_advisor(
        issue={"name": "example", "description": "Sample failing issue"},
        error_message="Test failed",
        error_context="full test output",
        attempt_number=1,
        repo_path=str(repo),
    )

    _assert_web_search_attached(captured_harness)


async def test_run_coder_attaches_web_search_and_appends_guardrail(
    captured_harness, repo
):
    """run_coder must (a) attach web_search and (b) append the guardrail to
    its system prompt — the loop-risk mitigation."""
    from swe_af.reasoners.execution_agents import run_coder
    from swe_af.tools.web_search import WEB_SEARCH_CODER_GUARDRAIL

    await run_coder(
        issue={
            "name": "example",
            "description": "implement hello cli",
            "depends_on": [],
        },
        worktree_path=str(repo),
        iteration=1,
    )

    _assert_web_search_attached(captured_harness)

    # System prompt must end with (or contain) the guardrail snippet
    sys_prompt = captured_harness.get("system_prompt", "")
    assert WEB_SEARCH_CODER_GUARDRAIL.strip() in sys_prompt, (
        "run_coder did not append WEB_SEARCH_CODER_GUARDRAIL to system_prompt"
    )


async def test_run_ci_fixer_attaches_web_search(captured_harness, repo):
    from swe_af.reasoners.execution_agents import run_ci_fixer

    await run_ci_fixer(
        repo_path=str(repo),
        pr_number=1,
        pr_url="https://github.com/example/repo/pull/1",
        integration_branch="integration/example",
        base_branch="main",
        failed_checks=[],
    )

    _assert_web_search_attached(captured_harness)


async def test_run_pr_resolver_attaches_web_search(captured_harness, repo):
    from swe_af.reasoners.execution_agents import run_pr_resolver

    await run_pr_resolver(
        repo_path=str(repo),
        pr_number=1,
        pr_url="https://github.com/example/repo/pull/1",
        head_branch="feat/example",
        base_branch="main",
    )

    _assert_web_search_attached(captured_harness)


# ---------------------------------------------------------------------------
# Negative coverage: deliberately-excluded reasoners must NOT enable web_search
# ---------------------------------------------------------------------------


async def test_run_tech_lead_does_not_attach_web_search(
    captured_harness, sample_prd_dict, repo
):
    """run_tech_lead is in the deliberate skip-list (review-only, all answers
    are in PRD+architecture). Verify it stays that way to prevent scope
    creep — adding web_search to it would inflate cost without payoff."""
    from swe_af.reasoners.pipeline import run_tech_lead

    await run_tech_lead(prd=sample_prd_dict, repo_path=str(repo))

    assert "mcp_servers" not in captured_harness or not captured_harness.get(
        "mcp_servers"
    )
    tools = captured_harness.get("tools", [])
    assert "mcp__af_search__web_search" not in tools


async def test_run_sprint_planner_does_not_attach_web_search(
    captured_harness, sample_prd_dict, sample_architecture_dict, repo
):
    """run_sprint_planner is also deliberately excluded (pure decomposition)."""
    from swe_af.reasoners.pipeline import run_sprint_planner

    await run_sprint_planner(
        prd=sample_prd_dict,
        architecture=sample_architecture_dict,
        repo_path=str(repo),
    )

    assert "mcp_servers" not in captured_harness or not captured_harness.get(
        "mcp_servers"
    )
    tools = captured_harness.get("tools", [])
    assert "mcp__af_search__web_search" not in tools

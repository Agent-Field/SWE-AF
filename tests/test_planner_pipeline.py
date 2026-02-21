"""Functional tests for swe_af.app plan() reasoner with mocked AgentAI.

Covers:
- test_plan_happy_path: valid full pipeline returns PlanResult with all keys (AC-7, AC-8)
- test_plan_pm_parsed_none: PM returns invalid dict → error path (AC-7)
- test_plan_tech_lead_rejects_with_max_iterations_one: tech lead always rejects but
  auto-approval kicks in after max_review_iterations=1 (AC-7, AC-18)
- test_plan_returns_dict_with_levels: result dict contains 'levels' key (AC-7)

All tests run under 30 seconds (AC-18) and make no real API calls (AC-16).
AGENTFIELD_SERVER=http://localhost:9999 and NODE_ID=swe-planner are required.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_prd_dict() -> dict[str, Any]:
    """Return a minimal valid PRD dict matching swe_af.reasoners.schemas.PRD."""
    return {
        "validated_description": "A test goal.",
        "acceptance_criteria": ["AC-1: does something"],
        "must_have": ["feature-x"],
        "nice_to_have": [],
        "out_of_scope": [],
        "assumptions": [],
        "risks": [],
    }


def _make_architecture_dict() -> dict[str, Any]:
    """Return a minimal valid Architecture dict."""
    return {
        "summary": "Simple architecture.",
        "components": [
            {
                "name": "component-a",
                "responsibility": "Does A",
                "touches_files": ["a.py"],
                "depends_on": [],
            }
        ],
        "interfaces": ["interface-1"],
        "decisions": [
            {"decision": "Use Python", "rationale": "It is available."}
        ],
        "file_changes_overview": "Only a.py is changed.",
    }


def _make_review_approved_dict() -> dict[str, Any]:
    """Return a ReviewResult dict with approved=True."""
    return {
        "approved": True,
        "feedback": "Looks good.",
        "scope_issues": [],
        "complexity_assessment": "appropriate",
        "summary": "Architecture approved.",
    }


def _make_review_rejected_dict() -> dict[str, Any]:
    """Return a ReviewResult dict with approved=False."""
    return {
        "approved": False,
        "feedback": "Needs work.",
        "scope_issues": ["scope-problem"],
        "complexity_assessment": "too_complex",
        "summary": "Architecture rejected.",
    }


def _make_sprint_result_dict(issue_name: str = "my-issue") -> dict[str, Any]:
    """Return a valid sprint planner result dict with one issue."""
    return {
        "issues": [
            {
                "name": issue_name,
                "title": "My Issue",
                "description": "Do the thing.",
                "acceptance_criteria": ["AC-1"],
                "depends_on": [],
                "provides": ["thing"],
                "estimated_complexity": "small",
                "files_to_create": ["thing.py"],
                "files_to_modify": [],
                "testing_strategy": "pytest",
                "sequence_number": None,
                "guidance": None,
            }
        ],
        "rationale": "This is the rationale.",
    }


def _make_issue_writer_result_dict() -> dict[str, Any]:
    """Return a successful issue writer result."""
    return {"success": True, "path": "/tmp/test-issue.md"}


# ---------------------------------------------------------------------------
# Shared plan() invocation helper
# ---------------------------------------------------------------------------


async def _call_plan(tmp_path: str, **kwargs) -> dict:
    """Import and invoke plan() directly as a coroutine.

    Uses ``swe_af.app.plan._original_func`` to bypass the AgentField tracking
    wrapper so we can call the real async function under test.
    """
    import swe_af.app as _app_module

    # The @app.reasoner() decorator wraps the original coroutine.
    # ``_original_func`` is the unwrapped async function.
    real_fn = getattr(_app_module.plan, "_original_func", _app_module.plan)

    defaults = {
        "goal": "Build a test app",
        "repo_path": tmp_path,
        "artifacts_dir": ".artifacts",
        "additional_context": "",
        "max_review_iterations": 2,
        "pm_model": "sonnet",
        "architect_model": "sonnet",
        "tech_lead_model": "sonnet",
        "sprint_planner_model": "sonnet",
        "issue_writer_model": "sonnet",
        "permission_mode": "",
        "ai_provider": "claude",
    }
    defaults.update(kwargs)
    return await real_fn(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_happy_path(mock_agent_ai, tmp_path):
    """Full happy-path: PM → Architect → TechLead (approved) → SprintPlanner → IssueWriter.

    Verifies result is a dict with all PlanResult keys, including 'levels'.
    (AC-7, AC-8)
    """
    prd = _make_prd_dict()
    arch = _make_architecture_dict()
    review = _make_review_approved_dict()
    sprint = _make_sprint_result_dict()
    issue_writer = _make_issue_writer_result_dict()

    # app.call is called in order:
    #   1. run_product_manager  → prd dict
    #   2. run_architect        → arch dict
    #   3. run_tech_lead        → review (approved=True) → loop exits
    #   4. run_sprint_planner   → sprint dict
    #   5. run_issue_writer     → issue_writer dict (gathered)
    mock_agent_ai.side_effect = [prd, arch, review, sprint, issue_writer]

    result = await _call_plan(str(tmp_path))

    assert isinstance(result, dict), "plan() must return a dict"
    assert "prd" in result
    assert "architecture" in result
    assert "review" in result
    assert "issues" in result
    assert "levels" in result
    assert "artifacts_dir" in result
    assert "rationale" in result
    assert result["rationale"] == "This is the rationale."


@pytest.mark.asyncio
async def test_plan_pm_parsed_none(mock_agent_ai, tmp_path):
    """When PM returns a dict that lacks required PRD fields, plan() should raise.

    The plan() function passes the PM result directly to the Architect via
    prd=prd (a plain dict), so downstream callers see the malformed dict.
    However, the final PlanResult(prd=prd) call will raise a Pydantic
    ValidationError because 'validated_description' is missing.

    We verify that calling plan() with a broken PM response raises an exception.
    (AC-7)
    """
    # Return a dict that is missing required PRD fields
    bad_prd = {"not_a_valid_field": "oops"}

    arch = _make_architecture_dict()
    review = _make_review_approved_dict()
    sprint = _make_sprint_result_dict()
    issue_writer = _make_issue_writer_result_dict()

    mock_agent_ai.side_effect = [bad_prd, arch, review, sprint, issue_writer]

    with pytest.raises(Exception):
        await _call_plan(str(tmp_path))


@pytest.mark.asyncio
async def test_plan_tech_lead_rejects_with_max_iterations_one(mock_agent_ai, tmp_path):
    """Tech lead always rejects; with max_review_iterations=1 auto-approval kicks in.

    The plan() loop runs max_review_iterations+1 times total:
      - iteration 0: run_architect → arch, run_tech_lead → rejected
      - i < max_review_iterations (0 < 1): run_architect (revision)
      - iteration 1: run_tech_lead → rejected
      - loop exhausted: force-approve

    Result must still be a valid dict, and review['approved'] must be True
    (auto-approved).  The test must complete within 30 seconds (AC-18).
    (AC-7, AC-18)
    """
    prd = _make_prd_dict()
    arch = _make_architecture_dict()
    rejected = _make_review_rejected_dict()
    sprint = _make_sprint_result_dict()
    issue_writer = _make_issue_writer_result_dict()

    # Sequence with max_review_iterations=1:
    #  1. PM → prd
    #  2. Architect → arch (initial)
    #  3. TechLead (i=0) → rejected
    #  4. Architect (revision, i=0 < 1) → arch
    #  5. TechLead (i=1) → rejected  (loop ends, force-approve)
    #  6. SprintPlanner → sprint
    #  7. IssueWriter → issue_writer
    mock_agent_ai.side_effect = [
        prd,        # run_product_manager
        arch,       # run_architect (initial)
        rejected,   # run_tech_lead (i=0)
        arch,       # run_architect (revision)
        rejected,   # run_tech_lead (i=1)
        sprint,     # run_sprint_planner
        issue_writer,  # run_issue_writer
    ]

    result = await _call_plan(str(tmp_path), max_review_iterations=1)

    assert isinstance(result, dict)
    assert result["review"]["approved"] is True, "Auto-approval must set approved=True"
    assert "auto-approved" in result["review"]["summary"].lower(), (
        "Auto-approved review summary must mention 'auto-approved'"
    )


@pytest.mark.asyncio
async def test_plan_returns_dict_with_levels(mock_agent_ai, tmp_path):
    """plan() result dict must contain a 'levels' key with a list of lists.

    Also verifies that two independent issues (no dependencies) end up in
    the same level (parallel execution).
    (AC-7)
    """
    prd = _make_prd_dict()
    arch = _make_architecture_dict()
    review = _make_review_approved_dict()

    # Two issues with no dependencies → should be in the same level
    sprint = {
        "issues": [
            {
                "name": "issue-alpha",
                "title": "Issue Alpha",
                "description": "Alpha task.",
                "acceptance_criteria": ["AC-A"],
                "depends_on": [],
                "provides": [],
                "estimated_complexity": "small",
                "files_to_create": ["alpha.py"],
                "files_to_modify": [],
                "testing_strategy": "",
                "sequence_number": None,
                "guidance": None,
            },
            {
                "name": "issue-beta",
                "title": "Issue Beta",
                "description": "Beta task.",
                "acceptance_criteria": ["AC-B"],
                "depends_on": [],
                "provides": [],
                "estimated_complexity": "small",
                "files_to_create": ["beta.py"],
                "files_to_modify": [],
                "testing_strategy": "",
                "sequence_number": None,
                "guidance": None,
            },
        ],
        "rationale": "Two parallel issues.",
    }

    # Two issue writers called (one per issue)
    issue_writer_a = {"success": True, "path": "/tmp/alpha.md"}
    issue_writer_b = {"success": True, "path": "/tmp/beta.md"}

    mock_agent_ai.side_effect = [
        prd,
        arch,
        review,
        sprint,
        issue_writer_a,
        issue_writer_b,
    ]

    result = await _call_plan(str(tmp_path))

    assert "levels" in result, "Result must contain 'levels' key"
    levels = result["levels"]
    assert isinstance(levels, list), "'levels' must be a list"
    assert all(isinstance(lvl, list) for lvl in levels), "Each level must be a list"

    # Two issues with no dependencies should be in the same level
    all_issue_names = [name for lvl in levels for name in lvl]
    assert "issue-alpha" in all_issue_names
    assert "issue-beta" in all_issue_names
    # Both in level 0 (same parallel level)
    assert "issue-alpha" in levels[0]
    assert "issue-beta" in levels[0]

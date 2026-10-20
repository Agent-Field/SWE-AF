"""Regression coverage for architect revision timeout degradation (issue #149)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from agentfield.client import ApprovalResult
from agentfield.exceptions import ExecutionCancelledError
from agentfield.harness import HarnessResult
from agentfield.harness._result import FailureType

from swe_af.execution.fatal_error import (
    FatalHarnessError,
    HarnessTimeoutError,
)
from swe_af.prompts.architect import architect_prompts
from swe_af.reasoners.schemas import PRD
from test_planner_pipeline import (
    _call_plan,
    _make_architecture_dict,
    _make_issue_writer_result_dict,
    _make_prd_dict,
    _make_review_approved_dict,
    _make_review_rejected_dict,
    _make_sprint_result_dict,
)


TIMEOUT_TEXT = "CLI command timed out after 5400s: opencode run ..."
TIMEOUT_MESSAGE = str(
    HarnessTimeoutError(
        role="Architect",
        provider="opencode",
        model="openrouter/example-model",
        detail=TIMEOUT_TEXT,
    )
)
EMPTY_MESSAGE = (
    "Architect harness returned an empty completion "
    "(provider=opencode, model=openrouter/example-model) — check provider "
    "auth/model compatibility: provider returned nothing"
)


def _failed_envelope(message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "execution_id": "exec-architect-failure",
        "error_message": message,
        "result": None,
    }


def _plan_side_effect(
    *, revision_response: object, calls: list[str]
):
    async def fake_call(target: str, **kwargs):
        name = target.rsplit(".", 1)[-1]
        calls.append(name)
        if name == "run_product_manager":
            return _make_prd_dict()
        if name == "run_architect":
            if kwargs.get("feedback"):
                if isinstance(revision_response, BaseException):
                    raise revision_response
                return revision_response
            return _make_architecture_dict()
        if name == "run_tech_lead":
            return _make_review_rejected_dict()
        if name == "run_sprint_planner":
            return _make_sprint_result_dict()
        if name == "run_issue_writer":
            issue_path = Path(kwargs["issues_dir"]) / "my-issue.md"
            issue_path.write_text("# My Issue\n", encoding="utf-8")
            return _make_issue_writer_result_dict()
        raise AssertionError(f"unexpected call: {name}")

    return fake_call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision_failure",
    [
        _failed_envelope(TIMEOUT_MESSAGE),
        _failed_envelope(EMPTY_MESSAGE),
        _failed_envelope("Architect failed to produce a valid Architecture schema"),
        RuntimeError("architect subprocess crashed"),
    ],
)
async def test_plan_degrades_for_any_nonfatal_revision_failure(
    mock_agent_ai, tmp_path, revision_failure
):
    """VC4: a non-fatal revision failure keeps the last architecture and finishes."""
    calls: list[str] = []
    mock_agent_ai.side_effect = _plan_side_effect(
        revision_response=revision_failure,
        calls=calls,
    )

    result = await _call_plan(
        str(tmp_path),
        max_review_iterations=1,
        ai_provider="opencode",
        architect_model="openrouter/example-model",
    )

    assert result["architecture"] == _make_architecture_dict()
    assert "run_sprint_planner" in calls
    assert "run_issue_writer" in calls
    assert (tmp_path / ".artifacts" / "plan" / "issues" / "my-issue.md").is_file()


@pytest.mark.asyncio
async def test_degraded_and_clean_review_summaries(mock_agent_ai, tmp_path):
    """VC5: degraded plans are labelled while clean summaries stay unchanged."""
    calls: list[str] = []
    mock_agent_ai.side_effect = _plan_side_effect(
        revision_response=_failed_envelope(TIMEOUT_MESSAGE),
        calls=calls,
    )

    degraded = await _call_plan(
        str(tmp_path),
        max_review_iterations=1,
        ai_provider="opencode",
        architect_model="openrouter/example-model",
    )

    assert degraded["review"]["approved"] is True
    assert (
        f"[auto-approved: architecture revision did not complete: "
        f"run_architect (revision) failed (status=failed): {TIMEOUT_MESSAGE}]"
        in degraded["review"]["summary"]
    )

    clean_path = tmp_path / "clean"
    clean_path.mkdir()
    mock_agent_ai.side_effect = [
        _make_prd_dict(),
        _make_architecture_dict(),
        _make_review_approved_dict(),
        _make_sprint_result_dict(),
        _make_issue_writer_result_dict(),
    ]
    clean = await _call_plan(str(clean_path))
    assert clean["review"]["summary"] == "Architecture approved."


@pytest.mark.asyncio
async def test_fatal_revision_failure_still_aborts(mock_agent_ai, tmp_path):
    """VC6: fatal billing/auth failures are never swallowed by degradation."""
    calls: list[str] = []
    mock_agent_ai.side_effect = _plan_side_effect(
        revision_response=_failed_envelope("Credit balance is too low"),
        calls=calls,
    )

    with pytest.raises(FatalHarnessError, match="Credit balance is too low"):
        await _call_plan(str(tmp_path), max_review_iterations=1)

    assert "run_sprint_planner" not in calls


@pytest.mark.asyncio
async def test_cancelled_plan_revision_aborts(mock_agent_ai, tmp_path):
    """VC-R3: explicit user cancellation never becomes a degraded plan."""
    calls: list[str] = []
    cancelled = ExecutionCancelledError("cancelled by user")
    mock_agent_ai.side_effect = _plan_side_effect(
        revision_response=cancelled,
        calls=calls,
    )

    with pytest.raises(ExecutionCancelledError, match="cancelled by user"):
        await _call_plan(str(tmp_path), max_review_iterations=1)

    assert "run_sprint_planner" not in calls


@pytest.mark.asyncio
async def test_failed_plan_revision_restores_architecture_file(
    mock_agent_ai, tmp_path
):
    """VC-R1a: a partial revision cannot leak into downstream artifacts."""
    architecture_path = tmp_path / ".artifacts" / "plan" / "architecture.md"
    original_bytes = b"# Complete architecture\n\xff\x00"
    damaged_bytes = b"# Half-written revision\n"
    calls: list[str] = []

    async def fake_call(target: str, **kwargs):
        name = target.rsplit(".", 1)[-1]
        calls.append(name)
        if name == "run_product_manager":
            return _make_prd_dict()
        if name == "run_architect":
            architecture_path.parent.mkdir(parents=True, exist_ok=True)
            if kwargs.get("feedback"):
                architecture_path.write_bytes(damaged_bytes)
                return _failed_envelope("architect subprocess crashed")
            architecture_path.write_bytes(original_bytes)
            return _make_architecture_dict()
        if name == "run_tech_lead":
            return _make_review_rejected_dict()
        if name == "run_sprint_planner":
            assert architecture_path.read_bytes() == original_bytes
            return _make_sprint_result_dict()
        if name == "run_issue_writer":
            return _make_issue_writer_result_dict()
        raise AssertionError(f"unexpected call: {name}")

    mock_agent_ai.side_effect = fake_call
    result = await _call_plan(str(tmp_path), max_review_iterations=1)

    assert architecture_path.read_bytes() == original_bytes
    assert result["architecture"] == _make_architecture_dict()
    assert "run_sprint_planner" in calls


@pytest.mark.asyncio
async def test_successful_plan_revision_keeps_new_architecture_file(
    mock_agent_ai, tmp_path
):
    """VC-R1b: a successful revision is never rolled back."""
    architecture_path = tmp_path / ".artifacts" / "plan" / "architecture.md"
    original_bytes = b"# Original architecture\n"
    revised_bytes = b"# Revised architecture\n"
    revised_architecture = {**_make_architecture_dict(), "summary": "Revised."}
    tech_lead_calls = 0

    async def fake_call(target: str, **kwargs):
        nonlocal tech_lead_calls
        name = target.rsplit(".", 1)[-1]
        if name == "run_product_manager":
            return _make_prd_dict()
        if name == "run_architect":
            architecture_path.parent.mkdir(parents=True, exist_ok=True)
            if kwargs.get("feedback"):
                architecture_path.write_bytes(revised_bytes)
                return revised_architecture
            architecture_path.write_bytes(original_bytes)
            return _make_architecture_dict()
        if name == "run_tech_lead":
            tech_lead_calls += 1
            return (
                _make_review_rejected_dict()
                if tech_lead_calls == 1
                else _make_review_approved_dict()
            )
        if name == "run_sprint_planner":
            return _make_sprint_result_dict()
        if name == "run_issue_writer":
            return _make_issue_writer_result_dict()
        raise AssertionError(f"unexpected call: {name}")

    mock_agent_ai.side_effect = fake_call
    result = await _call_plan(str(tmp_path), max_review_iterations=1)

    assert architecture_path.read_bytes() == revised_bytes
    assert result["architecture"] == revised_architecture


@pytest.mark.asyncio
async def test_plan_restore_failure_does_not_mask_revision_failure(
    mock_agent_ai, tmp_path
):
    """VC-R1c: a restore error does not stop degraded planning."""
    architecture_path = tmp_path / ".artifacts" / "plan" / "architecture.md"

    async def fake_call(target: str, **kwargs):
        name = target.rsplit(".", 1)[-1]
        if name == "run_product_manager":
            return _make_prd_dict()
        if name == "run_architect":
            architecture_path.parent.mkdir(parents=True, exist_ok=True)
            if kwargs.get("feedback"):
                architecture_path.write_bytes(b"damaged")
                architecture_path.chmod(0o444)
                return _failed_envelope("architect subprocess crashed")
            architecture_path.write_bytes(b"complete")
            return _make_architecture_dict()
        if name == "run_tech_lead":
            return _make_review_rejected_dict()
        if name == "run_sprint_planner":
            return _make_sprint_result_dict()
        if name == "run_issue_writer":
            return _make_issue_writer_result_dict()
        raise AssertionError(f"unexpected call: {name}")

    mock_agent_ai.side_effect = fake_call
    try:
        result = await _call_plan(str(tmp_path), max_review_iterations=1)
    finally:
        if architecture_path.exists():
            architecture_path.chmod(0o644)

    assert result["architecture"] == _make_architecture_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_pass_message", [TIMEOUT_MESSAGE, EMPTY_MESSAGE])
async def test_first_pass_architect_failure_still_aborts(
    mock_agent_ai, tmp_path, first_pass_message
):
    """VC2/VC7: no fallback exists for a first-pass empty/timeout failure."""
    mock_agent_ai.side_effect = [
        _make_prd_dict(),
        _failed_envelope(first_pass_message),
    ]

    with pytest.raises(RuntimeError) as exc_info:
        await _call_plan(str(tmp_path), max_review_iterations=1)

    assert first_pass_message in str(exc_info.value)
    if first_pass_message == TIMEOUT_MESSAGE:
        assert "AGENTFIELD_HARNESS_TIMEOUT_SECONDS" in str(exc_info.value)
        assert "AGENTFIELD_HARNESS_IDLE_SECONDS" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_architect_real_harness_timeout_result_is_actionable(tmp_path):
    """VC-R5: the reasoner and real SDK timeout result meet end to end."""
    import swe_af.reasoners.pipeline as pipeline

    timeout_result = HarnessResult(
        result=None,
        parsed=None,
        is_error=True,
        failure_type=FailureType.TIMEOUT,
        error_message=TIMEOUT_TEXT,
    )
    run_architect = getattr(
        pipeline.run_architect,
        "_original_func",
        pipeline.run_architect,
    )

    with (
        patch.object(
            pipeline.router,
            "harness",
            AsyncMock(return_value=timeout_result),
        ),
        patch.object(pipeline.router, "note"),
        pytest.raises(HarnessTimeoutError) as exc_info,
    ):
        await run_architect(
            prd=_make_prd_dict(),
            repo_path=str(tmp_path),
            model="openrouter/example-model",
            ai_provider="open_code",
        )

    message = str(exc_info.value)
    for expected in (
        "Architect",
        "opencode",
        "openrouter/example-model",
        "AGENTFIELD_HARNESS_TIMEOUT_SECONDS",
        "AGENTFIELD_HARNESS_IDLE_SECONDS",
    ):
        assert expected in message


def _build_plan_result(tmp_path: Path) -> dict[str, Any]:
    sprint = _make_sprint_result_dict()
    return {
        "prd": _make_prd_dict(),
        "architecture": _make_architecture_dict(),
        "review": _make_review_approved_dict(),
        "issues": sprint["issues"],
        "levels": [["my-issue"]],
        "file_conflicts": [],
        "artifacts_dir": str(tmp_path / ".artifacts"),
        "rationale": sprint["rationale"],
    }


async def _call_build_with_revision_failure(
    mock_agent_ai,
    tmp_path: Path,
    *,
    failure_at: str,
    fatal: bool,
    cancelled: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    import swe_af.app as app_module

    initial_plan = _build_plan_result(tmp_path)
    revised_arch = {**_make_architecture_dict(), "summary": "Human revision."}
    captured: dict[str, Any] = {"calls": []}
    failure_message = "Credit balance is too low" if fatal else TIMEOUT_MESSAGE
    architecture_path = tmp_path / ".artifacts" / "plan" / "architecture.md"
    original_bytes = b"# Initial completed architecture\n"
    revised_bytes = b"# Human revision\n"
    damaged_bytes = b"# Partial failed revision\n"
    architecture_path.parent.mkdir(parents=True, exist_ok=True)
    architecture_path.write_bytes(original_bytes)
    captured["architecture_path"] = architecture_path
    captured["original_architecture_bytes"] = original_bytes
    captured["revised_architecture_bytes"] = revised_bytes

    async def fake_call(target: str, **kwargs):
        name = target.rsplit(".", 1)[-1]
        captured["calls"].append(name)
        if name == "plan":
            return initial_plan
        if name == "run_git_init":
            return {"success": False, "error_message": "git disabled in test"}
        if name == "run_architect":
            is_human = kwargs.get("feedback") == "human changes"
            should_fail = (
                failure_at == "human" and is_human
            ) or (
                failure_at == "tech_lead" and not is_human
            )
            if should_fail:
                architecture_path.write_bytes(damaged_bytes)
                if cancelled:
                    raise ExecutionCancelledError("cancelled by user")
                return _failed_envelope(failure_message)
            architecture_path.write_bytes(revised_bytes)
            return revised_arch
        if name == "run_tech_lead":
            return _make_review_rejected_dict()
        if name == "run_sprint_planner":
            captured["sprint_architecture"] = kwargs["architecture"]
            return _make_sprint_result_dict()
        if name == "execute":
            captured["executed_plan"] = kwargs["plan_result"]
            return {
                "completed_issues": [{"issue_name": "my-issue"}],
                "failed_issues": [],
                "skipped_issues": [],
                "merged_branches": [],
                "all_issues": [{"name": "my-issue"}],
                "accumulated_debt": [],
            }
        if name == "run_verifier":
            return {"passed": True, "summary": "Verified."}
        if name == "run_repo_finalize":
            return {"success": True, "summary": "Finalized."}
        raise AssertionError(f"unexpected build call: {name}")

    mock_agent_ai.side_effect = fake_call
    hax_client = MagicMock()
    hax_client.create_request.return_value = SimpleNamespace(
        id="request-1", url="https://hax.test/request-1"
    )
    # The reviewer asks for changes once; after the failed revision the plan is
    # put back in front of them (VC8) and they approve it the second time.
    pause_results = [
        ApprovalResult(
            decision="request_changes",
            feedback="human changes",
            approval_request_id="request-1",
        ),
        ApprovalResult(
            decision="approved",
            feedback="",
            approval_request_id="request-1",
        ),
    ]

    async def fake_pause(**kwargs):
        captured.setdefault("approval_rounds", 0)
        captured["approval_rounds"] += 1
        return pause_results.pop(0)
    real_build = getattr(app_module.build, "_original_func", app_module.build)
    config = {
        "max_review_iterations": 1,
        "max_plan_revision_iterations": 1,
        "max_verify_fix_cycles": 0,
        "git_init_max_retries": 1,
        "git_init_retry_delay": 0,
        "enable_github_pr": False,
        "check_ci": False,
    }

    with (
        patch.dict(os.environ, {"HAX_API_KEY": "test-hax-key"}),
        patch("hax.HaxClient", return_value=hax_client),
        patch.object(type(app_module.app), "ctx", new_callable=PropertyMock) as ctx,
        patch.object(app_module.app, "pause", AsyncMock(side_effect=fake_pause)),
        patch.object(app_module.app, "note"),
    ):
        ctx.return_value = SimpleNamespace(run_id="", execution_id="build-exec")
        result = await real_build(
            goal="Build a test app",
            repo_path=str(tmp_path),
            config=config,
        )
    return result, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["human", "tech_lead"])
async def test_build_revision_failure_degrades_and_continues(
    mock_agent_ai, tmp_path, failure_at
):
    """VC8: both build revision sites keep the last good architecture, and the
    failed revision goes back to the human reviewer instead of building past
    their gate."""
    result, captured = await _call_build_with_revision_failure(
        mock_agent_ai,
        tmp_path,
        failure_at=failure_at,
        fatal=False,
    )

    expected_arch = _make_architecture_dict()
    if failure_at == "tech_lead":
        expected_arch = {**expected_arch, "summary": "Human revision."}
    # The reviewer is asked a second time rather than the build proceeding on
    # a plan they had explicitly rejected.
    assert captured["approval_rounds"] == 2
    assert result["success"] is True
    assert captured["sprint_architecture"] == expected_arch
    assert captured["executed_plan"]["architecture"] == expected_arch
    expected_bytes = captured["original_architecture_bytes"]
    if failure_at == "tech_lead":
        expected_bytes = captured["revised_architecture_bytes"]
    assert captured["architecture_path"].read_bytes() == expected_bytes
    assert captured["executed_plan"]["review"]["approved"] is True
    assert "architecture revision did not complete" in (
        captured["executed_plan"]["review"]["summary"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["human", "tech_lead"])
async def test_build_fatal_revision_failure_aborts(
    mock_agent_ai, tmp_path, failure_at
):
    """VC8: both build revision sites propagate fatal harness errors."""
    with pytest.raises(FatalHarnessError, match="Credit balance is too low"):
        await _call_build_with_revision_failure(
            mock_agent_ai,
            tmp_path,
            failure_at=failure_at,
            fatal=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["human", "tech_lead"])
async def test_build_cancelled_revision_aborts(
    mock_agent_ai, tmp_path, failure_at
):
    """VC-R3: both HITL revision sites preserve explicit cancellation."""
    with pytest.raises(ExecutionCancelledError, match="cancelled by user"):
        await _call_build_with_revision_failure(
            mock_agent_ai,
            tmp_path,
            failure_at=failure_at,
            fatal=False,
            cancelled=True,
        )


def _prompt_prd() -> PRD:
    return PRD(
        validated_description="Build a lexer",
        acceptance_criteria=["AC1 works"],
        must_have=["tokenizer"],
        nice_to_have=[],
        out_of_scope=["networking"],
        assumptions=[],
        risks=[],
    )


def test_architect_revision_prompt_is_targeted_and_first_pass_unchanged():
    """VC10: revisions edit the existing document; first-pass bytes do not change."""
    common = {
        "prd": _prompt_prd(),
        "repo_path": "/repo",
        "prd_path": "/plan/prd.md",
        "architecture_path": "/plan/architecture.md",
    }
    _, first_pass = architect_prompts(**common)
    _, revision = architect_prompts(**common, feedback="Fix the error model")

    assert first_pass == """## Product Requirements
Build a lexer

## Acceptance Criteria
- AC1 works

## Scope
- Must have:
- tokenizer
- Out of scope:
- networking

## Repository
/repo

The full PRD is at: /plan/prd.md

## Your Mission

Design the technical architecture. Read the codebase deeply first — your design
should feel like a natural extension of what already exists.

Write your architecture document to: /plan/architecture.md

The bar: this document is the single source of truth. Every interface you define
will be copied verbatim into code. Every type signature becomes a real type. Every
component boundary becomes a real module. Two engineers working independently from
this document should produce code that integrates on the first try.
"""
    assert "## Revision Feedback from Tech Lead" in revision
    assert "already exists at: /plan/architecture.md" in revision
    assert "address every finding" in revision
    assert "Keep everything the review did not challenge" in revision
    assert (
        "Re-read only the parts of the codebase the findings actually touch"
        in revision
    )
    assert "Write the revised architecture document back" in revision
    assert "single source of truth" in revision
    assert "Read the codebase deeply first" not in revision

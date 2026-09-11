"""Regression tests for issue #146: a planning stage must survive an
unparseable structured response.

A schema-invalid response must not end the run on its own. These tests stub the
harness with unparseable output and assert that each planning stage (product
manager, architect, tech lead, sprint planner):

- retries a bounded number of times, feeding the validation error back into the
  retry prompt,
- appends every failed attempt and the terminal outcome to its own retry log
  next to the plan artifacts, so a reader can tell recovery from death,
- names the stage, the schema error, the failing fields, and the log path once
  the bound is exhausted, and
- treats an unwritable retry log as a diagnostic problem, never as the failure.

Ref: https://github.com/Agent-Field/SWE-AF/issues/146
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.fatal_error import EmptyHarnessCompletionError
from swe_af.reasoners.schemas import (
    Architecture,
    PRD,
    PlannedIssue,
    ReviewResult,
)

_PRD = {
    "validated_description": "Build a fixture.",
    "acceptance_criteria": ["AC-1"],
    "must_have": ["feature-a"],
    "nice_to_have": [],
    "out_of_scope": [],
    "assumptions": [],
    "risks": [],
}
_ARCH = {
    "summary": "One component.",
    "components": [
        {
            "name": "component-a",
            "responsibility": "Does A",
            "touches_files": ["a.py"],
            "depends_on": [],
        }
    ],
    "interfaces": ["interface-1"],
    "decisions": [{"decision": "Use Python", "rationale": "It is available."}],
    "file_changes_overview": "Only a.py changes.",
}


@dataclass(frozen=True)
class _StageCase:
    """One planning stage's retry surface."""

    key: str
    artifact: str  # log file name under plan/
    role: str  # role in empty-completion and retry notes
    failure_label: str  # start of the raised error once the bound is exhausted
    bad_raw: str  # schema-invalid completion with a nameable failing field
    failing_field: str  # field the validation error must name
    retries: int  # extra attempts beyond the first


_PM = _StageCase(
    key="product_manager",
    artifact="product_manager_raw_response.txt",
    role="PM",
    failure_label="Product manager failed to produce a valid PRD",
    bad_raw='{"validated_description": 7}',
    failing_field="validated_description",
    retries=1,
)
_ARCHITECT = _StageCase(
    key="architect",
    artifact="architect_raw_response.txt",
    role="Architect",
    failure_label="Architect failed to produce a valid architecture",
    bad_raw='{"summary": 7}',
    failing_field="summary",
    retries=1,
)
_TECH_LEAD = _StageCase(
    key="tech_lead",
    artifact="tech_lead_raw_response.txt",
    role="Tech lead",
    failure_label="Tech lead failed to produce a valid review",
    bad_raw='{"approved": 3, "feedback": "ok", "summary": "x"}',
    failing_field="approved",
    retries=1,
)
# Two extra attempts: the planner's issue set feeds every downstream issue.
_SPRINT_PLANNER = _StageCase(
    key="sprint_planner",
    artifact="sprint_planner_raw_response.txt",
    role="Sprint planner",
    failure_label="Sprint planner failed to produce valid issues",
    bad_raw='{"issues": "not-a-list", "rationale": 7}',
    failing_field="issues",
    retries=2,
)
_NEW_STAGES = [_PM, _ARCHITECT, _TECH_LEAD]
_ALL_STAGES = [*_NEW_STAGES, _SPRINT_PLANNER]


@pytest.fixture(autouse=True)
def _disable_hax(monkeypatch):
    """Keep the PM ask-user wrapper single-shot."""
    monkeypatch.delenv("HAX_API_KEY", raising=False)


def _log_path(repo_path, case: _StageCase):
    return repo_path.joinpath(".artifacts", "plan", case.artifact)


def _ok_result(case: _StageCase) -> SimpleNamespace:
    if case.key == "product_manager":
        parsed = PRD(
            validated_description="Build a fixture.",
            acceptance_criteria=["AC-1"],
            must_have=["feature-a"],
            nice_to_have=[],
            out_of_scope=[],
        )
    elif case.key == "architect":
        parsed = Architecture(**_ARCH)
    elif case.key == "tech_lead":
        parsed = ReviewResult(approved=True, feedback="Looks good.", summary="Approved.")
    else:
        parsed = SimpleNamespace(
            issues=[
                PlannedIssue(
                    name="issue-a",
                    title="Issue A",
                    description="Do A.",
                    acceptance_criteria=["AC-1"],
                )
            ],
            rationale="split by layer",
        )
    return SimpleNamespace(
        parsed=parsed, result="", text="", error_message=None, is_error=False
    )


def _bad_result(case: _StageCase) -> SimpleNamespace:
    return SimpleNamespace(
        parsed=None,
        result=case.bad_raw,
        text="",
        error_message="Schema validation failed after retries.",
        is_error=True,
        failure_type="schema",
    )


def _empty_result() -> SimpleNamespace:
    return SimpleNamespace(
        parsed=None, result="", text="", error_message=None, is_error=False
    )


def _make_router(results: list) -> MagicMock:
    mock_router = MagicMock()
    mock_router.harness = AsyncMock(side_effect=list(results))
    mock_router.note = MagicMock()
    mock_router.agentfield_server = "http://localhost:9999"
    return mock_router


def _unwrapped(fn):
    return getattr(fn, "_original_func", fn)


async def _invoke_stage(case: _StageCase, repo_path, mock_router: MagicMock):
    import swe_af.reasoners.pipeline as pipeline

    common = {
        "repo_path": str(repo_path),
        "artifacts_dir": ".artifacts",
        "model": "deepseek-v4-flash",
        "ai_provider": "open_code",
    }
    if case.key == "product_manager":
        coro = _unwrapped(pipeline.run_product_manager)(goal="Build a fixture.", **common)
    elif case.key == "architect":
        coro = _unwrapped(pipeline.run_architect)(prd=dict(_PRD), **common)
    elif case.key == "tech_lead":
        coro = _unwrapped(pipeline.run_tech_lead)(prd=dict(_PRD), **common)
    else:
        coro = _unwrapped(pipeline.run_sprint_planner)(
            prd=dict(_PRD), architecture=dict(_ARCH), **common
        )
    with patch.object(pipeline, "router", mock_router):
        return await coro


def _notes(mock_router: MagicMock) -> str:
    return "\n".join(str(call.args[0]) for call in mock_router.note.call_args_list)


@pytest.mark.parametrize("case", _ALL_STAGES, ids=lambda c: c.key)
def test_retries_unparseable_output_and_records_recovery(tmp_path, case) -> None:
    """Invalid responses then a valid one: the stage retries, the validation
    error is fed back into the retry prompt, every failed attempt is retained,
    and the terminal outcome records the recovery.
    """
    attempts = case.retries + 1
    results = [_bad_result(case)] * case.retries + [_ok_result(case)]
    mock_router = _make_router(results)

    result = asyncio.run(_invoke_stage(case, tmp_path, mock_router))

    assert mock_router.harness.await_count == attempts
    assert isinstance(result, dict)

    retry_prompt = mock_router.harness.call_args_list[1].kwargs["prompt"]
    assert "Retry Context" in retry_prompt
    assert case.failing_field in retry_prompt  # from the validation error

    log = _log_path(tmp_path, case).read_text(encoding="utf-8")
    assert case.bad_raw in log
    assert f"attempt 1/{attempts} failed" in log
    assert f"outcome: succeeded on attempt {attempts}/{attempts}" in log


@pytest.mark.parametrize("case", _ALL_STAGES, ids=lambda c: c.key)
def test_bound_exhausted_error_names_stage_error_and_fields(tmp_path, case) -> None:
    """With the bound exhausted, the raised error names the stage, provider,
    model, the failing fields, and where the raw response was written. The log
    retains every failed attempt and records the terminal failure.
    """
    attempts = case.retries + 1
    mock_router = _make_router([_bad_result(case) for _ in range(attempts)])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_invoke_stage(case, tmp_path, mock_router))

    message = str(excinfo.value)
    assert mock_router.harness.await_count == attempts
    assert message.startswith(case.failure_label)
    assert f"after {attempts} attempt(s)" in message
    assert "provider=opencode" in message
    assert "model=deepseek-v4-flash" in message
    assert case.failing_field in message
    assert case.artifact in message

    log = _log_path(tmp_path, case).read_text(encoding="utf-8")
    assert case.bad_raw in log
    assert f"attempt {attempts}/{attempts} failed" in log
    assert f"outcome: FAILED after {attempts} attempt(s)" in log


@pytest.mark.parametrize("case", _ALL_STAGES, ids=lambda c: c.key)
def test_empty_completion_is_not_retried(tmp_path, case) -> None:
    """An empty completion (no parsed object and no raw text at all) is the
    provider/model-mismatch signature, not a schema-quality problem: it must
    fail on the first call at every stage rather than burn the retry bound.
    """
    mock_router = _make_router([_empty_result()])

    with pytest.raises(EmptyHarnessCompletionError) as excinfo:
        asyncio.run(_invoke_stage(case, tmp_path, mock_router))

    assert mock_router.harness.await_count == 1
    assert case.role in str(excinfo.value)
    assert "empty completion" in str(excinfo.value)


def test_unwritable_log_does_not_abort_a_recovering_run(tmp_path) -> None:
    """A retry-log write failure is a diagnostic problem: it is noted, the
    retry continues, and the stage still returns its result.
    """
    case = _SPRINT_PLANNER
    path = _log_path(tmp_path, case)
    path.parent.mkdir(parents=True)
    path.mkdir()  # a directory at the log path makes every append fail

    mock_router = _make_router([_bad_result(case), _ok_result(case)])

    result = asyncio.run(_invoke_stage(case, tmp_path, mock_router))

    assert mock_router.harness.await_count == 2
    assert isinstance(result, dict)
    assert path.is_dir()
    assert "could not write" in _notes(mock_router)


def test_unwritable_log_still_raises_the_schema_failure(tmp_path) -> None:
    """A failed log write must never replace the real failure: the stage keeps
    retrying and the raised error still names the schema failure and reports
    that the raw response could not be written.
    """
    case = _SPRINT_PLANNER
    path = _log_path(tmp_path, case)
    path.parent.mkdir(parents=True)
    path.mkdir()

    mock_router = _make_router(
        [_bad_result(case) for _ in range(case.retries + 1)]
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_invoke_stage(case, tmp_path, mock_router))

    message = str(excinfo.value)
    assert mock_router.harness.await_count == case.retries + 1
    assert f"{case.failure_label} after {case.retries + 1} attempt(s)" in message
    assert case.failing_field in message
    assert "raw response could not be written" in message
    assert "could not write" in _notes(mock_router)

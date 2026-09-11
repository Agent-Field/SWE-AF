"""Regression tests for issue #146: the sprint planner must survive a single
unparseable structured response.

A schema-invalid response must not end the run on its own. These tests stub the
harness with unparseable output and assert that the stage:

- retries a bounded number of times, feeding the validation error back into the
  retry prompt,
- persists the raw response next to the plan artifacts, and
- names the stage, the schema error and the failing fields once the bound is
  exhausted.

Ref: https://github.com/Agent-Field/SWE-AF/issues/146
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe_af.execution.fatal_error import EmptyHarnessCompletionError
from swe_af.reasoners.schemas import PlannedIssue

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
# Valid JSON that fails the SprintPlanOutput schema: issues is not a list and
# rationale is not a string, so the raised error can name the failing fields.
_UNPARSEABLE = '{"issues": "not-a-list", "rationale": 7}'


def _ok_result() -> SimpleNamespace:
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
    return SimpleNamespace(parsed=parsed, result="", error_message=None, is_error=False)


def _bad_result(raw: str = _UNPARSEABLE) -> SimpleNamespace:
    return SimpleNamespace(
        parsed=None,
        result=raw,
        error_message="Schema validation failed after retries.",
        is_error=True,
        failure_type="schema",
    )


def _make_router(results: list) -> MagicMock:
    mock_router = MagicMock()
    mock_router.harness = AsyncMock(side_effect=list(results))
    mock_router.note = MagicMock()
    mock_router.agentfield_server = "http://localhost:9999"
    return mock_router


async def _call_sprint_planner(repo_path, mock_router: MagicMock, **kwargs) -> dict:
    import swe_af.reasoners.pipeline as pipeline

    fn = getattr(
        pipeline.run_sprint_planner,
        "_original_func",
        pipeline.run_sprint_planner,
    )
    with patch.object(pipeline, "router", mock_router):
        return await fn(
            prd=dict(_PRD),
            architecture=dict(_ARCH),
            repo_path=str(repo_path),
            artifacts_dir=".artifacts",
            model="deepseek-v4-flash",
            ai_provider="open_code",
            **kwargs,
        )


def test_retries_unparseable_output_and_retains_raw_response(tmp_path) -> None:
    """Two invalid responses then a valid one: the call is retried (default
    bound = 2), the validation error is fed back into the retry prompt, and the
    raw response is left next to the plan artifacts.
    """
    mock_router = _make_router([_bad_result(), _bad_result(), _ok_result()])

    result = asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    assert mock_router.harness.await_count == 3
    assert result["rationale"] == "split by layer"

    retry_prompt = mock_router.harness.call_args_list[1].kwargs["prompt"]
    assert "Retry Context" in retry_prompt
    assert "issues" in retry_prompt  # failing field from the validation error

    raw_path = tmp_path / ".artifacts" / "plan" / "sprint_planner_raw_response.txt"
    assert raw_path.exists(), "raw response must be persisted on parse failure"
    assert _UNPARSEABLE in raw_path.read_text(encoding="utf-8")


def test_bound_exhausted_error_names_stage_error_and_fields(tmp_path) -> None:
    """With the bound exhausted, the raised error names the stage, provider,
    model, the failing fields, and where the raw response was written.
    ``max_schema_retries`` is configurable per call (here reduced to 1).
    """
    mock_router = _make_router([_bad_result(), _bad_result()])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_call_sprint_planner(tmp_path, mock_router, max_schema_retries=1))

    message = str(excinfo.value)
    assert mock_router.harness.await_count == 2
    assert message.startswith("Sprint planner failed to produce valid issues")
    assert "provider=opencode" in message
    assert "model=deepseek-v4-flash" in message
    assert "issues" in message  # failing field from schema validation
    assert "sprint_planner_raw_response.txt" in message


def test_empty_completion_is_not_retried(tmp_path) -> None:
    """An empty completion (no parsed object and no raw text at all) is the
    provider/model-mismatch signature, not a schema-quality problem: it must
    fail on the first call rather than burn the retry bound.
    """
    empty = SimpleNamespace(
        parsed=None, result="", text="", error_message=None, is_error=False
    )
    mock_router = _make_router([empty])

    with pytest.raises(EmptyHarnessCompletionError):
        asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    assert mock_router.harness.await_count == 1

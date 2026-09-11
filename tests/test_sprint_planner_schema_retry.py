"""Regression tests for issue #146: the sprint planner must survive a single
unparseable structured response.

A schema-invalid response must not end the run on its own. These tests stub the
harness with unparseable output and assert that the stage:

- retries a bounded number of times, feeding the validation error back into the
  retry prompt,
- appends every failed attempt and the terminal outcome to the retry log next
  to the plan artifacts, so a reader can tell recovery from death,
- names the stage, the schema error and the failing fields once the bound is
  exhausted, and
- treats an unwritable retry log as a diagnostic problem, never as the failure.

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
# Distinct invalid payloads so the tests can prove each failed attempt survives
# in the retry log. All three fail validation on the ``issues`` field.
_INVALID_ATTEMPT_1 = '{"issues": "not-a-list", "rationale": 7}'
_INVALID_ATTEMPT_2 = '{"issues": 42, "rationale": "ok"}'
_INVALID_ATTEMPT_3 = '{"issues": {"nested": true}, "rationale": "ok"}'

# The retry log lives next to the other plan artifacts.
_LOG_PARTS = (".artifacts", "plan", "sprint_planner_raw_response.txt")


def _log_path(tmp_path):
    return tmp_path.joinpath(*_LOG_PARTS)


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


def _bad_result(raw: str) -> SimpleNamespace:
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


async def _call_sprint_planner(repo_path, mock_router: MagicMock) -> dict:
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
        )


def _notes(mock_router: MagicMock) -> str:
    return "\n".join(str(call.args[0]) for call in mock_router.note.call_args_list)


def test_retries_unparseable_output_and_records_recovery(tmp_path) -> None:
    """Two invalid responses then a valid one: the call is retried (default
    bound = 2), the validation error is fed back into the retry prompt, every
    failed attempt is retained, and the terminal outcome records the recovery.
    """
    mock_router = _make_router(
        [_bad_result(_INVALID_ATTEMPT_1), _bad_result(_INVALID_ATTEMPT_2), _ok_result()]
    )

    result = asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    assert mock_router.harness.await_count == 3
    assert result["rationale"] == "split by layer"

    retry_prompt = mock_router.harness.call_args_list[1].kwargs["prompt"]
    assert "Retry Context" in retry_prompt
    assert "issues" in retry_prompt  # failing field from the validation error

    log = _log_path(tmp_path).read_text(encoding="utf-8")
    assert _INVALID_ATTEMPT_1 in log
    assert _INVALID_ATTEMPT_2 in log
    assert "attempt 1/3 failed" in log
    assert "attempt 2/3 failed" in log
    assert "outcome: succeeded on attempt 3/3" in log


def test_bound_exhausted_error_names_stage_error_and_fields(tmp_path) -> None:
    """With the bound exhausted, the raised error names the stage, provider,
    model, the failing fields, and where the raw response was written. The log
    retains every failed attempt and records the terminal failure.
    """
    payloads = [_INVALID_ATTEMPT_1, _INVALID_ATTEMPT_2, _INVALID_ATTEMPT_3]
    mock_router = _make_router([_bad_result(payload) for payload in payloads])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    message = str(excinfo.value)
    assert mock_router.harness.await_count == 3
    assert message.startswith("Sprint planner failed to produce valid issues")
    assert "after 3 attempt(s)" in message
    assert "provider=opencode" in message
    assert "model=deepseek-v4-flash" in message
    assert "issues" in message  # failing field from schema validation
    assert "sprint_planner_raw_response.txt" in message

    log = _log_path(tmp_path).read_text(encoding="utf-8")
    for payload in payloads:
        assert payload in log
    assert "outcome: FAILED after 3 attempt(s)" in log


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


def test_unwritable_log_does_not_abort_a_recovering_run(tmp_path) -> None:
    """A retry-log write failure is a diagnostic problem: it is noted, the
    retry continues, and the stage still returns its result.
    """
    path = _log_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.mkdir()  # a directory at the log path makes every append fail

    mock_router = _make_router([_bad_result(_INVALID_ATTEMPT_1), _ok_result()])

    result = asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    assert mock_router.harness.await_count == 2
    assert result["rationale"] == "split by layer"
    assert path.is_dir()
    assert "could not write" in _notes(mock_router)


def test_unwritable_log_still_raises_the_schema_failure(tmp_path) -> None:
    """A failed log write must never replace the real failure: the stage keeps
    retrying and the raised error still names the schema failure and reports
    that the raw response could not be written.
    """
    path = _log_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.mkdir()

    mock_router = _make_router(
        [
            _bad_result(_INVALID_ATTEMPT_1),
            _bad_result(_INVALID_ATTEMPT_2),
            _bad_result(_INVALID_ATTEMPT_3),
        ]
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_call_sprint_planner(tmp_path, mock_router))

    message = str(excinfo.value)
    assert mock_router.harness.await_count == 3
    assert "Sprint planner failed to produce valid issues after 3 attempt(s)" in message
    assert "issues" in message
    assert "raw response could not be written" in message
    assert "could not write" in _notes(mock_router)

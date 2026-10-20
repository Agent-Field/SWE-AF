"""Regression tests for the QA synthesizer dropping the AI's decision (#113).

``run_qa_synthesizer`` originally called ``router.ai()``, which returns the
validated schema instance directly; reading ``.parsed`` off it raised, the broad
``except`` swallowed that, and the crude tests_passed/review_approved heuristic
replaced the synthesizer's decision on *every* call.

It now goes through ``router.harness()`` like every other agent (so it shares
the runtime's credentials, e.g. a Claude subscription login), which returns a
``HarnessResult`` wrapper carrying ``.parsed``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe_af.execution.coding_loop import run_coding_loop
from swe_af.execution.fatal_error import FatalHarnessError
from swe_af.execution.schemas import (
    DEFAULT_AGENT_MAX_TURNS,
    CodeReviewResult,
    DAGState,
    ExecutionConfig,
    QASynthesisAction,
    QASynthesisResult,
)
from swe_af.reasoners import execution_agents

# Inputs the heuristic fallback resolves to APPROVE, so any other action in the
# result can only have come from the synthesizer itself.
FALLBACK_APPROVES = {
    "qa_result": {"passed": True},
    "review_result": {"approved": True, "blocking": False},
}


def _harness_result(parsed) -> SimpleNamespace:
    return SimpleNamespace(parsed=parsed, is_error=False, error_message=None, result="")


def _error_notes(router: MagicMock) -> list:
    return [
        c for c in router.note.call_args_list if "error" in c.kwargs.get("tags", [])
    ]


async def test_ai_decision_wins_over_heuristic_fallback(monkeypatch) -> None:
    """A BLOCK from the synthesizer survives inputs the fallback would approve."""
    decision = QASynthesisResult(
        action=QASynthesisAction.BLOCK,
        summary="Tests pass but the fix regresses the public API.",
        stuck=True,
    )
    router = MagicMock(harness=AsyncMock(return_value=_harness_result(decision)))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-7", **FALLBACK_APPROVES
    )

    assert router.harness.await_args.kwargs["schema"] is QASynthesisResult
    assert out["action"] == QASynthesisAction.BLOCK
    assert out["summary"] == decision.summary
    assert out["stuck"] is True
    assert out["iteration_id"] == "iter-7"
    assert _error_notes(router) == []


async def test_non_schema_response_falls_back_and_says_so(monkeypatch) -> None:
    """An unparseable response reaches the heuristic, but not silently.

    Issue #113 was diagnosed from the note this path emits, so the fallback
    must stay loud.
    """
    router = MagicMock(harness=AsyncMock(return_value=_harness_result(None)))
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[], iteration_id="iter-8", **FALLBACK_APPROVES
    )

    router.harness.assert_awaited_once()
    assert out["action"] == QASynthesisAction.APPROVE
    assert _error_notes(router), "operators lose their only signal for this path"


@pytest.fixture
def clean_model_env(monkeypatch):
    for key in (
        "SWE_DEFAULT_RUNTIME",
        "SWE_DEFAULT_MODEL",
        "AI_MODEL",
        "HARNESS_MODEL",
        "SWE_MODEL_LOW",
        "SWE_MODEL_MED",
        "SWE_MODEL_HIGH",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "INFRON_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "api_key")


@pytest.mark.parametrize(
    "runtime,models,env,provider,expected_model",
    [
        ("claude_code", None, {}, "claude-code", "haiku"),
        (
            "open_code",
            None,
            {},
            "opencode",
            "openrouter/deepseek/deepseek-v4-flash-0731",
        ),
        ("codex", None, {}, "codex", "gpt-5.3-codex"),
        ("codex", None, {"SWE_CODEX_AUTH_MODE": "chatgpt"}, "codex", "gpt-5.5"),
        (
            "claude_code",
            {"qa_synthesizer": "claude-sonnet-5-5"},
            {},
            "claude-code",
            "claude-sonnet-5-5",
        ),
        (
            "open_code",
            {"qa_synthesizer": "minimax-global-openai/MiniMax-M3"},
            {},
            "opencode",
            "minimax-global-openai/MiniMax-M3",
        ),
        ("codex", {"qa_synthesizer": "gpt-5.5"}, {}, "codex", "gpt-5.5"),
        ("claude_code", None, {"SWE_DEFAULT_MODEL": "sonnet"}, "claude-code", "sonnet"),
        (
            "claude_code",
            None,
            {"SWE_MODEL_LOW": "claude-haiku-4-5-20251001"},
            "claude-code",
            "claude-haiku-4-5-20251001",
        ),
        (
            "open_code",
            None,
            {"HARNESS_MODEL": "openrouter/minimax/minimax-m2.6"},
            "opencode",
            "openrouter/minimax/minimax-m2.6",
        ),
        (
            "open_code",
            None,
            {"INFRON_API_KEY": "test-key"},
            "opencode",
            "infron/deepseek/deepseek-v4-flash-0731",
        ),
        (
            "claude_code",
            {"default": "sonnet", "qa_synthesizer": "opus"},
            {"SWE_MODEL_LOW": "haiku"},
            "claude-code",
            "opus",
        ),
    ],
)
async def test_configured_runtime_model_reaches_harness(
    monkeypatch,
    clean_model_env,
    runtime,
    models,
    env,
    provider,
    expected_model,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = ExecutionConfig(runtime=runtime, models=models)
    decision = QASynthesisResult(action="block", stuck=True)
    router = MagicMock(
        harness=AsyncMock(return_value=_harness_result(decision)),
        ai=AsyncMock(
            side_effect=AssertionError("must use runtime harness credentials")
        ),
    )
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        iteration_history=[],
        model=config.qa_synthesizer_model,
        ai_provider=config.ai_provider,
        **FALLBACK_APPROVES,
    )

    router.harness.assert_awaited_once()
    router.ai.assert_not_called()
    assert router.harness.await_args.kwargs["provider"] == provider
    assert router.harness.await_args.kwargs["model"] == expected_model
    assert out["action"] == "block"
    assert out["stuck"] is True


@pytest.mark.parametrize(
    "runtime,model,expected_model",
    [
        ("claude", "anthropic/claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
        (
            "claude_code",
            "anthropic/claude-sonnet-4-5-20250929",
            "claude-sonnet-4-5-20250929",
        ),
        (
            "claude-code",
            "anthropic/claude-sonnet-4-5-20250929",
            "claude-sonnet-4-5-20250929",
        ),
        ("codex", "openai/gpt-5.3-codex", "gpt-5.3-codex"),
        (
            "opencode",
            "anthropic/claude-haiku-4-5-20251001",
            "anthropic/claude-haiku-4-5-20251001",
        ),
        ("open_code", "openai/gpt-4o", "openai/gpt-4o"),
    ],
)
async def test_direct_provider_model_ids_remain_usable(
    monkeypatch,
    runtime,
    model,
    expected_model,
) -> None:
    """LiteLLM provider prefixes must not become part of a native CLI model id."""
    router = MagicMock(
        harness=AsyncMock(
            return_value=_harness_result(
                QASynthesisResult(action="block", stuck=True),
            )
        )
    )
    monkeypatch.setattr(execution_agents, "router", router)

    await execution_agents.run_qa_synthesizer(
        iteration_history=[],
        model=model,
        ai_provider=runtime,
        **FALLBACK_APPROVES,
    )

    router.harness.assert_awaited_once()
    assert router.harness.await_args.kwargs["model"] == expected_model


@pytest.mark.parametrize(
    "worktree,artifacts,permission,expected_cwd,expected_permission",
    [
        ("/repo/worktree", "/artifacts", "auto", "/repo/worktree", "auto"),
        ("", "/artifacts", "plan", "/artifacts", "plan"),
        ("", "", "", None, None),
    ],
)
async def test_harness_options_and_judge_tools(
    monkeypatch,
    worktree,
    artifacts,
    permission,
    expected_cwd,
    expected_permission,
) -> None:
    async def harness(*args, schema, **kwargs):
        if schema is CodeReviewResult:
            return _harness_result(CodeReviewResult(approved=True))
        return _harness_result(QASynthesisResult(action="block"))

    router = MagicMock(harness=AsyncMock(side_effect=harness))
    monkeypatch.setattr(execution_agents, "router", router)
    await execution_agents.run_code_reviewer(
        worktree_path=worktree,
        coder_result={},
        issue={},
    )
    reviewer_tools = set(router.harness.await_args.kwargs["tools"])
    router.harness.reset_mock()

    await execution_agents.run_qa_synthesizer(
        iteration_history=[],
        worktree_path=worktree,
        artifacts_dir=artifacts,
        permission_mode=permission,
        **FALLBACK_APPROVES,
    )

    router.harness.assert_awaited_once()
    options = router.harness.await_args.kwargs
    assert options["cwd"] == expected_cwd
    assert options["permission_mode"] == expected_permission
    assert options["max_turns"] == DEFAULT_AGENT_MAX_TURNS
    assert options["schema"] is QASynthesisResult
    assert options["system_prompt"] == execution_agents.QA_SYNTHESIZER_SYSTEM_PROMPT
    assert set(options["tools"]) == {"Read", "Write", "Glob", "Grep"}
    assert set(options["tools"]) <= reviewer_tools


@pytest.mark.parametrize("failure", ["unparsed", "exception", "nonfatal_error"])
@pytest.mark.parametrize(
    "qa,review,action",
    [
        ({"passed": True}, {"approved": True, "blocking": False}, "approve"),
        ({"passed": True}, {"approved": True, "blocking": True}, "block"),
        ({"passed": False}, {"approved": True}, "fix"),
        ({}, {}, "fix"),
    ],
)
async def test_recoverable_failures_keep_heuristic_and_error_note(
    monkeypatch,
    failure,
    qa,
    review,
    action,
) -> None:
    result = _harness_result(None)
    if failure == "nonfatal_error":
        result.is_error = True
        result.error_message = "provider temporarily unavailable"
    router = MagicMock(
        harness=AsyncMock(
            return_value=result,
            side_effect=RuntimeError("temporary failure")
            if failure == "exception"
            else None,
        )
    )
    monkeypatch.setattr(execution_agents, "router", router)

    out = await execution_agents.run_qa_synthesizer(
        qa_result=qa,
        review_result=review,
        iteration_history=[],
        iteration_id="fallback-1",
    )

    router.harness.assert_awaited_once()
    assert out["action"] == action
    assert out["stuck"] is False
    assert out["iteration_id"] == "fallback-1"
    assert set(out) == {"action", "summary", "stuck", "iteration_id"}
    assert "Synthesizer failed" in out["summary"]
    assert _error_notes(router)


@pytest.mark.parametrize("raised", [False, True])
@pytest.mark.parametrize("parsed", [None, QASynthesisResult(action="approve")])
async def test_fatal_harness_error_escapes_even_with_parsed_output(
    monkeypatch,
    raised,
    parsed,
) -> None:
    message = "Credit balance is too low"
    result = _harness_result(parsed)
    result.is_error = True
    result.error_message = message
    router = MagicMock(
        harness=AsyncMock(
            return_value=result,
            side_effect=FatalHarnessError(message) if raised else None,
        )
    )
    monkeypatch.setattr(execution_agents, "router", router)

    with pytest.raises(FatalHarnessError, match=message):
        await execution_agents.run_qa_synthesizer(
            iteration_history=[],
            **FALLBACK_APPROVES,
        )

    router.harness.assert_awaited_once()
    assert _error_notes(router) == []


async def test_coding_loop_propagates_synthesizer_fatal_result(
    monkeypatch,
    tmp_path,
    clean_model_env,
) -> None:
    result = _harness_result(None)
    result.is_error = True
    result.error_message = "Credit balance is too low"
    router = MagicMock(harness=AsyncMock(return_value=result))
    monkeypatch.setattr(execution_agents, "router", router)
    calls = []

    async def call_fn(target, **kwargs):
        calls.append(target)
        if target.endswith(".run_coder"):
            return {"files_changed": []}
        if target.endswith(".run_qa"):
            return {"passed": True}
        if target.endswith(".run_code_reviewer"):
            return {"approved": True, "blocking": False}
        if target.endswith(".run_qa_synthesizer"):
            return await execution_agents.run_qa_synthesizer(**kwargs)
        raise AssertionError(f"Unexpected role: {target}")

    with pytest.raises(FatalHarnessError, match="Credit balance is too low"):
        await run_coding_loop(
            issue={"name": "fatal-qa", "guidance": {"needs_deeper_qa": True}},
            dag_state=DAGState(
                repo_path=str(tmp_path),
                artifacts_dir=str(tmp_path / "artifacts"),
            ),
            call_fn=call_fn,
            node_id="test-node",
            config=ExecutionConfig(runtime="claude_code", max_coding_iterations=2),
        )

    router.harness.assert_awaited_once()
    assert calls.count("test-node.run_coder") == 1
    assert calls.count("test-node.run_qa_synthesizer") == 1

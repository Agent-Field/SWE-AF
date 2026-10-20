"""Offline regressions for runtime dispatch through the public fast build."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("claude_code", "claude"),
        ("open_code", "opencode"),
        ("codex", "codex"),
        ("command_code", "command_code"),
    ],
)
async def test_public_fast_build_dispatches_selected_runtime(
    monkeypatch, tmp_path, runtime, expected
):
    import swe_af.fast.app as fast_app

    calls = []

    async def offline_call(target, **kwargs):
        calls.append((target, kwargs))
        if target.endswith("fast_plan_tasks"):
            return {"result": {"tasks": [], "rationale": "offline"}}
        if target.endswith("fast_execute_tasks"):
            return {"result": {"task_results": [], "completed_count": 0,
                               "failed_count": 0, "timed_out": False}}
        if target.endswith("fast_verify"):
            return {"result": {"passed": True}}
        return {"result": {"success": False}}

    monkeypatch.setattr(fast_app, "_prepare_repo", lambda *args: None)
    monkeypatch.setattr(fast_app.app, "call", offline_call)
    monkeypatch.setattr(fast_app.app, "note", MagicMock())
    build = getattr(fast_app.build, "_original_func", fast_app.build)

    result = await build(
        goal="Offline routing check",
        repo_path=str(tmp_path),
        config={"runtime": runtime, "enable_github_pr": False},
    )

    assert result["success"] is True
    assert [target for target, _ in calls] == [
        f"{fast_app.NODE_ID}.{stage}"
        for stage in ("run_git_init", "fast_plan_tasks", "fast_execute_tasks",
                      "fast_verify", "run_repo_finalize")
    ]
    assert [kwargs["ai_provider"] for _, kwargs in calls] == [expected] * 5


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("claude", "claude"),
        ("claude-code", "claude"),
        ("opencode", "opencode"),
        ("command_code", "command_code"),
        ("commandcode", "command_code"),
        ("command-code", "command_code"),
        ("cmd", "command_code"),
        (" COMMAND-CODE ", "command_code"),
        ("other", "opencode"),
        ("", "opencode"),
    ],
)
def test_private_runtime_mapper_preserves_aliases_and_unknown_fallback(runtime, expected):
    from swe_af.fast.app import _runtime_to_provider

    assert _runtime_to_provider(runtime) == expected

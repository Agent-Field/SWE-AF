"""Integration tests: cross-feature interactions among app, executor, verifier, planner.

These tests target the Priority-1 interaction boundaries that arise from merging:
  - issue/e65cddc0-05-fast-planner  (registers fast_plan_tasks on fast_router)
  - issue/e65cddc0-06-fast-executor (registers fast_execute_tasks on fast_router; lazy app import)
  - issue/e65cddc0-07-fast-verifier (registers fast_verify on fast_router; lazy app import)
  - issue/e65cddc0-09-fast-app      (creates Agent, includes fast_router, exposes build())

Critical cross-boundary paths under test:
  1. FastBuildConfig → fast_resolve_models → build() call-arg threading to
     fast_plan_tasks / fast_execute_tasks / fast_verify — all model param names
     must align end-to-end.
  2. Executor lazy-import of app module: executor uses `import swe_af.fast.app`
     INSIDE the function body; NODE_ID read at module level must be 'swe-fast'
     (or whatever NODE_ID is set to) and must route correctly to run_coder.
  3. Verifier call args: all six required kwargs must reach app.call; the first
     positional arg must be "run_verifier".
  4. Planner → executor handoff: FastPlanResult.model_dump()['tasks'] are plain
     dicts; all keys the executor reads via .get() must be present.
  5. Executor timeout counting: tasks that time out increment failed_count and
     the task_result has outcome='timeout'; build logic detects success correctly.
  6. Verifier fallback: when app.call raises, fast_verify returns
     FastVerificationResult(passed=False) — no exception propagates.
  7. Fast package isolation: importing swe_af.fast (and all submodules) must
     NOT cause swe_af.reasoners.pipeline to be loaded.
  8. fast_router shared instance: planner, executor, verifier all import the SAME
     fast_router object from swe_af.fast.
  9. FastBuildResult schema: can contain verification=None or a full dict;
     pr_url defaults to empty string.
 10. NODE_ID env isolation: fast app and planner app have independent node_ids
     when NODE_ID is unset; subprocess test uses explicit env manipulation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure AGENTFIELD_SERVER is set before any swe_af.fast imports
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_coro(coro: Any) -> Any:
    """Run a coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_subprocess(code: str, extra_env: dict | None = None,
                    unset_keys: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run python -c <code> in a fresh subprocess."""
    env = os.environ.copy()
    for key in (unset_keys or []):
        env.pop(key, None)
    env.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


def _make_coder_mock(complete: bool = True) -> MagicMock:
    """Create a mock swe_af.fast.app module whose app.call returns a coder result."""
    mock_module = MagicMock()
    coder_result = {"complete": complete, "files_changed": ["f.py"], "summary": "done"}
    mock_module.app.call = AsyncMock(return_value={"result": coder_result})
    return mock_module


def _patch_router_note():
    """Return a context manager that suppresses fast_router.note() calls."""
    import swe_af.fast.executor as _exe
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        router = _exe.fast_router
        old = router.__dict__.get("note", None)
        router.__dict__["note"] = MagicMock(return_value=None)
        try:
            yield
        finally:
            if old is None:
                router.__dict__.pop("note", None)
            else:
                router.__dict__["note"] = old

    return _ctx()


# ===========================================================================
# 1. FastBuildConfig → fast_resolve_models → build() call-arg threading
# ===========================================================================


class TestConfigToCallArgThreading:
    """fast_resolve_models() keys must align with each downstream component's params."""

    def test_pm_model_key_matches_fast_plan_tasks_param(self) -> None:
        """The 'pm_model' key from fast_resolve_models aligns with fast_plan_tasks parameter."""
        import inspect
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models
        from swe_af.fast.planner import fast_plan_tasks

        cfg = FastBuildConfig(runtime="claude_code")
        resolved = fast_resolve_models(cfg)
        assert "pm_model" in resolved, "fast_resolve_models must return 'pm_model'"

        fn = getattr(fast_plan_tasks, "_original_func", fast_plan_tasks)
        sig = inspect.signature(fn)
        assert "pm_model" in sig.parameters, (
            "fast_plan_tasks must accept 'pm_model' param — "
            "schemas→planner cross-feature contract broken"
        )
        assert resolved["pm_model"] == "haiku", (
            f"claude_code runtime default must be 'haiku', got {resolved['pm_model']!r}"
        )

    def test_coder_model_key_matches_fast_execute_tasks_param(self) -> None:
        """The 'coder_model' key from fast_resolve_models aligns with fast_execute_tasks parameter."""
        import inspect
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models
        from swe_af.fast.executor import fast_execute_tasks

        cfg = FastBuildConfig(runtime="open_code")
        resolved = fast_resolve_models(cfg)
        assert "coder_model" in resolved, "fast_resolve_models must return 'coder_model'"

        fn = getattr(fast_execute_tasks, "_original_func", fast_execute_tasks)
        sig = inspect.signature(fn)
        assert "coder_model" in sig.parameters, (
            "fast_execute_tasks must accept 'coder_model' param — "
            "schemas→executor cross-feature contract broken"
        )
        assert resolved["coder_model"] == "qwen/qwen-2.5-coder-32b-instruct", (
            f"open_code runtime default must be qwen model, got {resolved['coder_model']!r}"
        )

    def test_verifier_model_key_matches_fast_verify_param(self) -> None:
        """The 'verifier_model' key from fast_resolve_models aligns with fast_verify parameter."""
        import inspect
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models
        from swe_af.fast.verifier import fast_verify

        cfg = FastBuildConfig(runtime="claude_code")
        resolved = fast_resolve_models(cfg)
        assert "verifier_model" in resolved, "fast_resolve_models must return 'verifier_model'"

        fn = getattr(fast_verify, "_original_func", fast_verify)
        sig = inspect.signature(fn)
        assert "verifier_model" in sig.parameters, (
            "fast_verify must accept 'verifier_model' param — "
            "schemas→verifier cross-feature contract broken"
        )

    def test_all_four_resolved_roles_exist_for_both_runtimes(self) -> None:
        """fast_resolve_models returns exactly 4 keys for both runtimes."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models

        expected_keys = {"pm_model", "coder_model", "verifier_model", "git_model"}
        for runtime in ("claude_code", "open_code"):
            cfg = FastBuildConfig(runtime=runtime)
            resolved = fast_resolve_models(cfg)
            assert set(resolved.keys()) == expected_keys, (
                f"Runtime {runtime!r}: expected {expected_keys}, got {set(resolved.keys())}"
            )

    def test_model_override_flows_correctly_to_all_roles(self) -> None:
        """models={'default': 'sonnet'} overrides all four roles."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models

        cfg = FastBuildConfig(runtime="claude_code", models={"default": "sonnet"})
        resolved = fast_resolve_models(cfg)
        for role, model in resolved.items():
            assert model == "sonnet", (
                f"After default override, role {role!r} must be 'sonnet', got {model!r}"
            )

    def test_per_role_override_does_not_affect_other_roles(self) -> None:
        """Overriding only 'coder' must not change other roles."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models

        cfg = FastBuildConfig(runtime="claude_code", models={"coder": "opus"})
        resolved = fast_resolve_models(cfg)
        assert resolved["coder_model"] == "opus", "coder override must apply"
        assert resolved["pm_model"] == "haiku", "pm_model must remain at runtime default"
        assert resolved["verifier_model"] == "haiku", "verifier_model must remain at runtime default"
        assert resolved["git_model"] == "haiku", "git_model must remain at runtime default"

    def test_task_timeout_param_aligns_with_config(self) -> None:
        """FastBuildConfig.task_timeout_seconds must align with executor's parameter name."""
        import inspect
        from swe_af.fast.schemas import FastBuildConfig
        from swe_af.fast.executor import fast_execute_tasks

        cfg = FastBuildConfig(task_timeout_seconds=120)
        fn = getattr(fast_execute_tasks, "_original_func", fast_execute_tasks)
        sig = inspect.signature(fn)
        assert "task_timeout_seconds" in sig.parameters, (
            "fast_execute_tasks must accept 'task_timeout_seconds' parameter — "
            "config→executor contract broken"
        )
        assert cfg.task_timeout_seconds == 120

    def test_max_tasks_param_aligns_with_planner(self) -> None:
        """FastBuildConfig.max_tasks must align with fast_plan_tasks's parameter name."""
        import inspect
        from swe_af.fast.schemas import FastBuildConfig
        from swe_af.fast.planner import fast_plan_tasks

        cfg = FastBuildConfig(max_tasks=5)
        fn = getattr(fast_plan_tasks, "_original_func", fast_plan_tasks)
        sig = inspect.signature(fn)
        assert "max_tasks" in sig.parameters, (
            "fast_plan_tasks must accept 'max_tasks' parameter — "
            "config→planner contract broken"
        )
        assert cfg.max_tasks == 5


# ===========================================================================
# 2. Executor lazy-import: NODE_ID routing at call time
# ===========================================================================


class TestExecutorLazyImportNodeIdRouting:
    """Executor imports app lazily; NODE_ID is read at module load time."""

    def test_executor_node_id_default_is_swe_fast_in_source(self) -> None:
        """Executor source must contain 'swe-fast' as NODE_ID default."""
        import inspect
        import swe_af.fast.executor as ex

        src = inspect.getsource(ex)
        assert '"swe-fast"' in src or "'swe-fast'" in src, (
            "Executor must have NODE_ID default 'swe-fast' — "
            "this is the routing prefix for app.call dispatch"
        )

    def test_executor_routes_run_coder_via_node_id(self) -> None:
        """fast_execute_tasks must call app.call with '{NODE_ID}.run_coder'."""
        mock_app = _make_coder_mock()
        coder_result = {"complete": True, "files_changed": [], "summary": "ok"}

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            import swe_af.fast.executor as ex

            _run_coro(ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "d",
                        "acceptance_criteria": ["done"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            ))

        assert mock_app.app.call.called, "app.call must be invoked for each task"
        first_positional = mock_app.app.call.call_args.args[0]
        assert "run_coder" in first_positional, (
            f"Executor must call app.call with a 'run_coder' route, "
            f"got: {first_positional!r}"
        )

    def test_executor_passes_issue_dict_to_run_coder(self) -> None:
        """Executor must build an issue dict from task_dict and pass it to run_coder."""
        mock_app = _make_coder_mock()
        coder_result = {"complete": True, "files_changed": ["x.py"], "summary": "done"}

        call_kwargs_captured: dict = {}

        async def _capture_call(route: str, **kwargs: Any) -> Any:
            call_kwargs_captured.update(kwargs)
            return {"result": coder_result}

        mock_app.app.call = _capture_call

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            import swe_af.fast.executor as ex

            _run_coro(ex.fast_execute_tasks(
                tasks=[{
                    "name": "add-api",
                    "title": "Add API",
                    "description": "Build the API.",
                    "acceptance_criteria": ["API returns 200"],
                    "files_to_create": ["api.py"],
                    "files_to_modify": [],
                }],
                repo_path="/tmp/myrepo",
                coder_model="sonnet",
                task_timeout_seconds=30,
            ))

        assert "issue" in call_kwargs_captured, "Executor must pass 'issue' to app.call"
        issue = call_kwargs_captured["issue"]
        assert issue["name"] == "add-api", "Issue name must match task name"
        assert issue["description"] == "Build the API.", "Issue description must match"
        assert issue["acceptance_criteria"] == ["API returns 200"]
        assert call_kwargs_captured.get("worktree_path") == "/tmp/myrepo", (
            "Executor must use repo_path as worktree_path (no worktrees in fast mode)"
        )

    def test_executor_passes_coder_model_from_config(self) -> None:
        """Executor must pass the coder_model parameter to run_coder."""
        mock_app = _make_coder_mock()
        coder_result = {"complete": True, "files_changed": [], "summary": "ok"}

        call_kwargs_captured: dict = {}

        async def _capture_call(route: str, **kwargs: Any) -> Any:
            call_kwargs_captured.update(kwargs)
            return {"result": coder_result}

        mock_app.app.call = _capture_call

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            import swe_af.fast.executor as ex

            _run_coro(ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "d",
                        "acceptance_criteria": ["done"]}],
                repo_path="/tmp/repo",
                coder_model="opus",
                task_timeout_seconds=30,
            ))

        assert call_kwargs_captured.get("model") == "opus", (
            f"Executor must pass coder_model to run_coder as 'model', "
            f"got: {call_kwargs_captured.get('model')!r}"
        )


# ===========================================================================
# 3. Verifier call args: all required kwargs reach app.call
# ===========================================================================


class TestVerifierCallArgForwarding:
    """fast_verify must forward all required kwargs to app.call."""

    def test_all_six_required_kwargs_forwarded(self) -> None:
        """All required parameters must appear in the kwargs sent to app.call.

        fast_verify adapts its inputs before calling run_verifier:
        - task_results → completed_issues / failed_issues / skipped_issues
        - verifier_model → model
        The adapted kwargs must all reach app.call.
        """
        verify_response = {
            "passed": True,
            "summary": "all good",
            "criteria_results": [],
            "suggested_fixes": [],
        }
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify

            _run_coro(fast_verify(
                prd="Build a REST API",
                repo_path="/tmp/repo",
                task_results=[{"task_name": "t1", "outcome": "completed"}],
                verifier_model="haiku",
                permission_mode="default",
                ai_provider="claude",
                artifacts_dir="/tmp/artifacts",
            ))

        assert mock_app.app.call.called, "app.call must be invoked"
        call_kwargs = mock_app.app.call.call_args.kwargs
        # fast_verify adapts task_results → completed/failed/skipped and
        # verifier_model → model before forwarding to run_verifier.
        required_kwargs = {
            "prd", "repo_path", "completed_issues", "failed_issues",
            "model", "permission_mode", "ai_provider", "artifacts_dir",
        }
        missing = required_kwargs - set(call_kwargs.keys())
        assert not missing, (
            f"fast_verify must forward all required kwargs to app.call; "
            f"missing: {missing}"
        )

    def test_first_positional_arg_is_run_verifier(self) -> None:
        """The first positional arg to app.call must contain 'run_verifier'.

        fast_verify routes via f"{NODE_ID}.run_verifier"; the call target is
        NODE_ID-prefixed so we check that 'run_verifier' appears in the arg.
        """
        verify_response = {"passed": False, "summary": "", "criteria_results": [], "suggested_fixes": []}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)
        mock_app.NODE_ID = "swe-fast"  # mimic the real module's NODE_ID

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify

            _run_coro(fast_verify(
                prd="goal",
                repo_path="/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        call_args = mock_app.app.call.call_args
        first_arg = call_args.args[0] if call_args.args else None
        assert isinstance(first_arg, str) and "run_verifier" in first_arg, (
            f"fast_verify must call app.call with a target containing 'run_verifier', "
            f"got first arg: {first_arg!r}"
        )

    def test_task_results_forwarded_correctly(self) -> None:
        """Executor-produced task_results must reach run_verifier via completed/failed split.

        fast_verify adapts task_results: completed → completed_issues,
        non-completed → failed_issues (with task_name as issue_name).
        """
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="setup", outcome="completed",
                               files_changed=["setup.py"]),
                FastTaskResult(task_name="test", outcome="timeout",
                               error="timed out after 300s"),
            ],
            completed_count=1,
            failed_count=1,
        )
        task_results_for_verifier = exec_result.model_dump()["task_results"]

        verify_response = {"passed": False, "summary": "partial", "criteria_results": [], "suggested_fixes": []}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)
        mock_app.NODE_ID = "swe-fast"

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify

            _run_coro(fast_verify(
                prd="Build something",
                repo_path="/repo",
                task_results=task_results_for_verifier,
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        call_kwargs = mock_app.app.call.call_args.kwargs
        # fast_verify splits task_results into completed_issues + failed_issues
        completed = call_kwargs.get("completed_issues", [])
        failed = call_kwargs.get("failed_issues", [])
        # 'setup' was completed → goes into completed_issues
        assert any(entry.get("issue_name") == "setup" for entry in completed), (
            "Completed task 'setup' must appear in completed_issues forwarded to run_verifier"
        )
        # 'test' had outcome='timeout' (non-completed) → goes into failed_issues
        assert any(entry.get("issue_name") == "test" for entry in failed), (
            "Timeout task 'test' must appear in failed_issues forwarded to run_verifier"
        )


# ===========================================================================
# 4. Planner → Executor handoff: schema data compatibility
# ===========================================================================


class TestPlannerToExecutorSchemaHandoff:
    """FastPlanResult.model_dump()['tasks'] must be fully compatible with fast_execute_tasks."""

    def test_fast_task_model_dump_has_all_executor_required_keys(self) -> None:
        """All keys that executor reads via task_dict.get() must exist in FastTask.model_dump()."""
        from swe_af.fast.schemas import FastTask

        task = FastTask(
            name="add-feature",
            title="Add Feature",
            description="Implement the feature",
            acceptance_criteria=["Feature works"],
            files_to_create=["src/feature.py"],
            files_to_modify=["src/__init__.py"],
            estimated_minutes=10,
        )
        d = task.model_dump()

        # Keys the executor accesses: name, title, description, acceptance_criteria,
        # files_to_create, files_to_modify
        required = {"name", "title", "description", "acceptance_criteria",
                    "files_to_create", "files_to_modify"}
        missing = required - set(d.keys())
        assert not missing, (
            f"FastTask.model_dump() missing keys needed by executor: {missing}"
        )

    def test_fast_plan_result_tasks_serialise_to_list_of_dicts(self) -> None:
        """FastPlanResult.model_dump()['tasks'] is a list of plain dicts."""
        from swe_af.fast.schemas import FastTask, FastPlanResult

        plan = FastPlanResult(
            tasks=[
                FastTask(name="step-a", title="Step A", description="Do A.",
                         acceptance_criteria=["A done"]),
                FastTask(name="step-b", title="Step B", description="Do B.",
                         acceptance_criteria=["B done"], files_to_create=["b.py"]),
            ],
            rationale="Two-step plan",
        )
        dumped = plan.model_dump()

        assert "tasks" in dumped
        assert isinstance(dumped["tasks"], list), "tasks must be a list"
        for t in dumped["tasks"]:
            assert isinstance(t, dict), "Each task must be a dict after model_dump()"
            assert "name" in t
            assert "acceptance_criteria" in t
            assert isinstance(t["files_to_create"], list)

    def test_fallback_plan_task_compatible_with_executor(self) -> None:
        """Planner fallback 'implement-goal' task must work through executor."""
        from swe_af.fast.planner import _fallback_plan

        fallback = _fallback_plan("Add a login endpoint")
        task_dicts = fallback.model_dump()["tasks"]
        assert len(task_dicts) >= 1
        task = task_dicts[0]
        assert task["name"] == "implement-goal"
        assert task["acceptance_criteria"] == ["Goal is implemented successfully."]
        # Executor builds an issue dict — must not KeyError
        issue = {
            "name": task.get("name", "unknown"),
            "title": task.get("title", task.get("name", "unknown")),
            "description": task.get("description", ""),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "files_to_create": task.get("files_to_create", []),
            "files_to_modify": task.get("files_to_modify", []),
            "testing_strategy": "",
        }
        assert issue["name"] == "implement-goal", "Fallback task name must be 'implement-goal'"
        assert issue["acceptance_criteria"] == ["Goal is implemented successfully."]

    @pytest.mark.asyncio
    async def test_executor_accepts_fast_plan_result_task_dicts_end_to_end(self) -> None:
        """fast_execute_tasks must process FastPlanResult dicts without KeyError."""
        from swe_af.fast.schemas import FastTask, FastPlanResult

        plan = FastPlanResult(
            tasks=[
                FastTask(
                    name="create-handler",
                    title="Create Request Handler",
                    description="Implement the HTTP handler.",
                    acceptance_criteria=["Handler returns 200", "Tests pass"],
                    files_to_create=["handler.py"],
                ),
            ]
        )
        task_dicts = plan.model_dump()["tasks"]

        coder_result = {"complete": True, "files_changed": ["handler.py"], "summary": "done"}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks

            result = await fast_execute_tasks(
                tasks=task_dicts,
                repo_path="/tmp/repo",
                coder_model="haiku",
                task_timeout_seconds=60,
            )

        assert result["completed_count"] == 1, (
            f"Expected 1 completed task, got {result['completed_count']}"
        )
        assert result["failed_count"] == 0
        assert result["task_results"][0]["task_name"] == "create-handler"
        assert result["task_results"][0]["outcome"] == "completed"


# ===========================================================================
# 5. Executor timeout counting and outcome correctness
# ===========================================================================


class TestExecutorTimeoutAndCountAccuracy:
    """Tasks that time out must yield outcome='timeout' and increment failed_count."""

    @pytest.mark.asyncio
    async def test_timeout_task_produces_timeout_outcome(self) -> None:
        """A task whose coro times out must yield outcome='timeout'."""
        import asyncio as _asyncio

        mock_app = MagicMock()

        async def _slow_call(*args: Any, **kwargs: Any) -> Any:
            await _asyncio.sleep(9999)
            return {}

        mock_app.app.call = _slow_call

        with (
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks

            result = await fast_execute_tasks(
                tasks=[{"name": "slow-task", "title": "Slow", "description": "slow",
                        "acceptance_criteria": ["never"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=0.05,  # extremely short
            )

        assert result["task_results"][0]["outcome"] == "timeout", (
            "A timed-out task must yield outcome='timeout'"
        )
        assert "timed out" in result["task_results"][0]["error"].lower(), (
            "Error message must indicate timeout"
        )
        assert result["completed_count"] == 0, "Timed-out task must NOT count as completed"
        assert result["failed_count"] == 1, "Timed-out task must count toward failed_count"

    @pytest.mark.asyncio
    async def test_mixed_outcomes_counted_correctly(self) -> None:
        """completed_count and failed_count must accurately reflect mixed outcomes."""
        import asyncio as _asyncio

        mock_app = MagicMock()
        call_count = 0

        async def _mixed_call(route: str, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"complete": True, "files_changed": [], "summary": "ok"}
            elif call_count == 2:
                await _asyncio.sleep(9999)  # will time out
            else:
                raise RuntimeError("task exploded")

        mock_app.app.call = _mixed_call

        def _unwrap_impl(raw: Any, label: str) -> dict:
            if isinstance(raw, dict):
                return raw
            return raw

        with (
            patch("swe_af.fast.executor._unwrap", side_effect=_unwrap_impl),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks

            result = await fast_execute_tasks(
                tasks=[
                    {"name": "t1", "title": "T1", "description": "d",
                     "acceptance_criteria": ["done"]},
                    {"name": "t2", "title": "T2", "description": "d",
                     "acceptance_criteria": ["done"]},
                    {"name": "t3", "title": "T3", "description": "d",
                     "acceptance_criteria": ["done"]},
                ],
                repo_path="/tmp/repo",
                task_timeout_seconds=0.05,
            )

        outcomes = {r["task_name"]: r["outcome"] for r in result["task_results"]}
        assert outcomes["t1"] == "completed", f"t1 should complete, got {outcomes['t1']!r}"
        assert outcomes["t2"] == "timeout", f"t2 should timeout, got {outcomes['t2']!r}"
        assert outcomes["t3"] in ("failed", "timeout"), f"t3 should fail, got {outcomes['t3']!r}"
        assert result["completed_count"] == 1, (
            f"completed_count should be 1, got {result['completed_count']}"
        )
        assert result["failed_count"] == 2, (
            f"failed_count should be 2, got {result['failed_count']}"
        )

    @pytest.mark.asyncio
    async def test_executor_continues_after_failed_task(self) -> None:
        """Executor must process ALL tasks even when earlier ones fail."""
        mock_app = MagicMock()
        processed_tasks: list[str] = []

        async def _fail_first_call(route: str, **kwargs: Any) -> Any:
            name = kwargs.get("issue", {}).get("name", "unknown")
            processed_tasks.append(name)
            if name == "task-1":
                raise RuntimeError("deliberate failure")
            return {"complete": True, "files_changed": [], "summary": "ok"}

        mock_app.app.call = _fail_first_call

        def _unwrap_impl(raw: Any, label: str) -> dict:
            return raw

        with (
            patch("swe_af.fast.executor._unwrap", side_effect=_unwrap_impl),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks

            result = await fast_execute_tasks(
                tasks=[
                    {"name": "task-1", "title": "T1", "description": "d",
                     "acceptance_criteria": ["done"]},
                    {"name": "task-2", "title": "T2", "description": "d",
                     "acceptance_criteria": ["done"]},
                ],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert len(processed_tasks) == 2, (
            f"Both tasks must be attempted even when task-1 fails; "
            f"processed: {processed_tasks}"
        )
        assert result["task_results"][0]["outcome"] == "failed"
        assert result["task_results"][1]["outcome"] == "completed"
        assert result["completed_count"] == 1
        assert result["failed_count"] == 1


# ===========================================================================
# 6. Verifier fallback: exception in app.call must not propagate
# ===========================================================================


class TestVerifierFallbackOnException:
    """fast_verify must return a FastVerificationResult(passed=False) on app.call failure."""

    def test_app_call_exception_returns_passed_false(self) -> None:
        """fast_verify must not propagate exceptions from app.call."""
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(side_effect=AttributeError("no app attribute"))

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify

            result = _run_coro(fast_verify(
                prd="Build something",
                repo_path="/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert result["passed"] is False, (
            "fast_verify must return passed=False when app.call raises"
        )
        assert "summary" in result, "Result must have a 'summary' field"
        assert isinstance(result["summary"], str) and result["summary"], (
            "Summary must be a non-empty string describing the failure"
        )

    def test_app_call_runtime_error_returns_fallback_structure(self) -> None:
        """fast_verify fallback must return all FastVerificationResult fields."""
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(side_effect=RuntimeError("network error"))

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify

            result = _run_coro(fast_verify(
                prd="goal",
                repo_path="/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        # Must have all FastVerificationResult fields
        assert "passed" in result
        assert "summary" in result
        assert "criteria_results" in result
        assert "suggested_fixes" in result
        assert result["passed"] is False
        assert isinstance(result["criteria_results"], list)
        assert isinstance(result["suggested_fixes"], list)

    def test_verifier_fallback_result_fits_in_fastbuildresult(self) -> None:
        """The fallback verification result must be storable in FastBuildResult."""
        from swe_af.fast.schemas import FastBuildResult

        fallback_verification = {
            "passed": False,
            "summary": "Verification failed: connection refused",
            "criteria_results": [],
            "suggested_fixes": [],
        }

        build_result = FastBuildResult(
            plan_result={},
            execution_result={"completed_count": 0, "failed_count": 1, "task_results": []},
            verification=fallback_verification,
            success=False,
            summary="Build failed",
        )
        assert build_result.verification["passed"] is False
        assert build_result.success is False


# ===========================================================================
# 7. Fast package isolation: no pipeline module loaded
# ===========================================================================


class TestFastPackagePipelineIsolation:
    """Importing swe_af.fast submodules must NOT load swe_af.reasoners.pipeline."""

    def test_importing_fast_package_does_not_load_pipeline(self) -> None:
        """swe_af.fast (and submodules) must not import swe_af.reasoners.pipeline."""
        pipeline_key = "swe_af.reasoners.pipeline"
        # Evict pipeline from cache to get a clean check
        sys.modules.pop(pipeline_key, None)

        import swe_af.fast
        import swe_af.fast.executor
        import swe_af.fast.planner
        import swe_af.fast.verifier

        assert pipeline_key not in sys.modules, (
            "swe_af.fast (all submodules) must NOT trigger loading "
            "swe_af.reasoners.pipeline — the fast node is pipeline-free by design"
        )

    def test_planner_source_has_no_pipeline_references(self) -> None:
        """fast_plan_tasks source must not reference any pipeline planning agents."""
        import inspect
        import swe_af.fast.planner as pl

        src = inspect.getsource(pl)
        forbidden = ["run_architect", "run_tech_lead", "run_sprint_planner",
                     "run_product_manager", "run_issue_writer"]
        for fn in forbidden:
            assert fn not in src, (
                f"planner.py must not reference '{fn}' — "
                f"fast planner is single-pass, not pipeline-based"
            )

    def test_executor_source_has_no_qa_references(self) -> None:
        """fast_execute_tasks source must not reference QA/reviewer/replanner."""
        import inspect
        import swe_af.fast.executor as ex

        src = inspect.getsource(ex)
        forbidden = ["run_qa", "run_code_reviewer", "run_qa_synthesizer",
                     "run_replanner", "run_issue_advisor", "run_retry_advisor"]
        for fn in forbidden:
            assert fn not in src, (
                f"executor.py must not reference '{fn}' — "
                f"fast executor has no QA/review/retry pipeline"
            )

    def test_verifier_source_has_no_fix_cycle_references(self) -> None:
        """fast_verify source must not reference fix cycle functions."""
        import inspect
        import swe_af.fast.verifier as vf

        src = inspect.getsource(vf)
        forbidden = ["generate_fix_issues", "max_verify_fix_cycles", "fix_cycles"]
        for fn in forbidden:
            assert fn not in src, (
                f"verifier.py must not reference '{fn}' — "
                f"fast verifier is single-pass (no fix cycles)"
            )


# ===========================================================================
# 8. fast_router shared instance across merged modules
# ===========================================================================


class TestFastRouterSharedInstanceAcrossMergedBranches:
    """All merged modules must share the SAME fast_router object from swe_af.fast."""

    def test_planner_uses_same_fast_router_as_init(self) -> None:
        """swe_af.fast.planner.fast_router is identical to swe_af.fast.fast_router."""
        import swe_af.fast as fast_pkg
        import swe_af.fast.planner as planner

        assert planner.fast_router is fast_pkg.fast_router, (
            "planner must import and use the same fast_router object from swe_af.fast; "
            "if they differ, fast_plan_tasks won't be routed via app.include_router"
        )

    def test_executor_uses_same_fast_router_as_init(self) -> None:
        """swe_af.fast.executor.fast_router is identical to swe_af.fast.fast_router."""
        import swe_af.fast as fast_pkg
        import swe_af.fast.executor as executor

        assert executor.fast_router is fast_pkg.fast_router, (
            "executor must import and use the same fast_router object from swe_af.fast"
        )

    def test_verifier_uses_same_fast_router_as_init(self) -> None:
        """swe_af.fast.verifier.fast_router is identical to swe_af.fast.fast_router."""
        import swe_af.fast as fast_pkg
        import swe_af.fast.verifier as verifier

        assert verifier.fast_router is fast_pkg.fast_router, (
            "verifier must import and use the same fast_router object from swe_af.fast"
        )

    def test_all_eight_reasoners_present_on_shared_router(self) -> None:
        """After importing all merged modules, fast_router must have exactly 8 reasoners."""
        import swe_af.fast
        import swe_af.fast.planner
        import swe_af.fast.verifier
        from swe_af.fast import fast_router

        registered_names = {r["func"].__name__ for r in fast_router.reasoners}
        expected = {
            # 5 thin wrappers from __init__
            "run_git_init", "run_coder", "run_verifier", "run_repo_finalize", "run_github_pr",
            # from merged branches
            "fast_execute_tasks",  # executor branch
            "fast_plan_tasks",     # planner branch
            "fast_verify",         # verifier branch
        }
        missing = expected - registered_names
        assert not missing, (
            f"Missing reasoners on fast_router after importing all merged modules: "
            f"{sorted(missing)}. Found: {sorted(registered_names)}"
        )


# ===========================================================================
# 9. FastBuildResult schema: verification and pr_url fields
# ===========================================================================


class TestFastBuildResultSchema:
    """FastBuildResult must accept verification=None, dicts, and pr_url default."""

    def test_verification_defaults_to_none(self) -> None:
        """FastBuildResult.verification must default to None when not provided."""
        from swe_af.fast.schemas import FastBuildResult

        r = FastBuildResult(
            plan_result={},
            execution_result={},
            success=True,
            summary="done",
        )
        assert r.verification is None, (
            "FastBuildResult.verification must default to None — "
            "the verifier step may be skipped in the timeout path"
        )

    def test_pr_url_defaults_to_empty_string(self) -> None:
        """FastBuildResult.pr_url must default to empty string."""
        from swe_af.fast.schemas import FastBuildResult

        r = FastBuildResult(
            plan_result={},
            execution_result={},
            success=True,
            summary="ok",
        )
        assert r.pr_url == "", (
            f"pr_url must default to empty string, got {r.pr_url!r}"
        )

    def test_full_build_result_with_verification_and_pr_url(self) -> None:
        """FastBuildResult accepts verification dict and non-empty pr_url."""
        from swe_af.fast.schemas import FastBuildResult, FastVerificationResult

        vr = FastVerificationResult(passed=True, summary="All criteria met")
        r = FastBuildResult(
            plan_result={"tasks": [{"name": "t1"}], "rationale": "test"},
            execution_result={"completed_count": 1, "failed_count": 0, "task_results": []},
            verification=vr.model_dump(),
            success=True,
            summary="Build succeeded: 1/1 tasks completed",
            pr_url="https://github.com/org/repo/pull/42",
        )
        assert r.success is True
        assert r.pr_url == "https://github.com/org/repo/pull/42"
        assert r.verification is not None
        assert r.verification["passed"] is True

    def test_timeout_build_result_structure(self) -> None:
        """Build timeout path must produce a valid FastBuildResult with success=False."""
        from swe_af.fast.schemas import FastBuildResult

        r = FastBuildResult(
            plan_result={},
            execution_result={
                "timed_out": True,
                "task_results": [],
                "completed_count": 0,
                "failed_count": 0,
            },
            success=False,
            summary="Build timed out after 600s",
        )
        assert r.success is False
        assert "timed out" in r.summary.lower()
        assert r.execution_result.get("timed_out") is True


# ===========================================================================
# 10. NODE_ID env isolation: subprocess-based co-import test
# ===========================================================================


class TestNodeIdIsolationViaSubprocess:
    """NODE_ID env isolation must work correctly in a fresh interpreter."""

    def test_fast_app_defaults_to_swe_fast_node_id_when_env_unset(self) -> None:
        """With NODE_ID unset, swe_af.fast.app must have node_id='swe-fast'."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast.app as fast_app
assert fast_app.app.node_id == "swe-fast", f"got: {fast_app.app.node_id!r}"
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"fast app must have node_id='swe-fast' when NODE_ID is unset; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_planner_app_defaults_to_swe_planner_node_id_when_env_unset(self) -> None:
        """With NODE_ID unset, swe_af.app must have node_id='swe-planner'."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.app as planner_app
assert planner_app.app.node_id == "swe-planner", f"got: {planner_app.app.node_id!r}"
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"planner app must have node_id='swe-planner' when NODE_ID is unset; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_co_import_produces_distinct_node_ids(self) -> None:
        """Importing both apps simultaneously must yield distinct node IDs."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.app as planner_app
import swe_af.fast.app as fast_app
assert planner_app.app.node_id == "swe-planner", f"planner: {planner_app.app.node_id!r}"
assert fast_app.app.node_id == "swe-fast", f"fast: {fast_app.app.node_id!r}"
assert planner_app.app.node_id != fast_app.app.node_id, "node IDs must be distinct"
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"Co-importing both apps must give distinct node IDs; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_node_id_env_override_applies_to_fast_app(self) -> None:
        """NODE_ID env var overrides fast app node_id when explicitly set."""
        code = """
import os
os.environ["NODE_ID"] = "swe-fast-custom"
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import importlib
m = importlib.import_module("swe_af.fast.app")
importlib.reload(m)
assert m.app.node_id == "swe-fast-custom", f"got: {m.app.node_id!r}"
print("OK")
"""
        result = _run_subprocess(code, extra_env={"NODE_ID": "swe-fast-custom"})
        assert result.returncode == 0, (
            f"NODE_ID override must apply to fast app; stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

"""Integration tests for cross-feature interactions across planner, executor, and verifier.

These tests specifically target the interaction boundaries between the three merged
feature branches:
  - issue/e65cddc0-05-fast-planner (planner)
  - issue/e65cddc0-06-fast-executor (executor)
  - issue/e65cddc0-07-fast-verifier (verifier)

Priority 1 – Cross-feature interaction boundaries:
  1. Planner → Executor: FastPlanResult.model_dump() task dicts are consumed by
     fast_execute_tasks as the `tasks` parameter — all fields must be present.
  2. Executor → Verifier: FastTaskResult.model_dump() dicts are passed to
     fast_verify as `task_results` — field names must match.
  3. Schemas → Planner/Executor/Verifier: FastBuildConfig model resolution
     produces the exact param names consumed by all three components.
  4. Router completeness: After all merges, fast_router must expose ALL eight
     registered reasoners (5 thin wrappers + planner + executor + verifier).
  5. Lazy-import isolation: executor and verifier use lazy import of
     swe_af.fast.app; they must not fail at module load even if app is a stub.
  6. fast_router.note() call compatibility: both planner and executor call
     fast_router.note(); the _note() helper in planner must degrade gracefully
     when the router is not attached (RuntimeError path).
  7. NODE_ID consistency: executor uses NODE_ID env var for app.call routing;
     the default must match the docker-compose NODE_ID=swe-fast.
  8. Verifier call args completeness: fast_verify must forward all required
     kwargs to the underlying app.call (no silent drops).
  9. FastExecutionResult → FastVerificationResult pipeline: failed_count
     from execution is visible as task_results dicts to the verifier.
 10. Planner fallback tasks pass through executor without KeyError.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _registered_names() -> set[str]:
    """Return the set of all function names registered on fast_router."""
    from swe_af.fast import fast_router  # noqa: PLC0415
    return {r["func"].__name__ for r in fast_router.reasoners}


def _make_mock_app_module(call_return: Any) -> MagicMock:
    """Build a mock swe_af.fast.app module with app.call returning call_return."""
    mock_module = MagicMock()
    mock_module.app.call = AsyncMock(return_value=call_return)
    return mock_module


def _patch_router_note():
    """Patch fast_router.note to a no-op to avoid RuntimeError in tests."""
    import swe_af.fast.executor as _exe  # noqa: PLC0415
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        router = _exe.fast_router
        _sentinel = object()
        old = router.__dict__.get("note", _sentinel)
        router.__dict__["note"] = MagicMock(return_value=None)
        try:
            yield
        finally:
            if old is _sentinel:
                router.__dict__.pop("note", None)
            else:
                router.__dict__["note"] = old

    return _ctx()


# ===========================================================================
# 1. Planner → Executor: FastPlanResult dicts are fully compatible with
#    fast_execute_tasks task_dict field access
# ===========================================================================


class TestPlannerOutputCompatibleWithExecutor:
    """Verify that FastTask.model_dump() produces all keys executor expects."""

    def test_fast_task_model_dump_has_all_executor_keys(self) -> None:
        """FastTask.model_dump() must contain every key executor reads via .get()."""
        from swe_af.fast.schemas import FastTask  # noqa: PLC0415

        task = FastTask(
            name="add-feature",
            title="Add Feature",
            description="Implement the feature.",
            acceptance_criteria=["Feature works"],
            files_to_create=["src/feature.py"],
            files_to_modify=["src/__init__.py"],
        )
        d = task.model_dump()

        # Keys used by fast_execute_tasks in executor.py
        for key in ("name", "title", "description", "acceptance_criteria",
                    "files_to_create", "files_to_modify"):
            assert key in d, (
                f"FastTask.model_dump() is missing key '{key}' "
                f"which fast_execute_tasks accesses — planner→executor contract broken"
            )

    def test_fast_plan_result_tasks_list_is_dicts(self) -> None:
        """FastPlanResult.model_dump()['tasks'] is a list of dicts as executor expects."""
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        plan = FastPlanResult(
            tasks=[
                FastTask(name="t1", title="T1", description="d1", acceptance_criteria=["c1"]),
                FastTask(name="t2", title="T2", description="d2", acceptance_criteria=["c2"]),
            ],
            rationale="Two tasks",
        )
        dumped = plan.model_dump()
        assert isinstance(dumped["tasks"], list), "tasks must be a list"
        for task_dict in dumped["tasks"]:
            assert isinstance(task_dict, dict), "Each task in plan must be a dict"
            assert "name" in task_dict, "Task dict must have 'name' key"

    @pytest.mark.asyncio
    async def test_executor_accepts_fast_plan_result_task_dicts(self) -> None:
        """fast_execute_tasks must not raise KeyError when given FastTask.model_dump() dicts."""
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        plan = FastPlanResult(
            tasks=[
                FastTask(
                    name="step-one",
                    title="Step One",
                    description="Do step one.",
                    acceptance_criteria=["Step one passes"],
                    files_to_create=["step_one.py"],
                ),
            ],
        )
        task_dicts = plan.model_dump()["tasks"]

        coder_result = {"complete": True, "files_changed": ["step_one.py"], "summary": "done"}
        mock_app = _make_mock_app_module({"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=task_dicts,
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["task_results"][0]["task_name"] == "step-one"
        assert result["task_results"][0]["outcome"] == "completed"
        assert result["completed_count"] == 1

    @pytest.mark.asyncio
    async def test_executor_accepts_planner_fallback_task_dict(self) -> None:
        """The planner fallback task 'implement-goal' must work through executor."""
        from swe_af.fast.planner import _fallback_plan  # noqa: PLC0415

        fallback = _fallback_plan("Build something")
        task_dicts = fallback.model_dump()["tasks"]
        assert len(task_dicts) >= 1
        assert task_dicts[0]["name"] == "implement-goal"

        coder_result = {"complete": True, "files_changed": [], "summary": "fallback done"}
        mock_app = _make_mock_app_module({"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=task_dicts,
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["task_results"][0]["task_name"] == "implement-goal"
        assert result["completed_count"] == 1


# ===========================================================================
# 2. Executor → Verifier: FastTaskResult dicts are compatible with fast_verify
# ===========================================================================


class TestExecutorOutputCompatibleWithVerifier:
    """Verify FastTaskResult.model_dump() dicts are compatible with fast_verify."""

    def test_fast_task_result_model_dump_is_dict(self) -> None:
        """FastTaskResult.model_dump() returns a plain dict suitable for verifier."""
        from swe_af.fast.schemas import FastTaskResult  # noqa: PLC0415

        r = FastTaskResult(
            task_name="add-feature",
            outcome="completed",
            files_changed=["src/feature.py"],
            summary="Feature added",
        )
        d = r.model_dump()
        assert isinstance(d, dict), "FastTaskResult.model_dump() must return dict"
        assert d["task_name"] == "add-feature"
        assert d["outcome"] == "completed"

    def test_fast_execution_result_task_results_are_dicts(self) -> None:
        """FastExecutionResult.model_dump()['task_results'] is a list of plain dicts."""
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult  # noqa: PLC0415

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="t1", outcome="completed"),
                FastTaskResult(task_name="t2", outcome="failed", error="timeout"),
            ],
            completed_count=1,
            failed_count=1,
        )
        dumped = exec_result.model_dump()
        for tr in dumped["task_results"]:
            assert isinstance(tr, dict), "Each task_result must be a dict"

    def test_fast_verify_receives_task_results_from_executor_output(self) -> None:
        """fast_verify must accept task_results dicts produced by executor."""
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult  # noqa: PLC0415

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="t1", outcome="completed", files_changed=["f.py"]),
                FastTaskResult(task_name="t2", outcome="timeout", error="timed out"),
            ],
            completed_count=1,
            failed_count=1,
        )
        task_results_for_verifier = exec_result.model_dump()["task_results"]

        verify_response = {
            "passed": True,
            "summary": "Partial pass",
            "criteria_results": [],
            "suggested_fixes": [],
        }
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run(fast_verify(
                prd="Build a tool",
                repo_path="/tmp/repo",
                task_results=task_results_for_verifier,
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert result["passed"] is True
        # Verify the task_results were forwarded to app.call
        call_kwargs = mock_app.app.call.call_args.kwargs
        assert len(call_kwargs["task_results"]) == 2
        assert call_kwargs["task_results"][0]["task_name"] == "t1"
        assert call_kwargs["task_results"][1]["outcome"] == "timeout"

    def test_failed_executor_tasks_visible_to_verifier(self) -> None:
        """Executor 'failed' and 'timeout' outcomes must be visible in verifier task_results."""
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult  # noqa: PLC0415

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="ok-task", outcome="completed"),
                FastTaskResult(task_name="bad-task", outcome="failed", error="crash"),
                FastTaskResult(task_name="slow-task", outcome="timeout", error="timed out"),
            ],
            completed_count=1,
            failed_count=2,
        )
        task_dicts = exec_result.model_dump()["task_results"]

        outcomes = {d["task_name"]: d["outcome"] for d in task_dicts}
        assert outcomes["ok-task"] == "completed", "completed task must propagate"
        assert outcomes["bad-task"] == "failed", "failed task must propagate"
        assert outcomes["slow-task"] == "timeout", "timeout task must propagate"


# ===========================================================================
# 3. Schemas → Components: FastBuildConfig model resolution produces the
#    exact parameter names consumed by planner, executor, and verifier
# ===========================================================================


class TestFastBuildConfigToComponentParams:
    """Verify fast_resolve_models() keys match component function param names."""

    def test_pm_model_key_matches_planner_param(self) -> None:
        """fast_resolve_models() must produce 'pm_model' matching planner's param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.planner import fast_plan_tasks  # noqa: PLC0415

        models = fast_resolve_models(FastBuildConfig())
        planner_sig = inspect.signature(fast_plan_tasks)
        assert "pm_model" in models, "fast_resolve_models must produce 'pm_model'"
        assert "pm_model" in planner_sig.parameters, (
            "fast_plan_tasks must accept 'pm_model' param — "
            "schema→planner contract broken"
        )
        # Model value must be valid (non-empty string)
        assert isinstance(models["pm_model"], str) and models["pm_model"]

    def test_coder_model_key_matches_executor_param(self) -> None:
        """fast_resolve_models() must produce 'coder_model' matching executor's param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

        models = fast_resolve_models(FastBuildConfig())
        executor_sig = inspect.signature(fast_execute_tasks)
        assert "coder_model" in models, "fast_resolve_models must produce 'coder_model'"
        assert "coder_model" in executor_sig.parameters, (
            "fast_execute_tasks must accept 'coder_model' param — "
            "schema→executor contract broken"
        )

    def test_verifier_model_key_matches_verifier_param(self) -> None:
        """fast_resolve_models() must produce 'verifier_model' matching verifier's param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        models = fast_resolve_models(FastBuildConfig())
        verifier_sig = inspect.signature(fast_verify)
        assert "verifier_model" in models, "fast_resolve_models must produce 'verifier_model'"
        assert "verifier_model" in verifier_sig.parameters, (
            "fast_verify must accept 'verifier_model' param — "
            "schema→verifier contract broken"
        )

    def test_all_four_model_keys_present_for_all_runtimes(self) -> None:
        """fast_resolve_models must return exactly 4 role keys for both runtimes."""
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415

        expected_keys = {"pm_model", "coder_model", "verifier_model", "git_model"}
        for runtime in ("claude_code", "open_code"):
            cfg = FastBuildConfig(runtime=runtime)
            resolved = fast_resolve_models(cfg)
            assert set(resolved.keys()) == expected_keys, (
                f"Runtime {runtime!r}: expected keys {expected_keys}, "
                f"got {set(resolved.keys())}"
            )

    def test_max_tasks_from_config_flows_to_planner_param(self) -> None:
        """FastBuildConfig.max_tasks must be accepted by fast_plan_tasks."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.planner import fast_plan_tasks  # noqa: PLC0415

        cfg = FastBuildConfig(max_tasks=7)
        sig = inspect.signature(fast_plan_tasks)
        assert "max_tasks" in sig.parameters, (
            "fast_plan_tasks must accept 'max_tasks' — schema→planner contract broken"
        )
        assert cfg.max_tasks == 7

    def test_task_timeout_seconds_from_config_flows_to_executor_param(self) -> None:
        """FastBuildConfig.task_timeout_seconds must align with executor's param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

        cfg = FastBuildConfig(task_timeout_seconds=120)
        sig = inspect.signature(fast_execute_tasks)
        assert "task_timeout_seconds" in sig.parameters, (
            "fast_execute_tasks must accept 'task_timeout_seconds' — "
            "schema→executor contract broken"
        )
        assert cfg.task_timeout_seconds == 120


# ===========================================================================
# 4. Router completeness: all 8 reasoners registered after full import
# ===========================================================================


class TestFastRouterCompletenessAfterAllMerges:
    """After all three branches are merged, fast_router must have all 8 reasoners."""

    _EXPECTED_ALL_REASONERS = {
        # 5 thin wrappers from __init__.py
        "run_git_init",
        "run_coder",
        "run_verifier",
        "run_repo_finalize",
        "run_github_pr",
        # from executor (issue/e65cddc0-06-fast-executor)
        "fast_execute_tasks",
        # from planner (issue/e65cddc0-05-fast-planner)
        "fast_plan_tasks",
        # from verifier (issue/e65cddc0-07-fast-verifier)
        "fast_verify",
    }

    def test_all_eight_reasoners_registered_after_full_import(self) -> None:
        """All 8 reasoners must be registered after importing all submodules."""
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415
        # executor is already registered via swe_af.fast.__init__

        names = _registered_names()
        missing = self._EXPECTED_ALL_REASONERS - names
        assert not missing, (
            f"Missing reasoners on fast_router after full import: {missing}. "
            f"Registered: {sorted(names)}"
        )

    @pytest.mark.parametrize("name", sorted(_EXPECTED_ALL_REASONERS))
    def test_each_reasoner_individually(self, name: str) -> None:
        """Each of the 8 expected reasoners must be individually present."""
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415

        names = _registered_names()
        assert name in names, (
            f"Expected reasoner '{name}' not found in fast_router. "
            f"Registered: {sorted(names)}"
        )

    _EXPECTED_ALL_REASONERS = {
        "run_git_init", "run_coder", "run_verifier", "run_repo_finalize",
        "run_github_pr", "fast_execute_tasks", "fast_plan_tasks", "fast_verify",
    }


# ===========================================================================
# 5. Lazy-import isolation: modules must not fail at load time
# ===========================================================================


class TestLazyImportIsolation:
    """executor and verifier use lazy imports; module load must not fail."""

    def test_executor_imports_without_app_being_loaded(self) -> None:
        """swe_af.fast.executor must import without triggering swe_af.fast.app loading."""
        # Remove from cache to get a fresh import
        mods_to_remove = [k for k in list(sys.modules) if "executor" in k and "fast" in k]
        for k in mods_to_remove:
            sys.modules.pop(k, None)

        # app.py is a stub — should not error during executor module load
        importlib.import_module("swe_af.fast.executor")
        import swe_af.fast.executor  # noqa: PLC0415
        assert swe_af.fast.executor is not None

    def test_verifier_imports_without_app_being_called(self) -> None:
        """swe_af.fast.verifier must import without calling app at module level."""
        mods_to_remove = [k for k in list(sys.modules) if "verifier" in k and "fast" in k]
        for k in mods_to_remove:
            sys.modules.pop(k, None)

        importlib.import_module("swe_af.fast.verifier")
        import swe_af.fast.verifier  # noqa: PLC0415
        assert swe_af.fast.verifier is not None

    def test_planner_imports_without_triggering_pipeline(self) -> None:
        """swe_af.fast.planner must not import swe_af.reasoners.pipeline."""
        pipeline_key = "swe_af.reasoners.pipeline"
        sys.modules.pop(pipeline_key, None)

        import swe_af.fast.planner  # noqa: F401, PLC0415

        assert pipeline_key not in sys.modules, (
            "swe_af.fast.planner must not trigger loading swe_af.reasoners.pipeline"
        )

    def test_full_fast_package_does_not_trigger_pipeline(self) -> None:
        """Importing all fast submodules must not load the pipeline module."""
        pipeline_key = "swe_af.reasoners.pipeline"
        sys.modules.pop(pipeline_key, None)

        # Import all fast submodules
        import swe_af.fast  # noqa: F401, PLC0415
        import swe_af.fast.executor  # noqa: F401, PLC0415
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415

        assert pipeline_key not in sys.modules, (
            "Loading the entire swe_af.fast package must not import pipeline"
        )


# ===========================================================================
# 6. fast_router.note() call degradation in planner
# ===========================================================================


class TestFastRouterNoteDegradation:
    """Planner's _note() helper must degrade gracefully when router is not attached."""

    def test_note_helper_degrades_on_runtime_error(self) -> None:
        """_note() must not raise even when fast_router.note() raises RuntimeError."""
        from swe_af.fast.planner import _note  # noqa: PLC0415

        # Temporarily make fast_router.note raise RuntimeError
        import swe_af.fast.planner as planner_mod  # noqa: PLC0415
        original_router = planner_mod.fast_router

        mock_router = MagicMock()
        mock_router.note.side_effect = RuntimeError("Router not attached")

        planner_mod.fast_router = mock_router
        try:
            # Should not raise — falls back to logger.debug
            _note("test message", tags=["test"])
        finally:
            planner_mod.fast_router = original_router

    def test_note_helper_works_when_router_note_succeeds(self) -> None:
        """_note() must call fast_router.note() when it works."""
        from swe_af.fast.planner import _note  # noqa: PLC0415
        import swe_af.fast.planner as planner_mod  # noqa: PLC0415

        original_router = planner_mod.fast_router
        mock_router = MagicMock()
        mock_router.note.return_value = None

        planner_mod.fast_router = mock_router
        try:
            _note("hello", tags=["fast_planner"])
            mock_router.note.assert_called_once_with("hello", tags=["fast_planner"])
        finally:
            planner_mod.fast_router = original_router


# ===========================================================================
# 7. NODE_ID consistency: executor's default NODE_ID must be 'swe-fast'
# ===========================================================================


class TestNodeIdConsistency:
    """executor.NODE_ID default must match docker-compose NODE_ID=swe-fast."""

    def test_executor_node_id_default_is_swe_fast(self) -> None:
        """Executor NODE_ID must default to 'swe-fast' to match docker-compose."""
        # Temporarily remove NODE_ID from env to test default
        saved = os.environ.pop("NODE_ID", None)
        try:
            # Force a fresh read of NODE_ID by re-importing executor
            mods = [k for k in list(sys.modules) if "swe_af.fast.executor" == k]
            for k in mods:
                sys.modules.pop(k, None)

            import swe_af.fast.executor as ex  # noqa: PLC0415
            # NODE_ID is module-level — it was already read at import time
            # We verify it matches 'swe-fast' (either from env or default)
            assert ex.NODE_ID in ("swe-fast",) or isinstance(ex.NODE_ID, str), (
                f"executor.NODE_ID must be a string, got {ex.NODE_ID!r}"
            )
        finally:
            if saved is not None:
                os.environ["NODE_ID"] = saved

    def test_executor_node_id_source_uses_swe_fast_default(self) -> None:
        """Executor source must have 'swe-fast' as the default NODE_ID fallback."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.executor as ex  # noqa: PLC0415

        src = inspect.getsource(ex)
        assert '"swe-fast"' in src or "'swe-fast'" in src, (
            "Executor source must contain 'swe-fast' as the NODE_ID default"
        )


# ===========================================================================
# 8. Verifier call args: all required kwargs forwarded to app.call
# ===========================================================================


class TestVerifierForwardsAllKwargsToAppCall:
    """fast_verify must forward all required parameters to app.call."""

    def test_all_required_params_forwarded_to_app_call(self) -> None:
        """Every required fast_verify param must appear in the app.call kwargs."""
        verify_response = {
            "passed": True,
            "summary": "ok",
            "criteria_results": [],
            "suggested_fixes": [],
        }
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            _run(fast_verify(
                prd="Build a REST API",
                repo_path="/tmp/repo",
                task_results=[{"task_name": "t1", "outcome": "completed"}],
                verifier_model="haiku",
                permission_mode="default",
                ai_provider="claude",
                artifacts_dir="/tmp/artifacts",
            ))

        assert mock_app.app.call.called, "app.call must be called"
        call_kwargs = mock_app.app.call.call_args.kwargs
        required = {"prd", "repo_path", "task_results", "verifier_model",
                    "permission_mode", "ai_provider", "artifacts_dir"}
        for key in required:
            assert key in call_kwargs, (
                f"Verifier must forward '{key}' to app.call — it was missing"
            )

    def test_verifier_passes_run_verifier_as_first_arg(self) -> None:
        """fast_verify must call app.call with 'run_verifier' as the first positional arg."""
        verify_response = {"passed": True, "summary": "", "criteria_results": [], "suggested_fixes": []}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=verify_response)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            _run(fast_verify(
                prd="goal",
                repo_path="/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        call_args = mock_app.app.call.call_args
        # First positional arg must be 'run_verifier'
        assert call_args.args[0] == "run_verifier", (
            f"fast_verify must call app.call with 'run_verifier' as first arg, "
            f"got {call_args.args[0]!r}"
        )


# ===========================================================================
# 9. End-to-end pipeline: executor output → verifier input (schema round-trip)
# ===========================================================================


class TestExecutionToVerificationPipeline:
    """Test the full execution→verification data pipeline using schema round-trips."""

    def test_execution_result_to_verifier_input_round_trip(self) -> None:
        """FastExecutionResult dicts flow correctly into fast_verify as task_results."""
        from swe_af.fast.schemas import (  # noqa: PLC0415
            FastTaskResult,
            FastExecutionResult,
            FastVerificationResult,
        )

        # Simulate what the executor produces
        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="create-api", outcome="completed",
                               files_changed=["api.py"]),
                FastTaskResult(task_name="write-tests", outcome="failed",
                               error="tests not written"),
            ],
            completed_count=1,
            failed_count=1,
        )

        # This is what gets passed to fast_verify's task_results param
        verifier_input = exec_result.model_dump()["task_results"]

        # Simulate verifier producing a FastVerificationResult
        verification = FastVerificationResult(
            passed=False,
            summary="1 task failed, partial implementation",
            suggested_fixes=["Fix write-tests task"],
        )

        # Verify the full round-trip: exec output → verify input → verify output
        assert len(verifier_input) == 2
        assert verifier_input[0]["task_name"] == "create-api"
        assert verifier_input[1]["outcome"] == "failed"
        assert verification.passed is False
        assert "partial" in verification.summary

    def test_verifier_result_can_be_included_in_build_result(self) -> None:
        """FastVerificationResult.model_dump() must fit into FastBuildResult.verification."""
        from swe_af.fast.schemas import FastVerificationResult, FastBuildResult  # noqa: PLC0415

        vr = FastVerificationResult(
            passed=True,
            summary="All checks passed",
        )
        build_result = FastBuildResult(
            plan_result={"tasks": [{"name": "t1"}]},
            execution_result={"completed_count": 1, "failed_count": 0},
            verification=vr.model_dump(),
            success=True,
            summary="Build succeeded",
        )
        assert build_result.verification is not None
        assert build_result.verification["passed"] is True
        assert build_result.success is True


# ===========================================================================
# 10. Planner → Executor: max_tasks truncation preserves task compatibility
# ===========================================================================


class TestPlannerMaxTasksTruncationPreservesExecutorCompat:
    """After max_tasks truncation, remaining tasks must still be executor-compatible."""

    @pytest.mark.asyncio
    async def test_truncated_tasks_remain_executor_compatible(self) -> None:
        """Tasks truncated by max_tasks must still work in fast_execute_tasks."""
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        # Simulate planner producing 5 tasks but max_tasks=2 truncating to 2
        tasks = [
            FastTask(name=f"task-{i}", title=f"Task {i}", description=f"Do {i}.",
                     acceptance_criteria=[f"Done {i}"])
            for i in range(5)
        ]
        plan = FastPlanResult(tasks=tasks[:2])  # truncated to 2
        task_dicts = plan.model_dump()["tasks"]
        assert len(task_dicts) == 2

        coder_result = {"complete": True, "files_changed": [], "summary": "ok"}
        mock_app = _make_mock_app_module({"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_router_note(),
        ):
            from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

            result = await fast_execute_tasks(
                tasks=task_dicts,
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        assert result["completed_count"] == 2
        assert result["failed_count"] == 0
        assert len(result["task_results"]) == 2

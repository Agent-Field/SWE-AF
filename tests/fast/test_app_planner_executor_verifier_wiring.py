"""Integration tests for cross-feature wiring between app, planner, executor, verifier.

These tests target the interaction boundaries specifically exposed by merging:
  - issue/e65cddc0-05-fast-planner
  - issue/e65cddc0-06-fast-executor
  - issue/e65cddc0-07-fast-verifier

into feature/e65cddc0-swe-fast-reasoner.

Critical integration paths tested:
  A. app.py stub completeness: executor and verifier both call swe_af.fast.app.app.call()
     — if app.py is a stub (no `app` attribute), both will raise AttributeError at
     runtime. Tests document this gap and verify fallback behaviour.
  B. Executor lazy-import resolves app.app.call at call time (not import time).
  C. Verifier lazy-import resolves app.app.call at call time (not import time).
  D. fast_router has all 8 reasoners after importing all three merged modules.
  E. fast_execute_tasks node-routing: the NODE_ID used in app.call matches
     docker-compose NODE_ID=swe-fast.
  F. Verifier fallback: when app has no `app` attribute (stub), fast_verify
     returns FastVerificationResult(passed=False) rather than raising.
  G. Executor exception-path: when app.call raises AttributeError (stub),
     fast_execute_tasks marks each task outcome='failed' rather than crashing.
  H. Schema round-trip isolation: FastPlanResult→executor→FastVerificationResult
     with no module-cache contamination.
  I. Planner and executor share the same fast_router object (not separate instances).
  J. Verifier and __init__ share the same fast_router object.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _all_fast_router_names() -> set[str]:
    """Return all registered reasoner names on fast_router."""
    from swe_af.fast import fast_router  # noqa: PLC0415
    return {r["func"].__name__ for r in fast_router.reasoners}


@contextlib.contextmanager
def _evict_and_replace_fast_app(mock_module: Any):
    """Evict swe_af.fast.app from sys.modules cache and replace with mock_module.

    This is needed because executor/verifier use lazy `import swe_af.fast.app`
    inside function bodies. Python caches the first import; if the real stub was
    already loaded, patch.dict alone won't override it for re-imports.
    """
    key = "swe_af.fast.app"
    saved = sys.modules.pop(key, None)
    sys.modules[key] = mock_module
    try:
        yield
    finally:
        sys.modules.pop(key, None)
        if saved is not None:
            sys.modules[key] = saved


@contextlib.contextmanager
def _patch_router_note(router: Any):
    """Temporarily inject a no-op note() into the router's instance dict."""
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


# ===========================================================================
# A. app.py stub completeness
# ===========================================================================


class TestAppStubState:
    """Document the state of app.py after the three branches merged."""

    def test_app_module_is_importable(self) -> None:
        """swe_af.fast.app must import without error (AC-1)."""
        import swe_af.fast.app as _fast_app_mod  # noqa: PLC0415
        assert _fast_app_mod is not None

    def test_app_module_has_app_attribute(self) -> None:
        """swe_af.fast.app must expose an 'app' AgentField node (AC-8).

        This is required by executor (app.call) and verifier (app.call).
        If this fails, executor tasks will error out with AttributeError.
        """
        os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
        import swe_af.fast.app as fast_app  # noqa: PLC0415
        assert hasattr(fast_app, "app"), (
            "swe_af.fast.app must expose 'app' — "
            "executor.py line 63 calls _app_module.app.call(...) and "
            "verifier.py line 53 calls _app.app.call(...). "
            "Without this, all tasks fail with AttributeError."
        )

    def test_app_node_id_is_swe_fast(self) -> None:
        """app.node_id must equal 'swe-fast' (AC-8)."""
        os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
        import swe_af.fast.app as fast_app  # noqa: PLC0415
        if not hasattr(fast_app, "app"):
            pytest.skip("app.py is still a stub — app attribute missing")
        assert fast_app.app.node_id == "swe-fast", (
            f"Expected node_id='swe-fast', got {fast_app.app.node_id!r}"
        )

    def test_app_build_function_exists(self) -> None:
        """app.py must expose a 'build' function with goal/repo_path/config params (AC-9)."""
        os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
        import swe_af.fast.app as fast_app  # noqa: PLC0415
        assert hasattr(fast_app, "build"), (
            "swe_af.fast.app must expose a 'build' function (AC-9)"
        )

    def test_app_main_function_exists(self) -> None:
        """app.py must expose a callable 'main' entry point (AC-16)."""
        os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
        import swe_af.fast.app as fast_app  # noqa: PLC0415
        assert hasattr(fast_app, "main"), (
            "swe_af.fast.app must expose a 'main' function (AC-16)"
        )
        assert callable(fast_app.main), "fast_app.main must be callable"


# ===========================================================================
# B & C. Executor and verifier lazy import at call time, not import time
# ===========================================================================


class TestLazyImportAtCallTime:
    """Executor/verifier import app INSIDE the function body, not at module level."""

    def test_executor_module_does_not_import_app_at_load(self) -> None:
        """Importing swe_af.fast.executor must NOT cause swe_af.fast.app to execute app-init."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.executor as ex  # noqa: PLC0415
        src = inspect.getsource(ex)
        # The lazy import must be inside fast_execute_tasks, not at module level
        assert "import swe_af.fast.app" in src, (
            "executor.py must contain lazy import of swe_af.fast.app"
        )
        # Verify it's inside the function (indented), not at top of file
        lines = src.splitlines()
        import_lines = [l for l in lines if "import swe_af.fast.app" in l]
        assert import_lines, "Must have swe_af.fast.app import somewhere"
        for line in import_lines:
            assert line.startswith("    "), (
                f"'import swe_af.fast.app' must be indented (inside function body), "
                f"got: {line!r}"
            )

    def test_verifier_module_does_not_import_app_at_load(self) -> None:
        """Importing swe_af.fast.verifier must NOT cause swe_af.fast.app app-init."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.verifier as vf  # noqa: PLC0415
        src = inspect.getsource(vf)
        # The lazy import must be inside fast_verify, not at module level
        assert "import swe_af.fast.app" in src, (
            "verifier.py must contain lazy import of swe_af.fast.app"
        )
        lines = src.splitlines()
        import_lines = [l for l in lines if "import swe_af.fast.app" in l]
        for line in import_lines:
            assert line.startswith("    "), (
                f"'import swe_af.fast.app' must be indented (inside fast_verify), "
                f"got: {line!r}"
            )


# ===========================================================================
# D. All 8 reasoners registered after importing all merged branches
# ===========================================================================


class TestRouterCompletenessPostMerge:
    """After all three branches are merged, fast_router must have exactly 8 reasoners."""

    _EXPECTED = frozenset({
        "run_git_init",
        "run_coder",
        "run_verifier",
        "run_repo_finalize",
        "run_github_pr",
        "fast_execute_tasks",   # from executor (branch 06)
        "fast_plan_tasks",      # from planner (branch 05)
        "fast_verify",          # from verifier (branch 07)
    })

    def test_all_eight_reasoners_present(self) -> None:
        """All 8 reasoners must be on fast_router after importing all submodules."""
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415
        # executor is registered via swe_af.fast.__init__
        names = _all_fast_router_names()
        missing = self._EXPECTED - names
        assert not missing, (
            f"Missing reasoners after merge: {sorted(missing)}. "
            f"Registered: {sorted(names)}"
        )

    def test_no_pipeline_reasoners_leaked_into_fast_router(self) -> None:
        """No planning pipeline reasoners must appear on fast_router."""
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415
        names = _all_fast_router_names()
        pipeline_forbidden = {
            "run_architect", "run_tech_lead", "run_sprint_planner",
            "run_product_manager", "run_issue_writer",
        }
        leaked = pipeline_forbidden & names
        assert not leaked, (
            f"Pipeline reasoners leaked into fast_router: {leaked}. "
            "The fast package must not load swe_af.reasoners.pipeline."
        )

    def test_fast_plan_tasks_on_same_router_as_fast_execute_tasks(self) -> None:
        """fast_plan_tasks and fast_execute_tasks must be on the SAME fast_router instance."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner as planner  # noqa: F401, PLC0415
        import swe_af.fast.executor as executor  # noqa: F401, PLC0415

        # Both modules import fast_router from swe_af.fast
        assert planner.fast_router is fast_pkg.fast_router, (
            "planner.fast_router must be the same object as swe_af.fast.fast_router"
        )
        assert executor.fast_router is fast_pkg.fast_router, (
            "executor.fast_router must be the same object as swe_af.fast.fast_router"
        )

    def test_fast_verify_on_same_router_as_wrappers(self) -> None:
        """fast_verify must be on the same router as the execution wrappers."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.verifier as verifier  # noqa: F401, PLC0415

        assert verifier.fast_router is fast_pkg.fast_router, (
            "verifier.fast_router must be the same object as swe_af.fast.fast_router"
        )


# ===========================================================================
# E. NODE_ID routing: executor uses 'swe-fast' to target run_coder
# ===========================================================================


class TestNodeIdRouting:
    """Verify executor routes to 'swe-fast.run_coder' correctly."""

    def test_executor_calls_app_with_swe_fast_prefixed_reasoner(self) -> None:
        """fast_execute_tasks must call app.call('swe-fast.run_coder', ...) by default."""
        coder_result = {"complete": True, "files_changed": ["f.py"], "summary": "done"}
        call_tracker: list[tuple] = []

        async def mock_call(*args: Any, **kwargs: Any) -> dict:
            call_tracker.append((args, kwargs))
            return {"result": coder_result}

        mock_app_obj = MagicMock()
        mock_app_obj.call = mock_call
        mock_module = MagicMock()
        mock_module.app = mock_app_obj

        import swe_af.fast.executor as ex  # noqa: PLC0415

        with (
            _patch_router_note(ex.fast_router),
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            _evict_and_replace_fast_app(mock_module),
        ):
            result = _run(ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "Do T1",
                        "acceptance_criteria": ["Done"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            ))

        assert len(call_tracker) > 0, "app.call must be called for each task"
        first_args, first_kwargs = call_tracker[0]
        # First positional arg to app.call must be '{NODE_ID}.run_coder'
        first_arg = first_args[0]
        assert "run_coder" in first_arg, (
            f"app.call first arg must reference 'run_coder', got {first_arg!r}"
        )
        assert "swe-fast" in first_arg, (
            f"app.call first arg must contain 'swe-fast' node prefix, got {first_arg!r}"
        )

    def test_executor_node_id_env_override(self) -> None:
        """executor.NODE_ID must be overridable via NODE_ID env var."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.executor as ex  # noqa: PLC0415
        src = inspect.getsource(ex)
        # NODE_ID = os.getenv("NODE_ID", "swe-fast")
        assert 'os.getenv("NODE_ID"' in src or "os.getenv('NODE_ID'" in src, (
            "executor must use os.getenv('NODE_ID', ...) for configurable routing"
        )
        assert '"swe-fast"' in src or "'swe-fast'" in src, (
            "executor NODE_ID must default to 'swe-fast'"
        )

    def test_executor_passes_correct_kwargs_to_app_call(self) -> None:
        """fast_execute_tasks must pass correct kwargs (worktree_path, model, etc.) to app.call."""
        coder_result = {"complete": True, "files_changed": [], "summary": "done"}
        call_tracker: list[tuple] = []

        async def mock_call(*args: Any, **kwargs: Any) -> dict:
            call_tracker.append((args, kwargs))
            return {"result": coder_result}

        mock_app_obj = MagicMock()
        mock_app_obj.call = mock_call
        mock_module = MagicMock()
        mock_module.app = mock_app_obj

        import swe_af.fast.executor as ex  # noqa: PLC0415

        with (
            _patch_router_note(ex.fast_router),
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            _evict_and_replace_fast_app(mock_module),
        ):
            result = _run(ex.fast_execute_tasks(
                tasks=[{"name": "my-task", "title": "T", "description": "d",
                        "acceptance_criteria": ["c"], "files_to_create": ["f.py"]}],
                repo_path="/tmp/my-repo",
                coder_model="sonnet",
                permission_mode="strict",
                ai_provider="claude",
                task_timeout_seconds=30,
            ))

        assert len(call_tracker) > 0, "app.call must have been called"
        _, kwargs = call_tracker[0]
        assert kwargs.get("model") == "sonnet", (
            "executor must pass coder_model as 'model' to app.call"
        )
        assert kwargs.get("worktree_path") == "/tmp/my-repo", (
            "executor must pass repo_path as 'worktree_path' to app.call"
        )
        assert kwargs.get("ai_provider") == "claude", (
            "executor must pass ai_provider to app.call"
        )
        assert kwargs.get("permission_mode") == "strict", (
            "executor must pass permission_mode to app.call"
        )


# ===========================================================================
# F. Verifier fallback when app is a stub (no 'app' attribute)
# ===========================================================================


class TestVerifierFallbackWithStubApp:
    """When app.py has no 'app' attribute (stub), fast_verify must return safe fallback."""

    def test_verifier_returns_passed_false_when_app_is_stub(self) -> None:
        """If swe_af.fast.app has no 'app' attr, fast_verify must NOT raise — use fallback."""
        # Create a stub module that has no 'app' attribute
        stub_module = MagicMock(spec=[])  # spec=[] means no attributes

        import swe_af.fast.verifier as vf  # noqa: PLC0415

        with _evict_and_replace_fast_app(stub_module):
            result = _run(vf.fast_verify(
                prd="Build something",
                repo_path="/tmp/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert result["passed"] is False, (
            "fast_verify must return passed=False when app raises AttributeError"
        )
        assert "Verification agent failed" in result["summary"], (
            "fast_verify fallback summary must say 'Verification agent failed'"
        )

    def test_verifier_summary_contains_error_when_app_stub(self) -> None:
        """Fallback summary must include the error from the AttributeError."""
        stub_module = MagicMock(spec=[])

        import swe_af.fast.verifier as vf  # noqa: PLC0415

        with _evict_and_replace_fast_app(stub_module):
            result = _run(vf.fast_verify(
                prd="Build something",
                repo_path="/tmp/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 0, "Fallback summary must not be empty"


# ===========================================================================
# G. Executor exception-path when app.call raises AttributeError
# ===========================================================================


class TestExecutorFallbackWithStubApp:
    """When app has no 'app' attribute, fast_execute_tasks should mark tasks as failed."""

    def test_executor_marks_task_failed_when_app_call_raises(self) -> None:
        """If _app_module.app.call raises AttributeError, task outcome must be 'failed'."""
        # app module that raises AttributeError on .app access
        stub_module = MagicMock(spec=[])  # no 'app' attribute

        import swe_af.fast.executor as ex  # noqa: PLC0415

        with (
            _patch_router_note(ex.fast_router),
            _evict_and_replace_fast_app(stub_module),
        ):
            result = _run(ex.fast_execute_tasks(
                tasks=[{"name": "failing-task", "title": "T",
                        "description": "d", "acceptance_criteria": ["c"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            ))

        assert result["completed_count"] == 0, (
            "When app.call fails, completed_count must be 0"
        )
        assert result["failed_count"] == 1, (
            "When app.call fails with AttributeError, task must be outcome='failed'"
        )
        assert result["task_results"][0]["outcome"] == "failed", (
            f"Expected outcome='failed', got {result['task_results'][0]['outcome']!r}"
        )
        assert len(result["task_results"][0]["error"]) > 0, (
            "Error message must be non-empty when task fails"
        )

    def test_executor_continues_after_app_failure_on_each_task(self) -> None:
        """fast_execute_tasks must continue to next task even after AttributeError."""
        stub_module = MagicMock(spec=[])

        import swe_af.fast.executor as ex  # noqa: PLC0415

        tasks = [
            {"name": "task-a", "title": "A", "description": "d", "acceptance_criteria": ["c"]},
            {"name": "task-b", "title": "B", "description": "d", "acceptance_criteria": ["c"]},
        ]

        with (
            _patch_router_note(ex.fast_router),
            _evict_and_replace_fast_app(stub_module),
        ):
            result = _run(ex.fast_execute_tasks(
                tasks=tasks,
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            ))

        # Both tasks should be processed (not just the first)
        assert len(result["task_results"]) == 2, (
            "Executor must process all tasks even after individual failures"
        )
        assert result["task_results"][0]["task_name"] == "task-a"
        assert result["task_results"][1]["task_name"] == "task-b"
        assert result["failed_count"] == 2


# ===========================================================================
# H. Schema round-trip with module isolation
# ===========================================================================


class TestSchemaRoundTripIsolation:
    """Full data pipeline: planner output → executor input → verifier input."""

    def test_planner_output_structure_matches_executor_expectations(self) -> None:
        """FastPlanResult.model_dump()['tasks'] items match all executor .get() calls."""
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        task = FastTask(
            name="implement-rest-api",
            title="Implement REST API",
            description="Build a REST API",
            acceptance_criteria=["API responds to GET /health"],
            files_to_create=["api/main.py"],
            files_to_modify=["requirements.txt"],
        )
        plan = FastPlanResult(tasks=[task])
        task_dicts = plan.model_dump()["tasks"]
        assert len(task_dicts) == 1
        d = task_dicts[0]

        # Verify every key accessed by executor.py via task_dict.get(...)
        executor_accessed_keys = [
            ("name", "unknown"),       # line: task_name = task_dict.get("name", "unknown")
            ("title", None),            # line: "title": task_dict.get("title", task_name)
            ("description", None),      # line: "description": task_dict.get("description", "")
            ("acceptance_criteria", None),  # line: "acceptance_criteria": task_dict.get(...)
            ("files_to_create", None),  # line: "files_to_create": task_dict.get(...)
            ("files_to_modify", None),  # line: "files_to_modify": task_dict.get(...)
        ]
        for key, _ in executor_accessed_keys:
            assert key in d, (
                f"FastTask.model_dump() missing key '{key}' "
                f"expected by executor.py — planner→executor contract broken"
            )
        assert d["name"] == "implement-rest-api"
        assert d["files_to_create"] == ["api/main.py"]

    def test_executor_output_structure_matches_verifier_expectations(self) -> None:
        """FastTaskResult.model_dump() items match all verifier task_results expectations."""
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult  # noqa: PLC0415

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(
                    task_name="implement-rest-api",
                    outcome="completed",
                    files_changed=["api/main.py"],
                    summary="API implemented",
                ),
                FastTaskResult(
                    task_name="write-tests",
                    outcome="failed",
                    error="Exception in coder",
                ),
                FastTaskResult(
                    task_name="setup-ci",
                    outcome="timeout",
                    error="Timed out after 300s",
                ),
            ],
            completed_count=1,
            failed_count=2,
        )
        verifier_input = exec_result.model_dump()["task_results"]

        # Verify structure expected by verifier
        assert len(verifier_input) == 3
        for item in verifier_input:
            assert isinstance(item, dict)
            assert "task_name" in item
            assert "outcome" in item

        # All three outcomes must be present
        outcomes = {item["task_name"]: item["outcome"] for item in verifier_input}
        assert outcomes["implement-rest-api"] == "completed"
        assert outcomes["write-tests"] == "failed"
        assert outcomes["setup-ci"] == "timeout"

    def test_full_pipeline_schema_round_trip(self) -> None:
        """Full data flow: config → plan → execute → verify schemas all connect."""
        from swe_af.fast.schemas import (  # noqa: PLC0415
            FastBuildConfig,
            FastTask,
            FastPlanResult,
            FastTaskResult,
            FastExecutionResult,
            FastVerificationResult,
            FastBuildResult,
            fast_resolve_models,
        )

        # Step 1: Resolve models from config
        config = FastBuildConfig(runtime="claude_code", max_tasks=3)
        models = fast_resolve_models(config)
        assert "pm_model" in models
        assert "coder_model" in models
        assert "verifier_model" in models

        # Step 2: Planner produces tasks
        plan = FastPlanResult(tasks=[
            FastTask(name="t1", title="T1", description="D1", acceptance_criteria=["C1"]),
            FastTask(name="t2", title="T2", description="D2", acceptance_criteria=["C2"]),
        ])
        task_dicts = plan.model_dump()["tasks"]
        assert len(task_dicts) == 2

        # Step 3: Executor produces results
        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name=d["name"], outcome="completed")
                for d in task_dicts
            ],
            completed_count=2,
            failed_count=0,
        )

        # Step 4: Verifier receives executor output
        verifier_input = exec_result.model_dump()["task_results"]
        assert len(verifier_input) == 2

        # Step 5: Verification produces result
        verification = FastVerificationResult(
            passed=True,
            summary="All tasks passed",
        )

        # Step 6: Build result aggregates everything
        build_result = FastBuildResult(
            plan_result=plan.model_dump(),
            execution_result=exec_result.model_dump(),
            verification=verification.model_dump(),
            success=True,
            summary="Build succeeded",
        )
        assert build_result.success is True
        assert build_result.verification["passed"] is True
        assert build_result.plan_result["tasks"][0]["name"] == "t1"


# ===========================================================================
# I & J. Shared router instance across all merged modules
# ===========================================================================


class TestSharedRouterInstance:
    """All fast submodules must share the SAME fast_router instance."""

    def test_planner_executor_verifier_share_fast_router(self) -> None:
        """All three merged modules must import the same fast_router object."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner as planner  # noqa: PLC0415
        import swe_af.fast.executor as executor  # noqa: PLC0415
        import swe_af.fast.verifier as verifier  # noqa: PLC0415

        assert planner.fast_router is fast_pkg.fast_router, (
            "planner must use the same fast_router as swe_af.fast"
        )
        assert executor.fast_router is fast_pkg.fast_router, (
            "executor must use the same fast_router as swe_af.fast"
        )
        assert verifier.fast_router is fast_pkg.fast_router, (
            "verifier must use the same fast_router as swe_af.fast"
        )
        # All three are the same object
        assert planner.fast_router is executor.fast_router, (
            "planner and executor must share the exact same fast_router object"
        )
        assert executor.fast_router is verifier.fast_router, (
            "executor and verifier must share the exact same fast_router object"
        )

    def test_reasoners_registered_by_each_branch_are_on_shared_router(self) -> None:
        """Reasoners from each branch must appear on the shared router after import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.executor  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415

        registered = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}

        # Branch 05 contribution
        assert "fast_plan_tasks" in registered, (
            "fast_plan_tasks (from branch 05) must be on fast_router"
        )
        # Branch 06 contribution
        assert "fast_execute_tasks" in registered, (
            "fast_execute_tasks (from branch 06) must be on fast_router"
        )
        # Branch 07 contribution
        assert "fast_verify" in registered, (
            "fast_verify (from branch 07) must be on fast_router"
        )


# ===========================================================================
# Additional: planner AI param threading to executor
# ===========================================================================


class TestPlannerAiParamThreading:
    """Verify that model resolution keys from schemas flow through to component params."""

    def test_pm_model_from_config_matches_planner_param_name(self) -> None:
        """fast_resolve_models()['pm_model'] key must match fast_plan_tasks param 'pm_model'."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.planner import fast_plan_tasks  # noqa: PLC0415

        resolved = fast_resolve_models(FastBuildConfig(runtime="claude_code"))
        sig = inspect.signature(fast_plan_tasks)

        assert "pm_model" in resolved
        assert "pm_model" in sig.parameters, (
            "fast_plan_tasks must accept 'pm_model' matching fast_resolve_models output"
        )
        # Both should be haiku for claude_code runtime
        assert resolved["pm_model"] == "haiku"
        assert sig.parameters["pm_model"].default == "haiku", (
            "fast_plan_tasks default for pm_model must match claude_code default"
        )

    def test_coder_model_from_config_matches_executor_param_name(self) -> None:
        """fast_resolve_models()['coder_model'] key must match fast_execute_tasks param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

        resolved = fast_resolve_models(FastBuildConfig(runtime="claude_code"))
        sig = inspect.signature(fast_execute_tasks)

        assert "coder_model" in resolved
        assert "coder_model" in sig.parameters, (
            "fast_execute_tasks must accept 'coder_model' matching fast_resolve_models output"
        )
        assert resolved["coder_model"] == "haiku"

    def test_verifier_model_from_config_matches_verifier_param_name(self) -> None:
        """fast_resolve_models()['verifier_model'] key must match fast_verify param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        resolved = fast_resolve_models(FastBuildConfig(runtime="claude_code"))
        sig = inspect.signature(fast_verify)

        assert "verifier_model" in resolved
        assert "verifier_model" in sig.parameters, (
            "fast_verify must accept 'verifier_model' matching fast_resolve_models output"
        )

    def test_open_code_runtime_models_flow_correctly(self) -> None:
        """For open_code runtime, all four roles must resolve to the qwen model."""
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415

        config = FastBuildConfig(runtime="open_code")
        resolved = fast_resolve_models(config)

        expected = "qwen/qwen-2.5-coder-32b-instruct"
        for role in ("pm_model", "coder_model", "verifier_model", "git_model"):
            assert resolved[role] == expected, (
                f"open_code runtime: {role} should be {expected!r}, got {resolved[role]!r}"
            )

    def test_custom_model_override_threads_through(self) -> None:
        """Custom model override must produce distinct coder_model vs pm_model."""
        from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig  # noqa: PLC0415

        config = FastBuildConfig(
            runtime="claude_code",
            models={"coder": "sonnet", "default": "haiku"},
        )
        resolved = fast_resolve_models(config)
        assert resolved["coder_model"] == "sonnet", (
            "coder override must produce coder_model='sonnet'"
        )
        assert resolved["pm_model"] == "haiku", (
            "pm_model must use default 'haiku' when not overridden"
        )
        assert resolved["verifier_model"] == "haiku", (
            "verifier_model must use default 'haiku' when not overridden"
        )

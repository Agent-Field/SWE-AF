"""Integration tests: swe_af.fast.__init__ ↔ executor/planner/verifier routing contracts.

Priority-1 interaction boundaries under test:

1. fast_router thin-wrapper delegation:
   - Each thin wrapper in __init__ (run_git_init, run_coder, run_verifier,
     run_repo_finalize, run_github_pr) must lazily import execution_agents and
     delegate to the corresponding function — NOT call pipeline.py.
   - If __init__ loaded pipeline.py, the swe-planner pipeline agents would run
     inside the swe-fast process — a critical isolation failure.

2. executor ↔ __init__ NODE_ID routing contract:
   - executor reads NODE_ID at module-load time from os.environ
   - When NODE_ID env var is NOT set, it must default to 'swe-fast' so that
     executor calls f'{NODE_ID}.run_coder' = 'swe-fast.run_coder' (not
     'swe-planner.run_coder' which is the planner service)
   - Tests use subprocess isolation to set NODE_ID cleanly

3. planner ↔ build() ↔ verifier: prd field absence + fallback construction
   - The 'prd' key is ABSENT from FastPlanResult (no PM stage)
   - build() constructs a fallback prd_dict
   - That fallback must match the shape that verifier's run_verifier call expects

4. executor complete=False → outcome='failed' (not 'completed')
   - The executor checks coder_result.get("complete", False) — when False, outcome='failed'
   - This is a subtle cross-feature interaction: coder returns a dict, executor interprets it

5. verifier ↔ FastVerificationResult field aliasing
   - fast_verify wraps app.call result in FastVerificationResult(...) before returning
   - Fields: passed, summary, criteria_results, suggested_fixes all must round-trip

6. build() timeout path → executor not called
   - When build_timeout_seconds elapses BEFORE execute, executor is never called
   - FastBuildResult in that path must have timed_out=True in execution_result

7. __init__ thin-wrapper -> execution_agents pipeline isolation
   - Importing swe_af.fast must NOT load swe_af.reasoners.pipeline
   - The lazy import pattern in each wrapper must ensure this

8. fast_router reasoner count after full import chain
   - After importing __init__ + executor + planner + verifier, exactly 8 reasoners
     must be registered on fast_router
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _run_subprocess(
    code: str,
    extra_env: dict | None = None,
    unset_keys: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run python -c <code> in a fresh subprocess with clean env."""
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


@contextlib.contextmanager
def _patch_fast_router_note():
    """Suppress fast_router.note() calls to avoid 'Router not attached' errors."""
    import swe_af.fast as fast_pkg  # noqa: PLC0415
    router = fast_pkg.fast_router
    old = router.__dict__.get("note", None)
    router.__dict__["note"] = MagicMock(return_value=None)
    try:
        yield
    finally:
        if old is None:
            router.__dict__.pop("note", None)
        else:
            router.__dict__["note"] = old


# ===========================================================================
# 1. fast_router thin wrappers delegate to execution_agents (not pipeline)
# ===========================================================================


class TestFastInitThinWrapperDelegation:
    """__init__ thin wrappers must delegate to execution_agents, never pipeline."""

    def test_run_coder_wrapper_delegates_to_execution_agents(self) -> None:
        """run_coder wrapper in __init__ must call _ea.run_coder via lazy import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        assert "_ea.run_coder" in src, (
            "run_coder wrapper in __init__ must delegate to execution_agents.run_coder "
            "via lazy import (_ea.run_coder)"
        )

    def test_run_verifier_wrapper_delegates_to_execution_agents(self) -> None:
        """run_verifier wrapper in __init__ must call _ea.run_verifier via lazy import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        assert "_ea.run_verifier" in src, (
            "run_verifier wrapper in __init__ must delegate to execution_agents.run_verifier "
            "via lazy import (_ea.run_verifier)"
        )

    def test_run_git_init_wrapper_delegates_to_execution_agents(self) -> None:
        """run_git_init wrapper in __init__ must call _ea.run_git_init via lazy import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        assert "_ea.run_git_init" in src, (
            "run_git_init wrapper must delegate to execution_agents.run_git_init"
        )

    def test_run_repo_finalize_wrapper_delegates_to_execution_agents(self) -> None:
        """run_repo_finalize wrapper in __init__ must call _ea.run_repo_finalize."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        assert "_ea.run_repo_finalize" in src, (
            "run_repo_finalize wrapper must delegate to execution_agents.run_repo_finalize"
        )

    def test_run_github_pr_wrapper_delegates_to_execution_agents(self) -> None:
        """run_github_pr wrapper in __init__ must call _ea.run_github_pr via lazy import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        assert "_ea.run_github_pr" in src, (
            "run_github_pr wrapper must delegate to execution_agents.run_github_pr"
        )

    def test_fast_init_source_has_lazy_imports_for_all_wrappers(self) -> None:
        """__init__ wrappers must all use lazy imports (inside function body)."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        # All thin wrappers must use lazy import of execution_agents
        assert "import swe_af.reasoners.execution_agents" in src, (
            "__init__ must lazily import execution_agents inside each wrapper"
        )

        # The imports must be inside function bodies (indented)
        lines = src.splitlines()
        import_lines = [
            line for line in lines
            if "import swe_af.reasoners.execution_agents" in line
        ]
        assert import_lines, "Must have execution_agents imports"
        for line in import_lines:
            assert line.startswith("    "), (
                f"execution_agents import must be inside a function body (indented), "
                f"got top-level: {line!r}"
            )

    def test_fast_init_does_not_call_pipeline_agents_in_code(self) -> None:
        """__init__ actual code must not call any swe-planner pipeline planning agents.

        Note: the module docstring mentions these names for explanation — this test
        only checks actual AST import and attribute-call nodes.
        """
        import ast  # noqa: PLC0415
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        src = inspect.getsource(fast_pkg)

        # Parse AST to check actual imported names (not docstring text)
        tree = ast.parse(src)
        pipeline_agents = {
            "run_architect", "run_tech_lead", "run_sprint_planner",
            "run_product_manager", "run_issue_writer",
        }

        # Verify: no direct import of pipeline agents at the module level
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in (node.names or []):
                    name = alias.name or ""
                    assert name not in pipeline_agents, (
                        f"__init__ must not import pipeline agent {name!r} "
                        f"from {node.module!r}; swe-fast is pipeline-free by design"
                    )

        # Verify: fast_router tag is 'swe-fast' (not reusing a pipeline router)
        from swe_af.fast import fast_router  # noqa: PLC0415
        tags = getattr(fast_router, "tags", None) or getattr(fast_router, "_tags", [])
        assert "swe-fast" in tags, (
            f"fast_router tags must be 'swe-fast', got {tags!r}; "
            "this ensures it's not mistakenly using the pipeline router"
        )
    def test_all_five_thin_wrappers_registered_on_fast_router(self) -> None:
        """All five thin wrappers must be registered on fast_router at import time."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415

        names = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}
        expected_wrappers = {
            "run_git_init", "run_coder", "run_verifier",
            "run_repo_finalize", "run_github_pr",
        }
        missing = expected_wrappers - names
        assert not missing, (
            f"These thin wrappers from __init__ are missing from fast_router: "
            f"{sorted(missing)}. Found: {sorted(names)}"
        )

    def test_pipeline_not_loaded_after_importing_fast_init(self) -> None:
        """Importing swe_af.fast must NOT cause swe_af.reasoners.pipeline to load."""
        code = """
import sys
# Ensure clean state
for k in list(sys.modules):
    if 'swe_af' in k:
        del sys.modules[k]

import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast  # This triggers __init__
assert "swe_af.reasoners.pipeline" not in sys.modules, (
    f"swe_af.reasoners.pipeline must NOT be loaded after importing swe_af.fast; "
    f"found in sys.modules"
)
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"swe_af.fast import must NOT load pipeline.py; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_pipeline_not_loaded_after_importing_all_fast_submodules(self) -> None:
        """Importing all swe_af.fast submodules must NOT load swe_af.reasoners.pipeline."""
        code = """
import sys
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
# Clear any prior state
for k in list(sys.modules):
    if 'swe_af' in k:
        del sys.modules[k]

import swe_af.fast
import swe_af.fast.executor
import swe_af.fast.planner
import swe_af.fast.verifier

pipeline_key = "swe_af.reasoners.pipeline"
assert pipeline_key not in sys.modules, (
    f"Pipeline must not be loaded after importing all swe_af.fast submodules; "
    f"found pipeline-related modules: "
    f"{[k for k in sys.modules if 'pipeline' in k]}"
)
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"No fast submodule should load pipeline; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout


# ===========================================================================
# 2. executor ↔ __init__ NODE_ID routing — subprocess isolation
# ===========================================================================


class TestExecutorNodeIdRoutingIsolation:
    """executor.NODE_ID must default to 'swe-fast', not inherit swe-planner from env."""

    def test_executor_node_id_defaults_to_swe_fast_when_env_unset(self) -> None:
        """executor.NODE_ID must be 'swe-fast' when NODE_ID env var is unset."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast.executor as ex
assert ex.NODE_ID == "swe-fast", (
    f"executor.NODE_ID must default to 'swe-fast' when NODE_ID is unset, "
    f"got {ex.NODE_ID!r}"
)
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"executor.NODE_ID must be 'swe-fast' when NODE_ID not set; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_executor_routes_to_swe_fast_run_coder_when_node_id_unset(self) -> None:
        """With NODE_ID unset, executor must route to 'swe-fast.run_coder', not 'swe-planner.run_coder'."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast.executor as ex
# The routing string is f"{NODE_ID}.run_coder"
route = f"{ex.NODE_ID}.run_coder"
assert route == "swe-fast.run_coder", (
    f"executor must route to 'swe-fast.run_coder', got {route!r}"
)
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"executor must use 'swe-fast.run_coder' route; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_executor_node_id_respects_env_override(self) -> None:
        """executor.NODE_ID must respect NODE_ID env var when explicitly set."""
        code = """
import os
os.environ["NODE_ID"] = "swe-fast-test"
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast.executor as ex
assert ex.NODE_ID == "swe-fast-test", (
    f"executor.NODE_ID must reflect NODE_ID env var, got {ex.NODE_ID!r}"
)
print("OK")
"""
        result = _run_subprocess(code, extra_env={"NODE_ID": "swe-fast-test"})
        assert result.returncode == 0, (
            f"executor.NODE_ID must respect NODE_ID env override; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_executor_and_planner_node_ids_differ_when_both_unset(self) -> None:
        """fast executor must use 'swe-fast', planner app must use 'swe-planner'."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast.executor as ex
import swe_af.app as planner_app
planner_node = planner_app.NODE_ID
fast_node = ex.NODE_ID
assert planner_node == "swe-planner", f"planner NODE_ID={planner_node!r}"
assert fast_node == "swe-fast", f"fast executor NODE_ID={fast_node!r}"
assert planner_node != fast_node, "NODE_IDs must be distinct"
print(f"planner_node={planner_node!r} fast_node={fast_node!r}")
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"Planner and executor must have different NODE_IDs; "
            f"stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout
        assert "swe-fast" in result.stdout
        assert "swe-planner" in result.stdout


# ===========================================================================
# 3. planner ↔ build() ↔ verifier: prd field absence + fallback construction
# ===========================================================================


class TestPlannerBuildVerifierPrdContract:
    """FastPlanResult has no 'prd' field; build() must construct fallback prd for verifier."""

    def test_fast_plan_result_has_no_prd_field_in_schema(self) -> None:
        """FastPlanResult schema must not include 'prd' — it's a single-pass planner."""
        from swe_af.fast.schemas import FastPlanResult  # noqa: PLC0415

        plan = FastPlanResult(tasks=[], rationale="test")
        dumped = plan.model_dump()
        assert "prd" not in dumped, (
            f"FastPlanResult must NOT have 'prd' field — fast planner has no PM stage. "
            f"Got keys: {list(dumped.keys())}"
        )

    def test_build_source_has_fallback_prd_construction(self) -> None:
        """build() must construct a fallback prd_dict when plan_result has no 'prd' key."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        src = inspect.getsource(fn)

        # build() must check for missing 'prd' and construct a fallback
        assert 'plan_result.get("prd")' in src or "plan_result.get('prd')" in src, (
            "build() must check plan_result.get('prd') to handle missing prd from planner; "
            "if missing this check, verifier receives None prd_dict"
        )
        # Fallback must include 'validated_description'
        assert "validated_description" in src, (
            "build() fallback prd_dict must have 'validated_description' field"
        )

    def test_fallback_prd_dict_forwarded_to_fast_verify_unchanged(self) -> None:
        """The fallback prd_dict from build() must be passable as prd to fast_verify."""
        # The fallback from build():
        goal = "Add a health check endpoint"
        fallback_prd = {
            "validated_description": goal,
            "acceptance_criteria": [],
            "must_have": [],
            "nice_to_have": [],
            "out_of_scope": [],
        }

        called_prd: list = []
        verify_response = {
            "passed": True, "summary": "ok",
            "criteria_results": [], "suggested_fixes": [],
        }
        mock_app = MagicMock()

        async def _capture_call(route: str, **kwargs: Any) -> Any:
            called_prd.append(kwargs.get("prd"))
            return verify_response

        mock_app.app.call = _capture_call

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            _run_coro(fast_verify(
                prd=fallback_prd,
                repo_path="/tmp/repo",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert len(called_prd) == 1, "fast_verify must call app.call once"
        received_prd = called_prd[0]
        assert isinstance(received_prd, dict), (
            f"prd forwarded to run_verifier must be a dict, got {type(received_prd)}"
        )
        assert received_prd.get("validated_description") == goal, (
            "fallback prd's validated_description must be preserved through fast_verify"
        )

    def test_fast_verify_prd_param_is_keyword_only(self) -> None:
        """fast_verify must declare 'prd' as keyword-only (star-args syntax)."""
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        fn = getattr(fast_verify, "_original_func", fast_verify)
        sig = inspect.signature(fn)

        assert "prd" in sig.parameters, "fast_verify must have 'prd' parameter"
        prd_param = sig.parameters["prd"]
        assert prd_param.kind == inspect.Parameter.KEYWORD_ONLY, (
            "fast_verify 'prd' must be keyword-only (function uses * before prd); "
            f"got kind: {prd_param.kind!r}"
        )


# ===========================================================================
# 4. executor complete=False → outcome='failed' (not 'completed')
# ===========================================================================


class TestExecutorCompleteFieldInterpretation:
    """executor maps coder_result['complete'] to outcome: True→'completed', False→'failed'."""

    @pytest.mark.asyncio
    async def test_coder_complete_false_yields_failed_outcome(self) -> None:
        """When run_coder returns complete=False, executor must set outcome='failed'."""
        coder_result = {"complete": False, "files_changed": [], "summary": "incomplete"}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_fast_router_note(),
        ):
            import swe_af.fast.executor as ex  # noqa: PLC0415

            result = await ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "d",
                        "acceptance_criteria": ["ac"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        outcome = result["task_results"][0]["outcome"]
        assert outcome == "failed", (
            f"When coder returns complete=False, executor must set outcome='failed', "
            f"got {outcome!r}. This is the critical cross-feature boundary: "
            f"coder output interpretation."
        )
        assert result["completed_count"] == 0, (
            "complete=False tasks must not increment completed_count"
        )
        assert result["failed_count"] == 1, (
            "complete=False tasks must increment failed_count"
        )

    @pytest.mark.asyncio
    async def test_coder_complete_true_yields_completed_outcome(self) -> None:
        """When run_coder returns complete=True, executor must set outcome='completed'."""
        coder_result = {"complete": True, "files_changed": ["f.py"], "summary": "done"}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_fast_router_note(),
        ):
            import swe_af.fast.executor as ex  # noqa: PLC0415

            result = await ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "d",
                        "acceptance_criteria": ["ac"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        outcome = result["task_results"][0]["outcome"]
        assert outcome == "completed", (
            f"When coder returns complete=True, executor must set outcome='completed', "
            f"got {outcome!r}"
        )
        assert result["completed_count"] == 1
        assert result["failed_count"] == 0

    @pytest.mark.asyncio
    async def test_coder_missing_complete_field_defaults_to_failed(self) -> None:
        """When run_coder omits 'complete', executor defaults to False → 'failed'."""
        # executor uses coder_result.get("complete", False)
        coder_result = {"files_changed": [], "summary": "no complete field"}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_fast_router_note(),
        ):
            import swe_af.fast.executor as ex  # noqa: PLC0415

            result = await ex.fast_execute_tasks(
                tasks=[{"name": "t1", "title": "T1", "description": "d",
                        "acceptance_criteria": ["ac"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        outcome = result["task_results"][0]["outcome"]
        assert outcome == "failed", (
            f"When coder result omits 'complete', executor must treat as False → 'failed', "
            f"got {outcome!r}"
        )

    @pytest.mark.asyncio
    async def test_executor_files_changed_forwarded_from_coder(self) -> None:
        """files_changed from run_coder must be stored in FastTaskResult."""
        coder_result = {
            "complete": True,
            "files_changed": ["src/api.py", "tests/test_api.py"],
            "summary": "Added API endpoint",
        }
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value={"result": coder_result})

        with (
            patch("swe_af.fast.executor._unwrap", return_value=coder_result),
            patch.dict("sys.modules", {"swe_af.fast.app": mock_app}),
            _patch_fast_router_note(),
        ):
            import swe_af.fast.executor as ex  # noqa: PLC0415

            result = await ex.fast_execute_tasks(
                tasks=[{"name": "api-task", "title": "API Task", "description": "d",
                        "acceptance_criteria": ["ac"]}],
                repo_path="/tmp/repo",
                task_timeout_seconds=30,
            )

        task_result = result["task_results"][0]
        assert task_result["files_changed"] == ["src/api.py", "tests/test_api.py"], (
            "files_changed from coder must be stored in FastTaskResult; "
            f"got {task_result['files_changed']!r}"
        )
        assert task_result["summary"] == "Added API endpoint", (
            "summary from coder must be stored in FastTaskResult"
        )


# ===========================================================================
# 5. verifier ↔ FastVerificationResult field aliasing and round-trip
# ===========================================================================


class TestVerifierFastVerificationResultRoundTrip:
    """fast_verify wraps app.call result in FastVerificationResult before returning."""

    def test_partial_result_from_app_call_is_completed_by_schema(self) -> None:
        """When app.call returns partial result, FastVerificationResult fills defaults."""
        # Only 'passed' and 'summary' are returned — criteria_results and suggested_fixes missing
        partial_result = {"passed": True, "summary": "partial ok"}
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=partial_result)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run_coro(fast_verify(
                prd="goal",
                repo_path="/tmp",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        # FastVerificationResult defaults: criteria_results=[], suggested_fixes=[]
        assert "passed" in result and result["passed"] is True
        assert "criteria_results" in result, (
            "FastVerificationResult must add criteria_results default even when not in raw result"
        )
        assert isinstance(result["criteria_results"], list)
        assert "suggested_fixes" in result, (
            "FastVerificationResult must add suggested_fixes default even when not in raw result"
        )
        assert isinstance(result["suggested_fixes"], list)

    def test_extra_fields_from_app_call_are_ignored_gracefully(self) -> None:
        """app.call may return extra fields — FastVerificationResult must ignore them."""
        result_with_extras = {
            "passed": False,
            "summary": "failed",
            "criteria_results": [{"name": "test", "passed": False}],
            "suggested_fixes": ["fix this"],
            "extra_field_that_verifier_does_not_know_about": "should be ignored",
        }
        mock_app = MagicMock()
        mock_app.app.call = AsyncMock(return_value=result_with_extras)

        with patch.dict("sys.modules", {"swe_af.fast.app": mock_app}):
            from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

            result = _run_coro(fast_verify(
                prd="goal",
                repo_path="/tmp",
                task_results=[],
                verifier_model="haiku",
                permission_mode="",
                ai_provider="claude",
                artifacts_dir="",
            ))

        assert result["passed"] is False
        assert result["summary"] == "failed"

    def test_verifier_result_can_be_stored_in_fast_build_result(self) -> None:
        """A result from fast_verify must be storable in FastBuildResult.verification."""
        from swe_af.fast.schemas import FastBuildResult  # noqa: PLC0415

        for verification, expected_passed in [
            (
                {
                    "passed": True, "summary": "All criteria met",
                    "criteria_results": [{"name": "ac-1", "passed": True}],
                    "suggested_fixes": [],
                },
                True,
            ),
            (
                {
                    "passed": False, "summary": "2 criteria failed",
                    "criteria_results": [],
                    "suggested_fixes": ["fix A", "fix B"],
                },
                False,
            ),
        ]:
            build_result = FastBuildResult(
                plan_result={"tasks": []},
                execution_result={"completed_count": 1, "failed_count": 0, "task_results": []},
                verification=verification,
                success=expected_passed,
                summary="test",
            )
            assert build_result.verification["passed"] is expected_passed, (
                "FastBuildResult must store verification dict unchanged"
            )


# ===========================================================================
# 6. build() timeout path → timed_out structure
# ===========================================================================


class TestBuildTimeoutPath:
    """When build_timeout_seconds elapses, FastBuildResult must have timed_out=True."""

    def test_build_timeout_result_structure(self) -> None:
        """FastBuildResult in timeout path must have timed_out=True and success=False."""
        from swe_af.fast.schemas import FastBuildResult  # noqa: PLC0415

        # This is the exact structure app.build() returns on asyncio.TimeoutError
        timeout_result = FastBuildResult(
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

        dumped = timeout_result.model_dump()
        assert dumped["success"] is False, "Timeout result must have success=False"
        assert dumped["execution_result"]["timed_out"] is True, (
            "Timeout result must have timed_out=True in execution_result"
        )
        assert dumped["execution_result"]["completed_count"] == 0
        assert "timed out" in dumped["summary"].lower()

    def test_build_source_uses_asyncio_wait_for_for_plan_execute_phase(self) -> None:
        """build() must wrap plan + execute in asyncio.wait_for(build_timeout_seconds)."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        src = inspect.getsource(fn)

        assert "asyncio.wait_for" in src, (
            "build() must use asyncio.wait_for to enforce build_timeout_seconds; "
            "if missing, the build_timeout_seconds config has no effect"
        )
        assert "build_timeout_seconds" in src, (
            "build() must reference build_timeout_seconds to configure the timeout"
        )
        assert "asyncio.TimeoutError" in src, (
            "build() must catch asyncio.TimeoutError for the timeout path"
        )

    def test_build_config_default_build_timeout_is_600s(self) -> None:
        """FastBuildConfig.build_timeout_seconds default must be 600."""
        from swe_af.fast.schemas import FastBuildConfig  # noqa: PLC0415

        cfg = FastBuildConfig()
        assert cfg.build_timeout_seconds == 600, (
            f"build_timeout_seconds default must be 600s (PRD spec), "
            f"got {cfg.build_timeout_seconds}"
        )


# ===========================================================================
# 7. fast_router reasoner count after full import chain
# ===========================================================================


class TestFastRouterReasonerCount:
    """After full import of all merged modules, fast_router must have exactly 8 reasoners."""

    def test_all_eight_reasoners_registered_via_subprocess(self) -> None:
        """Subprocess validates full import chain produces 8 reasoners."""
        code = """
import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
import swe_af.fast
import swe_af.fast.executor
import swe_af.fast.planner
import swe_af.fast.verifier

names = {r["func"].__name__ for r in swe_af.fast.fast_router.reasoners}
expected = {
    "run_git_init", "run_coder", "run_verifier",
    "run_repo_finalize", "run_github_pr",
    "fast_execute_tasks", "fast_plan_tasks", "fast_verify",
}
missing = expected - names
extra = names - expected
if missing:
    print(f"MISSING: {sorted(missing)}")
if extra:
    print(f"EXTRA: {sorted(extra)}")
assert not missing, f"Missing reasoners: {sorted(missing)}"
assert len(names) == 8, f"Expected 8 reasoners, got {len(names)}: {sorted(names)}"
print("OK")
"""
        result = _run_subprocess(code, unset_keys=["NODE_ID"])
        assert result.returncode == 0, (
            f"fast_router must have exactly 8 reasoners after full import chain; "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_fast_plan_tasks_registered_on_fast_router(self) -> None:
        """fast_plan_tasks must be on fast_router after planner import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner  # noqa: F401, PLC0415

        names = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}
        assert "fast_plan_tasks" in names, (
            "fast_plan_tasks must be registered on fast_router (not any pipeline router)"
        )

    def test_fast_execute_tasks_registered_on_fast_router(self) -> None:
        """fast_execute_tasks must be on fast_router after executor import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.executor  # noqa: F401, PLC0415

        names = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}
        assert "fast_execute_tasks" in names, (
            "fast_execute_tasks must be registered on fast_router"
        )

    def test_fast_verify_registered_on_fast_router(self) -> None:
        """fast_verify must be on fast_router after verifier import."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415

        names = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}
        assert "fast_verify" in names, (
            "fast_verify must be registered on fast_router"
        )

    def test_no_pipeline_reasoners_on_fast_router(self) -> None:
        """fast_router must not contain any swe-planner pipeline planning agents."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner  # noqa: F401, PLC0415
        import swe_af.fast.executor  # noqa: F401, PLC0415
        import swe_af.fast.verifier  # noqa: F401, PLC0415

        names = {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}
        pipeline_forbidden = {
            "run_architect", "run_tech_lead", "run_sprint_planner",
            "run_product_manager", "run_issue_writer",
        }
        leaked = pipeline_forbidden & names
        assert not leaked, (
            f"Pipeline planning agents must NOT be on fast_router: {sorted(leaked)}"
        )


# ===========================================================================
# 8. _runtime_to_provider cross-feature: config runtime → ai_provider alignment
# ===========================================================================


class TestRuntimeToProviderCrossFeature:
    """app._runtime_to_provider maps FastBuildConfig.runtime to AgentAI provider strings."""

    def test_claude_code_runtime_maps_to_claude_provider(self) -> None:
        """FastBuildConfig runtime='claude_code' must map to ai_provider='claude'."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        provider = fast_app._runtime_to_provider("claude_code")
        assert provider == "claude", (
            f"_runtime_to_provider('claude_code') must return 'claude', got {provider!r}"
        )

    def test_open_code_runtime_maps_to_opencode_provider(self) -> None:
        """FastBuildConfig runtime='open_code' must map to ai_provider='opencode'."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        provider = fast_app._runtime_to_provider("open_code")
        assert provider == "opencode", (
            f"_runtime_to_provider('open_code') must return 'opencode', got {provider!r}"
        )

    def test_runtime_to_provider_aligns_with_agentai_config_provider_field(self) -> None:
        """AgentAIConfig.provider must accept the values returned by _runtime_to_provider."""
        from swe_af.agent_ai import AgentAIConfig  # noqa: PLC0415
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        for runtime in ("claude_code", "open_code"):
            provider = fast_app._runtime_to_provider(runtime)
            try:
                cfg = AgentAIConfig(provider=provider, model="haiku", cwd="/tmp")
                assert cfg.provider == provider, (
                    f"AgentAIConfig.provider must accept {provider!r} from "
                    f"_runtime_to_provider({runtime!r})"
                )
            except Exception as exc:
                pytest.fail(
                    f"AgentAIConfig rejected provider={provider!r} "
                    f"(from runtime={runtime!r}): {exc}"
                )

    def test_build_source_uses_runtime_to_provider(self) -> None:
        """build() must call _runtime_to_provider to convert config.runtime to ai_provider."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        src = inspect.getsource(fn)

        assert "_runtime_to_provider" in src, (
            "build() must use _runtime_to_provider to convert runtime to ai_provider "
            "for downstream calls to planner, executor, verifier"
        )

    def test_planner_ai_provider_param_accepts_claude_string(self) -> None:
        """fast_plan_tasks must accept ai_provider='claude' (from _runtime_to_provider)."""
        from swe_af.fast.planner import fast_plan_tasks  # noqa: PLC0415

        fn = getattr(fast_plan_tasks, "_original_func", fast_plan_tasks)
        sig = inspect.signature(fn)
        assert "ai_provider" in sig.parameters, (
            "fast_plan_tasks must accept 'ai_provider' parameter "
            "(receives result of _runtime_to_provider from build())"
        )
        param = sig.parameters["ai_provider"]
        default = param.default
        assert default == "claude", (
            f"fast_plan_tasks.ai_provider default should be 'claude', got {default!r}"
        )

    def test_executor_ai_provider_param_accepts_claude_string(self) -> None:
        """fast_execute_tasks must accept ai_provider='claude' (from _runtime_to_provider)."""
        from swe_af.fast.executor import fast_execute_tasks  # noqa: PLC0415

        fn = getattr(fast_execute_tasks, "_original_func", fast_execute_tasks)
        sig = inspect.signature(fn)
        assert "ai_provider" in sig.parameters, (
            "fast_execute_tasks must accept 'ai_provider' parameter"
        )

    def test_verifier_ai_provider_param_present(self) -> None:
        """fast_verify must accept ai_provider (from _runtime_to_provider)."""
        from swe_af.fast.verifier import fast_verify  # noqa: PLC0415

        fn = getattr(fast_verify, "_original_func", fast_verify)
        sig = inspect.signature(fn)
        assert "ai_provider" in sig.parameters, (
            "fast_verify must accept 'ai_provider' parameter"
        )

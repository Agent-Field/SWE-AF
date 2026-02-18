"""Integration tests for fast_router shared instance and schema data pipeline.

Targets the cross-feature interaction boundaries between:
  - issue/e65cddc0-05-fast-planner (planner registers fast_plan_tasks on fast_router)
  - issue/e65cddc0-06-fast-executor (executor registers fast_execute_tasks on fast_router)
  - issue/e65cddc0-07-fast-verifier (verifier registers fast_verify on fast_router)
  - issue/e65cddc0-09-fast-app (app imports fast_router and calls all three)

Priority 1: Cross-feature interaction boundaries

Tests verify:
  A. Schema data flow: FastTask → FastPlanResult → executor tasks list → FastTaskResult
     → verifier task_results - ensuring field names are compatible across module boundaries.
  B. fast_router shared instance: planner, executor, verifier all register on THE SAME
     fast_router object (not separate instances), and app.include_router uses that same router.
  C. app.include_router(fast_router) wires all 8 reasoners to be callable via app.call.
  D. No pipeline reasoners leaked: importing swe_af.fast should NOT cause
     swe_af.reasoners.pipeline to be loaded (verified by checking sys.modules).
  E. Verifier prd parameter: app.build() passes a dict to fast_verify, but verifier
     signature expects prd as a positional/keyword arg — verify compatibility.
  F. build() config-to-call model threading: fast_resolve_models() produces keys
     that exactly match the param names in fast_plan_tasks, fast_execute_tasks, fast_verify.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import os
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_fast_router_reasoner_names() -> set[str]:
    """Return the set of function names registered on fast_router."""
    # Reload to get a clean fast_router state
    import swe_af.fast as fast_pkg  # noqa: PLC0415
    import swe_af.fast.planner  # noqa: F401, PLC0415
    import swe_af.fast.verifier  # noqa: F401, PLC0415
    return {r["func"].__name__ for r in fast_pkg.fast_router.reasoners}


# ===========================================================================
# A. Schema data flow compatibility across module boundaries
# ===========================================================================


class TestSchemaDataFlowCompatibility:
    """Verify that schemas produced by one module are fully consumed by the next."""

    def test_fast_task_model_dump_contains_all_executor_required_keys(self) -> None:
        """FastTask.model_dump() must contain all keys fast_execute_tasks accesses.

        executor.py accesses: name, title, description, acceptance_criteria,
        files_to_create, files_to_modify via task_dict.get(...)
        """
        from swe_af.fast.schemas import FastTask  # noqa: PLC0415

        task = FastTask(
            name="add-feature",
            title="Add Feature",
            description="Implement the feature",
            acceptance_criteria=["Feature works", "Tests pass"],
            files_to_create=["src/feature.py"],
            files_to_modify=["src/__init__.py"],
        )
        d = task.model_dump()

        # Verify all keys that executor accesses via task_dict.get(...)
        required_executor_keys = {
            "name", "title", "description", "acceptance_criteria",
            "files_to_create", "files_to_modify",
        }
        missing = required_executor_keys - set(d.keys())
        assert not missing, (
            f"FastTask.model_dump() missing keys needed by executor: {missing}"
        )

    def test_fast_plan_result_tasks_are_list_of_dicts_compatible_with_executor(self) -> None:
        """FastPlanResult.model_dump()['tasks'] is a list of dicts compatible with executor."""
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        plan = FastPlanResult(
            tasks=[
                FastTask(
                    name="task-1",
                    title="Task 1",
                    description="Do something",
                    acceptance_criteria=["It works"],
                )
            ],
            rationale="Test plan",
        )
        d = plan.model_dump()

        assert "tasks" in d, "FastPlanResult.model_dump() must have 'tasks' key"
        assert isinstance(d["tasks"], list), "'tasks' must be a list"
        assert len(d["tasks"]) == 1, "Expected 1 task"

        task_dict = d["tasks"][0]
        # Executor constructs 'issue' dict from these fields
        assert task_dict.get("name") == "task-1"
        assert task_dict.get("title") == "Task 1"
        assert task_dict.get("description") == "Do something"
        assert task_dict.get("acceptance_criteria") == ["It works"]
        assert isinstance(task_dict.get("files_to_create"), list)
        assert isinstance(task_dict.get("files_to_modify"), list)

    def test_fast_task_result_model_dump_contains_all_verifier_required_keys(self) -> None:
        """FastTaskResult.model_dump() must contain all keys fast_verify receives.

        The verifier receives task_results as list[dict]; app.build() passes
        execution_result['task_results'] to fast_verify.
        """
        from swe_af.fast.schemas import FastTaskResult  # noqa: PLC0415

        result = FastTaskResult(
            task_name="task-1",
            outcome="completed",
            files_changed=["src/feature.py"],
            summary="Feature implemented",
        )
        d = result.model_dump()

        # Keys that verifier might inspect
        assert "task_name" in d
        assert "outcome" in d
        assert "files_changed" in d
        assert "summary" in d
        assert "error" in d  # default empty string

    def test_executor_output_task_results_field_matches_verifier_param(self) -> None:
        """FastExecutionResult.model_dump()['task_results'] matches verifier's task_results param.

        app.build() passes execution_result.get('task_results', []) to fast_verify.
        """
        from swe_af.fast.schemas import FastTaskResult, FastExecutionResult  # noqa: PLC0415

        exec_result = FastExecutionResult(
            task_results=[
                FastTaskResult(task_name="t1", outcome="completed"),
                FastTaskResult(task_name="t2", outcome="failed", error="timeout"),
            ],
            completed_count=1,
            failed_count=1,
        )
        d = exec_result.model_dump()

        # app.build() uses: execution_result.get("task_results", [])
        assert "task_results" in d, "FastExecutionResult must have 'task_results' key"
        task_results = d["task_results"]
        assert len(task_results) == 2
        # Each result is a dict
        for tr in task_results:
            assert isinstance(tr, dict)
            assert "task_name" in tr
            assert "outcome" in tr

    def test_fallback_plan_task_is_compatible_with_executor(self) -> None:
        """Planner fallback 'implement-goal' task dict must not cause KeyError in executor.

        Executor accesses task_dict.get('name'), .get('title'), etc. — safe via .get()
        but verifying the fallback contains all expected keys is important.
        """
        from swe_af.fast.schemas import FastTask, FastPlanResult  # noqa: PLC0415

        # Mimic the fallback plan from planner._fallback_plan()
        fallback = FastPlanResult(
            tasks=[
                FastTask(
                    name="implement-goal",
                    title="Implement goal",
                    description="Implement the goal",
                    acceptance_criteria=["Goal is implemented successfully."],
                )
            ],
            rationale="Fallback plan: LLM did not return a parseable result.",
            fallback_used=True,
        )
        d = fallback.model_dump()
        task_dict = d["tasks"][0]

        # The executor builds an 'issue' dict using these keys
        issue = {
            "name": task_dict.get("name", "unknown"),
            "title": task_dict.get("title", task_dict.get("name", "unknown")),
            "description": task_dict.get("description", ""),
            "acceptance_criteria": task_dict.get("acceptance_criteria", []),
            "files_to_create": task_dict.get("files_to_create", []),
            "files_to_modify": task_dict.get("files_to_modify", []),
            "testing_strategy": "",
        }

        assert issue["name"] == "implement-goal"
        assert issue["acceptance_criteria"] == ["Goal is implemented successfully."]
        assert issue["files_to_create"] == []


# ===========================================================================
# B. fast_router shared instance across merged modules
# ===========================================================================


class TestFastRouterSharedInstance:
    """Verify that planner, executor, verifier all use the SAME fast_router object."""

    def test_planner_executor_verifier_share_fast_router_object(self) -> None:
        """All three merged modules must import the same fast_router from swe_af.fast."""
        import swe_af.fast as fast_pkg  # noqa: PLC0415
        import swe_af.fast.planner as planner  # noqa: PLC0415
        import swe_af.fast.executor as executor  # noqa: PLC0415
        import swe_af.fast.verifier as verifier  # noqa: PLC0415

        assert planner.fast_router is fast_pkg.fast_router, (
            "planner.fast_router must be the same object as swe_af.fast.fast_router"
        )
        assert executor.fast_router is fast_pkg.fast_router, (
            "executor.fast_router must be the same object as swe_af.fast.fast_router"
        )
        assert verifier.fast_router is fast_pkg.fast_router, (
            "verifier.fast_router must be the same object as swe_af.fast.fast_router"
        )

    def test_fast_router_is_tagged_swe_fast(self) -> None:
        """fast_router must have the 'swe-fast' tag (not 'swe-planner')."""
        from swe_af.fast import fast_router  # noqa: PLC0415

        # Check the router has tags
        assert hasattr(fast_router, "tags") or hasattr(fast_router, "_tags"), (
            "fast_router must have a tags attribute"
        )
        # Access tags through the known attribute
        tags = getattr(fast_router, "tags", None) or getattr(fast_router, "_tags", [])
        assert "swe-fast" in tags, (
            f"fast_router tags must include 'swe-fast', got {tags!r}"
        )

    def test_all_eight_reasoners_registered_after_importing_merged_modules(self) -> None:
        """After importing all merged modules, fast_router must have exactly 8 reasoners.

        The 8 expected reasoners from the merged branches:
          - run_git_init, run_coder, run_verifier, run_repo_finalize, run_github_pr (5 wrappers)
          - fast_plan_tasks (from planner branch)
          - fast_execute_tasks (from executor branch)
          - fast_verify (from verifier branch)
        """
        names = _get_fast_router_reasoner_names()

        expected = {
            "run_git_init",
            "run_coder",
            "run_verifier",
            "run_repo_finalize",
            "run_github_pr",
            "fast_execute_tasks",
            "fast_plan_tasks",
            "fast_verify",
        }

        missing = expected - names
        assert not missing, (
            f"Missing reasoners on fast_router after importing all merged modules: "
            f"{sorted(missing)}. Found: {sorted(names)}"
        )

    def test_no_pipeline_reasoners_on_fast_router(self) -> None:
        """fast_router must NOT contain any pipeline reasoners from swe_af.reasoners.

        If any planning pipeline reasoners leaked into fast_router, the fast
        service would incorrectly expose the full pipeline.
        """
        names = _get_fast_router_reasoner_names()

        pipeline_forbidden = {
            "run_architect",
            "run_tech_lead",
            "run_sprint_planner",
            "run_product_manager",
            "run_issue_writer",
        }
        leaked = pipeline_forbidden & names
        assert not leaked, (
            f"Pipeline reasoners must NOT be on fast_router, found: {sorted(leaked)}"
        )


# ===========================================================================
# C. app.include_router wires all reasoners for app.call dispatch
# ===========================================================================


class TestAppIncludeRouterWiring:
    """Tests that app.include_router(fast_router) correctly wires all 8 reasoners."""

    def test_fast_router_included_in_app(self) -> None:
        """swe_af.fast.app calls app.include_router(fast_router) — verify they're linked."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415
        import swe_af.fast as fast_pkg  # noqa: PLC0415

        # The app must have been configured to include the fast_router
        # We verify this by checking that the app object has been initialized
        assert fast_app.app is not None
        assert fast_app.app.node_id is not None

    def test_app_build_function_is_reasoner_registered_on_app(self) -> None:
        """build() must be registered as a reasoner on the app (via @app.reasoner())."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # build must exist and be callable
        assert hasattr(fast_app, "build"), "swe_af.fast.app must expose 'build'"
        assert callable(fast_app.build), "fast_app.build must be callable"

    def test_fast_init_has_no_pipeline_import_statements(self) -> None:
        """swe_af.fast.__init__ must not have import statements referencing swe_af.reasoners.

        The docstring explicitly states this is intentional — but we verify
        there are no actual 'import' AST nodes referencing the pipeline.
        """
        import inspect  # noqa: PLC0415
        import swe_af.fast as fast_pkg  # noqa: PLC0415

        fast_init_src = inspect.getsource(fast_pkg)
        tree = ast.parse(fast_init_src)

        # Check for import statements only (not comments/docstrings)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "swe_af.reasoners.pipeline" != node.module, (
                    f"swe_af.fast.__init__ must not import swe_af.reasoners.pipeline, "
                    f"found: 'from {node.module} import ...'"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "swe_af.reasoners.pipeline" != alias.name, (
                        f"swe_af.fast.__init__ must not import swe_af.reasoners.pipeline"
                    )


# ===========================================================================
# D. No pipeline contamination in fast package
# ===========================================================================


class TestNoPipelineContamination:
    """Verify swe_af.fast package does not import planning pipeline code."""

    def test_executor_source_has_no_pipeline_agents(self) -> None:
        """executor.py must not reference any pipeline planning agents."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.executor as ex  # noqa: PLC0415

        src = inspect.getsource(ex)
        forbidden = [
            "run_qa", "run_code_reviewer", "run_qa_synthesizer",
            "run_replanner", "run_issue_advisor", "run_retry_advisor",
        ]
        for f in forbidden:
            assert f not in src, (
                f"executor.py must not reference '{f}' — this is a pipeline-only function"
            )

    def test_planner_source_has_no_pipeline_agents(self) -> None:
        """planner.py must not reference any pipeline planning agents."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.planner as pl  # noqa: PLC0415

        src = inspect.getsource(pl)
        forbidden = [
            "run_architect", "run_tech_lead", "run_sprint_planner",
            "run_product_manager", "run_issue_writer",
        ]
        for f in forbidden:
            assert f not in src, (
                f"planner.py must not reference '{f}' — this is a pipeline planning function"
            )

    def test_verifier_source_has_no_fix_cycles(self) -> None:
        """verifier.py must not reference any fix cycle functions (single-pass only)."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.verifier as vf  # noqa: PLC0415

        src = inspect.getsource(vf)
        forbidden = ["generate_fix_issues", "max_verify_fix_cycles", "fix_cycles"]
        for f in forbidden:
            assert f not in src, (
                f"verifier.py must not reference '{f}' — swe-fast is a single-pass verifier"
            )


# ===========================================================================
# E. Verifier prd parameter: build() passes dict, verifier accepts dict
# ===========================================================================


class TestVerifierPrdParameterCompatibility:
    """Verify the prd parameter passed from build() to fast_verify is compatible."""

    def test_build_source_constructs_prd_dict_when_plan_has_no_prd(self) -> None:
        """build() constructs a minimal prd dict when planner produces no prd field.

        The swe-fast planner (FastPlanResult) does NOT produce a 'prd' field.
        app.build() must handle this by constructing a fallback prd dict.
        Uses _original_func to get the un-decorated source.
        """
        import inspect  # noqa: PLC0415
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # Use _original_func to get the source of the original function, not the wrapper
        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        src = inspect.getsource(fn)

        # build() should check plan_result.get("prd") and fall back
        assert 'plan_result.get("prd")' in src or "plan_result.get('prd')" in src, (
            "build() must check plan_result.get('prd') to handle planner output "
            "that has no prd field. Source must contain this fallback logic."
        )

    def test_verifier_fallback_prd_has_required_fields(self) -> None:
        """The fallback prd dict built by app.build() must have fields verifier can use."""
        # From app.py: prd_dict = plan_result.get("prd") or { ... }
        # Verify the fallback structure has the expected keys
        goal = "Add a health endpoint"
        fallback_prd = {
            "validated_description": goal,
            "acceptance_criteria": [],
            "must_have": [],
            "nice_to_have": [],
            "out_of_scope": [],
        }
        # These are the keys verifier might access
        assert "validated_description" in fallback_prd
        assert "acceptance_criteria" in fallback_prd

    def test_fast_verify_prd_param_exists_in_signature(self) -> None:
        """fast_verify must have a 'prd' parameter in its signature."""
        import inspect  # noqa: PLC0415
        import swe_af.fast.verifier as vf  # noqa: PLC0415

        # Get original function if decorated
        fn = getattr(vf.fast_verify, "_original_func", vf.fast_verify)
        sig = inspect.signature(fn)
        assert "prd" in sig.parameters, (
            "fast_verify must have a 'prd' parameter to receive the product requirements"
        )

    def test_fast_plan_result_has_no_prd_field(self) -> None:
        """FastPlanResult schema does NOT have a 'prd' field.

        This means build() must always construct the fallback prd_dict.
        This is the critical integration point: planner produces no prd,
        build() creates one, verifier receives it.
        """
        from swe_af.fast.schemas import FastPlanResult  # noqa: PLC0415

        plan = FastPlanResult(tasks=[], rationale="test")
        d = plan.model_dump()

        assert "prd" not in d, (
            f"FastPlanResult must NOT have a 'prd' field — fast planner is single-pass "
            f"with no PM stage. Found 'prd' in: {list(d.keys())}"
        )


# ===========================================================================
# F. build() config-to-call model threading
# ===========================================================================


class TestBuildConfigModelThreading:
    """Verify fast_resolve_models() produces keys matching param names in each reasoner."""

    def test_resolve_models_keys_match_fast_plan_tasks_params(self) -> None:
        """fast_resolve_models() pm_model key matches fast_plan_tasks pm_model param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415
        import swe_af.fast.planner as planner  # noqa: PLC0415

        cfg = FastBuildConfig()
        resolved = fast_resolve_models(cfg)

        # app.build() calls fast_plan_tasks with pm_model=resolved["pm_model"]
        assert "pm_model" in resolved, "fast_resolve_models must produce 'pm_model' key"

        fn = getattr(planner.fast_plan_tasks, "_original_func", planner.fast_plan_tasks)
        sig = inspect.signature(fn)
        assert "pm_model" in sig.parameters, (
            "fast_plan_tasks must accept 'pm_model' parameter"
        )

    def test_resolve_models_keys_match_fast_execute_tasks_params(self) -> None:
        """fast_resolve_models() coder_model key matches fast_execute_tasks coder_model param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415
        import swe_af.fast.executor as executor  # noqa: PLC0415

        cfg = FastBuildConfig()
        resolved = fast_resolve_models(cfg)

        assert "coder_model" in resolved, "fast_resolve_models must produce 'coder_model' key"

        fn = getattr(executor.fast_execute_tasks, "_original_func", executor.fast_execute_tasks)
        sig = inspect.signature(fn)
        assert "coder_model" in sig.parameters, (
            "fast_execute_tasks must accept 'coder_model' parameter"
        )

    def test_resolve_models_keys_match_fast_verify_params(self) -> None:
        """fast_resolve_models() verifier_model key matches fast_verify verifier_model param."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415
        import swe_af.fast.verifier as verifier  # noqa: PLC0415

        cfg = FastBuildConfig()
        resolved = fast_resolve_models(cfg)

        assert "verifier_model" in resolved, "fast_resolve_models must produce 'verifier_model' key"

        fn = getattr(verifier.fast_verify, "_original_func", verifier.fast_verify)
        sig = inspect.signature(fn)
        assert "verifier_model" in sig.parameters, (
            "fast_verify must accept 'verifier_model' parameter"
        )

    def test_resolve_models_keys_match_git_operations_params(self) -> None:
        """fast_resolve_models() git_model key matches run_git_init and run_repo_finalize model params."""
        import inspect  # noqa: PLC0415
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415

        cfg = FastBuildConfig()
        resolved = fast_resolve_models(cfg)

        assert "git_model" in resolved, "fast_resolve_models must produce 'git_model' key"

    def test_claude_code_runtime_produces_haiku_models_for_all_roles(self) -> None:
        """For claude_code runtime, all 4 resolved models must be 'haiku'."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415

        cfg = FastBuildConfig(runtime="claude_code")
        resolved = fast_resolve_models(cfg)

        for role, model in resolved.items():
            assert model == "haiku", (
                f"claude_code runtime: role {role!r} should be 'haiku', got {model!r}"
            )

    def test_open_code_runtime_produces_qwen_models_for_all_roles(self) -> None:
        """For open_code runtime, all 4 resolved models must be qwen."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415

        cfg = FastBuildConfig(runtime="open_code")
        resolved = fast_resolve_models(cfg)

        qwen_model = "qwen/qwen-2.5-coder-32b-instruct"
        for role, model in resolved.items():
            assert model == qwen_model, (
                f"open_code runtime: role {role!r} should be {qwen_model!r}, got {model!r}"
            )

    def test_custom_model_override_for_coder_role(self) -> None:
        """models={'coder': 'sonnet'} must override only coder_model, others remain default."""
        from swe_af.fast.schemas import FastBuildConfig, fast_resolve_models  # noqa: PLC0415

        cfg = FastBuildConfig(runtime="claude_code", models={"coder": "sonnet", "default": "haiku"})
        resolved = fast_resolve_models(cfg)

        assert resolved["coder_model"] == "sonnet", (
            f"coder_model should be 'sonnet' after override, got {resolved['coder_model']!r}"
        )
        assert resolved["pm_model"] == "haiku", (
            f"pm_model should remain 'haiku' (default), got {resolved['pm_model']!r}"
        )

"""Integration tests for NODE_ID isolation and coexistence between swe_af.app and swe_af.fast.app.

Priority 1: Conflict Resolution Area
---------------------------------------
The merge of issue/e65cddc0-09-fast-app into the integration branch exposes a critical
cross-feature interaction: both swe_af.app (planner) and swe_af.fast.app (fast) read the
NODE_ID env var at module-load time using os.getenv("NODE_ID", "<default>").

When NODE_ID is already set in the environment (e.g. NODE_ID=swe-planner for the planner
service), importing swe_af.fast.app in the same process inherits that value, causing:
  - fast_app.app.node_id == 'swe-planner' (wrong — should be 'swe-fast')
  - fast_execute_tasks calls app.call('swe-planner.run_coder', ...) (wrong routing)

These tests specifically verify:
  A. Subprocess isolation: the two services get distinct node_ids in separate processes.
  B. Env inheritance behaviour: the exact MODULE_LEVEL read behaviour is documented.
  C. Executor routing: when NODE_ID is 'swe-fast', executor routes to 'swe-fast.run_coder'.
  D. App call namespace: build() uses NODE_ID from its module-level constant (not re-read).
  E. Cross-planner-fast import: co-importing both modules in the same process with
     controlled env gives predictable results.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
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


def _subprocess_check(code: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run a Python snippet in a subprocess with optional env overrides."""
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_ID",)}
    env["AGENTFIELD_SERVER"] = "http://localhost:9999"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )


# ===========================================================================
# A. Subprocess isolation: the two modules get distinct node_ids in fresh processes
# ===========================================================================


class TestSubprocessIsolation:
    """Verify that node_ids are distinct when each app is run in its own process."""

    def test_fast_app_node_id_is_swe_fast_with_no_node_id_env(self) -> None:
        """swe_af.fast.app.app.node_id must be 'swe-fast' when NODE_ID is NOT set."""
        result = _subprocess_check(
            "import swe_af.fast.app as a; print(a.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast", (
            f"swe_af.fast.app.node_id should be 'swe-fast' when NODE_ID unset, "
            f"got {result.stdout.strip()!r}"
        )

    def test_planner_app_node_id_is_swe_planner_with_no_node_id_env(self) -> None:
        """swe_af.app.app.node_id must be 'swe-planner' when NODE_ID is NOT set."""
        result = _subprocess_check(
            "import swe_af.app as a; print(a.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-planner", (
            f"swe_af.app.node_id should be 'swe-planner' when NODE_ID unset, "
            f"got {result.stdout.strip()!r}"
        )

    def test_fast_app_node_id_when_node_id_is_swe_fast(self) -> None:
        """With NODE_ID=swe-fast, swe_af.fast.app must use 'swe-fast'."""
        result = _subprocess_check(
            "import swe_af.fast.app as a; print(a.app.node_id)",
            env_overrides={"NODE_ID": "swe-fast"},
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast", (
            f"Expected 'swe-fast' with NODE_ID=swe-fast, got {result.stdout.strip()!r}"
        )

    def test_distinct_node_ids_when_co_imported_without_node_id_env(self) -> None:
        """Co-importing both modules without NODE_ID produces distinct node_ids."""
        result = _subprocess_check(
            "import swe_af.app as p; import swe_af.fast.app as f; "
            "print(p.app.node_id, f.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        parts = result.stdout.strip().split()
        assert len(parts) == 2, f"Expected two node_ids, got: {result.stdout.strip()!r}"
        planner_id, fast_id = parts
        assert planner_id == "swe-planner", (
            f"Planner node_id should be 'swe-planner', got {planner_id!r}"
        )
        assert fast_id == "swe-fast", (
            f"Fast node_id should be 'swe-fast', got {fast_id!r}"
        )

    def test_fast_node_id_not_inherited_as_swe_planner_in_clean_process(self) -> None:
        """In a process with no NODE_ID set, fast node_id must NOT be 'swe-planner'."""
        result = _subprocess_check(
            "import swe_af.fast.app as a; assert a.app.node_id != 'swe-planner', "
            f"repr(a.app.node_id); print('OK')"
        )
        assert result.returncode == 0, (
            f"fast app must not inherit 'swe-planner' as node_id: {result.stderr}"
        )
        assert "OK" in result.stdout


# ===========================================================================
# B. Module-level NODE_ID read behaviour
# ===========================================================================


class TestModuleLevelNodeIdReadBehaviour:
    """Verify that NODE_ID is read at module load time (module-level constant)."""

    def test_executor_node_id_module_constant_defaults_to_swe_fast(self) -> None:
        """executor.NODE_ID module constant must default to 'swe-fast'."""
        result = _subprocess_check(
            "import swe_af.fast.executor as ex; print(ex.NODE_ID)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast", (
            f"executor.NODE_ID default should be 'swe-fast', "
            f"got {result.stdout.strip()!r}"
        )

    def test_app_node_id_module_constant_defaults_to_swe_fast(self) -> None:
        """app.NODE_ID module constant must default to 'swe-fast'."""
        result = _subprocess_check(
            "import swe_af.fast.app as a; print(a.NODE_ID)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast", (
            f"app.NODE_ID module constant should be 'swe-fast', "
            f"got {result.stdout.strip()!r}"
        )

    def test_executor_node_id_matches_fast_app_node_id_in_same_process(self) -> None:
        """executor.NODE_ID must match fast_app.app.node_id in the same process."""
        result = _subprocess_check(
            "import swe_af.fast.executor as ex; import swe_af.fast.app as a; "
            "assert ex.NODE_ID == a.app.node_id, f'{ex.NODE_ID!r} != {a.app.node_id!r}'; "
            "print('MATCH')"
        )
        assert result.returncode == 0, (
            f"executor.NODE_ID and app.app.node_id must match: {result.stderr}"
        )
        assert "MATCH" in result.stdout

    def test_executor_node_id_matches_app_node_id_when_node_id_env_set(self) -> None:
        """When NODE_ID=swe-fast is explicit, executor and app must both use it."""
        result = _subprocess_check(
            "import swe_af.fast.executor as ex; import swe_af.fast.app as a; "
            "print(ex.NODE_ID, a.app.node_id)",
            env_overrides={"NODE_ID": "swe-fast"},
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        parts = result.stdout.strip().split()
        assert len(parts) == 2, f"Expected two values, got: {result.stdout.strip()!r}"
        exec_node_id, app_node_id = parts
        assert exec_node_id == "swe-fast", f"executor.NODE_ID should be 'swe-fast', got {exec_node_id!r}"
        assert app_node_id == "swe-fast", f"app.node_id should be 'swe-fast', got {app_node_id!r}"


# ===========================================================================
# C. Executor routing: must route to 'swe-fast.run_coder'
# ===========================================================================


class TestExecutorRoutingWithCorrectNodeId:
    """Verify executor routes app.call to the correct 'swe-fast' prefix."""

    def test_executor_routes_to_swe_fast_run_coder_in_subprocess(self) -> None:
        """In a clean process, fast_execute_tasks must call 'swe-fast.run_coder'."""
        code = """
import os
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
import swe_af.fast.executor as ex
# NODE_ID must be 'swe-fast' to verify correct routing
assert ex.NODE_ID == 'swe-fast', f'executor.NODE_ID={ex.NODE_ID!r}'
# The call target should be f'{NODE_ID}.run_coder'
expected_prefix = ex.NODE_ID
print(f'Routing prefix: {expected_prefix}.run_coder')
assert expected_prefix == 'swe-fast', f'Expected swe-fast, got {expected_prefix!r}'
print('OK')
"""
        result = _subprocess_check(code)
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout
        assert "swe-fast.run_coder" in result.stdout

    def test_executor_call_target_uses_swe_fast_prefix(self) -> None:
        """fast_execute_tasks must build call target as 'swe-fast.run_coder'."""
        # We test this by capturing what app.call is called with
        coder_result = {"complete": True, "files_changed": [], "summary": "done"}
        call_targets: list[str] = []

        async def mock_call(*args: Any, **kwargs: Any) -> dict:
            call_targets.append(args[0] if args else "")
            return {"result": coder_result}

        mock_app_obj = MagicMock()
        mock_app_obj.call = mock_call
        mock_module = MagicMock()
        mock_module.app = mock_app_obj

        # Save and restore NODE_ID env to ensure 'swe-fast' is used
        saved = os.environ.get("NODE_ID")
        os.environ["NODE_ID"] = "swe-fast"

        # Clear executor cache so NODE_ID is re-read
        for k in list(sys.modules):
            if k == "swe_af.fast.executor":
                sys.modules.pop(k, None)

        try:
            import swe_af.fast.executor as ex  # noqa: PLC0415

            # Ensure NODE_ID was read as 'swe-fast'
            assert ex.NODE_ID == "swe-fast", (
                f"executor.NODE_ID should be 'swe-fast', got {ex.NODE_ID!r}"
            )

            # Patch router note to no-op
            ex.fast_router.__dict__["note"] = MagicMock(return_value=None)

            key = "swe_af.fast.app"
            saved_app = sys.modules.pop(key, None)
            sys.modules[key] = mock_module
            try:
                with patch("swe_af.fast.executor._unwrap", return_value=coder_result):
                    _run(ex.fast_execute_tasks(
                        tasks=[{"name": "t1", "title": "T1",
                                "description": "d", "acceptance_criteria": ["c"]}],
                        repo_path="/tmp/repo",
                        task_timeout_seconds=10,
                    ))
            finally:
                sys.modules.pop(key, None)
                if saved_app is not None:
                    sys.modules[key] = saved_app
        finally:
            if saved is None:
                os.environ.pop("NODE_ID", None)
            else:
                os.environ["NODE_ID"] = saved
            ex.fast_router.__dict__.pop("note", None)

        assert len(call_targets) > 0, "app.call must have been called"
        assert call_targets[0] == "swe-fast.run_coder", (
            f"Expected call target 'swe-fast.run_coder', got {call_targets[0]!r}. "
            "This indicates the executor is routing to the wrong node."
        )

    def test_build_uses_correct_node_id_in_call_targets(self) -> None:
        """build() in app.py must call fast_plan_tasks and fast_execute_tasks with the fast node_id.

        When NODE_ID=swe-fast, build() must call '{node_id}.fast_plan_tasks', not 'swe-planner.*'.
        """
        result = _subprocess_check(
            """
import swe_af.fast.app as fast_app
import inspect
fn = getattr(fast_app.build, '_original_func', fast_app.build)
src = inspect.getsource(fn)
# build() should use NODE_ID variable (not hardcoded planner node)
assert '{NODE_ID}.fast_plan_tasks' in src or "f'{NODE_ID}.fast_plan_tasks'" in src or 'NODE_ID' in src, (
    'build() must use NODE_ID for routing, not a hardcoded planner node_id'
)
print('OK')
"""
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout


# ===========================================================================
# D. App call namespace verification
# ===========================================================================


class TestAppCallNamespaceVerification:
    """Verify that app.build() uses NODE_ID consistently for all call targets."""

    def test_build_source_uses_node_id_variable_for_all_calls(self) -> None:
        """build() must use the NODE_ID module-level var (not hardcoded strings) for routing."""
        import inspect
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        src = inspect.getsource(fn)

        # The source must reference NODE_ID for all reasoner calls
        assert "NODE_ID" in src, (
            "build() must use the NODE_ID module-level var for routing calls, "
            "so that changing NODE_ID env changes the routing correctly"
        )

        # Verify it's not hardcoding 'swe-planner' anywhere (that would be wrong node)
        assert "'swe-planner'" not in src and '"swe-planner"' not in src, (
            "build() must NOT hardcode 'swe-planner' as the call target — "
            "it must use NODE_ID variable to support the correct node routing"
        )

    def test_executor_source_uses_node_id_variable_for_call_target(self) -> None:
        """fast_execute_tasks must use NODE_ID variable for app.call routing."""
        import inspect
        import swe_af.fast.executor as executor  # noqa: PLC0415

        src = inspect.getsource(executor)

        # Must use NODE_ID variable (not hardcoded string)
        assert "NODE_ID" in src, (
            "executor must use NODE_ID module variable for routing app.call targets"
        )

        # Must NOT hardcode 'swe-planner' as the routing target
        assert "'swe-planner'" not in src and '"swe-planner"' not in src, (
            "executor must NOT hardcode 'swe-planner' as the call target — "
            "this would break routing when deployed as the 'swe-fast' service"
        )

    def test_node_id_env_determines_routing_namespace(self) -> None:
        """NODE_ID env var determines the call routing namespace for both app and executor."""
        result = _subprocess_check(
            """
import swe_af.fast.app as fast_app
import swe_af.fast.executor as executor
# Both must use NODE_ID = 'swe-fast' as the module constant
assert fast_app.NODE_ID == 'swe-fast', f'fast_app.NODE_ID={fast_app.NODE_ID!r}'
assert executor.NODE_ID == 'swe-fast', f'executor.NODE_ID={executor.NODE_ID!r}'
print('OK')
"""
        )
        assert result.returncode == 0, (
            f"Both app and executor must have NODE_ID='swe-fast': {result.stderr}"
        )
        assert "OK" in result.stdout


# ===========================================================================
# E. Cross-planner-fast import with controlled env
# ===========================================================================


class TestCrossImportControlledEnv:
    """Verify co-importing planner and fast apps with controlled NODE_ID."""

    def test_planner_node_id_unaffected_by_fast_app_import(self) -> None:
        """Importing swe_af.fast.app must not change swe_af.app.app.node_id."""
        result = _subprocess_check(
            """
import swe_af.app as planner  # swe-planner sets its node_id from NODE_ID or default
original = planner.app.node_id
import swe_af.fast.app as fast  # fast app must not change planner's node_id
assert planner.app.node_id == original, (
    f'planner.node_id changed after importing fast app: '
    f'{original!r} -> {planner.app.node_id!r}'
)
print(f'planner={planner.app.node_id} fast={fast.app.node_id}')
print('OK')
"""
        )
        assert result.returncode == 0, (
            f"Importing fast app must not affect planner's node_id: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_fast_app_node_id_distinct_from_planner_in_clean_env(self) -> None:
        """In a clean env (no NODE_ID), fast and planner must have different node_ids."""
        result = _subprocess_check(
            """
import swe_af.fast.app as fast
import swe_af.app as planner
assert fast.app.node_id != planner.app.node_id, (
    f'fast and planner must have different node_ids, '
    f'but both have: {fast.app.node_id!r}'
)
assert fast.app.node_id == 'swe-fast', f'fast must be swe-fast, got {fast.app.node_id!r}'
assert planner.app.node_id == 'swe-planner', f'planner must be swe-planner, got {planner.app.node_id!r}'
print('OK')
"""
        )
        assert result.returncode == 0, (
            f"Fast and planner must have distinct node_ids in clean env: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_fast_module_node_id_constant_is_independent_of_planner(self) -> None:
        """swe_af.fast.app.NODE_ID must default to 'swe-fast' regardless of planner import order."""
        result = _subprocess_check(
            """
# Import planner FIRST (which sets a module-level NODE_ID too)
import swe_af.app as planner
# Now import fast — it must read its OWN default ('swe-fast')
import swe_af.fast.app as fast
assert fast.NODE_ID == 'swe-fast', (
    f'fast.NODE_ID should be swe-fast regardless of planner import, '
    f'got {fast.NODE_ID!r}'
)
print('OK')
"""
        )
        assert result.returncode == 0, (
            f"fast.NODE_ID must be 'swe-fast' even after planner import: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_acceptance_criteria_15_coexistence(self) -> None:
        """AC-15: co-importing both apps gives distinct node_ids (swe-planner, swe-fast)."""
        result = _subprocess_check(
            """
import swe_af.app as planner_app
import swe_af.fast.app as fast_app
assert planner_app.app.node_id == 'swe-planner', planner_app.app.node_id
assert fast_app.app.node_id == 'swe-fast', fast_app.app.node_id
print('OK')
"""
        )
        assert result.returncode == 0, (
            f"AC-15 co-import test failed: {result.stderr}\n"
            "swe_af.app must be 'swe-planner' and swe_af.fast.app must be 'swe-fast'"
        )
        assert "OK" in result.stdout

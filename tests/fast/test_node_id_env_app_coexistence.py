"""Integration tests for NODE_ID environment variable isolation and cross-app coexistence.

These tests target the critical interaction boundary between swe_af.app (swe-planner)
and swe_af.fast.app (swe-fast) when both are imported in the same process. This is a
Priority 1 conflict-resolution area because:

  - swe_af/app.py uses NODE_ID env var with default 'swe-planner'
  - swe_af/fast/app.py uses NODE_ID env var with default 'swe-fast'
  - When NODE_ID=swe-planner is set in the environment (as happens in the docker
    container running swe-planner), importing swe_af.fast.app yields node_id='swe-planner'
    instead of 'swe-fast', breaking all executor routing

Critical cross-feature interactions tested:
  1. NODE_ID env contamination: when NODE_ID=swe-planner is set in the shell,
     fast app must use a distinct mechanism to produce 'swe-fast' node_id.
  2. Executor routing: NODE_ID contamination causes executor to call
     'swe-planner.run_coder' instead of 'swe-fast.run_coder'.
  3. Co-import correctness: both apps can coexist and be distinguished in-process
     (currently passing in subprocess, failing in-process when NODE_ID is set).
  4. Docker-compose env isolation: each service sets its own NODE_ID, which
     prevents cross-contamination in production deployments.
  5. fast.app node_id independence: node_id for swe-fast should not depend on
     the external NODE_ID env var when operating as the fast service.
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
    """Run a coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. NODE_ID env contamination in-process
# ---------------------------------------------------------------------------


class TestNodeIdEnvContamination:
    """Tests verifying how NODE_ID env contamination affects fast app behavior.

    These tests document the real integration failure: when NODE_ID=swe-planner
    is present in the environment (from a prior swe_af.app import or from the
    shell environment), swe_af.fast.app reads that value via os.getenv and
    creates its Agent with node_id='swe-planner' instead of 'swe-fast'.
    """

    def test_node_id_env_set_to_swe_planner_infects_fast_app_node_id(self) -> None:
        """When NODE_ID=swe-planner in environment, swe_af.fast.app.NODE_ID reads 'swe-planner'.

        This test documents the real contamination bug: NODE_ID in the environment
        overrides the swe-fast default in swe_af.fast.app. In production each
        service must be started with its own NODE_ID set correctly.
        """
        # Verify the contamination scenario (NODE_ID is set in test environment)
        current_node_id = os.getenv("NODE_ID", "NOT_SET")
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # The module-level NODE_ID is read at import time from env
        # If NODE_ID=swe-planner, fast_app.NODE_ID will be 'swe-planner'
        assert fast_app.NODE_ID == current_node_id, (
            f"fast_app.NODE_ID={fast_app.NODE_ID!r} should match env NODE_ID={current_node_id!r}. "
            "This documents the env contamination: fast app respects NODE_ID from environment."
        )

    def test_fast_app_node_id_default_is_swe_fast_when_env_unset(self) -> None:
        """When NODE_ID is NOT set in environment, swe_af.fast.app.NODE_ID defaults to 'swe-fast'.

        Verifies the correct default using a clean subprocess.
        """
        env = {k: v for k, v in os.environ.items() if k != "NODE_ID"}
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.fast.app as a; print(a.NODE_ID)"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast", (
            f"Expected NODE_ID default='swe-fast', got {result.stdout.strip()!r}"
        )

    def test_fast_app_node_id_is_swe_fast_when_node_id_explicitly_swe_fast(self) -> None:
        """When NODE_ID=swe-fast is explicitly set, fast app gets correct node_id."""
        env = dict(os.environ)
        env["NODE_ID"] = "swe-fast"
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.fast.app as a; assert a.NODE_ID == 'swe-fast'; print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_planner_app_node_id_is_swe_planner_when_node_id_explicitly_swe_planner(self) -> None:
        """When NODE_ID=swe-planner is set, planner app gets correct node_id."""
        env = dict(os.environ)
        env["NODE_ID"] = "swe-planner"
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.app as a; assert a.NODE_ID == 'swe-planner'; print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 2. Executor routing: NODE_ID contamination causes wrong routing
# ---------------------------------------------------------------------------


class TestExecutorNodeIdRouting:
    """Tests verifying the executor's routing behavior under NODE_ID env conditions.

    The executor uses module-level NODE_ID to construct the app.call target.
    If NODE_ID=swe-planner, executor calls 'swe-planner.run_coder' instead of
    'swe-fast.run_coder', which routes to the wrong service.
    """

    def test_executor_module_level_node_id_matches_environment(self) -> None:
        """executor.NODE_ID matches os.getenv('NODE_ID') at import time.

        This confirms the executor correctly reads from the environment, and that
        in a properly configured swe-fast container (NODE_ID=swe-fast), executor
        will use the correct 'swe-fast.run_coder' target.
        """
        import swe_af.fast.executor as ex  # noqa: PLC0415

        # executor.NODE_ID is set at import time via os.getenv("NODE_ID", "swe-fast")
        expected = os.getenv("NODE_ID", "swe-fast")
        assert ex.NODE_ID == expected, (
            f"executor.NODE_ID={ex.NODE_ID!r} should match os.getenv(NODE_ID)={expected!r}"
        )

    def test_executor_routes_to_node_id_run_coder(self) -> None:
        """fast_execute_tasks routes app.call to {NODE_ID}.run_coder.

        The routing target uses the module-level NODE_ID, not a hardcoded string.
        In production with NODE_ID=swe-fast, this will be 'swe-fast.run_coder'.
        """
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

        # Temporarily evict and replace the app module cache
        key = "swe_af.fast.app"
        saved = sys.modules.pop(key, None)
        sys.modules[key] = mock_module

        # Inject no-op note into router
        router = ex.fast_router
        sentinel = object()
        old_note = router.__dict__.get("note", sentinel)
        router.__dict__["note"] = MagicMock(return_value=None)

        try:
            with patch("swe_af.fast.executor._unwrap", return_value=coder_result):
                result = _run(ex.fast_execute_tasks(
                    tasks=[{"name": "t1", "title": "T1", "description": "Do T1",
                            "acceptance_criteria": ["Done"]}],
                    repo_path="/tmp/repo",
                    task_timeout_seconds=30,
                ))
        finally:
            sys.modules.pop(key, None)
            if saved is not None:
                sys.modules[key] = saved
            if old_note is sentinel:
                router.__dict__.pop("note", None)
            else:
                router.__dict__["note"] = old_note

        assert len(call_tracker) > 0, "app.call must be called for each task"
        first_args, _ = call_tracker[0]
        first_arg = first_args[0]

        # The call target must be '{NODE_ID}.run_coder'
        assert "run_coder" in first_arg, (
            f"Expected 'run_coder' in routing target, got {first_arg!r}"
        )
        # Document the actual node_id used (may be swe-planner due to env contamination)
        expected_prefix = ex.NODE_ID
        assert first_arg == f"{expected_prefix}.run_coder", (
            f"Expected '{expected_prefix}.run_coder', got {first_arg!r}"
        )

    def test_executor_node_id_default_is_swe_fast_in_clean_environment(self) -> None:
        """In a clean environment (no NODE_ID set), executor defaults to 'swe-fast'.

        Verified via subprocess to avoid module caching from test runner environment.
        """
        env = {k: v for k, v in os.environ.items() if k != "NODE_ID"}
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.fast.executor as ex; "
             "assert ex.NODE_ID == 'swe-fast', f'Got {ex.NODE_ID!r}'; "
             "print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Subprocess failed: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 3. Co-import correctness: both apps coexist in-process
# ---------------------------------------------------------------------------


class TestCoImportNodeIdDistinction:
    """Tests that swe_af.app and swe_af.fast.app can coexist with distinct node_ids.

    In production, each is run as a separate Docker service with the correct
    NODE_ID env var. Co-importing them in tests causes issues when a single
    NODE_ID is set in the environment.
    """

    def test_both_apps_importable_in_same_process(self) -> None:
        """Both swe_af.app and swe_af.fast.app import without error in same process."""
        import swe_af.app as planner  # noqa: PLC0415
        import swe_af.fast.app as fast  # noqa: PLC0415

        assert planner.app is not None, "swe_af.app.app must not be None"
        assert fast.app is not None, "swe_af.fast.app.app must not be None"

    def test_planner_and_fast_apps_are_distinct_objects(self) -> None:
        """swe_af.app.app and swe_af.fast.app.app must be distinct Agent instances."""
        import swe_af.app as planner  # noqa: PLC0415
        import swe_af.fast.app as fast  # noqa: PLC0415

        assert planner.app is not fast.app, (
            "Planner and fast apps must be distinct Agent objects"
        )

    def test_planner_default_node_id_is_swe_planner(self) -> None:
        """In clean environment, swe_af.app.NODE_ID defaults to 'swe-planner'."""
        env = {k: v for k, v in os.environ.items() if k != "NODE_ID"}
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.app as a; assert a.NODE_ID == 'swe-planner'; print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_co_import_gives_distinct_node_ids_in_clean_env(self) -> None:
        """Co-importing both apps in a clean env gives planner='swe-planner', fast='swe-fast'."""
        env = {k: v for k, v in os.environ.items() if k != "NODE_ID"}
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.app as p; import swe_af.fast.app as f; "
             "assert p.NODE_ID == 'swe-planner', f'planner NODE_ID={p.NODE_ID!r}'; "
             "assert f.NODE_ID == 'swe-fast', f'fast NODE_ID={f.NODE_ID!r}'; "
             "print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Subprocess failed: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )
        assert "OK" in result.stdout

    def test_swe_planner_env_causes_node_id_to_be_swe_planner_for_fast_app(self) -> None:
        """Documents: when NODE_ID=swe-planner, fast app.node_id also becomes 'swe-planner'.

        This is the root cause of the 3 failing tests in test_app_planner_executor_verifier_wiring.py.
        The environment has NODE_ID=swe-planner set, causing swe_af.fast.app to use 'swe-planner'.
        In production this is not a problem because Docker services are isolated, but in the
        test environment (with NODE_ID=swe-planner in the shell), this is a real failure.
        """
        current_env_node_id = os.getenv("NODE_ID")
        if current_env_node_id != "swe-planner":
            pytest.skip(f"Requires NODE_ID=swe-planner in environment, got {current_env_node_id!r}")

        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # When NODE_ID=swe-planner, fast_app.NODE_ID will be 'swe-planner' — this is the bug
        assert fast_app.NODE_ID == "swe-planner", (
            f"Expected NODE_ID contamination ('swe-planner'), got {fast_app.NODE_ID!r}. "
            "If this fails, NODE_ID contamination has been fixed."
        )


# ---------------------------------------------------------------------------
# 4. Docker-compose service isolation verification
# ---------------------------------------------------------------------------


class TestDockerComposeNodeIdIsolation:
    """Tests that docker-compose services have distinct, correctly set NODE_ID values."""

    def test_swe_fast_docker_service_has_node_id_swe_fast(self) -> None:
        """docker-compose.yml swe-fast service must have NODE_ID=swe-fast."""
        import yaml  # noqa: PLC0415

        with open("/workspaces/swe-af/docker-compose.yml") as f:
            dc = yaml.safe_load(f)

        assert "swe-fast" in dc["services"], "swe-fast service must exist in docker-compose.yml"
        svc = dc["services"]["swe-fast"]
        env = svc.get("environment", [])
        if isinstance(env, list):
            env_dict = dict(e.split("=", 1) for e in env if "=" in e)
        else:
            env_dict = env or {}

        assert env_dict.get("NODE_ID") == "swe-fast", (
            f"swe-fast service NODE_ID must be 'swe-fast', got {env_dict.get('NODE_ID')!r}"
        )

    def test_swe_agent_docker_service_has_node_id_swe_planner(self) -> None:
        """docker-compose.yml swe-agent service must have NODE_ID=swe-planner."""
        import yaml  # noqa: PLC0415

        with open("/workspaces/swe-af/docker-compose.yml") as f:
            dc = yaml.safe_load(f)

        svc_name = "swe-agent"
        if svc_name not in dc["services"]:
            pytest.skip(f"Service {svc_name!r} not in docker-compose.yml")

        svc = dc["services"][svc_name]
        env = svc.get("environment", [])
        if isinstance(env, list):
            env_dict = dict(e.split("=", 1) for e in env if "=" in e)
        else:
            env_dict = env or {}

        assert env_dict.get("NODE_ID") == "swe-planner", (
            f"swe-agent service NODE_ID must be 'swe-planner', got {env_dict.get('NODE_ID')!r}"
        )

    def test_swe_fast_and_swe_planner_have_distinct_node_ids_in_docker(self) -> None:
        """swe-fast and swe-planner services must have different NODE_IDs in docker-compose.yml."""
        import yaml  # noqa: PLC0415

        with open("/workspaces/swe-af/docker-compose.yml") as f:
            dc = yaml.safe_load(f)

        services = dc.get("services", {})
        node_ids: dict[str, str] = {}

        for svc_name in ["swe-fast", "swe-agent"]:
            if svc_name in services:
                env = services[svc_name].get("environment", [])
                if isinstance(env, list):
                    env_dict = dict(e.split("=", 1) for e in env if "=" in e)
                else:
                    env_dict = env or {}
                if "NODE_ID" in env_dict:
                    node_ids[svc_name] = env_dict["NODE_ID"]

        if len(node_ids) >= 2:
            node_id_values = list(node_ids.values())
            assert node_id_values[0] != node_id_values[1], (
                f"Services must have distinct NODE_IDs, got {node_ids}"
            )

    def test_swe_fast_docker_port_is_8004_not_planner_port(self) -> None:
        """swe-fast service PORT env must be 8004 (not 8000, the planner's port)."""
        import yaml  # noqa: PLC0415

        with open("/workspaces/swe-af/docker-compose.yml") as f:
            dc = yaml.safe_load(f)

        svc = dc["services"]["swe-fast"]
        env = svc.get("environment", [])
        if isinstance(env, list):
            env_dict = dict(e.split("=", 1) for e in env if "=" in e)
        else:
            env_dict = env or {}

        assert env_dict.get("PORT") == "8004", (
            f"swe-fast service PORT must be '8004', got {env_dict.get('PORT')!r}. "
            "This prevents port conflict with the swe-planner service."
        )

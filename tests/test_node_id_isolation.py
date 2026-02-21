"""Tests for NODE_ID env var isolation in swe-planner (swe_af.app) and swe-fast (swe_af.fast.app).

Covers AC-12: NODE_ID env var isolation for both apps.

Each app reads NODE_ID at module import time via os.getenv, so tests use
subprocess execution or importlib.reload to observe the env var's effect.

No real API calls are made — AGENTFIELD_SERVER must be set to a local address.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

# Ensure AGENTFIELD_SERVER is set before importing app modules
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run_python(code: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run *code* in a clean subprocess with AGENTFIELD_SERVER set."""
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_ID",)}
    env["AGENTFIELD_SERVER"] = "http://localhost:9999"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# AC-12: swe-planner (swe_af.app) NODE_ID isolation
# ---------------------------------------------------------------------------


class TestPlannerNodeIdIsolation:
    """AC-12: swe_af.app reads NODE_ID from env at import time."""

    def test_planner_app_node_id_default_is_swe_planner(self) -> None:
        """When NODE_ID is unset, swe_af.app.app.node_id defaults to 'swe-planner'."""
        result = _run_python(
            "import swe_af.app as a; print(a.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-planner"

    def test_planner_app_node_id_matches_env_when_set(self) -> None:
        """When NODE_ID=custom-planner, swe_af.app.app.node_id is 'custom-planner'."""
        result = _run_python(
            "import swe_af.app as a; print(a.app.node_id)",
            extra_env={"NODE_ID": "custom-planner"},
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "custom-planner"

    def test_planner_node_id_module_constant_default(self) -> None:
        """swe_af.app.NODE_ID module constant defaults to 'swe-planner' when env is unset."""
        result = _run_python(
            "import swe_af.app as a; print(a.NODE_ID)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-planner"

    def test_planner_app_node_id_reflects_reload_with_new_env(self) -> None:
        """After reload with NODE_ID=new-planner, swe_af.app.NODE_ID updates."""
        saved = os.environ.pop("NODE_ID", None)
        try:
            import swe_af.app as planner_app  # noqa: PLC0415

            # First check: default value
            original_node_id = planner_app.NODE_ID

            # Reload with a different NODE_ID
            os.environ["NODE_ID"] = "new-planner"
            importlib.reload(planner_app)
            assert planner_app.NODE_ID == "new-planner"
            assert planner_app.app.node_id == "new-planner"
        finally:
            # Restore environment and reload to original state
            os.environ.pop("NODE_ID", None)
            if saved is not None:
                os.environ["NODE_ID"] = saved
            importlib.reload(planner_app)
            # After restore, NODE_ID should be back to default
            assert planner_app.NODE_ID == (saved if saved else "swe-planner")

    def test_planner_node_id_default_stored_in_module_constant(self) -> None:
        """swe_af.app module has NODE_ID attribute equal to app.node_id."""
        import swe_af.app as planner_app  # noqa: PLC0415

        assert hasattr(planner_app, "NODE_ID")
        assert planner_app.NODE_ID == planner_app.app.node_id


# ---------------------------------------------------------------------------
# AC-12: swe-fast (swe_af.fast.app) NODE_ID isolation
# ---------------------------------------------------------------------------


class TestFastNodeIdIsolation:
    """AC-12: swe_af.fast.app reads NODE_ID from env at import time."""

    def test_fast_app_node_id_default_is_swe_fast(self) -> None:
        """When NODE_ID is unset, swe_af.fast.app.app.node_id defaults to 'swe-fast'."""
        result = _run_python(
            "import swe_af.fast.app as a; print(a.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast"

    def test_fast_app_node_id_matches_env_when_set(self) -> None:
        """When NODE_ID=custom-fast, swe_af.fast.app.app.node_id is 'custom-fast'."""
        result = _run_python(
            "import swe_af.fast.app as a; print(a.app.node_id)",
            extra_env={"NODE_ID": "custom-fast"},
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "custom-fast"

    def test_fast_node_id_module_constant_default(self) -> None:
        """swe_af.fast.app.NODE_ID module constant defaults to 'swe-fast' when env is unset."""
        result = _run_python(
            "import swe_af.fast.app as a; print(a.NODE_ID)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast"

    def test_fast_app_node_id_reflects_reload_with_new_env(self) -> None:
        """After reload with NODE_ID=new-fast, swe_af.fast.app.NODE_ID updates."""
        saved = os.environ.pop("NODE_ID", None)
        try:
            import swe_af.fast.app as fast_app  # noqa: PLC0415

            # Reload with a different NODE_ID
            os.environ["NODE_ID"] = "new-fast"
            importlib.reload(fast_app)
            assert fast_app.NODE_ID == "new-fast"
            assert fast_app.app.node_id == "new-fast"
        finally:
            # Restore environment and reload to original state
            os.environ.pop("NODE_ID", None)
            if saved is not None:
                os.environ["NODE_ID"] = saved
            importlib.reload(fast_app)
            assert fast_app.NODE_ID == (saved if saved else "swe-fast")

    def test_fast_node_id_default_stored_in_module_constant(self) -> None:
        """swe_af.fast.app module has NODE_ID attribute equal to app.node_id."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        assert hasattr(fast_app, "NODE_ID")
        assert fast_app.NODE_ID == fast_app.app.node_id


# ---------------------------------------------------------------------------
# AC-12: Cross-isolation — planner and fast have distinct node_ids
# ---------------------------------------------------------------------------


class TestPlannerFastNodeIdDistinction:
    """AC-12: swe_af.app and swe_af.fast.app have distinct node_ids when NODE_ID is unset."""

    def test_planner_and_fast_have_distinct_default_node_ids_in_subprocess(self) -> None:
        """When NODE_ID is unset, planner gets 'swe-planner' and fast gets 'swe-fast'."""
        result = _run_python(
            "import swe_af.app as p; import swe_af.fast.app as f; "
            "assert p.app.node_id != f.app.node_id, "
            "f'Expected distinct node_ids but both are {p.app.node_id!r}'; "
            "print(p.app.node_id, f.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        parts = result.stdout.strip().split()
        assert len(parts) == 2
        planner_id, fast_id = parts
        assert planner_id == "swe-planner"
        assert fast_id == "swe-fast"

    def test_planner_node_id_is_swe_planner_and_fast_is_swe_fast_by_default(self) -> None:
        """Confirm exact default values: planner='swe-planner', fast='swe-fast'."""
        import swe_af.app as planner_app  # noqa: PLC0415
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        planner_expected = os.environ.get("NODE_ID", "swe-planner")
        fast_expected = os.environ.get("NODE_ID", "swe-fast")

        assert planner_app.app.node_id == planner_expected
        assert fast_app.app.node_id == fast_expected

    def test_planner_and_fast_are_distinct_agent_instances(self) -> None:
        """swe_af.app.app and swe_af.fast.app.app are distinct Agent objects."""
        import swe_af.app as planner_app  # noqa: PLC0415
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        assert planner_app.app is not fast_app.app

    def test_planner_node_id_constant_is_swe_planner_in_subprocess(self) -> None:
        """MODULE-LEVEL: swe_af.app.NODE_ID == 'swe-planner' when NODE_ID env is absent."""
        result = _run_python(
            "import swe_af.app as a; assert a.NODE_ID == 'swe-planner', a.NODE_ID; print('OK')"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_fast_node_id_constant_is_swe_fast_in_subprocess(self) -> None:
        """MODULE-LEVEL: swe_af.fast.app.NODE_ID == 'swe-fast' when NODE_ID env is absent."""
        result = _run_python(
            "import swe_af.fast.app as a; assert a.NODE_ID == 'swe-fast', a.NODE_ID; print('OK')"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout

    def test_planner_and_fast_node_ids_distinct_in_subprocess_co_import(self) -> None:
        """AC-12: co-import yields swe-planner and swe-fast as node_ids."""
        result = _run_python(
            "import swe_af.app as p; import swe_af.fast.app as f; "
            "assert p.app.node_id == 'swe-planner', p.app.node_id; "
            "assert f.app.node_id == 'swe-fast', f.app.node_id; "
            "print('planner:', p.app.node_id, 'fast:', f.app.node_id)"
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "planner: swe-planner" in result.stdout
        assert "fast: swe-fast" in result.stdout

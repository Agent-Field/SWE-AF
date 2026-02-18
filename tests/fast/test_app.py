"""Tests for swe_af.fast.app and swe_af.fast.__main__.

Covers:
- app.node_id == 'swe-fast' when NODE_ID defaults to 'swe-fast' (AC-8)
- build() signature has required parameters (AC-9)
- main is callable (AC-16)
- Co-import with swe_af.app gives distinct node_ids when env is unset (AC-15)
- build() timeout path returns FastBuildResult(success=False) with 'timed out' (AC-on-timeout)
- build() success path returns FastBuildResult(success=True)
- repo_url without repo_path auto-derives repo_path
- Missing both repo_path and repo_url raises ValueError
- __main__.py exists and imports main from swe_af.fast.app
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure AGENTFIELD_SERVER is set before importing app modules
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Unit tests: module importability and basic properties
# ---------------------------------------------------------------------------


class TestAppNodeId:
    """AC-8: app.node_id == 'swe-fast' with AGENTFIELD_SERVER set."""

    def test_app_node_id_matches_node_id_env(self) -> None:
        """app.node_id must match NODE_ID env var (default 'swe-fast')."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # node_id comes from NODE_ID env var with default "swe-fast"
        expected = os.environ.get("NODE_ID", "swe-fast")
        assert fast_app.app.node_id == expected

    def test_app_node_id_default_is_swe_fast_in_subprocess(self) -> None:
        """When NODE_ID is unset, app.node_id defaults to 'swe-fast'."""
        # Run in a subprocess to avoid module caching issues
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("NODE_ID",)
        }
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.fast.app as a; print(a.app.node_id)"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "swe-fast"

    def test_app_import_does_not_raise(self) -> None:
        """Importing swe_af.fast.app with AGENTFIELD_SERVER set does not raise."""
        import swe_af.fast.app as _fast_app  # noqa: PLC0415

        assert _fast_app is not None

    def test_app_node_id_is_swe_fast_when_node_id_set(self) -> None:
        """When NODE_ID=swe-fast, app.node_id is 'swe-fast'."""
        env = dict(os.environ)
        env["NODE_ID"] = "swe-fast"
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.fast.app as a; assert a.app.node_id == 'swe-fast'; print('OK')"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "OK" in result.stdout


class TestBuildSignature:
    """AC-9: build() signature has required parameters."""

    def _get_build_params(self) -> set[str]:
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # The @app.reasoner() decorator wraps the function; use _original_func
        # to get the true signature.
        fn = getattr(fast_app.build, "_original_func", fast_app.build)
        sig = inspect.signature(fn)
        return set(sig.parameters.keys())

    def test_build_has_goal_param(self) -> None:
        assert "goal" in self._get_build_params()

    def test_build_has_repo_path_param(self) -> None:
        assert "repo_path" in self._get_build_params()

    def test_build_has_repo_url_param(self) -> None:
        assert "repo_url" in self._get_build_params()

    def test_build_has_artifacts_dir_param(self) -> None:
        assert "artifacts_dir" in self._get_build_params()

    def test_build_has_additional_context_param(self) -> None:
        assert "additional_context" in self._get_build_params()

    def test_build_has_config_param(self) -> None:
        assert "config" in self._get_build_params()


class TestMainCallable:
    """AC-16: main is callable."""

    def test_main_is_callable(self) -> None:
        from swe_af.fast.app import main  # noqa: PLC0415

        assert callable(main)


class TestCoImport:
    """AC-15: Co-importing swe_af.app and swe_af.fast.app yields distinct node_ids."""

    def test_planner_and_fast_node_ids_are_distinct_in_subprocess(self) -> None:
        """When NODE_ID is unset, swe_af.app gets 'swe-planner', swe_af.fast.app gets 'swe-fast'."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("NODE_ID",)
        }
        env["AGENTFIELD_SERVER"] = "http://localhost:9999"
        result = subprocess.run(
            [sys.executable, "-c",
             "import swe_af.app as p; import swe_af.fast.app as f; "
             "assert p.app.node_id == 'swe-planner', p.app.node_id; "
             "assert f.app.node_id == 'swe-fast', f.app.node_id; "
             "print('planner:', p.app.node_id, 'fast:', f.app.node_id)"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        assert "planner: swe-planner" in result.stdout
        assert "fast: swe-fast" in result.stdout

    def test_fast_and_planner_modules_both_importable(self) -> None:
        """Both swe_af.app and swe_af.fast.app import without error."""
        import swe_af.app as _planner  # noqa: PLC0415
        import swe_af.fast.app as _fast  # noqa: PLC0415

        assert _planner.app is not None
        assert _fast.app is not None


class TestMainModuleExists:
    """__main__.py exists and imports main from swe_af.fast.app."""

    def test_main_module_file_exists(self) -> None:
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fast_app_path = os.path.dirname(fast_app.__file__)
        main_module_path = os.path.join(fast_app_path, "__main__.py")
        assert os.path.isfile(main_module_path), f"__main__.py not found at {main_module_path}"

    def test_main_module_imports_main(self) -> None:
        """__main__.py content imports main from swe_af.fast.app."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        fast_app_path = os.path.dirname(fast_app.__file__)
        main_module_path = os.path.join(fast_app_path, "__main__.py")
        with open(main_module_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "from swe_af.fast.app import main" in content


# ---------------------------------------------------------------------------
# Functional tests: mocked build pipeline
# ---------------------------------------------------------------------------


def _make_plan_result() -> dict:
    return {
        "tasks": [
            {
                "name": "task-1",
                "title": "Task 1",
                "description": "Do something.",
                "acceptance_criteria": ["AC 1"],
            }
        ],
        "rationale": "Simple plan",
        "fallback_used": False,
    }


def _make_execution_result() -> dict:
    return {
        "task_results": [
            {"task_name": "task-1", "outcome": "completed", "summary": "Done", "files_changed": []}
        ],
        "completed_count": 1,
        "failed_count": 0,
        "timed_out": False,
    }


def _make_verification_result(passed: bool = True) -> dict:
    return {
        "passed": passed,
        "summary": "All criteria met" if passed else "Some criteria failed",
        "criteria_results": [],
        "suggested_fixes": [],
    }


def _make_git_init_result() -> dict:
    return {
        "success": True,
        "integration_branch": "feature/build-abc123",
        "original_branch": "main",
        "initial_commit_sha": "abc123",
        "mode": "branch",
        "remote_url": "",
        "remote_default_branch": "main",
    }


def _make_finalize_result() -> dict:
    return {"success": True, "summary": "Finalized"}


class TestBuildSuccessPath:
    """Functional: build() success path returns FastBuildResult(success=True)."""

    @pytest.mark.asyncio
    async def test_build_success_returns_success_true(self, tmp_path) -> None:
        """build() with mocked app.call returns FastBuildResult(success=True)."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        plan_result = _make_plan_result()
        execution_result = _make_execution_result()
        verification_result = _make_verification_result(passed=True)
        git_init_result = _make_git_init_result()
        finalize_result = _make_finalize_result()

        # Map of call target prefix -> return value
        call_responses = {
            "run_git_init": git_init_result,
            "fast_plan_tasks": plan_result,
            "fast_execute_tasks": execution_result,
            "fast_verify": verification_result,
            "run_repo_finalize": finalize_result,
        }

        async def mock_call(target: str, **kwargs):
            for key, value in call_responses.items():
                if key in target:
                    return {"result": value}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
        ):
            result = await fast_app.build(
                goal="Add a health endpoint",
                repo_path=str(tmp_path),
            )

        assert result["success"] is True
        assert "Success" in result["summary"]

    @pytest.mark.asyncio
    async def test_build_failure_returns_success_false(self, tmp_path) -> None:
        """build() with failed verification returns FastBuildResult(success=False)."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        plan_result = _make_plan_result()
        execution_result = _make_execution_result()
        verification_result = _make_verification_result(passed=False)
        git_init_result = _make_git_init_result()
        finalize_result = _make_finalize_result()

        call_responses = {
            "run_git_init": git_init_result,
            "fast_plan_tasks": plan_result,
            "fast_execute_tasks": execution_result,
            "fast_verify": verification_result,
            "run_repo_finalize": finalize_result,
        }

        async def mock_call(target: str, **kwargs):
            for key, value in call_responses.items():
                if key in target:
                    return {"result": value}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
        ):
            result = await fast_app.build(
                goal="Add a health endpoint",
                repo_path=str(tmp_path),
            )

        assert result["success"] is False
        assert "Partial" in result["summary"]


class TestBuildTimeoutPath:
    """Functional: build() timeout path returns FastBuildResult(success=False) with 'timed out'."""

    @pytest.mark.asyncio
    async def test_build_timeout_returns_success_false_with_timed_out_message(
        self, tmp_path
    ) -> None:
        """asyncio.TimeoutError during plan+execute returns FastBuildResult(success=False)."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        git_init_result = _make_git_init_result()

        async def mock_call(target: str, **kwargs):
            if "run_git_init" in target:
                return {"result": git_init_result}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        # Mock asyncio.wait_for to raise TimeoutError to simulate build timeout
        async def mock_wait_for(coro, timeout):
            coro.close()  # close the coroutine to avoid warning
            raise asyncio.TimeoutError()

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
            patch("swe_af.fast.app.asyncio.wait_for", side_effect=mock_wait_for),
        ):
            result = await fast_app.build(
                goal="Add a health endpoint",
                repo_path=str(tmp_path),
            )

        assert result["success"] is False
        assert "timed out" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_build_timeout_result_has_timed_out_execution(self, tmp_path) -> None:
        """Timed out build result has execution_result.timed_out == True."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        git_init_result = _make_git_init_result()

        async def mock_call(target: str, **kwargs):
            if "run_git_init" in target:
                return {"result": git_init_result}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        # Mock asyncio.wait_for to raise TimeoutError to simulate build timeout
        async def mock_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
            patch("swe_af.fast.app.asyncio.wait_for", side_effect=mock_wait_for),
        ):
            result = await fast_app.build(
                goal="Add a health endpoint",
                repo_path=str(tmp_path),
            )

        assert result["execution_result"]["timed_out"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestBuildEdgeCases:
    """Edge cases for build()."""

    def test_missing_repo_path_and_repo_url_raises_value_error(self) -> None:
        """build() raises ValueError when both repo_path and repo_url are missing."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        with pytest.raises(ValueError, match="Either repo_path or repo_url must be provided"):
            asyncio.run(fast_app.build(goal="Do something"))

    @pytest.mark.asyncio
    async def test_repo_url_without_repo_path_auto_derives_repo_path(self, tmp_path) -> None:
        """repo_url without repo_path auto-derives repo_path from URL."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        # Track which repo_path gets used when calling
        called_with_repo_path: list[str] = []
        git_init_result = _make_git_init_result()

        # Track makedirs calls to see derived path
        derived_paths: list[str] = []
        original_makedirs = os.makedirs

        def capture_makedirs(path, exist_ok=False, **kwargs):
            derived_paths.append(str(path))
            # Don't create /workspaces/ paths
            if not str(path).startswith("/workspaces/"):
                original_makedirs(path, exist_ok=exist_ok, **kwargs)

        plan_result = _make_plan_result()
        execution_result = _make_execution_result()
        verification_result = _make_verification_result(passed=True)
        finalize_result = _make_finalize_result()

        async def mock_call(target: str, **kwargs):
            if "run_git_init" in target:
                called_with_repo_path.append(kwargs.get("repo_path", ""))
                return {"result": git_init_result}
            if "fast_plan_tasks" in target:
                return {"result": plan_result}
            if "fast_execute_tasks" in target:
                return {"result": execution_result}
            if "fast_verify" in target:
                return {"result": verification_result}
            if "run_repo_finalize" in target:
                return {"result": finalize_result}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
            patch("swe_af.fast.app.os.makedirs", side_effect=capture_makedirs),
        ):
            result = await fast_app.build(
                goal="Do something",
                repo_url="https://github.com/user/my-project.git",
            )

        # repo_path should have been derived from the URL
        all_paths = derived_paths + called_with_repo_path
        assert any("my-project" in p for p in all_paths), (
            f"Expected 'my-project' in derived paths {all_paths}"
        )

    def test_repo_name_from_url_helper(self) -> None:
        """_repo_name_from_url extracts name correctly from various URL formats."""
        from swe_af.fast.app import _repo_name_from_url  # noqa: PLC0415

        assert _repo_name_from_url("https://github.com/user/my-project.git") == "my-project"
        assert _repo_name_from_url("https://github.com/user/my-project") == "my-project"
        assert _repo_name_from_url("git@github.com:user/my-project.git") == "my-project"

    def test_runtime_to_provider_helper(self) -> None:
        """_runtime_to_provider maps runtime strings correctly."""
        from swe_af.fast.app import _runtime_to_provider  # noqa: PLC0415

        assert _runtime_to_provider("claude_code") == "claude"
        assert _runtime_to_provider("open_code") == "opencode"
        assert _runtime_to_provider("other") == "opencode"


class TestBuildNonFatalPaths:
    """Tests that non-fatal stages (git_init, finalize, PR) don't bubble up exceptions."""

    @pytest.mark.asyncio
    async def test_git_init_exception_is_non_fatal(self, tmp_path) -> None:
        """Exception during git_init does not prevent build from continuing."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        plan_result = _make_plan_result()
        execution_result = _make_execution_result()
        verification_result = _make_verification_result(passed=True)
        finalize_result = _make_finalize_result()

        async def mock_call(target: str, **kwargs):
            if "run_git_init" in target:
                raise RuntimeError("git init failed")
            if "fast_plan_tasks" in target:
                return {"result": plan_result}
            if "fast_execute_tasks" in target:
                return {"result": execution_result}
            if "fast_verify" in target:
                return {"result": verification_result}
            if "run_repo_finalize" in target:
                return {"result": finalize_result}
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
        ):
            # Should not raise — git_init exception is non-fatal
            result = await fast_app.build(
                goal="Do something",
                repo_path=str(tmp_path),
            )

        # Build should still complete
        assert "success" in result

    @pytest.mark.asyncio
    async def test_finalize_exception_is_non_fatal(self, tmp_path) -> None:
        """Exception during finalize does not prevent build from returning a result."""
        import swe_af.fast.app as fast_app  # noqa: PLC0415

        plan_result = _make_plan_result()
        execution_result = _make_execution_result()
        verification_result = _make_verification_result(passed=True)
        git_init_result = _make_git_init_result()

        async def mock_call(target: str, **kwargs):
            if "run_git_init" in target:
                return {"result": git_init_result}
            if "fast_plan_tasks" in target:
                return {"result": plan_result}
            if "fast_execute_tasks" in target:
                return {"result": execution_result}
            if "fast_verify" in target:
                return {"result": verification_result}
            if "run_repo_finalize" in target:
                raise RuntimeError("finalize failed")
            return {"result": {}}

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        with (
            patch.object(fast_app.app, "call", side_effect=mock_call),
            patch.object(fast_app.app, "note", return_value=None),
            patch("swe_af.fast.app._unwrap", side_effect=mock_unwrap),
        ):
            # Should not raise — finalize exception is non-fatal
            result = await fast_app.build(
                goal="Do something",
                repo_path=str(tmp_path),
            )

        assert result["success"] is True

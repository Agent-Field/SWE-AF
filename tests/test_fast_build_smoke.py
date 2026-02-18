"""Smoke tests for fast_build reasoner and related refactoring.

Verifies acceptance criteria for the fast_build feature:
- AC-1: fast_build is registered as a reasoner
- AC-2: fast_build and build have identical signatures
- AC-3: _run_build is async and not a registered reasoner
- AC-4: build is still registered as a reasoner
- AC-5: fast_build source contains all 12 fast default keys/values
- AC-6: fast_build applies fast defaults when no caller config is supplied
- AC-7: caller-supplied config overrides fast defaults
- AC-8: build() uses unmodified BuildConfig defaults
- AC-10: swe_af.app imports cleanly
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
import unittest.mock as mock

from swe_af.execution.schemas import BuildConfig, BuildResult


def _make_fake_run_build(captured: dict):
    """Return a fake _run_build coroutine that captures the cfg dict."""

    async def fake_run_build(goal, repo_path, artifacts_dir, additional_context, cfg, **kwargs):
        captured.update(cfg.model_dump())
        return BuildResult(
            plan_result={},
            dag_state={},
            verification=None,
            success=False,
            summary="",
            pr_url="",
        ).model_dump()

    return fake_run_build


class TestFastBuildSmoke(unittest.TestCase):

    # -----------------------------------------------------------------------
    # AC-10: import sanity (run first so other tests can import cleanly)
    # -----------------------------------------------------------------------

    def test_ac10_module_imports_cleanly(self):
        """AC-10: swe_af.app imports without errors."""
        import swe_af.app  # noqa: F401

    # -----------------------------------------------------------------------
    # AC-1: fast_build registered as a reasoner
    # -----------------------------------------------------------------------

    def test_ac1_fast_build_registered_as_reasoner(self):
        """AC-1: fast_build is present in app.reasoners."""
        from swe_af.app import app

        # app.reasoners returns List[Dict] with 'id' keys
        registered_ids = {r["id"] for r in app.reasoners}
        self.assertIn(
            "fast_build",
            registered_ids,
            f"fast_build not found in registered reasoners: {sorted(registered_ids)}",
        )

    # -----------------------------------------------------------------------
    # AC-2: fast_build signature identical to build
    # -----------------------------------------------------------------------

    def test_ac2_signatures_identical(self):
        """AC-2: build and fast_build have the same parameter signatures."""
        from swe_af.app import build, fast_build

        build_sig = inspect.signature(build)
        fast_sig = inspect.signature(fast_build)

        build_params = {
            k: (v.default, v.annotation)
            for k, v in build_sig.parameters.items()
        }
        fast_params = {
            k: (v.default, v.annotation)
            for k, v in fast_sig.parameters.items()
        }

        self.assertEqual(
            build_params,
            fast_params,
            f"Signatures differ:\nbuild: {build_params}\nfast_build: {fast_params}",
        )

    # -----------------------------------------------------------------------
    # AC-3: _run_build is async and NOT a registered reasoner
    # -----------------------------------------------------------------------

    def test_ac3_run_build_is_async_and_not_a_reasoner(self):
        """AC-3: _run_build exists, is a coroutine function, is not a reasoner."""
        from swe_af.app import _run_build, app

        self.assertTrue(
            inspect.iscoroutinefunction(_run_build),
            "_run_build must be an async function",
        )

        registered_ids = {r["id"] for r in app.reasoners}
        self.assertNotIn(
            "_run_build",
            registered_ids,
            "_run_build must NOT be registered as a reasoner",
        )

    # -----------------------------------------------------------------------
    # AC-4: build is still registered (no regression)
    # -----------------------------------------------------------------------

    def test_ac4_build_still_registered(self):
        """AC-4: build is still present in app.reasoners after refactoring."""
        from swe_af.app import app

        registered_ids = {r["id"] for r in app.reasoners}
        self.assertIn(
            "build",
            registered_ids,
            f"build not found in registered reasoners: {sorted(registered_ids)}",
        )

    # -----------------------------------------------------------------------
    # AC-5: fast_build source contains all 12 fast default keys+values
    # -----------------------------------------------------------------------

    def test_ac5_fast_defaults_present_in_source(self):
        """AC-5: All 12 fast default keys and values appear in fast_build source."""
        from swe_af import app as app_mod

        src = inspect.getsource(app_mod.fast_build)

        fast_defaults = {
            "max_review_iterations": "0",
            "max_retries_per_issue": "0",
            "max_replans": "0",
            "enable_replanning": "False",
            "max_verify_fix_cycles": "0",
            "max_coding_iterations": "1",
            "max_advisor_invocations": "0",
            "enable_issue_advisor": "False",
            "enable_integration_testing": "False",
            "agent_max_turns": "50",
            "agent_timeout_seconds": "900",
            "git_init_max_retries": "1",
        }

        for key, val in fast_defaults.items():
            self.assertIn(key, src, f"Key not found in fast_build source: {key!r}")
            self.assertIn(val, src, f"Value {val!r} not found in fast_build source (near {key!r})")

    # -----------------------------------------------------------------------
    # AC-6: fast_build with no config applies all fast defaults to BuildConfig
    # -----------------------------------------------------------------------

    def test_ac6_fast_defaults_applied_when_no_config(self):
        """AC-6: fast_build passes BuildConfig with all 12 fast defaults to _run_build."""
        import swe_af.app as mod

        captured: dict = {}
        fake = _make_fake_run_build(captured)

        with mock.patch.object(mod, "_run_build", fake):
            asyncio.run(mod.fast_build(goal="test", repo_path="/tmp/fake"))

        expects = {
            "max_review_iterations": 0,
            "max_retries_per_issue": 0,
            "max_replans": 0,
            "enable_replanning": False,
            "max_verify_fix_cycles": 0,
            "max_coding_iterations": 1,
            "max_advisor_invocations": 0,
            "enable_issue_advisor": False,
            "enable_integration_testing": False,
            "agent_max_turns": 50,
            "agent_timeout_seconds": 900,
            "git_init_max_retries": 1,
        }

        for key, expected_val in expects.items():
            self.assertEqual(
                captured[key],
                expected_val,
                f"{key}: expected {expected_val!r}, got {captured[key]!r}",
            )

    # -----------------------------------------------------------------------
    # AC-7: caller-supplied config keys override fast defaults
    # -----------------------------------------------------------------------

    def test_ac7_caller_config_overrides_fast_defaults(self):
        """AC-7: A caller-supplied config key overrides the fast default for that key."""
        import swe_af.app as mod

        captured: dict = {}
        fake = _make_fake_run_build(captured)

        with mock.patch.object(mod, "_run_build", fake):
            asyncio.run(
                mod.fast_build(
                    goal="test",
                    repo_path="/tmp/fake",
                    config={"max_coding_iterations": 3},
                )
            )

        # Caller override should take effect
        self.assertEqual(
            captured["max_coding_iterations"],
            3,
            f"Caller override ignored: expected 3, got {captured['max_coding_iterations']}",
        )
        # Other fast defaults should remain unchanged
        self.assertEqual(
            captured["max_review_iterations"],
            0,
            f"Unexpected change to max_review_iterations: {captured['max_review_iterations']}",
        )

    # -----------------------------------------------------------------------
    # AC-8: build() passes unmodified BuildConfig defaults to _run_build
    # -----------------------------------------------------------------------

    def test_ac8_build_uses_unmodified_buildconfig_defaults(self):
        """AC-8: build() with no config passes BuildConfig with library-default values."""
        import swe_af.app as mod

        captured: dict = {}
        fake = _make_fake_run_build(captured)

        with mock.patch.object(mod, "_run_build", fake):
            asyncio.run(mod.build(goal="test", repo_path="/tmp/fake"))

        defaults = BuildConfig()

        for field in ["max_review_iterations", "max_coding_iterations", "agent_max_turns"]:
            default_val = getattr(defaults, field)
            self.assertEqual(
                captured[field],
                default_val,
                f"build() modified {field}: expected {default_val!r}, got {captured[field]!r}",
            )


if __name__ == "__main__":
    unittest.main()

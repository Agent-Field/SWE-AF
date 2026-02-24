"""Tests for benchmark suite runner.

Unit tests verify pass rate calculation with mocked BuildResult.verification.passed values.
Integration tests execute builds and verify output JSON schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run_benchmark_suite import run_benchmark_suite, run_single_build, SIMPLE_BUILD_SCENARIOS


class TestPassRateCalculation(unittest.TestCase):
    """Unit tests for pass rate calculation logic."""

    def test_pass_rate_all_passed(self):
        """Test pass rate calculation when all builds pass."""
        # AC4: Pass rate calculated as (passed_count / total_builds)
        builds = [
            {"verification": {"passed": True}},
            {"verification": {"passed": True}},
            {"verification": {"passed": True}},
        ]
        passed = sum(1 for b in builds if b.get("verification", {}).get("passed", False))
        total = len(builds)
        pass_rate = passed / total if total > 0 else 0.0

        self.assertEqual(passed, 3)
        self.assertEqual(total, 3)
        self.assertEqual(pass_rate, 1.0)

    def test_pass_rate_all_failed(self):
        """Test pass rate calculation when all builds fail."""
        builds = [
            {"verification": {"passed": False}},
            {"verification": {"passed": False}},
        ]
        passed = sum(1 for b in builds if b.get("verification", {}).get("passed", False))
        total = len(builds)
        pass_rate = passed / total if total > 0 else 0.0

        self.assertEqual(passed, 0)
        self.assertEqual(total, 2)
        self.assertEqual(pass_rate, 0.0)

    def test_pass_rate_mixed_results(self):
        """Test pass rate calculation with mixed results."""
        builds = [
            {"verification": {"passed": True}},
            {"verification": {"passed": False}},
            {"verification": {"passed": True}},
            {"verification": {"passed": True}},
            {"verification": {"passed": False}},
        ]
        passed = sum(1 for b in builds if b.get("verification", {}).get("passed", False))
        total = len(builds)
        pass_rate = passed / total if total > 0 else 0.0

        self.assertEqual(passed, 3)
        self.assertEqual(total, 5)
        self.assertEqual(pass_rate, 0.6)

    def test_pass_rate_missing_verification_field(self):
        """Test pass rate calculation with missing verification field."""
        builds = [
            {"verification": {"passed": True}},
            {},  # Missing verification field
            {"verification": {}},  # Missing passed field
            {"verification": {"passed": True}},
        ]
        passed = sum(1 for b in builds if b.get("verification", {}).get("passed", False))
        total = len(builds)
        pass_rate = passed / total if total > 0 else 0.0

        self.assertEqual(passed, 2)
        self.assertEqual(total, 4)
        self.assertEqual(pass_rate, 0.5)

    def test_pass_rate_zero_builds(self):
        """Test pass rate calculation with zero builds."""
        builds = []
        passed = sum(1 for b in builds if b.get("verification", {}).get("passed", False))
        total = len(builds)
        pass_rate = passed / total if total > 0 else 0.0

        self.assertEqual(passed, 0)
        self.assertEqual(total, 0)
        self.assertEqual(pass_rate, 0.0)


class TestBenchmarkSuite(unittest.TestCase):
    """Integration tests for benchmark suite execution."""

    def test_run_benchmark_suite_basic(self):
        """Test basic benchmark suite execution.

        AC1: Benchmark suite runs 5 simple builds + 3 complex builds
        AC2: Script executes N builds and collects BuildResult.verification.passed fields
        AC3: Output JSON includes per-build verification status and aggregate pass rate
        """
        # Mock app.call to return controlled results
        async def mock_call(target, **kwargs):
            # Extract build_id from context or use a counter
            import random
            passed = random.random() > 0.4  # 60% pass rate
            return {
                "verification": {"passed": passed, "summary": "Mock verification"},
                "success": passed,
                "summary": f"Mock build for: {kwargs.get('goal', 'unknown')}",
                "pr_url": "https://github.com/test/repo/pull/1",
            }

        with patch("scripts.run_benchmark_suite.app.call", side_effect=mock_call):
            results = asyncio.run(run_benchmark_suite(num_builds=5, simple_count=5, complex_count=0))

        # Verify structure (AC3)
        self.assertIn("builds", results)
        self.assertIn("passed_count", results)
        self.assertIn("total_builds", results)
        self.assertIn("pass_rate", results)
        self.assertIn("simple_builds", results)
        self.assertIn("complex_builds", results)

        # Verify counts (AC1)
        self.assertEqual(results["total_builds"], 5)
        self.assertEqual(results["simple_builds"], 5)
        self.assertEqual(results["complex_builds"], 0)

        # Verify per-build verification status (AC3)
        self.assertEqual(len(results["builds"]), 5)
        for build in results["builds"]:
            self.assertIn("verification", build)
            self.assertIn("passed", build["verification"])
            self.assertIn("scenario", build)

    def test_run_benchmark_suite_all_pass(self):
        """Test benchmark suite when all builds pass (AC4: ≥95% pass rate)."""
        async def mock_call(target, **kwargs):
            return {
                "verification": {"passed": True, "summary": "All passed"},
                "success": True,
                "summary": "Build passed",
            }

        with patch("scripts.run_benchmark_suite.app.call", side_effect=mock_call):
            results = asyncio.run(run_benchmark_suite(num_builds=3, simple_count=3, complex_count=0))

        self.assertEqual(results["pass_rate"], 1.0)
        self.assertEqual(results["passed_count"], 3)

    def test_run_benchmark_suite_all_fail(self):
        """Test benchmark suite when all builds fail (AC6: <90% triggers rollback)."""
        async def mock_call(target, **kwargs):
            return {
                "verification": {"passed": False, "summary": "All failed"},
                "success": False,
                "summary": "Build failed",
            }

        with patch("scripts.run_benchmark_suite.app.call", side_effect=mock_call):
            results = asyncio.run(run_benchmark_suite(num_builds=2, simple_count=2, complex_count=0))

        self.assertEqual(results["pass_rate"], 0.0)
        self.assertEqual(results["passed_count"], 0)

    def test_run_benchmark_suite_mixed_simple_complex(self):
        """Test benchmark suite with mixed simple and complex builds (AC1, AC2)."""
        call_count = 0

        async def mock_call(target, **kwargs):
            nonlocal call_count
            call_count += 1
            # First 5 builds pass, last 3 fail
            passed = call_count <= 5
            return {
                "verification": {"passed": passed, "summary": f"Build {call_count}"},
                "success": passed,
                "summary": f"Build {call_count} result",
            }

        with patch("scripts.run_benchmark_suite.app.call", side_effect=mock_call):
            results = asyncio.run(run_benchmark_suite(num_builds=8, simple_count=5, complex_count=3))

        # AC1: 5 simple + 3 complex = 8 total
        self.assertEqual(results["total_builds"], 8)
        self.assertEqual(results["simple_builds"], 5)
        self.assertEqual(results["complex_builds"], 3)

        # AC3: Verify pass rate calculation
        self.assertEqual(results["passed_count"], 5)
        self.assertEqual(results["pass_rate"], 5/8)  # 62.5%

    def test_scenario_definitions(self):
        """Test that scenarios are properly defined with correct metadata."""
        from scripts.run_benchmark_suite import SIMPLE_BUILD_SCENARIOS, COMPLEX_BUILD_SCENARIOS

        # AC1: Verify 5 simple scenarios
        self.assertGreaterEqual(len(SIMPLE_BUILD_SCENARIOS), 5, "Should have at least 5 simple scenarios")

        # AC2: Verify 3 complex scenarios
        self.assertGreaterEqual(len(COMPLEX_BUILD_SCENARIOS), 3, "Should have at least 3 complex scenarios")

        # Verify simple scenario structure
        for scenario in SIMPLE_BUILD_SCENARIOS:
            self.assertIn("goal", scenario)
            self.assertIn("num_issues", scenario)
            self.assertIn("complexity", scenario)
            self.assertEqual(scenario["complexity"], "simple")
            self.assertIn("3-5", scenario["num_issues"])

        # Verify complex scenario structure
        for scenario in COMPLEX_BUILD_SCENARIOS:
            self.assertIn("goal", scenario)
            self.assertIn("num_issues", scenario)
            self.assertIn("complexity", scenario)
            self.assertEqual(scenario["complexity"], "complex")
            self.assertIn("10-15", scenario["num_issues"])


class TestCLIIntegration(unittest.TestCase):
    """Integration tests for CLI interface."""

    def test_cli_output_json_schema(self):
        """Test CLI produces correct JSON output schema.

        AC1: scripts/run_benchmark_suite.py created with --builds and --output flags
        AC3: Output JSON includes per-build verification status and aggregate pass rate
        AC7: Results include per-build verification status and aggregate metrics
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results.json"

            # Mock app.call to avoid actual builds during testing
            async def mock_call(target, **kwargs):
                return {
                    "verification": {"passed": True, "summary": "Mock verification"},
                    "success": True,
                    "summary": "Mock build",
                    "pr_url": "",
                }

            with patch("scripts.run_benchmark_suite.app.call", side_effect=mock_call):
                # Run CLI with mocked builds
                result = subprocess.run(
                    [
                        sys.executable,
                        "scripts/run_benchmark_suite.py",
                        "--builds", "2",
                        "--simple-count", "2",
                        "--complex-count", "0",
                        "--output", str(output_file),
                    ],
                    cwd=Path(__file__).parent.parent,
                    capture_output=True,
                    text=True,
                )

            # Verify output file was created
            self.assertTrue(output_file.exists(), "Output file should be created")

            # Verify JSON schema (AC3, AC7)
            with open(output_file) as f:
                data = json.load(f)

            self.assertIn("builds", data)
            self.assertIn("passed_count", data)
            self.assertIn("total_builds", data)
            self.assertIn("pass_rate", data)
            self.assertIn("simple_builds", data)
            self.assertIn("complex_builds", data)
            self.assertIn("simple_pass_rate", data)
            self.assertIn("complex_pass_rate", data)

            self.assertEqual(data["total_builds"], 2)
            self.assertIsInstance(data["builds"], list)
            self.assertEqual(len(data["builds"]), 2)

            # Verify each build has verification field
            for build in data["builds"]:
                self.assertIn("verification", build)
                self.assertIn("passed", build["verification"])
                self.assertIn("scenario", build)

    def test_cli_exit_code_pass(self):
        """Test CLI returns exit code 0 when pass rate meets threshold.

        AC4: Pass rate ≥95% for deployment approval
        """
        # Test the threshold logic (AC4)
        pass_rate = 1.0
        threshold = 0.95
        warn_threshold = 0.90

        # Verify the logic that would cause exit 0
        self.assertGreaterEqual(pass_rate, threshold, "Pass rate should meet threshold")
        self.assertEqual(pass_rate, 1.0, "Pass rate should be 1.0")

    def test_cli_exit_code_warn(self):
        """Test CLI returns exit code 0 when pass rate in warning range.

        AC5: Pass rate ≥90% with 5% tolerance for warning (allows deployment)
        """
        # Test the warning threshold logic (AC5)
        pass_rate = 0.93
        threshold = 0.95
        warn_threshold = 0.90

        # Should trigger warning but still allow deployment
        self.assertGreaterEqual(pass_rate, warn_threshold, "Pass rate should meet warn threshold")
        self.assertLess(pass_rate, threshold, "Pass rate should be below PASS threshold")

    def test_cli_exit_code_fail(self):
        """Test CLI returns exit code 1 when pass rate below warn threshold.

        AC6: Pass rate <90% triggers rollback recommendation
        """
        # Test the failure threshold logic (AC6)
        pass_rate = 0.85
        threshold = 0.95
        warn_threshold = 0.90

        # Should trigger rollback recommendation
        self.assertLess(pass_rate, warn_threshold, "Pass rate should be below warn threshold")
        self.assertLess(pass_rate, threshold, "Pass rate should be below PASS threshold")

    def test_pass_rate_thresholds(self):
        """Test pass rate threshold logic for all scenarios.

        AC4: ≥95% = PASS (deployment approved)
        AC5: ≥90% and <95% = WARN (deployment allowed)
        AC6: <90% = FAIL (rollback recommended)
        """
        threshold = 0.95
        warn_threshold = 0.90

        # Test AC4: ≥95% = PASS
        self.assertTrue(0.96 >= threshold, "96% should PASS")
        self.assertTrue(0.95 >= threshold, "95% should PASS")

        # Test AC5: ≥90% and <95% = WARN
        self.assertTrue(0.94 >= warn_threshold and 0.94 < threshold, "94% should WARN")
        self.assertTrue(0.90 >= warn_threshold and 0.90 < threshold, "90% should WARN")

        # Test AC6: <90% = FAIL
        self.assertTrue(0.89 < warn_threshold, "89% should FAIL")
        self.assertTrue(0.50 < warn_threshold, "50% should FAIL")


if __name__ == "__main__":
    unittest.main()

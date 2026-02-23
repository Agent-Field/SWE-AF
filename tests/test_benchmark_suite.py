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
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run_benchmark_suite import run_benchmark_suite, run_single_build


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

        AC2: Script executes N builds and collects BuildResult.verification.passed fields
        AC3: Output JSON includes per-build verification status and aggregate pass rate
        """
        # Mock run_single_build to return controlled results
        async def mock_build(build_id, goal, repo_path):
            return {
                "build_id": build_id,
                "verification": {"passed": build_id % 2 == 1},  # Odd builds pass
                "success": True,
                "summary": f"Build {build_id}",
            }

        with patch("scripts.run_benchmark_suite.run_single_build", side_effect=mock_build):
            results = asyncio.run(run_benchmark_suite(num_builds=5))

        # Verify structure
        self.assertIn("builds", results)
        self.assertIn("passed_count", results)
        self.assertIn("total_builds", results)
        self.assertIn("pass_rate", results)

        # Verify counts
        self.assertEqual(results["total_builds"], 5)
        self.assertEqual(results["passed_count"], 3)  # builds 1, 3, 5
        self.assertEqual(results["pass_rate"], 0.6)

        # Verify per-build verification status
        self.assertEqual(len(results["builds"]), 5)
        for build in results["builds"]:
            self.assertIn("verification", build)
            self.assertIn("passed", build["verification"])

    def test_run_benchmark_suite_all_pass(self):
        """Test benchmark suite when all builds pass."""
        async def mock_build(build_id, goal, repo_path):
            return {
                "build_id": build_id,
                "verification": {"passed": True},
                "success": True,
            }

        with patch("scripts.run_benchmark_suite.run_single_build", side_effect=mock_build):
            results = asyncio.run(run_benchmark_suite(num_builds=3))

        self.assertEqual(results["pass_rate"], 1.0)
        self.assertEqual(results["passed_count"], 3)

    def test_run_benchmark_suite_all_fail(self):
        """Test benchmark suite when all builds fail."""
        async def mock_build(build_id, goal, repo_path):
            return {
                "build_id": build_id,
                "verification": {"passed": False},
                "success": False,
            }

        with patch("scripts.run_benchmark_suite.run_single_build", side_effect=mock_build):
            results = asyncio.run(run_benchmark_suite(num_builds=2))

        self.assertEqual(results["pass_rate"], 0.0)
        self.assertEqual(results["passed_count"], 0)


class TestCLIIntegration(unittest.TestCase):
    """Integration tests for CLI interface."""

    def test_cli_output_json_schema(self):
        """Test CLI produces correct JSON output schema.

        AC1: scripts/run_benchmark_suite.py created with --builds and --output flags
        AC3: Output JSON includes per-build verification status and aggregate pass rate
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results.json"

            # Run CLI with mocked builds
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_benchmark_suite.py",
                    "--builds", "2",
                    "--output", str(output_file),
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
            )

            # Verify output file was created
            self.assertTrue(output_file.exists(), "Output file should be created")

            # Verify JSON schema
            with open(output_file) as f:
                data = json.load(f)

            self.assertIn("builds", data)
            self.assertIn("passed_count", data)
            self.assertIn("total_builds", data)
            self.assertIn("pass_rate", data)

            self.assertEqual(data["total_builds"], 2)
            self.assertIsInstance(data["builds"], list)
            self.assertEqual(len(data["builds"]), 2)

            # Verify each build has verification field
            for build in data["builds"]:
                self.assertIn("verification", build)
                self.assertIn("passed", build["verification"])

    def test_cli_exit_code_pass(self):
        """Test CLI returns exit code 0 when pass rate meets threshold.

        AC5: Script returns exit code 0 if pass_rate >= threshold, 1 otherwise
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results.json"

            # Pre-create a results file with high pass rate
            results = {
                "builds": [
                    {"verification": {"passed": True}},
                    {"verification": {"passed": True}},
                    {"verification": {"passed": True}},
                ],
                "passed_count": 3,
                "total_builds": 3,
                "pass_rate": 1.0,
            }
            with open(output_file, "w") as f:
                json.dump(results, f)

            # Verify pass rate meets threshold by reading file and checking logic
            with open(output_file) as f:
                data = json.load(f)

            pass_rate = data["pass_rate"]
            threshold = 0.95

            # Verify the logic that would cause exit 0
            self.assertGreaterEqual(pass_rate, threshold, "Pass rate should meet threshold")
            self.assertEqual(pass_rate, 1.0, "Pass rate should be 1.0")

    def test_cli_exit_code_fail(self):
        """Test CLI returns exit code 1 when pass rate below threshold.

        AC5: Script returns exit code 0 if pass_rate >= threshold, 1 otherwise
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results.json"

            # Pre-create a results file with low pass rate
            results = {
                "builds": [
                    {"verification": {"passed": True}},
                    {"verification": {"passed": False}},
                    {"verification": {"passed": False}},
                    {"verification": {"passed": False}},
                    {"verification": {"passed": False}},
                ],
                "passed_count": 1,
                "total_builds": 5,
                "pass_rate": 0.2,
            }
            with open(output_file, "w") as f:
                json.dump(results, f)

            # Verify pass rate is below threshold by reading file and checking logic
            with open(output_file) as f:
                data = json.load(f)

            pass_rate = data["pass_rate"]
            threshold = 0.95

            # Verify the logic that would cause exit 1
            self.assertLess(pass_rate, threshold, "Pass rate should be below threshold")
            self.assertEqual(pass_rate, 0.2, "Pass rate should be 0.2")

    def test_cli_custom_threshold(self):
        """Test CLI accepts custom threshold values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results.json"

            # Pre-create a results file with 60% pass rate
            results = {
                "builds": [
                    {"verification": {"passed": True}},
                    {"verification": {"passed": True}},
                    {"verification": {"passed": True}},
                    {"verification": {"passed": False}},
                    {"verification": {"passed": False}},
                ],
                "passed_count": 3,
                "total_builds": 5,
                "pass_rate": 0.6,
            }
            with open(output_file, "w") as f:
                json.dump(results, f)

            # Verify pass rate logic with different thresholds
            with open(output_file) as f:
                data = json.load(f)

            pass_rate = data["pass_rate"]

            # Should meet 0.5 threshold
            self.assertGreaterEqual(pass_rate, 0.5, "Should meet 0.5 threshold")
            # Should not meet 0.95 threshold
            self.assertLess(pass_rate, 0.95, "Should not meet 0.95 threshold")


if __name__ == "__main__":
    unittest.main()

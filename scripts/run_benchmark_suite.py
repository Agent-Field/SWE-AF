#!/usr/bin/env python3
"""Benchmark suite runner for pass rate validation.

Executes multiple builds and collects verification pass rates from BuildResult.
Used to validate optimizations don't degrade quality below 95% threshold.

Usage:
    python scripts/run_benchmark_suite.py --builds 8 --output results.json
    python scripts/run_benchmark_suite.py --builds 10 --output baseline.json --threshold 0.95
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

# Add parent directory to path to import swe_af
sys.path.insert(0, str(Path(__file__).parent.parent))

from swe_af.app import app


# Predefined test scenarios: simple builds (3-5 issues) and complex builds (10-15 issues)
SIMPLE_BUILD_SCENARIOS = [
    {
        "goal": "Add a README.md file with project description and usage instructions",
        "num_issues": "3-5",
        "complexity": "simple",
    },
    {
        "goal": "Add a config.py file with application settings and update main.py to use it",
        "num_issues": "3-5",
        "complexity": "simple",
    },
    {
        "goal": "Create a utils.py helper module with string formatting and validation functions",
        "num_issues": "3-5",
        "complexity": "simple",
    },
    {
        "goal": "Add logging configuration and integrate logger into existing modules",
        "num_issues": "3-5",
        "complexity": "simple",
    },
    {
        "goal": "Add CLI argument parsing with argparse and update entry point",
        "num_issues": "3-5",
        "complexity": "simple",
    },
]

COMPLEX_BUILD_SCENARIOS = [
    {
        "goal": "Implement a REST API with FastAPI including authentication, database models, and CRUD endpoints",
        "num_issues": "10-15",
        "complexity": "complex",
    },
    {
        "goal": "Build a data processing pipeline with ETL stages, validation, and error handling",
        "num_issues": "10-15",
        "complexity": "complex",
    },
    {
        "goal": "Create a plugin system with dynamic loading, configuration, and lifecycle management",
        "num_issues": "10-15",
        "complexity": "complex",
    },
]


async def run_single_build(build_id: int, scenario: dict, repo_path: str, config: dict | None = None) -> dict:
    """Execute a single build and return its result.

    Args:
        build_id: Sequential ID for this build
        scenario: Build scenario with goal, complexity, and expected num_issues
        repo_path: Path to the repository
        config: Optional BuildConfig dict for the build

    Returns:
        dict: BuildResult containing verification status and metadata
    """
    try:
        print(f"[Build {build_id}] Starting {scenario['complexity']} build: {scenario['goal'][:60]}...", file=sys.stderr)

        # Call the actual SWE-AF build function
        result = await app.call(
            f"{app.node_id}.build",
            goal=scenario["goal"],
            repo_path=repo_path,
            artifacts_dir=".artifacts",
            config=config or {},
        )

        # Extract BuildResult fields
        build_result = {
            "build_id": build_id,
            "scenario": scenario,
            "verification": result.get("verification", {"passed": False, "summary": "No verification data"}),
            "success": result.get("success", False),
            "summary": result.get("summary", ""),
            "pr_url": result.get("pr_url", ""),
        }

        passed = build_result["verification"].get("passed", False)
        status = "PASS" if passed else "FAIL"
        print(f"[Build {build_id}] {status}: {scenario['complexity']} build completed", file=sys.stderr)

        return build_result

    except Exception as e:
        print(f"[Build {build_id}] ERROR: {str(e)}", file=sys.stderr)
        # Return a failed build result
        return {
            "build_id": build_id,
            "scenario": scenario,
            "verification": {
                "passed": False,
                "summary": f"Build execution error: {str(e)}",
            },
            "success": False,
            "summary": f"Build failed with exception: {str(e)}",
            "error": str(e),
        }


async def run_benchmark_suite(
    num_builds: int,
    config: dict | None = None,
    simple_count: int | None = None,
    complex_count: int | None = None,
) -> dict:
    """Execute multiple builds and collect verification results.

    Args:
        num_builds: Total number of builds to execute
        config: Optional BuildConfig dict for builds
        simple_count: Number of simple builds (3-5 issues). If None, defaults to 5.
        complex_count: Number of complex builds (10-15 issues). If None, defaults to 3.

    Returns:
        dict: Results containing builds list, aggregate pass_rate, and per-component metrics
    """
    # Default to 5 simple + 3 complex = 8 total builds
    if simple_count is None and complex_count is None:
        if num_builds == 8:
            simple_count = 5
            complex_count = 3
        elif num_builds < 8:
            # For smaller runs, do all simple builds
            simple_count = num_builds
            complex_count = 0
        else:
            # For larger runs, maintain 5:3 ratio
            simple_count = min(5, num_builds)
            complex_count = num_builds - simple_count
    elif simple_count is None:
        simple_count = num_builds - (complex_count or 0)
    elif complex_count is None:
        complex_count = num_builds - simple_count

    # Ensure we don't exceed available scenarios
    simple_count = min(simple_count, len(SIMPLE_BUILD_SCENARIOS))
    complex_count = min(complex_count, len(COMPLEX_BUILD_SCENARIOS))

    # Select scenarios
    scenarios = []
    for i in range(simple_count):
        scenarios.append(SIMPLE_BUILD_SCENARIOS[i % len(SIMPLE_BUILD_SCENARIOS)])
    for i in range(complex_count):
        scenarios.append(COMPLEX_BUILD_SCENARIOS[i % len(COMPLEX_BUILD_SCENARIOS)])

    # Create temporary directories for each build to isolate them
    builds = []
    build_id = 1

    for scenario in scenarios:
        # Create a temporary repo for this build
        with tempfile.TemporaryDirectory(prefix=f"benchmark_build_{build_id}_") as tmpdir:
            repo_path = os.path.join(tmpdir, "test-repo")
            os.makedirs(repo_path)

            # Initialize git repo
            os.system(f"cd {repo_path} && git init && git config user.email 'test@example.com' && git config user.name 'Test User' && touch README.md && git add README.md && git commit -m 'Initial commit' > /dev/null 2>&1")

            print(f"Executing build {build_id}/{len(scenarios)} ({scenario['complexity']})...", file=sys.stderr)
            build_result = await run_single_build(build_id, scenario, repo_path, config)
            builds.append(build_result)

            build_id += 1

    # Calculate pass rate from BuildResult.verification.passed fields
    passed_count = sum(
        1 for build in builds
        if build.get("verification", {}).get("passed", False)
    )
    total_builds = len(builds)
    pass_rate = passed_count / total_builds if total_builds > 0 else 0.0

    # Calculate per-complexity metrics
    simple_builds = [b for b in builds if b["scenario"]["complexity"] == "simple"]
    complex_builds = [b for b in builds if b["scenario"]["complexity"] == "complex"]

    simple_passed = sum(1 for b in simple_builds if b.get("verification", {}).get("passed", False))
    complex_passed = sum(1 for b in complex_builds if b.get("verification", {}).get("passed", False))

    simple_pass_rate = simple_passed / len(simple_builds) if simple_builds else 0.0
    complex_pass_rate = complex_passed / len(complex_builds) if complex_builds else 0.0

    return {
        "builds": builds,
        "passed_count": passed_count,
        "total_builds": total_builds,
        "pass_rate": pass_rate,
        "simple_builds": len(simple_builds),
        "simple_passed": simple_passed,
        "simple_pass_rate": simple_pass_rate,
        "complex_builds": len(complex_builds),
        "complex_passed": complex_passed,
        "complex_pass_rate": complex_pass_rate,
    }


def main():
    """Main entry point for benchmark suite runner."""
    parser = argparse.ArgumentParser(
        description="Run benchmark suite for pass rate validation"
    )
    parser.add_argument(
        "--builds",
        type=int,
        default=8,
        help="Number of builds to execute (default: 8 = 5 simple + 3 complex)",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output JSON file path",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help="Pass rate threshold for PASS (default: 0.95 = 95%%)",
    )
    parser.add_argument(
        "--warn-threshold",
        type=float,
        default=0.90,
        help="Pass rate threshold for WARN vs FAIL (default: 0.90 = 90%%)",
    )
    parser.add_argument(
        "--simple-count",
        type=int,
        help="Number of simple builds (3-5 issues). Default: 5",
    )
    parser.add_argument(
        "--complex-count",
        type=int,
        help="Number of complex builds (10-15 issues). Default: 3",
    )
    parser.add_argument(
        "--verify-pass-rate",
        type=float,
        help="Alias for --threshold (for compatibility with testing strategy)",
    )

    args = parser.parse_args()

    # Handle verify-pass-rate alias
    if args.verify_pass_rate is not None:
        args.threshold = args.verify_pass_rate

    # Validate arguments
    if args.builds <= 0:
        print("Error: --builds must be a positive integer", file=sys.stderr)
        sys.exit(1)

    if args.threshold < 0.0 or args.threshold > 1.0:
        print("Error: --threshold must be between 0.0 and 1.0", file=sys.stderr)
        sys.exit(1)

    if args.warn_threshold < 0.0 or args.warn_threshold > 1.0:
        print("Error: --warn-threshold must be between 0.0 and 1.0", file=sys.stderr)
        sys.exit(1)

    # Run benchmark suite
    print(f"Running benchmark suite with {args.builds} builds...", file=sys.stderr)
    print(f"  Simple builds (3-5 issues): {args.simple_count or 'auto'}", file=sys.stderr)
    print(f"  Complex builds (10-15 issues): {args.complex_count or 'auto'}", file=sys.stderr)

    results = asyncio.run(
        run_benchmark_suite(
            num_builds=args.builds,
            simple_count=args.simple_count,
            complex_count=args.complex_count,
        )
    )

    # Write results to output file
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Print summary
    pass_rate = results["pass_rate"]
    passed = results["passed_count"]
    total = results["total_builds"]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Benchmark Suite Results", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Total builds:        {total}", file=sys.stderr)
    print(f"  Passed:              {passed}", file=sys.stderr)
    print(f"  Failed:              {total - passed}", file=sys.stderr)
    print(f"  Overall pass rate:   {pass_rate:.2%}", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"  Simple builds:       {results['simple_builds']} ({results['simple_passed']} passed, {results['simple_pass_rate']:.2%})", file=sys.stderr)
    print(f"  Complex builds:      {results['complex_builds']} ({results['complex_passed']} passed, {results['complex_pass_rate']:.2%})", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"  Thresholds:", file=sys.stderr)
    print(f"    PASS:  >= {args.threshold:.2%}", file=sys.stderr)
    print(f"    WARN:  >= {args.warn_threshold:.2%}", file=sys.stderr)
    print(f"    FAIL:  <  {args.warn_threshold:.2%}", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"  Results written to: {args.output}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    # Determine status based on thresholds (AC4, AC5, AC6)
    if pass_rate >= args.threshold:
        # AC4: Pass rate ≥95% for deployment approval
        print(f"✓ PASS: Pass rate {pass_rate:.2%} >= threshold {args.threshold:.2%}", file=sys.stderr)
        print(f"  Deployment APPROVED", file=sys.stderr)
        sys.exit(0)
    elif pass_rate >= args.warn_threshold:
        # AC5: Pass rate ≥90% with 5% tolerance for warning (allows deployment)
        print(f"⚠ WARN: Pass rate {pass_rate:.2%} >= {args.warn_threshold:.2%} but < {args.threshold:.2%}", file=sys.stderr)
        print(f"  Deployment ALLOWED with warning", file=sys.stderr)
        sys.exit(0)
    else:
        # AC6: Pass rate <90% triggers rollback recommendation
        print(f"✗ FAIL: Pass rate {pass_rate:.2%} < {args.warn_threshold:.2%}", file=sys.stderr)
        print(f"  Deployment REJECTED - ROLLBACK RECOMMENDED", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""CLI for SWE-AF.

Usage:
    swe-af                    Start the AgentField server (default)
    swe-af dagger [OPTIONS]   Run Dagger CI/CD pipeline

Dagger options:
    --pipeline {test,lint,build,full}   Pipeline to run (default: test)
    --path PATH                         Project path (default: current dir)
    --services LIST                     Comma-separated services (postgres,redis,mysql,mongodb)
    --timeout SECONDS                   Timeout per pipeline stage (default: 300)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="swe-af",
        description="SWE-AF: Autonomous Software Engineering Factory",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    dagger_parser = subparsers.add_parser(
        "dagger",
        help="Run Dagger CI/CD pipeline in isolated containers",
    )
    dagger_parser.add_argument(
        "--pipeline",
        "-p",
        choices=["test", "lint", "build", "full"],
        default="test",
        help="Pipeline to run (default: test)",
    )
    dagger_parser.add_argument(
        "--path",
        "-C",
        default=".",
        help="Project path (default: current directory)",
    )
    dagger_parser.add_argument(
        "--services",
        "-s",
        default="",
        help="Comma-separated services: postgres,redis,mysql,mongodb",
    )
    dagger_parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300)",
    )
    dagger_parser.add_argument(
        "--detect",
        "-d",
        action="store_true",
        help="Only detect project type, don't run pipeline",
    )

    return parser.parse_args(argv)


async def _run_dagger(args: argparse.Namespace) -> int:
    """Execute Dagger pipeline and print results."""
    from swe_af.reasoners.dagger_runner import (
        detect_project_type,
        run_pipeline_direct,
    )

    services = [s.strip() for s in args.services.split(",") if s.strip()]

    print()
    print("=" * 60)
    print(f"SWE-AF Dagger CI/CD")
    print("=" * 60)

    # Detect project
    print(f"\n[Detecting project: {args.path}]")
    project = await detect_project_type(args.path)
    print(f"  Language:     {project.language}")
    print(f"  Framework:    {project.framework or '(none)'}")
    print(f"  Build system: {project.build_system}")
    print(f"  Test command: {project.test_command}")
    print(f"  Lint command: {project.lint_command}")

    if args.detect:
        print("\n" + "=" * 60)
        print("Detection complete (--detect flag)")
        return 0

    print(f"\n[Running pipeline: {args.pipeline}]")
    if services:
        print(f"  Services: {', '.join(services)}")
    print(f"  Timeout: {args.timeout}s")
    print()

    result = await run_pipeline_direct(
        repo_path=args.path,
        pipeline=args.pipeline,
        services=services,
        timeout_seconds=args.timeout,
    )

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Pipeline:  {result['pipeline']}")
    print(f"Success:   {result['success']}")
    print(f"Duration:  {result['duration_seconds']}s")
    print(f"Summary:   {result['summary']}")

    if result.get("test_result"):
        tr = result["test_result"]
        print(f"\nTests:")
        print(f"  Run:    {tr.get('tests_run', 'N/A')}")
        print(f"  Passed: {tr.get('tests_passed', 'N/A')}")
        print(f"  Failed: {tr.get('tests_failed', 'N/A')}")
        if tr.get("test_failures"):
            print("\n  Failures:")
            for f in tr["test_failures"][:5]:
                print(f"    - {f.get('file', '?')}::{f.get('test_name', '?')}")

    if result.get("lint_result"):
        lr = result["lint_result"]
        print(f"\nLint:")
        print(f"  Issues: {lr.get('total_issues', 0)}")
        if lr.get("issues"):
            print("\n  Issues:")
            for issue in lr["issues"][:5]:
                print(f"    - {issue}")

    if result.get("build_result"):
        br = result["build_result"]
        print(f"\nBuild:")
        print(f"  Success: {br.get('success', False)}")
        print(f"  Output:  {br.get('output', '')[:200]}")

    print()
    print("=" * 60)
    status = "PASSED" if result["success"] else "FAILED"
    print(f"Pipeline {status}")
    print("=" * 60)
    print()

    return 0 if result["success"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for CLI."""
    args = _parse_args(argv)

    if args.command == "dagger":
        return asyncio.run(_run_dagger(args))

    from swe_af.app import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

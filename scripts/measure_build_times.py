#!/usr/bin/env python3
"""Build time measurement and optimization metrics validation.

Parses agent_logs.jsonl from instrumented builds to validate:
- Simple build duration ≤19min (Architecture BLOCKER #4 compromise)
- Complex build duration ≤60min (PRD target)
- Planning time reduction ≥20% vs baseline (Component 3)
- Trivial adoption rate ≥60% (Component 4)
- Advisor invocation rate ≤10% (Component 5)
- Replanner invocation rate reduced ≥50% (Component 6)
- Cost reduction ≥15% via haiku downgrades (Component 7)

Usage:
    python scripts/measure_build_times.py --simple-builds 3 --complex-builds 2 \\
        --baseline baseline_metrics.json --output metrics_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_jsonl_logs(log_path: Path) -> list[dict]:
    """Parse JSONL log file and return list of log entries.

    Args:
        log_path: Path to agent_logs.jsonl file

    Returns:
        List of parsed JSON objects from the log
    """
    logs = []
    if not log_path.exists():
        print(f"Warning: Log file not found: {log_path}", file=sys.stderr)
        return logs

    with open(log_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: Failed to parse line {line_num} in {log_path}: {e}", file=sys.stderr)

    return logs


def extract_tag_value(tags: list[str], prefix: str) -> str | None:
    """Extract value from tag with given prefix.

    Args:
        tags: List of tags (e.g., ["duration:123.4", "role:coder"])
        prefix: Prefix to match (e.g., "duration:")

    Returns:
        Value after prefix, or None if not found
    """
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def extract_build_durations(logs: list[dict]) -> list[float]:
    """Extract build durations from logs.

    Looks for tags like ["build", "metrics", "duration_s", "duration:1234.5"]

    Args:
        logs: Parsed JSONL log entries

    Returns:
        List of build durations in seconds
    """
    durations = []
    for log in logs:
        tags = log.get("tags", [])
        if "build" in tags and "duration_s" in tags and "metrics" in tags:
            duration_str = extract_tag_value(tags, "duration:")
            if duration_str:
                try:
                    durations.append(float(duration_str))
                except ValueError:
                    continue
    return durations


def extract_agent_durations(logs: list[dict]) -> dict[str, list[float]]:
    """Extract per-agent durations from logs.

    Looks for agent_metrics tags with role and duration.

    Args:
        logs: Parsed JSONL log entries

    Returns:
        Dict mapping role names to list of durations
    """
    agent_durations = {}
    for log in logs:
        tags = log.get("tags", [])
        if "agent_metrics" in tags:
            role = extract_tag_value(tags, "role:")
            duration_str = extract_tag_value(tags, "duration:")
            if role and duration_str:
                try:
                    duration = float(duration_str)
                    agent_durations.setdefault(role, []).append(duration)
                except ValueError:
                    continue
    return agent_durations


def extract_planning_durations(logs: list[dict]) -> dict[str, list[float]]:
    """Extract planning phase durations (PM, Architect, Tech Lead, Sprint Planner).

    Args:
        logs: Parsed JSONL log entries

    Returns:
        Dict with planning phase durations
    """
    planning_roles = ["pm", "architect", "tech_lead", "sprint_planner"]
    planning_durations = {}

    for log in logs:
        tags = log.get("tags", [])
        if "pipeline" in tags and "duration_s" in tags:
            # Extract role from tags
            for role in planning_roles:
                if role in tags:
                    duration_str = extract_tag_value(tags, "duration:")
                    if duration_str:
                        try:
                            duration = float(duration_str)
                            planning_durations.setdefault(role, []).append(duration)
                        except ValueError:
                            continue
                    break

    return planning_durations


def calculate_trivial_adoption_rate(logs: list[dict]) -> float:
    """Calculate trivial issue adoption rate.

    Args:
        logs: Parsed JSONL log entries

    Returns:
        Adoption rate as fraction (0.0 to 1.0)
    """
    trivial_count = 0
    total_issues = 0

    for log in logs:
        tags = log.get("tags", [])
        if "coding_loop" in tags:
            if "start" in tags:
                total_issues += 1
            if "trivial" in tags and "eligible" in tags:
                trivial_count += 1

    if total_issues == 0:
        return 0.0

    return trivial_count / total_issues


def count_advisor_invocations(logs: list[dict]) -> int:
    """Count Issue Advisor invocations.

    Args:
        logs: Parsed JSONL log entries

    Returns:
        Number of advisor invocations
    """
    count = 0
    for log in logs:
        tags = log.get("tags", [])
        if "issue_advisor" in tags and "complete" in tags:
            count += 1
    return count


def count_replanner_invocations(logs: list[dict]) -> int:
    """Count Replanner invocations.

    Args:
        logs: Parsed JSONL log entries

    Returns:
        Number of replanner invocations
    """
    count = 0
    for log in logs:
        tags = log.get("tags", [])
        if "replanner" in tags and "complete" in tags:
            count += 1
    return count


def calculate_mean(values: list[float]) -> float:
    """Calculate mean of values."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def estimate_cost_reduction(
    baseline_agent_durations: dict[str, list[float]],
    optimized_agent_durations: dict[str, list[float]],
    haiku_roles: list[str]
) -> float:
    """Estimate cost reduction from haiku model downgrades.

    Simplified estimation based on:
    - Haiku is ~80% cheaper than Sonnet per token
    - Assume token usage proportional to duration

    Args:
        baseline_agent_durations: Baseline agent durations
        optimized_agent_durations: Optimized agent durations
        haiku_roles: List of roles downgraded to haiku

    Returns:
        Estimated cost reduction as fraction (e.g., 0.15 = 15%)
    """
    # Cost multipliers (relative to haiku = 1.0)
    SONNET_COST = 5.0  # Sonnet is ~5x haiku cost
    HAIKU_COST = 1.0

    baseline_cost = 0.0
    optimized_cost = 0.0

    # Calculate baseline cost (all roles at sonnet pricing)
    for role, durations in baseline_agent_durations.items():
        total_duration = sum(durations)
        baseline_cost += total_duration * SONNET_COST

    # Calculate optimized cost (haiku roles at haiku pricing)
    for role, durations in optimized_agent_durations.items():
        total_duration = sum(durations)
        if role in haiku_roles:
            optimized_cost += total_duration * HAIKU_COST
        else:
            optimized_cost += total_duration * SONNET_COST

    if baseline_cost == 0.0:
        return 0.0

    reduction = (baseline_cost - optimized_cost) / baseline_cost
    return max(0.0, reduction)


def validate_metrics(
    simple_durations: list[float],
    complex_durations: list[float],
    baseline_planning_time: float,
    optimized_planning_time: float,
    trivial_adoption: float,
    advisor_count: int,
    baseline_replanner_count: int,
    optimized_replanner_count: int,
    cost_reduction: float,
    total_issues: int,
) -> dict[str, Any]:
    """Validate all metrics against PRD acceptance criteria.

    Args:
        simple_durations: List of simple build durations in seconds
        complex_durations: List of complex build durations in seconds
        baseline_planning_time: Baseline planning time in seconds
        optimized_planning_time: Optimized planning time in seconds
        trivial_adoption: Trivial adoption rate (0.0 to 1.0)
        advisor_count: Number of advisor invocations
        baseline_replanner_count: Baseline replanner invocations
        optimized_replanner_count: Optimized replanner invocations
        cost_reduction: Cost reduction fraction (0.0 to 1.0)
        total_issues: Total number of issues processed

    Returns:
        Dict with validation results for each metric
    """
    metrics = []

    # AC3: Simple build mean duration ≤19min
    simple_mean_min = calculate_mean(simple_durations) / 60.0 if simple_durations else 0.0
    metrics.append({
        "name": "Simple build duration",
        "target": "≤19min",
        "value": f"{simple_mean_min:.1f}min",
        "passed": simple_mean_min <= 19.0,
        "details": f"Mean of {len(simple_durations)} builds: {simple_mean_min:.1f}min"
    })

    # AC4: Complex build mean duration ≤60min
    complex_mean_min = calculate_mean(complex_durations) / 60.0 if complex_durations else 0.0
    metrics.append({
        "name": "Complex build duration",
        "target": "≤60min",
        "value": f"{complex_mean_min:.1f}min",
        "passed": complex_mean_min <= 60.0,
        "details": f"Mean of {len(complex_durations)} builds: {complex_mean_min:.1f}min"
    })

    # AC5: Planning time reduced ≥20% vs baseline
    if baseline_planning_time > 0:
        planning_reduction = (baseline_planning_time - optimized_planning_time) / baseline_planning_time
    else:
        planning_reduction = 0.0

    metrics.append({
        "name": "Planning time reduction",
        "target": "≥20%",
        "value": f"{planning_reduction*100:.1f}%",
        "passed": planning_reduction >= 0.20,
        "details": f"Baseline: {baseline_planning_time:.1f}s, Optimized: {optimized_planning_time:.1f}s"
    })

    # AC6: Trivial adoption rate ≥60%
    metrics.append({
        "name": "Trivial adoption rate",
        "target": "≥60%",
        "value": f"{trivial_adoption*100:.1f}%",
        "passed": trivial_adoption >= 0.60,
        "details": f"Trivial issues flagged and fast-pathed"
    })

    # AC7: Advisor invocation rate ≤10%
    advisor_rate = advisor_count / total_issues if total_issues > 0 else 0.0
    metrics.append({
        "name": "Advisor invocation rate",
        "target": "≤10%",
        "value": f"{advisor_rate*100:.1f}%",
        "passed": advisor_rate <= 0.10,
        "details": f"{advisor_count} invocations out of {total_issues} issues"
    })

    # AC8: Replanner invocation rate reduced ≥50%
    if baseline_replanner_count > 0:
        replanner_reduction = (baseline_replanner_count - optimized_replanner_count) / baseline_replanner_count
    else:
        replanner_reduction = 1.0  # No baseline replanner invocations = 100% reduction

    metrics.append({
        "name": "Replanner invocation reduction",
        "target": "≥50%",
        "value": f"{replanner_reduction*100:.1f}%",
        "passed": replanner_reduction >= 0.50,
        "details": f"Baseline: {baseline_replanner_count}, Optimized: {optimized_replanner_count}"
    })

    # AC9: Cost reduction ≥15% via haiku downgrades
    metrics.append({
        "name": "Cost reduction",
        "target": "≥15%",
        "value": f"{cost_reduction*100:.1f}%",
        "passed": cost_reduction >= 0.15,
        "details": f"Estimated from haiku model downgrades"
    })

    return {
        "metrics": metrics,
        "all_passed": all(m["passed"] for m in metrics)
    }


def process_build_logs(
    log_paths: list[Path],
    build_type: str,
) -> dict[str, Any]:
    """Process logs for a set of builds.

    Args:
        log_paths: List of paths to agent_logs.jsonl files
        build_type: "simple" or "complex"

    Returns:
        Dict with aggregated metrics for these builds
    """
    all_durations = []
    all_agent_durations = {}
    all_planning_durations = {}
    total_trivial = 0
    total_issues = 0
    total_advisor = 0
    total_replanner = 0

    for log_path in log_paths:
        logs = parse_jsonl_logs(log_path)

        # Extract build durations
        durations = extract_build_durations(logs)
        all_durations.extend(durations)

        # Extract agent durations
        agent_durations = extract_agent_durations(logs)
        for role, durations in agent_durations.items():
            all_agent_durations.setdefault(role, []).extend(durations)

        # Extract planning durations
        planning_durations = extract_planning_durations(logs)
        for role, durations in planning_durations.items():
            all_planning_durations.setdefault(role, []).extend(durations)

        # Count trivial adoptions
        trivial_rate = calculate_trivial_adoption_rate(logs)
        issue_count = sum(1 for log in logs if "coding_loop" in log.get("tags", []) and "start" in log.get("tags", []))
        total_trivial += int(trivial_rate * issue_count)
        total_issues += issue_count

        # Count advisor/replanner invocations
        total_advisor += count_advisor_invocations(logs)
        total_replanner += count_replanner_invocations(logs)

    return {
        "build_type": build_type,
        "num_builds": len(log_paths),
        "durations_seconds": all_durations,
        "mean_duration_min": calculate_mean(all_durations) / 60.0 if all_durations else 0.0,
        "agent_durations": all_agent_durations,
        "planning_durations": all_planning_durations,
        "trivial_adoption": total_trivial / total_issues if total_issues > 0 else 0.0,
        "total_issues": total_issues,
        "advisor_invocations": total_advisor,
        "replanner_invocations": total_replanner,
    }


def main():
    """Main entry point for build time measurement."""
    parser = argparse.ArgumentParser(
        description="Measure build times and validate optimization metrics"
    )
    parser.add_argument(
        "--simple-builds",
        type=int,
        default=3,
        help="Number of simple builds to process (default: 3)",
    )
    parser.add_argument(
        "--complex-builds",
        type=int,
        default=2,
        help="Number of complex builds to process (default: 2)",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        required=True,
        help="Path to baseline metrics JSON file",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output JSON file path for metrics report",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=".artifacts/logs",
        help="Directory containing agent_logs.jsonl files (default: .artifacts/logs)",
    )
    parser.add_argument(
        "--simple-log-pattern",
        type=str,
        default="simple_build_*.jsonl",
        help="Glob pattern for simple build logs",
    )
    parser.add_argument(
        "--complex-log-pattern",
        type=str,
        default="complex_build_*.jsonl",
        help="Glob pattern for complex build logs",
    )

    args = parser.parse_args()

    # Load baseline metrics
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"Error: Baseline file not found: {args.baseline}", file=sys.stderr)
        sys.exit(1)

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    # Find log files
    log_dir = Path(args.log_dir)
    simple_logs = sorted(log_dir.glob(args.simple_log_pattern))[:args.simple_builds]
    complex_logs = sorted(log_dir.glob(args.complex_log_pattern))[:args.complex_builds]

    if not simple_logs:
        print(f"Warning: No simple build logs found matching {args.simple_log_pattern}", file=sys.stderr)
    if not complex_logs:
        print(f"Warning: No complex build logs found matching {args.complex_log_pattern}", file=sys.stderr)

    print(f"Processing {len(simple_logs)} simple builds and {len(complex_logs)} complex builds...", file=sys.stderr)

    # Process builds
    simple_results = process_build_logs(simple_logs, "simple")
    complex_results = process_build_logs(complex_logs, "complex")

    # Calculate baseline planning time
    baseline_planning_time = sum(
        calculate_mean(baseline.get("planning_durations", {}).get(role, []))
        for role in ["pm", "architect", "tech_lead", "sprint_planner"]
    )

    # Calculate optimized planning time
    optimized_planning_time = sum(
        calculate_mean(simple_results["planning_durations"].get(role, []))
        for role in ["pm", "architect", "tech_lead", "sprint_planner"]
    )

    # Haiku roles from Component 7
    haiku_roles = ["git", "merger", "issue_writer", "qa_synthesizer"]

    # Calculate cost reduction
    cost_reduction = estimate_cost_reduction(
        baseline.get("agent_durations", {}),
        simple_results["agent_durations"],
        haiku_roles
    )

    # Get baseline replanner count
    baseline_replanner_count = baseline.get("replanner_invocations", 0)

    # Validate metrics
    validation = validate_metrics(
        simple_durations=simple_results["durations_seconds"],
        complex_durations=complex_results["durations_seconds"],
        baseline_planning_time=baseline_planning_time,
        optimized_planning_time=optimized_planning_time,
        trivial_adoption=simple_results["trivial_adoption"],
        advisor_count=simple_results["advisor_invocations"],
        baseline_replanner_count=baseline_replanner_count,
        optimized_replanner_count=simple_results["replanner_invocations"],
        cost_reduction=cost_reduction,
        total_issues=simple_results["total_issues"],
    )

    # Generate report
    report = {
        "simple_builds": simple_results,
        "complex_builds": complex_results,
        "baseline": baseline,
        "validation": validation,
    }

    # Write report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Print summary
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Build Time Measurement Report", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"\nSimple Builds ({simple_results['num_builds']} builds):", file=sys.stderr)
    print(f"  Mean duration: {simple_results['mean_duration_min']:.1f}min", file=sys.stderr)
    print(f"  Trivial adoption: {simple_results['trivial_adoption']*100:.1f}%", file=sys.stderr)
    print(f"  Advisor invocations: {simple_results['advisor_invocations']}", file=sys.stderr)
    print(f"  Replanner invocations: {simple_results['replanner_invocations']}", file=sys.stderr)

    print(f"\nComplex Builds ({complex_results['num_builds']} builds):", file=sys.stderr)
    print(f"  Mean duration: {complex_results['mean_duration_min']:.1f}min", file=sys.stderr)

    print(f"\nMetrics Validation:", file=sys.stderr)
    for metric in validation["metrics"]:
        status = "✓ PASS" if metric["passed"] else "✗ FAIL"
        print(f"  {status}: {metric['name']}: {metric['value']} (target: {metric['target']})", file=sys.stderr)
        print(f"       {metric['details']}", file=sys.stderr)

    print(f"\n{'='*60}", file=sys.stderr)

    if validation["all_passed"]:
        print(f"✓ ALL METRICS PASSED - Deployment approved", file=sys.stderr)
        sys.exit(0)
    else:
        print(f"✗ SOME METRICS FAILED - Review required", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

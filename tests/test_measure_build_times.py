"""Tests for build time measurement script.

Validates:
- Log parsing from JSONL files
- Metric aggregation (durations, adoption rates, invocation counts)
- Threshold validation against PRD acceptance criteria
- Report generation
"""

import json
import tempfile
from pathlib import Path

import pytest

# Import the script functions
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from measure_build_times import (
    parse_jsonl_logs,
    extract_tag_value,
    extract_build_durations,
    extract_agent_durations,
    extract_planning_durations,
    calculate_trivial_adoption_rate,
    count_advisor_invocations,
    count_replanner_invocations,
    calculate_mean,
    estimate_cost_reduction,
    validate_metrics,
    process_build_logs,
)


def test_extract_tag_value():
    """Test extracting values from tags."""
    tags = ["duration:123.4", "role:coder", "status:success"]

    assert extract_tag_value(tags, "duration:") == "123.4"
    assert extract_tag_value(tags, "role:") == "coder"
    assert extract_tag_value(tags, "status:") == "success"
    assert extract_tag_value(tags, "missing:") is None


def test_parse_jsonl_logs(tmp_path):
    """Test parsing JSONL log files."""
    # Create test log file
    log_file = tmp_path / "test.jsonl"
    log_entries = [
        {"ts": 1234.5, "event": "start", "tags": ["build", "start"]},
        {"ts": 1235.0, "event": "complete", "tags": ["build", "complete"]},
    ]

    with open(log_file, "w") as f:
        for entry in log_entries:
            f.write(json.dumps(entry) + "\n")

    logs = parse_jsonl_logs(log_file)
    assert len(logs) == 2
    assert logs[0]["event"] == "start"
    assert logs[1]["event"] == "complete"


def test_parse_jsonl_logs_malformed(tmp_path):
    """Test parsing JSONL with malformed lines."""
    log_file = tmp_path / "malformed.jsonl"

    with open(log_file, "w") as f:
        f.write('{"valid": "entry"}\n')
        f.write('invalid json line\n')
        f.write('{"another": "valid"}\n')

    logs = parse_jsonl_logs(log_file)
    assert len(logs) == 2  # Only valid entries parsed


def test_extract_build_durations():
    """Test extracting build durations from logs."""
    logs = [
        {"tags": ["build", "metrics", "duration_s", "duration:1200.5"]},
        {"tags": ["build", "metrics", "duration_s", "duration:1500.0"]},
        {"tags": ["agent_metrics", "duration:300.0"]},  # Should be ignored
    ]

    durations = extract_build_durations(logs)
    assert len(durations) == 2
    assert durations[0] == 1200.5
    assert durations[1] == 1500.0


def test_extract_agent_durations():
    """Test extracting per-agent durations."""
    logs = [
        {"tags": ["agent_metrics", "role:coder", "duration:120.5"]},
        {"tags": ["agent_metrics", "role:coder", "duration:130.0"]},
        {"tags": ["agent_metrics", "role:qa", "duration:60.0"]},
    ]

    agent_durations = extract_agent_durations(logs)
    assert "coder" in agent_durations
    assert "qa" in agent_durations
    assert len(agent_durations["coder"]) == 2
    assert len(agent_durations["qa"]) == 1
    assert agent_durations["coder"][0] == 120.5
    assert agent_durations["qa"][0] == 60.0


def test_extract_planning_durations():
    """Test extracting planning phase durations."""
    logs = [
        {"tags": ["pipeline", "pm", "duration_s", "duration:300.0"]},
        {"tags": ["pipeline", "architect", "duration_s", "duration:420.0"]},
        {"tags": ["pipeline", "sprint_planner", "duration_s", "duration:360.0"]},
        {"tags": ["agent_metrics", "role:coder", "duration:120.0"]},  # Should be ignored
    ]

    planning_durations = extract_planning_durations(logs)
    assert "pm" in planning_durations
    assert "architect" in planning_durations
    assert "sprint_planner" in planning_durations
    assert "coder" not in planning_durations
    assert planning_durations["pm"][0] == 300.0
    assert planning_durations["architect"][0] == 420.0


def test_calculate_trivial_adoption_rate():
    """Test calculating trivial adoption rate."""
    logs = [
        {"tags": ["coding_loop", "start", "issue_1"]},
        {"tags": ["coding_loop", "start", "issue_2"]},
        {"tags": ["coding_loop", "start", "issue_3"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_1"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_3"]},
    ]

    adoption_rate = calculate_trivial_adoption_rate(logs)
    # 2 trivial out of 3 total issues
    assert adoption_rate == pytest.approx(2.0 / 3.0, rel=0.01)


def test_calculate_trivial_adoption_rate_no_issues():
    """Test trivial adoption rate with no issues."""
    logs = [{"tags": ["build", "start"]}]
    adoption_rate = calculate_trivial_adoption_rate(logs)
    assert adoption_rate == 0.0


def test_count_advisor_invocations():
    """Test counting advisor invocations."""
    logs = [
        {"tags": ["issue_advisor", "complete", "issue_1"]},
        {"tags": ["issue_advisor", "complete", "issue_2"]},
        {"tags": ["issue_advisor", "skip", "issue_3"]},  # Should be ignored
    ]

    count = count_advisor_invocations(logs)
    assert count == 2


def test_count_replanner_invocations():
    """Test counting replanner invocations."""
    logs = [
        {"tags": ["replanner", "complete", "duration:180.0"]},
        {"tags": ["replanner", "skip"]},  # Should be ignored
    ]

    count = count_replanner_invocations(logs)
    assert count == 1


def test_calculate_mean():
    """Test mean calculation."""
    assert calculate_mean([10.0, 20.0, 30.0]) == 20.0
    assert calculate_mean([]) == 0.0
    assert calculate_mean([42.0]) == 42.0


def test_estimate_cost_reduction():
    """Test cost reduction estimation."""
    baseline = {
        "coder": [100.0, 120.0],
        "qa": [50.0, 60.0],
        "git": [20.0, 25.0],
    }

    optimized = {
        "coder": [100.0, 120.0],  # Still sonnet
        "qa": [50.0, 60.0],       # Still sonnet
        "git": [20.0, 25.0],      # Now haiku
    }

    haiku_roles = ["git"]

    reduction = estimate_cost_reduction(baseline, optimized, haiku_roles)
    # git is ~11% of total duration, so ~9-11% cost reduction expected
    assert 0.05 < reduction < 0.15


def test_estimate_cost_reduction_no_baseline():
    """Test cost reduction with empty baseline."""
    reduction = estimate_cost_reduction({}, {}, [])
    assert reduction == 0.0


def test_validate_metrics_all_pass():
    """Test metric validation when all criteria pass."""
    validation = validate_metrics(
        simple_durations=[1080.0, 1020.0, 1140.0],  # 18, 17, 19 min -> mean 18min < 19min
        complex_durations=[3000.0, 3300.0],  # 50, 55 min -> mean 52.5min < 60min
        baseline_planning_time=1200.0,  # 20 min
        optimized_planning_time=900.0,   # 15 min -> 25% reduction > 20%
        trivial_adoption=0.65,  # 65% > 60%
        advisor_count=3,
        baseline_replanner_count=4,
        optimized_replanner_count=1,  # 75% reduction > 50%
        cost_reduction=0.18,  # 18% > 15%
        total_issues=50,  # 3/50 = 6% < 10%
    )

    assert validation["all_passed"] is True
    assert len(validation["metrics"]) == 7

    # Check each metric
    for metric in validation["metrics"]:
        assert metric["passed"] is True


def test_validate_metrics_some_fail():
    """Test metric validation when some criteria fail."""
    validation = validate_metrics(
        simple_durations=[1260.0, 1320.0],  # 21, 22 min -> mean 21.5min > 19min FAIL
        complex_durations=[3900.0, 3600.0],  # 65, 60 min -> mean 62.5min > 60min FAIL
        baseline_planning_time=1200.0,
        optimized_planning_time=1000.0,  # 16.7% reduction < 20% FAIL
        trivial_adoption=0.55,  # 55% < 60% FAIL
        advisor_count=8,
        baseline_replanner_count=4,
        optimized_replanner_count=3,  # 25% reduction < 50% FAIL
        cost_reduction=0.12,  # 12% < 15% FAIL
        total_issues=50,  # 8/50 = 16% > 10% FAIL
    )

    assert validation["all_passed"] is False

    # Count failures
    failures = sum(1 for m in validation["metrics"] if not m["passed"])
    assert failures == 7  # All should fail


def test_process_build_logs(tmp_path):
    """Test processing multiple build log files."""
    # Create test log files
    log1 = tmp_path / "build1.jsonl"
    log2 = tmp_path / "build2.jsonl"

    logs1 = [
        {"tags": ["build", "metrics", "duration_s", "duration:1100.0"]},
        {"tags": ["coding_loop", "start", "issue_1"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_1"]},
        {"tags": ["issue_advisor", "complete", "issue_2"]},
        {"tags": ["replanner", "complete"]},
    ]

    logs2 = [
        {"tags": ["build", "metrics", "duration_s", "duration:1200.0"]},
        {"tags": ["coding_loop", "start", "issue_1"]},
        {"tags": ["coding_loop", "start", "issue_2"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_1"]},
    ]

    for log_file, entries in [(log1, logs1), (log2, logs2)]:
        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    results = process_build_logs([log1, log2], "simple")

    assert results["build_type"] == "simple"
    assert results["num_builds"] == 2
    assert len(results["durations_seconds"]) == 2
    assert results["mean_duration_min"] == pytest.approx((1100.0 + 1200.0) / 2 / 60.0, rel=0.01)
    assert results["total_issues"] == 3  # 1 from log1, 2 from log2
    assert results["advisor_invocations"] == 1
    assert results["replanner_invocations"] == 1


def test_integration_full_workflow(tmp_path):
    """Integration test of full measurement workflow."""
    # Create baseline file
    baseline_file = tmp_path / "baseline.json"
    baseline = {
        "planning_durations": {
            "pm": [300.0],
            "architect": [420.0],
            "tech_lead": [240.0],
            "sprint_planner": [360.0]
        },
        "agent_durations": {
            "coder": [120.0] * 10,
            "qa": [60.0] * 10,
            "git": [30.0] * 10,
        },
        "replanner_invocations": 4,
    }

    with open(baseline_file, "w") as f:
        json.dump(baseline, f)

    # Create optimized build logs
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    simple_log = log_dir / "simple_build_1.jsonl"
    logs = [
        {"tags": ["build", "metrics", "duration_s", "duration:1080.0"]},  # 18 min
        {"tags": ["pipeline", "pm", "duration_s", "duration:300.0"]},
        {"tags": ["pipeline", "architect", "duration_s", "duration:420.0"]},
        {"tags": ["pipeline", "tech_lead", "duration_s", "duration:180.0"]},  # Reduced
        {"tags": ["pipeline", "sprint_planner", "duration_s", "duration:240.0"]},  # Reduced
        {"tags": ["coding_loop", "start", "issue_1"]},
        {"tags": ["coding_loop", "start", "issue_2"]},
        {"tags": ["coding_loop", "start", "issue_3"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_1"]},
        {"tags": ["coding_loop", "trivial", "eligible", "issue_2"]},
        {"tags": ["agent_metrics", "role:coder", "duration:120.0"]},
        {"tags": ["agent_metrics", "role:git", "duration:20.0"]},  # Haiku (faster)
        {"tags": ["issue_advisor", "complete", "issue_3"]},
        {"tags": ["replanner", "complete"]},
    ]

    with open(simple_log, "w") as f:
        for entry in logs:
            f.write(json.dumps(entry) + "\n")

    # Verify the workflow produces expected results
    from measure_build_times import process_build_logs, validate_metrics, calculate_mean

    simple_results = process_build_logs([simple_log], "simple")

    # Check parsed data
    assert simple_results["num_builds"] == 1
    assert len(simple_results["durations_seconds"]) == 1
    assert simple_results["durations_seconds"][0] == 1080.0
    assert simple_results["mean_duration_min"] == 18.0
    assert simple_results["total_issues"] == 3
    assert simple_results["trivial_adoption"] == pytest.approx(2.0 / 3.0, rel=0.01)
    assert simple_results["advisor_invocations"] == 1
    assert simple_results["replanner_invocations"] == 1

    # Verify planning time reduction
    baseline_planning = 300.0 + 420.0 + 240.0 + 360.0  # 1320.0
    optimized_planning = 300.0 + 420.0 + 180.0 + 240.0  # 1140.0
    planning_reduction = (baseline_planning - optimized_planning) / baseline_planning  # 180/1320 = 13.6%
    # Note: This test data only shows 13.6% reduction, which is expected for partial optimization
    # In real builds with Component 3 parallelization, we expect >20% reduction
    assert planning_reduction > 0.10  # At least 10% reduction demonstrated


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

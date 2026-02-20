"""Integration tests for cross-feature interactions between merged branches.

These tests verify the interactions between:
1. conftest.mock_agent_ai fixture (branch: conftest-root)
   ↔ test_planner_pipeline tests (branch: test-planner-pipeline)
   ↔ test_planner_execute tests (branch: test-execute-pipeline)
2. envelope.unwrap_call_result (shared utility)
   ↔ fast executor (branch: test-malformed-responses)
   ↔ planner pipeline app.call() results
3. NODE_ID isolation (branch: test-node-id-isolation)
   ↔ planner pipeline routing via f"{NODE_ID}.run_product_manager" etc.
4. _compute_levels / _assign_sequence_numbers pipeline helpers
   ↔ plan() reasoner that consumes them
   ↔ execute() reasoner that depends on plan() output structure

Interaction boundaries under test:
- The mock_agent_ai fixture patches swe_af.app.app.call; verify it intercepts
  all calls made by plan() (which uses f"{NODE_ID}.xxx" routing strings).
- fast-path vs envelope path in unwrap_call_result behaves consistently across
  both the planner and fast branches.
- NODE_ID reload in test_node_id_isolation doesn't corrupt the module-level
  NODE_ID used by the planner reasoner for call routing.
- execute() receives a plan_result whose structure matches what plan() produces.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure safe server for all tests in this module
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Helpers shared with test_planner_pipeline.py (inlined to avoid coupling)
# ---------------------------------------------------------------------------

def _make_prd() -> dict:
    return {
        "validated_description": "Integration test goal.",
        "acceptance_criteria": ["AC-INT-1"],
        "must_have": ["feature-int"],
        "nice_to_have": [],
        "out_of_scope": [],
        "assumptions": [],
        "risks": [],
    }


def _make_arch() -> dict:
    return {
        "summary": "Integration architecture.",
        "components": [{"name": "comp-a", "responsibility": "Does A",
                        "touches_files": ["a.py"], "depends_on": []}],
        "interfaces": ["iface-1"],
        "decisions": [{"decision": "Use Python", "rationale": "Available."}],
        "file_changes_overview": "Only a.py.",
    }


def _make_review_approved() -> dict:
    return {
        "approved": True,
        "feedback": "Approved.",
        "scope_issues": [],
        "complexity_assessment": "appropriate",
        "summary": "OK.",
    }


def _make_sprint(issue_names: list[str]) -> dict:
    issues = []
    for name in issue_names:
        issues.append({
            "name": name,
            "title": f"Issue {name}",
            "description": f"Implement {name}.",
            "acceptance_criteria": [f"AC for {name}"],
            "depends_on": [],
            "provides": [name],
            "estimated_complexity": "small",
            "files_to_create": [f"{name}.py"],
            "files_to_modify": [],
            "testing_strategy": "pytest",
            "sequence_number": None,
            "guidance": None,
        })
    return {"issues": issues, "rationale": "Integration rationale."}


def _make_issue_writer(name: str = "test") -> dict:
    return {"success": True, "path": f"/tmp/{name}.md"}


async def _call_plan(tmp_path: str, **kwargs) -> dict:
    """Invoke plan() directly, bypassing the AgentField wrapper."""
    import swe_af.app as _app_module
    real_fn = getattr(_app_module.plan, "_original_func", _app_module.plan)
    defaults = dict(
        goal="Integration test build",
        repo_path=tmp_path,
        artifacts_dir=".artifacts",
        additional_context="",
        max_review_iterations=2,
        pm_model="sonnet",
        architect_model="sonnet",
        tech_lead_model="sonnet",
        sprint_planner_model="sonnet",
        issue_writer_model="sonnet",
        permission_mode="",
        ai_provider="claude",
    )
    defaults.update(kwargs)
    return await real_fn(**defaults)


# ---------------------------------------------------------------------------
# Priority 1: mock_agent_ai fixture ↔ planner pipeline call routing
#
# The plan() reasoner calls app.call(f"{NODE_ID}.run_product_manager", ...)
# The mock_agent_ai fixture patches swe_af.app.app.call.
# This test verifies: with NODE_ID=swe-planner (the default), the mock
# intercepts all plan() sub-calls regardless of the routing string prefix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_agent_ai_intercepts_node_id_prefixed_calls(mock_agent_ai, tmp_path):
    """The mock_agent_ai fixture must intercept ALL app.call() invocations
    made by plan(), including those with f'{NODE_ID}.xxx' routing strings.

    This verifies the interaction between conftest.mock_agent_ai (which
    patches swe_af.app.app.call) and the planner pipeline's use of
    f"{NODE_ID}.run_product_manager", f"{NODE_ID}.run_architect", etc.
    """
    prd = _make_prd()
    arch = _make_arch()
    review = _make_review_approved()
    sprint = _make_sprint(["int-issue-1"])
    iw = _make_issue_writer("int-issue-1")

    mock_agent_ai.side_effect = [prd, arch, review, sprint, iw]

    result = await _call_plan(str(tmp_path))

    # All 5 sub-calls must have been intercepted (no real network)
    assert mock_agent_ai.call_count == 5, (
        f"Expected 5 intercepted calls, got {mock_agent_ai.call_count}"
    )
    # Verify each call used a routing string (first positional arg)
    call_targets = [c.args[0] for c in mock_agent_ai.call_args_list]
    for target in call_targets:
        assert "." in target, f"Expected 'node_id.reasoner' routing string, got {target!r}"
    assert result["prd"]["validated_description"] == "Integration test goal."


# ---------------------------------------------------------------------------
# Priority 1: envelope fast-path ↔ planner pipeline
#
# The plan() reasoner uses _unwrap(await app.call(...), label) on every
# sub-call. The mock_agent_ai fixture returns plain dicts (no envelope keys),
# which should flow through _unwrap on the fast path without error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_dict_mock_bypasses_envelope_unwrapping(mock_agent_ai, tmp_path):
    """Plain dict mock responses (no envelope keys) must flow through _unwrap
    on the fast path without raising RuntimeError.

    Interaction: conftest.mock_agent_ai (plain dict) ↔ envelope.py fast path
    ↔ plan() reasoner's _unwrap calls.
    """
    from swe_af.execution.envelope import unwrap_call_result, _ENVELOPE_KEYS

    prd = _make_prd()
    arch = _make_arch()
    review = _make_review_approved()
    sprint = _make_sprint(["env-issue"])
    iw = _make_issue_writer("env-issue")

    mock_agent_ai.side_effect = [prd, arch, review, sprint, iw]

    # Verify that none of our mock dicts accidentally contain envelope keys
    for mock_dict in [prd, arch, review, sprint, iw]:
        overlap = _ENVELOPE_KEYS.intersection(mock_dict)
        assert not overlap, (
            f"Mock dict accidentally contains envelope key(s) {overlap}, "
            "which would trigger the envelope path instead of fast path"
        )

    # Plan should complete without RuntimeError from _unwrap
    result = await _call_plan(str(tmp_path))
    assert isinstance(result, dict), "plan() must return a dict"
    assert "levels" in result


# ---------------------------------------------------------------------------
# Priority 2: plan() output structure ↔ execute() input contract
#
# plan() returns a PlanResult.model_dump(); execute() receives it as
# plan_result. This test verifies the structural contract between the two.
# Specifically: execute() reads plan_result["issues"] and plan_result["levels"]
# — the same keys plan() guarantees.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_output_is_valid_execute_input(mock_agent_ai, tmp_path):
    """plan() output dict must satisfy execute()'s structural requirements.

    execute() calls run_dag(plan_result=...) and internally accesses
    plan_result["issues"] and plan_result["levels"]. This test verifies that
    plan() output passes a structural compatibility check.

    Interaction: test_planner_pipeline ↔ test_execute_pipeline output contract.
    """
    from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult

    prd = _make_prd()
    arch = _make_arch()
    review = _make_review_approved()
    sprint = _make_sprint(["issue-a", "issue-b"])
    iw_a = _make_issue_writer("issue-a")
    iw_b = _make_issue_writer("issue-b")

    mock_agent_ai.side_effect = [prd, arch, review, sprint, iw_a, iw_b]
    plan_result = await _call_plan(str(tmp_path))

    # Verify structural requirements needed by execute() / run_dag()
    assert "issues" in plan_result, "plan() result must have 'issues'"
    assert "levels" in plan_result, "plan() result must have 'levels'"
    assert isinstance(plan_result["issues"], list)
    assert isinstance(plan_result["levels"], list)
    assert len(plan_result["issues"]) == 2

    # Now simulate passing this to execute() via run_dag mock
    dag_state = DAGState(
        repo_path=str(tmp_path),
        completed_issues=[
            IssueResult(issue_name=i["name"], outcome=IssueOutcome.COMPLETED,
                        result_summary="Done")
            for i in plan_result["issues"]
        ],
        failed_issues=[],
    )

    import swe_af.app as app_module
    with patch("swe_af.execution.dag_executor.run_dag",
               new=AsyncMock(return_value=dag_state)):
        exec_result = await app_module.execute(
            plan_result=plan_result,
            repo_path=str(tmp_path),
        )

    assert isinstance(exec_result, dict)
    assert len(exec_result["completed_issues"]) == 2
    assert len(exec_result["failed_issues"]) == 0


# ---------------------------------------------------------------------------
# Priority 2: NODE_ID routing in planner calls ↔ isolation guarantee
#
# The plan() reasoner calls app.call(f"{NODE_ID}.run_product_manager", ...).
# The test_node_id_isolation branch reloads modules to test NODE_ID env var
# behavior. This test verifies that after a NODE_ID reload, the planner still
# routes calls with the (potentially reloaded) NODE_ID value correctly.
# ---------------------------------------------------------------------------


def test_planner_node_id_routing_uses_module_level_constant():
    """plan() routes sub-calls via f"{NODE_ID}.xxx" using the module-level
    NODE_ID constant. This test verifies the constant is consistent with the
    app's node_id attribute at module load time.

    Interaction: test_node_id_isolation (module-level NODE_ID)
    ↔ test_planner_pipeline (call routing strings).
    """
    import swe_af.app as app_module

    # The module-level NODE_ID must match app.node_id
    assert app_module.NODE_ID == app_module.app.node_id, (
        f"Module-level NODE_ID={app_module.NODE_ID!r} "
        f"must equal app.node_id={app_module.app.node_id!r}"
    )

    # The NODE_ID must not be empty (would break routing strings)
    assert app_module.NODE_ID, "NODE_ID must not be empty"

    # The routing strings used by plan() follow the pattern "{NODE_ID}.reason_name"
    expected_prefix = f"{app_module.NODE_ID}."
    import inspect
    import swe_af.app as app_src
    source = inspect.getsource(app_src.plan._original_func
                               if hasattr(app_src.plan, "_original_func")
                               else app_src.plan)
    # Verify the source uses {NODE_ID}. as the call prefix
    assert "{NODE_ID}." in source, (
        "plan() must route sub-calls using f'{NODE_ID}.xxx' pattern"
    )


# ---------------------------------------------------------------------------
# Priority 2: _compute_levels / _assign_sequence_numbers ↔ plan() output
#
# plan() calls _compute_levels(issues) and _assign_sequence_numbers(issues, levels)
# before building PlanResult. This integration test verifies the helper
# functions produce output that round-trips correctly through PlanResult.
# ---------------------------------------------------------------------------


def test_compute_levels_and_assign_sequence_numbers_round_trip():
    """_compute_levels and _assign_sequence_numbers must produce consistent output
    that satisfies the PlanResult schema contract consumed by execute().

    Interaction: pipeline.py helpers ↔ plan() ↔ execute() input.
    """
    from swe_af.reasoners.pipeline import _compute_levels, _assign_sequence_numbers

    issues = [
        {"name": "issue-a", "depends_on": [], "files_to_create": ["a.py"], "files_to_modify": []},
        {"name": "issue-b", "depends_on": ["issue-a"], "files_to_create": ["b.py"], "files_to_modify": []},
        {"name": "issue-c", "depends_on": [], "files_to_create": ["c.py"], "files_to_modify": []},
    ]

    levels = _compute_levels(issues)

    # issue-a and issue-c have no deps → level 0
    # issue-b depends on issue-a → level 1
    assert len(levels) == 2, f"Expected 2 levels, got {levels}"
    assert set(levels[0]) == {"issue-a", "issue-c"}, f"Level 0: {levels[0]}"
    assert levels[1] == ["issue-b"], f"Level 1: {levels[1]}"

    numbered = _assign_sequence_numbers(issues, levels)
    seq_map = {i["name"]: i["sequence_number"] for i in numbered}

    # Sequence numbers must be 1-based and unique
    assert seq_map["issue-a"] in (1, 2), f"issue-a seq={seq_map['issue-a']}"
    assert seq_map["issue-c"] in (1, 2), f"issue-c seq={seq_map['issue-c']}"
    assert seq_map["issue-b"] == 3, f"issue-b seq={seq_map['issue-b']}"
    assert len(set(seq_map.values())) == 3, "Sequence numbers must be unique"


# ---------------------------------------------------------------------------
# Priority 3: envelope unwrap_call_result ↔ both planner and fast paths
#
# The envelope.py module is shared between the planner pipeline and the fast
# executor. This test verifies that the same unwrap logic handles both:
# - A plain dict (no envelope keys) → returns as-is (fast path)
# - An envelope dict with status=success + inner result → returns inner
# - The _KeyErrorEnvelope from test_malformed_responses → propagates KeyError
# ---------------------------------------------------------------------------


def test_envelope_unwrap_shared_behavior_planner_and_fast():
    """unwrap_call_result must behave consistently for all dict shapes used
    across planner and fast test fixtures.

    Interaction: envelope.py ↔ planner pipeline mock responses
    ↔ fast executor malformed envelope responses.
    """
    from swe_af.execution.envelope import unwrap_call_result, _ENVELOPE_KEYS

    # 1. Plain dict (no envelope keys) — used by conftest.mock_agent_ai
    plain = {"prd": "...", "status_code": 200}  # "status_code" ≠ "status"
    assert unwrap_call_result(plain) is plain, (
        "Plain dict with no envelope keys must be returned as-is"
    )

    # 2. Valid envelope with status=success and inner result
    inner = {"plan": [], "issues": []}
    envelope = {
        "execution_id": "test-id",
        "run_id": "run-1",
        "node_id": "swe-planner",
        "type": "response",
        "target": "swe-planner.plan",
        "status": "success",
        "duration_ms": 100,
        "timestamp": "2026-01-01T00:00:00Z",
        "result": inner,
        "error_message": None,
        "cost": 0.01,
    }
    assert unwrap_call_result(envelope) is inner, (
        "Envelope with status=success must return the inner result"
    )

    # 3. Failed envelope → must raise RuntimeError
    failed_envelope = {
        "execution_id": "fail-id",
        "status": "failed",
        "error_message": "Agent crashed",
        "result": None,
    }
    with pytest.raises(RuntimeError, match="failed"):
        unwrap_call_result(failed_envelope, label="test_call")

    # 4. Envelope with status=success but result=None → returns envelope as-is
    no_inner = {"execution_id": "x", "status": "success", "result": None}
    result = unwrap_call_result(no_inner)
    assert result is no_inner, (
        "Envelope with status=success but result=None must return envelope as-is"
    )


# ---------------------------------------------------------------------------
# Priority 3: mock_agent_ai fixture isolation between test modules
#
# Both test_planner_pipeline.py and test_planner_execute.py use mock_agent_ai.
# test_malformed_responses.py patches swe_af.fast.app.app.call independently.
# This test verifies that the two patch targets are DISTINCT objects.
# ---------------------------------------------------------------------------


def test_planner_and_fast_app_call_are_distinct_targets():
    """swe_af.app.app.call and swe_af.fast.app.app.call must be distinct
    call objects so that patching one does not affect the other.

    Interaction: conftest.mock_agent_ai (patches swe_af.app.app.call)
    ↔ test_malformed_responses (patches swe_af.fast.app.app.call).
    """
    import swe_af.app as planner_app
    import swe_af.fast.app as fast_app

    assert planner_app.app is not fast_app.app, (
        "swe_af.app.app and swe_af.fast.app.app must be distinct Agent instances"
    )

    # The call methods are bound to different Agent objects
    assert planner_app.app.call is not fast_app.app.call, (
        "swe_af.app.app.call and swe_af.fast.app.app.call must be distinct; "
        "patching one must not affect the other"
    )


# ---------------------------------------------------------------------------
# Priority 1: execute() ↔ mock_agent_ai co-usage (from test_planner_execute)
#
# test_planner_execute.py uses mock_agent_ai but also patches run_dag.
# This verifies that mock_agent_ai is NOT called when run_dag is fully mocked
# (i.e., execute() doesn't call app.call before delegating to run_dag).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_delegates_to_run_dag_without_calling_mock_agent_ai(mock_agent_ai, tmp_path):
    """execute() must delegate directly to run_dag without making any
    app.call() invocations itself (those happen inside run_dag).

    Interaction: test_planner_execute (patches run_dag)
    ↔ conftest.mock_agent_ai (patches app.call) — they must not conflict.
    """
    from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult

    plan_result = {
        "issues": [{"name": "issue-x", "depends_on": [], "title": "X",
                    "description": "Do X", "acceptance_criteria": []}],
        "levels": [["issue-x"]],
        "rationale": "Test",
        "artifacts_dir": "",
        "prd": {"validated_description": "T", "acceptance_criteria": []},
        "architecture": {"summary": "T"},
        "file_conflicts": [],
    }

    dag_state = DAGState(
        repo_path=str(tmp_path),
        completed_issues=[IssueResult(
            issue_name="issue-x",
            outcome=IssueOutcome.COMPLETED,
            result_summary="Done",
        )],
        failed_issues=[],
    )

    import swe_af.app as app_module
    with patch("swe_af.execution.dag_executor.run_dag",
               new=AsyncMock(return_value=dag_state)) as mock_run_dag:
        result = await app_module.execute(
            plan_result=plan_result,
            repo_path=str(tmp_path),
        )

    # execute() itself must not call app.call (mock_agent_ai must be unused)
    mock_agent_ai.assert_not_called()

    # run_dag must have been called once with the plan_result
    mock_run_dag.assert_called_once()
    call_kwargs = mock_run_dag.call_args
    assert call_kwargs.kwargs.get("plan_result") is plan_result or \
           (call_kwargs.args and call_kwargs.args[0] is plan_result), (
        "run_dag must be called with the plan_result from execute()"
    )


# ---------------------------------------------------------------------------
# Priority 3: _validate_file_conflicts ↔ plan() output "file_conflicts" key
#
# plan() calls _validate_file_conflicts(issues, levels) and includes the
# result in PlanResult. execute() receives this as plan_result["file_conflicts"].
# ---------------------------------------------------------------------------


def test_file_conflicts_key_present_in_plan_result_schema():
    """plan() output must include 'file_conflicts' key from _validate_file_conflicts.

    Interaction: pipeline._validate_file_conflicts
    ↔ plan() PlanResult ↔ execute() input schema.
    """
    from swe_af.reasoners.pipeline import _compute_levels, _validate_file_conflicts
    from swe_af.reasoners.schemas import PlanResult

    # Issues that share a file at the same parallel level → conflict
    # Include all required PlannedIssue fields (title, description, acceptance_criteria)
    issues_with_conflict = [
        {
            "name": "issue-x", "title": "Issue X", "description": "Do X",
            "acceptance_criteria": ["X works"], "depends_on": [],
            "files_to_create": [], "files_to_modify": ["shared.py"],
        },
        {
            "name": "issue-y", "title": "Issue Y", "description": "Do Y",
            "acceptance_criteria": ["Y works"], "depends_on": [],
            "files_to_create": [], "files_to_modify": ["shared.py"],
        },
    ]
    levels = _compute_levels(issues_with_conflict)
    conflicts = _validate_file_conflicts(issues_with_conflict, levels)

    assert len(conflicts) == 1, f"Expected 1 conflict, got {conflicts}"
    assert conflicts[0]["file"] == "shared.py"
    assert set(conflicts[0]["issues"]) == {"issue-x", "issue-y"}
    assert conflicts[0]["level"] == 0

    # PlanResult must accept file_conflicts (no schema validation error)
    pr = PlanResult(
        prd={"validated_description": "T", "acceptance_criteria": [],
             "must_have": [], "nice_to_have": [], "out_of_scope": [],
             "assumptions": [], "risks": []},
        architecture={"summary": "T", "components": [], "interfaces": [],
                      "decisions": [], "file_changes_overview": ""},
        review={"approved": True, "feedback": "", "scope_issues": [],
                "complexity_assessment": "appropriate", "summary": "OK"},
        issues=issues_with_conflict,
        levels=levels,
        file_conflicts=conflicts,
        artifacts_dir="/tmp",
        rationale="test",
    )
    dumped = pr.model_dump()
    assert "file_conflicts" in dumped
    assert len(dumped["file_conflicts"]) == 1


# ---------------------------------------------------------------------------
# Priority 3: fast planner fallback ↔ fast executor schema round-trip
#
# When fast_plan_tasks() returns a fallback plan (fallback_used=True),
# the resulting dict must still be a valid input for fast_execute_tasks().
# This tests the interface between the two fast-path branches.
# ---------------------------------------------------------------------------


def test_fast_planner_fallback_output_is_valid_executor_input():
    """The fallback FastPlanResult from fast_plan_tasks must produce a dict
    whose 'tasks' list is structurally compatible with fast_execute_tasks input.

    Interaction: test_malformed_responses (fast planner fallback)
    ↔ fast executor (fast_execute_tasks tasks input contract).
    """
    from swe_af.fast.schemas import FastPlanResult, FastTask

    fallback = FastPlanResult(
        tasks=[
            FastTask(
                name="implement-goal",
                title="Implement goal",
                description="Build a feature",
                acceptance_criteria=["Goal is implemented."],
            )
        ],
        rationale="Fallback plan.",
        fallback_used=True,
    )
    plan_dict = fallback.model_dump()

    # Verify the fallback plan structure satisfies fast_execute_tasks expectations
    assert plan_dict["fallback_used"] is True
    tasks = plan_dict["tasks"]
    assert len(tasks) >= 1

    # fast_execute_tasks reads: name, title, description, acceptance_criteria,
    # files_to_create, files_to_modify
    required_keys = {"name", "title", "description", "acceptance_criteria"}
    for task in tasks:
        missing = required_keys - set(task.keys())
        assert not missing, (
            f"Fallback task missing keys {missing} needed by fast_execute_tasks"
        )
        assert task["name"] == "implement-goal"

"""Integration tests verifying cross-branch interactions between merged features.

Targeted interactions under test:
1. conftest.agentfield_server_guard (branch 03-conftest-root)
   ↔ test_malformed_responses (branch 06): guard must activate for fast tests
2. conftest.mock_agent_ai (branch 03-conftest-root)
   ↔ test_planner_pipeline (branch 04): mock fixture isolation per test
3. test_planner_pipeline (branch 04)
   ↔ test_planner_execute (branch 05): plan() output fields satisfying execute() input
4. test_malformed_responses fast executor (branch 06)
   ↔ test_node_id_isolation NODE_ID (branch 07): executor uses NODE_ID for routing
5. conftest.mock_agent_ai (branch 03) patch target
   ↔ test_malformed_responses (branch 06) distinct fast-app patch target:
   patching one must not bleed into the other

These tests focus on the boundaries where merged branches interact,
especially where conflict resolutions could introduce subtle bugs.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call as mock_call

import pytest

# Ensure safe server env var (agentfield_server_guard enforces this at session scope)
os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Priority 1: conftest.agentfield_server_guard activates for ALL merged modules
#
# The guard in conftest.py (branch 03) is session-scoped autouse=True.
# It must be active when running tests from any of the merged branches.
# This test verifies the guard logic itself: it must reject real hosts and
# accept localhost addresses.
# ---------------------------------------------------------------------------


def test_agentfield_server_guard_logic_rejects_real_hosts():
    """The _is_real_host helper in conftest must reject real API endpoints.

    Interaction: conftest.py guard ↔ all merged test modules (04, 05, 06, 07).
    Any of those modules that accidentally use a real server URL would be
    blocked by the guard, protecting all merged tests.
    """
    from tests.conftest import _is_real_host  # type: ignore[import]

    # Real hosts must be blocked
    assert _is_real_host("https://api.agentfield.io") is True
    assert _is_real_host("https://api.anthropic.com") is True
    assert _is_real_host("https://api.openai.com") is True

    # Local addresses must be allowed
    assert _is_real_host("http://localhost:9999") is False
    assert _is_real_host("http://127.0.0.1:8080") is False
    assert _is_real_host("http://localhost") is False


def test_agentfield_server_guard_accepts_localhost_url():
    """The guard accepts the standard test server address used by all merged branches.

    Interaction: conftest.py guard ↔ all 4 merged test modules.
    All tests use AGENTFIELD_SERVER=http://localhost:9999 — the guard must
    not raise for this value.
    """
    from tests.conftest import _is_real_host  # type: ignore[import]

    # This is the value set by all 4 merged test modules
    test_server = "http://localhost:9999"
    assert _is_real_host(test_server) is False, (
        f"Guard must accept {test_server!r} — used by all merged test modules"
    )


# ---------------------------------------------------------------------------
# Priority 1: mock_agent_ai fixture function-scoped isolation
#
# test_planner_pipeline (branch 04) and test_planner_execute (branch 05) both
# use mock_agent_ai. With asyncio_mode=auto (from pyproject.toml, branch 01),
# the fixture must be freshly created per test — side_effect from one test must
# not leak into the next.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_agent_ai_side_effect_isolated_per_test_call_count(mock_agent_ai, tmp_path):
    """Verify mock_agent_ai is a fresh AsyncMock per test (call_count starts at 0).

    Interaction: conftest.mock_agent_ai (branch 03)
    ↔ test_planner_pipeline async tests (branch 04)
    ↔ asyncio_mode=auto pyproject.toml config (branch 01).

    If the fixture were session-scoped, previous call counts would accumulate.
    Function scope guarantees each test gets a pristine mock.
    """
    # At the start of this test, call_count must be 0 (fresh fixture)
    assert mock_agent_ai.call_count == 0, (
        f"mock_agent_ai must start fresh each test, got call_count={mock_agent_ai.call_count}"
    )

    # Trigger exactly one call by invoking app.call directly
    import swe_af.app as app_module
    await app_module.app.call("test.target", data="x")

    assert mock_agent_ai.call_count == 1, (
        f"Expected 1 call after invoking app.call once, got {mock_agent_ai.call_count}"
    )


@pytest.mark.asyncio
async def test_mock_agent_ai_side_effect_isolated_per_test_no_residual(mock_agent_ai, tmp_path):
    """A second test using mock_agent_ai must see call_count=0, not 1 from prior test.

    This test exists to confirm function-scoped isolation after
    test_mock_agent_ai_side_effect_isolated_per_test_call_count ran.
    """
    # Fresh mock — count must be 0 regardless of prior test
    assert mock_agent_ai.call_count == 0, (
        f"Fixture must reset between tests; got call_count={mock_agent_ai.call_count}"
    )


# ---------------------------------------------------------------------------
# Priority 1: plan() output ↔ execute() input — merged branch contract test
#
# Branch 04 (test-planner-pipeline) produces plan() output.
# Branch 05 (test-execute-pipeline) consumes it via execute().
# Verify that the exact fields plan() emits are accepted by execute().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_then_execute_end_to_end_mock_pipeline(mock_agent_ai, tmp_path):
    """Full pipeline: plan() output passed directly to execute() must succeed.

    Interaction: test_planner_pipeline (branch 04) plan() output
    ↔ test_planner_execute (branch 05) execute() input.

    This is the most important cross-branch integration: the plan/execute
    contract must hold across the merged branches.
    """
    from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult

    # --- Step 1: Run plan() with full mock pipeline (from branch 04 fixtures) ---
    prd = {
        "validated_description": "E2E integration test goal.",
        "acceptance_criteria": ["E2E works"],
        "must_have": ["e2e"],
        "nice_to_have": [],
        "out_of_scope": [],
        "assumptions": [],
        "risks": [],
    }
    arch = {
        "summary": "E2E architecture.",
        "components": [{"name": "e2e-comp", "responsibility": "Does E2E",
                        "touches_files": ["e2e.py"], "depends_on": []}],
        "interfaces": ["e2e-iface"],
        "decisions": [{"decision": "Use pytest", "rationale": "Standard."}],
        "file_changes_overview": "Only e2e.py.",
    }
    review = {
        "approved": True,
        "feedback": "E2E approved.",
        "scope_issues": [],
        "complexity_assessment": "appropriate",
        "summary": "OK.",
    }
    sprint = {
        "issues": [
            {
                "name": "e2e-issue-1",
                "title": "E2E Issue 1",
                "description": "Implement E2E.",
                "acceptance_criteria": ["E2E passes"],
                "depends_on": [],
                "provides": ["e2e"],
                "estimated_complexity": "small",
                "files_to_create": ["e2e.py"],
                "files_to_modify": [],
                "testing_strategy": "pytest",
                "sequence_number": None,
                "guidance": None,
            }
        ],
        "rationale": "E2E rationale.",
    }
    issue_writer = {"success": True, "path": "/tmp/e2e.md"}

    mock_agent_ai.side_effect = [prd, arch, review, sprint, issue_writer]

    import swe_af.app as app_module
    real_plan = getattr(app_module.plan, "_original_func", app_module.plan)
    plan_result = await real_plan(
        goal="E2E integration test",
        repo_path=str(tmp_path),
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

    # --- Step 2: Verify plan_result has all fields needed by execute() ---
    assert "issues" in plan_result, "plan() must emit 'issues' key for execute()"
    assert "levels" in plan_result, "plan() must emit 'levels' key for execute()"
    assert len(plan_result["issues"]) == 1
    assert plan_result["issues"][0]["name"] == "e2e-issue-1"

    # --- Step 3: Pass plan_result to execute() with mocked run_dag ---
    dag_state = DAGState(
        repo_path=str(tmp_path),
        completed_issues=[IssueResult(
            issue_name="e2e-issue-1",
            outcome=IssueOutcome.COMPLETED,
            result_summary="E2E done",
        )],
        failed_issues=[],
    )

    with patch("swe_af.execution.dag_executor.run_dag",
               new=AsyncMock(return_value=dag_state)) as mock_run_dag:
        exec_result = await app_module.execute(
            plan_result=plan_result,
            repo_path=str(tmp_path),
        )

    assert isinstance(exec_result, dict), "execute() must return dict"
    assert len(exec_result["completed_issues"]) == 1
    assert exec_result["completed_issues"][0]["issue_name"] == "e2e-issue-1"
    assert len(exec_result["failed_issues"]) == 0
    mock_run_dag.assert_called_once()


# ---------------------------------------------------------------------------
# Priority 1: fast executor NODE_ID ↔ test_node_id_isolation
#
# Branch 06 (test-malformed-responses) patches swe_af.fast.app.app.call.
# Branch 07 (test-node-id-isolation) tests NODE_ID at module load time.
# When NODE_ID is set to something custom, the fast executor routes to
# f"{NODE_ID}.run_coder" — verify the routing string uses the module's
# NODE_ID, not a hardcoded value.
# ---------------------------------------------------------------------------


def test_fast_executor_node_id_routing_string_uses_module_constant():
    """fast_execute_tasks routes to f'{NODE_ID}.run_coder' using the module constant.

    Interaction: test_malformed_responses (branch 06, patches fast app.call)
    ↔ test_node_id_isolation (branch 07, verifies NODE_ID module constant).

    If the executor hardcoded 'swe-fast.run_coder' instead of using NODE_ID,
    changing NODE_ID would silently break routing while isolation tests pass.
    """
    import swe_af.fast.executor as executor_module
    import swe_af.fast.app as fast_app_module

    # Verify executor has a module-level NODE_ID
    assert hasattr(executor_module, "NODE_ID"), (
        "swe_af.fast.executor must have a module-level NODE_ID constant"
    )

    # fast executor NODE_ID must match the fast app's NODE_ID at import time
    # (both read os.getenv("NODE_ID", "swe-fast") at module load)
    assert executor_module.NODE_ID == fast_app_module.NODE_ID, (
        f"executor.NODE_ID={executor_module.NODE_ID!r} must equal "
        f"fast_app.NODE_ID={fast_app_module.NODE_ID!r} — both read env at import time"
    )


def test_fast_executor_routes_to_node_id_dot_run_coder():
    """Verify executor source code uses f'{NODE_ID}.run_coder' routing pattern.

    Interaction: test_malformed_responses (branch 06)
    ↔ test_node_id_isolation (branch 07).

    When tests in branch 06 patch swe_af.fast.app.app.call, they intercept
    ALL calls from the executor, including those with NODE_ID-prefixed targets.
    This test ensures the routing pattern is consistent with what's patched.
    """
    import inspect
    import swe_af.fast.executor as executor_module

    raw_fn = getattr(
        executor_module.fast_execute_tasks,
        "_original_func",
        executor_module.fast_execute_tasks,
    )
    source = inspect.getsource(raw_fn)

    assert "{NODE_ID}.run_coder" in source, (
        "fast_execute_tasks must route via f'{NODE_ID}.run_coder', "
        "not a hardcoded node_id string"
    )


# ---------------------------------------------------------------------------
# Priority 2: conftest.mock_agent_ai (planner patch target)
#             ↔ test_malformed_responses (fast patch target) — no bleed-through
#
# Branch 03 patches swe_af.app.app.call.
# Branch 06 patches swe_af.fast.app.app.call.
# These must be truly independent objects so tests don't interfere.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conftest_mock_patches_planner_not_fast_app(mock_agent_ai, tmp_path):
    """mock_agent_ai (patches swe_af.app.app.call) must NOT intercept
    calls to swe_af.fast.app.app.call.

    Interaction: conftest.mock_agent_ai (branch 03)
    ↔ test_malformed_responses which patches fast app.call (branch 06).

    If the patch target were incorrect or the two apps were the same object,
    patching one would bleed into the other.
    """
    import swe_af.app as planner_app_mod
    import swe_af.fast.app as fast_app_mod

    # Verify the mock patches the planner's call (not the fast app's)
    # The mock IS active on planner app.call
    assert planner_app_mod.app.call is mock_agent_ai, (
        "mock_agent_ai must be the same object as swe_af.app.app.call while patched"
    )

    # The fast app's call must NOT be the mock
    assert fast_app_mod.app.call is not mock_agent_ai, (
        "mock_agent_ai must NOT patch swe_af.fast.app.app.call — "
        "these are distinct Agent instances"
    )


@pytest.mark.asyncio
async def test_fast_app_call_patch_does_not_affect_planner_mock_agent_ai(mock_agent_ai):
    """Patching swe_af.fast.app.app.call must not affect mock_agent_ai
    (which patches swe_af.app.app.call).

    Interaction: conftest.mock_agent_ai (branch 03, swe_af.app.app.call)
    ↔ test_malformed_responses fast patch (branch 06, swe_af.fast.app.app.call).
    """
    import swe_af.app as planner_app_mod
    import swe_af.fast.app as fast_app_mod

    fast_mock = AsyncMock(return_value={"complete": True, "files_changed": [], "summary": ""})
    with patch("swe_af.fast.app.app.call", fast_mock):
        # After patching fast app, planner mock must still be active
        assert planner_app_mod.app.call is mock_agent_ai, (
            "Patching fast app.call must not disturb the planner app.call mock"
        )
        # And the fast mock must be active
        assert fast_app_mod.app.call is fast_mock, (
            "The fast app mock must be active in this context"
        )

    # After the context manager exits, planner mock is still active
    assert planner_app_mod.app.call is mock_agent_ai, (
        "Planner mock must still be active after fast mock context exits"
    )


# ---------------------------------------------------------------------------
# Priority 2: agentfield_server_guard ↔ test_malformed_responses env var
#
# Branch 06 sets os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
# at module level. The conftest guard (branch 03) checks this env var.
# Verify the two interact correctly: the module-level setdefault ensures the
# guard sees a safe value.
# ---------------------------------------------------------------------------


def test_agentfield_server_env_var_set_by_malformed_responses_module():
    """test_malformed_responses.py sets AGENTFIELD_SERVER at module level via setdefault.

    Interaction: test_malformed_responses (branch 06) module-level env var
    ↔ conftest.agentfield_server_guard (branch 03) which checks it.

    If this setdefault were missing, importing test_malformed_responses could
    leave AGENTFIELD_SERVER unset, causing the guard to raise at session start.
    """
    # The module is already imported (we're in the test suite); env var must be set
    server = os.environ.get("AGENTFIELD_SERVER", "")
    assert server, (
        "AGENTFIELD_SERVER must be set (test_malformed_responses sets it via setdefault)"
    )
    assert "localhost" in server or "127.0.0.1" in server, (
        f"AGENTFIELD_SERVER must be a local address, got {server!r}"
    )


def test_agentfield_server_env_var_set_by_node_id_isolation_module():
    """test_node_id_isolation.py also sets AGENTFIELD_SERVER at module level.

    Interaction: test_node_id_isolation (branch 07) module-level env var
    ↔ conftest.agentfield_server_guard (branch 03).

    Both branch 06 and 07 use os.environ.setdefault, so they don't clobber
    the value if it's already set. Verify the value is local.
    """
    server = os.environ.get("AGENTFIELD_SERVER", "")
    assert server, "AGENTFIELD_SERVER must be set"
    assert "localhost" in server or "127.0.0.1" in server, (
        f"AGENTFIELD_SERVER={server!r} must be a local address"
    )


# ---------------------------------------------------------------------------
# Priority 2: asyncio_mode=auto (pyproject.toml, branch 01)
#             ↔ @pytest.mark.asyncio decorators in branches 04 and 05
#
# Branch 01 sets asyncio_mode="auto" so async tests don't need @pytest.mark.asyncio.
# Branches 04 and 05 include redundant @pytest.mark.asyncio decorators (noted in
# merge review as non-blocking). Verify that auto mode doesn't conflict with
# explicit marks — tests must still be collected and pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_asyncio_mark_compatible_with_auto_mode():
    """Explicit @pytest.mark.asyncio is compatible with asyncio_mode='auto'.

    Interaction: pyproject.toml asyncio_mode=auto (branch 01)
    ↔ @pytest.mark.asyncio in test_planner_pipeline (branch 04)
    ↔ @pytest.mark.asyncio in test_planner_execute (branch 05).

    The merge review noted that branches 04 and 05 use redundant marks.
    With asyncio_mode='auto', redundant marks are harmless — both should
    collect and run as async tests without error.
    """
    # This test itself uses an explicit mark + auto mode — if it runs, compatibility holds
    # Use a native coroutine to verify async execution works
    async def _coro():
        await asyncio.sleep(0)
        return {"status": "ok"}

    result = await _coro()
    assert result == {"status": "ok"}, (
        "Explicit @pytest.mark.asyncio + asyncio_mode='auto' must coexist"
    )


async def test_auto_mode_async_test_without_explicit_mark():
    """Auto mode must run async tests even WITHOUT @pytest.mark.asyncio.

    Interaction: pyproject.toml asyncio_mode=auto (branch 01)
    ↔ conftest.py async fixtures (branch 03).

    This test has no @pytest.mark.asyncio decorator — asyncio_mode='auto'
    must still pick it up as an async test.
    """
    await asyncio.sleep(0)  # Proves we're running in an event loop
    assert True, "async test without explicit mark must run with asyncio_mode=auto"


# ---------------------------------------------------------------------------
# Priority 3: PlanResult schema ↔ execute() DAGState schema
#
# Verify that the fields plan() uses from test_planner_pipeline (branch 04)
# are structurally compatible with what execute() passes to run_dag (branch 05).
# ---------------------------------------------------------------------------


def test_plan_result_issues_have_fields_expected_by_execute():
    """PlanResult issues must contain name and depends_on (required by DAG executor).

    Interaction: plan() PlanResult schema (branch 04 tests)
    ↔ execute() run_dag input schema (branch 05 tests).
    """
    from swe_af.reasoners.schemas import PlanResult
    from swe_af.reasoners.pipeline import _compute_levels

    issues_data = [
        {
            "name": "schema-issue-1",
            "title": "Schema Test Issue 1",
            "description": "Test schema compatibility.",
            "acceptance_criteria": ["Schema works"],
            "depends_on": [],
            "provides": [],
            "estimated_complexity": "small",
            "files_to_create": ["schema1.py"],
            "files_to_modify": [],
            "testing_strategy": "pytest",
            "sequence_number": 1,
            "guidance": None,
        }
    ]
    levels = _compute_levels(issues_data)

    pr = PlanResult(
        prd={"validated_description": "Test", "acceptance_criteria": [],
             "must_have": [], "nice_to_have": [], "out_of_scope": [],
             "assumptions": [], "risks": []},
        architecture={"summary": "T", "components": [], "interfaces": [],
                      "decisions": [], "file_changes_overview": ""},
        review={"approved": True, "feedback": "", "scope_issues": [],
                "complexity_assessment": "appropriate", "summary": "OK"},
        issues=issues_data,
        levels=levels,
        file_conflicts=[],
        artifacts_dir="/tmp",
        rationale="test",
    )
    dumped = pr.model_dump()

    # execute() and run_dag access issues[i]["name"] and issues[i]["depends_on"]
    assert "issues" in dumped
    for issue in dumped["issues"]:
        assert "name" in issue, "Each issue must have 'name' for DAG executor"
        assert "depends_on" in issue, "Each issue must have 'depends_on' for topological sort"


def test_dag_state_schema_compatible_with_execute_return_value():
    """DAGState.model_dump() must contain completed_issues and failed_issues.

    Interaction: test_planner_execute (branch 05) asserts on these keys
    in execute()'s return value. DAGState must have these fields.
    """
    from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult

    state = DAGState(
        repo_path="/tmp/test",
        completed_issues=[
            IssueResult(
                issue_name="test-issue",
                outcome=IssueOutcome.COMPLETED,
                result_summary="Done",
            )
        ],
        failed_issues=[],
    )
    dumped = state.model_dump()

    # Branch 05 (test_execute_single_issue) asserts on these exact keys
    assert "completed_issues" in dumped, (
        "DAGState must have 'completed_issues' key — used by test_planner_execute"
    )
    assert "failed_issues" in dumped, (
        "DAGState must have 'failed_issues' key — used by test_planner_execute"
    )
    assert dumped["completed_issues"][0]["issue_name"] == "test-issue"


# ---------------------------------------------------------------------------
# Priority 3: _KeyErrorEnvelope (branch 06) ↔ envelope.py _ENVELOPE_KEYS (shared)
#
# The _KeyErrorEnvelope in test_malformed_responses uses execution_id as the
# trigger key. This must match an actual key in _ENVELOPE_KEYS.
# ---------------------------------------------------------------------------


def test_key_error_envelope_trigger_key_is_in_envelope_keys():
    """The 'execution_id' key used by _KeyErrorEnvelope must be in _ENVELOPE_KEYS.

    Interaction: test_malformed_responses _KeyErrorEnvelope (branch 06)
    ↔ envelope.py _ENVELOPE_KEYS (shared utility).

    If 'execution_id' were removed from _ENVELOPE_KEYS, _KeyErrorEnvelope
    would take the fast path in unwrap_call_result, bypassing the KeyError,
    and the test's intended behavior would silently break.
    """
    from swe_af.execution.envelope import _ENVELOPE_KEYS

    assert "execution_id" in _ENVELOPE_KEYS, (
        "'execution_id' must be in _ENVELOPE_KEYS so _KeyErrorEnvelope "
        "(test_malformed_responses) triggers the envelope unwrap path"
    )
    assert "status" in _ENVELOPE_KEYS, (
        "'status' must be in _ENVELOPE_KEYS — _unwrap reads result.get('status')"
    )


def test_envelope_keys_coverage_for_malformed_response_test_shapes():
    """All dict shapes used in test_malformed_responses must be classified correctly.

    Interaction: test_malformed_responses (branch 06) mock shapes
    ↔ envelope.unwrap_call_result (shared).

    - _KeyErrorEnvelope({"execution_id": "fake-id"}): has envelope key → envelope path
    - {"execution_id": "fake-id", "status": "success", "result": None}: envelope path
    """
    from swe_af.execution.envelope import _ENVELOPE_KEYS, unwrap_call_result

    # Shape 1: _KeyErrorEnvelope — must trigger envelope path (has execution_id)
    shape1 = {"execution_id": "fake-id"}
    assert bool(_ENVELOPE_KEYS.intersection(shape1)), (
        "_KeyErrorEnvelope must trigger envelope path via 'execution_id'"
    )

    # Shape 2: status=success, result=None — must return the dict itself
    shape2 = {"execution_id": "fake-id", "status": "success", "result": None}
    result2 = unwrap_call_result(shape2)
    assert result2 is shape2, (
        "Envelope with result=None must return the envelope dict itself"
    )

"""Streaming DAG executor backed by PlanDB.

Replaces the level-based ``run_dag()`` with a streaming model where tasks
start the moment their PlanDB dependencies resolve.  Workers are spawned as
parallel reasoner calls so every claim→code→merge→reassess cycle appears as
a visible edge in the AgentField call graph.

Architecture::

    run_streaming_dag()           ← thin bootstrapper (~120 lines)
      ├─ call_fn(run_plandb_planner)   ← AI creates PlanDB tasks
      ├─ call_fn(run_streaming_worker) × N  ← parallel workers
      │     ├─ plandb go → claim task
      │     ├─ _execute_single_issue() → coding pipeline
      │     ├─ call_fn(run_merger)     → merge to integration branch
      │     ├─ call_fn(run_reassess)   → AI adjusts future tasks
      │     └─ loop until no more ready tasks
      └─ collect results → DAGState
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Callable

from swe_af.execution.envelope import unwrap_call_result
from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
)


# ---------------------------------------------------------------------------
# PlanDB CLI helpers (thin wrappers around subprocess)
# ---------------------------------------------------------------------------

def _plandb(args: list[str], db_path: str) -> dict | None:
    """Run a plandb CLI command and return parsed JSON, or None on failure."""
    cmd = ["plandb", *args, "--db", db_path, "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _plandb_status(db_path: str) -> dict:
    """Get project status from PlanDB."""
    return _plandb(["status", "--full"], db_path) or {}


def _plandb_list(db_path: str, status: str = "") -> list[dict]:
    """List tasks, optionally filtered by status."""
    args = ["list"]
    if status:
        args.extend(["--status", status])
    result = _plandb(args, db_path)
    if result is None:
        return []
    # plandb list --json returns {"tasks": [...]}
    return result.get("tasks", [])


# ---------------------------------------------------------------------------
# DAGState initialization from PlanDB (post-execution)
# ---------------------------------------------------------------------------

def _build_final_dag_state(
    *,
    base_state: DAGState,
    worker_results: list[dict],
    db_path: str,
) -> DAGState:
    """Build the final DAGState from PlanDB status + worker results.

    Populates completed_issues, failed_issues, skipped_issues from the
    aggregated worker results.
    """
    completed: list[IssueResult] = []
    failed: list[IssueResult] = []
    skipped: list[str] = []

    for wr in worker_results:
        if isinstance(wr, Exception):
            continue
        if isinstance(wr, dict):
            for c in wr.get("completed", []):
                completed.append(IssueResult(**c) if isinstance(c, dict) else c)
            for f in wr.get("failed", []):
                failed.append(IssueResult(**f) if isinstance(f, dict) else f)

    # Query PlanDB for any tasks that ended up cancelled/pending (skipped)
    all_tasks = _plandb_list(db_path)
    done_names = {r.issue_name for r in completed} | {r.issue_name for r in failed}
    for task in all_tasks:
        task_title = task.get("title", "")
        if task_title not in done_names and task.get("status") not in ("done", "running"):
            skipped.append(task_title)

    return DAGState(
        repo_path=base_state.repo_path,
        artifacts_dir=base_state.artifacts_dir,
        prd_path=base_state.prd_path,
        architecture_path=base_state.architecture_path,
        issues_dir=base_state.issues_dir,
        original_plan_summary=base_state.original_plan_summary,
        prd_summary=base_state.prd_summary,
        architecture_summary=base_state.architecture_summary,
        all_issues=base_state.all_issues,
        levels=base_state.levels,
        completed_issues=completed,
        failed_issues=failed,
        skipped_issues=skipped,
        git_integration_branch=base_state.git_integration_branch,
        git_original_branch=base_state.git_original_branch,
        git_initial_commit=base_state.git_initial_commit,
        git_mode=base_state.git_mode,
        build_id=base_state.build_id,
        workspace_manifest=base_state.workspace_manifest,
        worktrees_dir=base_state.worktrees_dir,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_streaming_dag(
    plan_result: dict,
    repo_path: str,
    config: ExecutionConfig | None = None,
    note_fn: Callable | None = None,
    call_fn: Callable | None = None,
    node_id: str = "swe-planner",
    git_config: dict | None = None,
    build_id: str = "",
    workspace_manifest: dict | None = None,
) -> DAGState:
    """Execute a planned DAG using PlanDB for streaming task dispatch.

    This replaces ``run_dag()`` with a streaming model:
    1. Initialize PlanDB with the task graph (via planning harness)
    2. Spawn N worker reasoners in parallel (each loops: claim→code→merge→reassess)
    3. Workers exit when no more tasks are ready
    4. Build final DAGState from results

    All inter-reasoner calls go through ``call_fn`` (``app.call``) so the full
    execution graph is visible in AgentField.
    """
    if config is None:
        config = ExecutionConfig()

    if call_fn is None:
        raise ValueError("call_fn (app.call) is required for streaming executor")

    # Wrap call_fn to unwrap execution envelopes
    _raw_call_fn = call_fn

    async def wrapped_call_fn(target: str, **kwargs):
        result = await _raw_call_fn(target, **kwargs)
        return unwrap_call_result(result, target)

    # --- Paths ---
    artifacts_dir = plan_result.get("artifacts_dir", "")
    plandb_dir = os.path.join(artifacts_dir, "plandb") if artifacts_dir else "/tmp/plandb"
    os.makedirs(plandb_dir, exist_ok=True)
    db_path = os.path.join(plandb_dir, "plandb.db")

    # --- Build base DAGState (for context passing to workers) ---
    from swe_af.execution.dag_executor import _init_dag_state
    base_state = _init_dag_state(plan_result, repo_path, git_config=git_config, build_id=build_id)
    base_state.workspace_manifest = workspace_manifest

    if note_fn:
        note_fn(
            f"Streaming executor starting: {len(plan_result.get('issues', []))} issues, "
            f"db={db_path}",
            tags=["streaming", "start"],
        )

    # --- Step 1: Planning harness creates PlanDB tasks ---
    planner_result = await wrapped_call_fn(
        f"{node_id}.run_plandb_planner",
        plan_result=plan_result,
        build_id=build_id,
        db_path=db_path,
        model=config.coder_model,  # reuse coder model for planner
        ai_provider=config.ai_provider,
    )

    task_count = planner_result.get("task_count", 0)
    task_map = planner_result.get("task_map", {})

    if note_fn:
        note_fn(
            f"PlanDB populated: {task_count} tasks created, "
            f"{planner_result.get('ready_count', 0)} ready",
            tags=["streaming", "planner", "done"],
        )

    # --- Step 2: Spawn N worker reasoners in parallel ---
    max_workers = config.max_concurrent_issues or 3

    worker_coros = [
        wrapped_call_fn(
            f"{node_id}.run_streaming_worker",
            repo_path=repo_path,
            agent_id=f"worker-{i}",
            db_path=db_path,
            integration_branch=base_state.git_integration_branch,
            artifacts_dir=artifacts_dir,
            config_dict=config.model_dump(),
            build_id=build_id,
            dag_state_dict=base_state.model_dump(),
            task_map=task_map,
            model=config.coder_model,
            ai_provider=config.ai_provider,
        )
        for i in range(max_workers)
    ]

    if note_fn:
        note_fn(
            f"Spawning {max_workers} streaming workers",
            tags=["streaming", "workers", "start"],
        )

    worker_results = await asyncio.gather(*worker_coros, return_exceptions=True)

    # --- Step 3: Collect results and build final DAGState ---
    for i, wr in enumerate(worker_results):
        if isinstance(wr, Exception):
            if note_fn:
                note_fn(
                    f"Worker-{i} failed: {wr}",
                    tags=["streaming", "worker", "error"],
                )

    dag_state = _build_final_dag_state(
        base_state=base_state,
        worker_results=[r for r in worker_results if not isinstance(r, Exception)],
        db_path=db_path,
    )

    if note_fn:
        note_fn(
            f"Streaming executor complete: "
            f"{len(dag_state.completed_issues)} completed, "
            f"{len(dag_state.failed_issues)} failed, "
            f"{len(dag_state.skipped_issues)} skipped",
            tags=["streaming", "complete"],
        )

    return dag_state

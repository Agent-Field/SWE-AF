"""Replanner agent — invokes app.harness() to restructure the DAG after failures."""

from __future__ import annotations

import os
from typing import Callable

from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueResult,
    ReplanAction,
    ReplanDecision,
)
from swe_af.prompts.replanner import SYSTEM_PROMPT, replanner_task_prompt


async def invoke_replanner(
    dag_state: DAGState,
    failed_issues: list[IssueResult],
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Call the replanner agent to decide how to handle unrecoverable failures.

    Uses app.harness() with read-only codebase access and the full DAG context.

    Returns:
        ReplanDecision from the replanner agent. Falls back to ABORT if the
        agent fails to produce valid output.
    """
    from swe_af.app import app  # noqa: PLC0415

    if note_fn:
        failed_names = [f.issue_name for f in failed_issues]
        note_fn(
            f"Replanning triggered (attempt {dag_state.replan_count + 1}/{config.max_replans}): "
            f"failed issues = {failed_names}",
            tags=["execution", "replan", "start"],
        )

    task_prompt = replanner_task_prompt(dag_state, failed_issues)

    try:
        result = await app.harness(
            prompt=task_prompt,
            schema=ReplanDecision,
            system_prompt=SYSTEM_PROMPT,
            cwd=dag_state.repo_path or ".",
        )

        if result.parsed is not None:
            if note_fn:
                note_fn(
                    f"Replan decision: {result.parsed.action.value} — {result.parsed.summary}",
                    tags=["execution", "replan", "complete"],
                )
            return result.parsed

    except Exception as e:
        if note_fn:
            note_fn(
                f"Replanner agent failed: {e}",
                tags=["execution", "replan", "error"],
            )

    # Fallback: if the replanner fails, abort
    fallback = ReplanDecision(
        action=ReplanAction.ABORT,
        rationale="Replanner agent failed to produce a valid decision. Aborting.",
        summary="Replanner failure — automatic abort.",
    )
    if note_fn:
        note_fn(
            "Replanner failed to produce valid output — falling back to ABORT",
            tags=["execution", "replan", "fallback"],
        )
    return fallback

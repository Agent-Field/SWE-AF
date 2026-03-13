"""Replanner agent — backward-compat direct invocation via router.harness()."""

from __future__ import annotations

import os
from typing import Callable

from swe_af.execution.schemas import (
    DAGState,
    DEFAULT_AGENT_MAX_TURNS,
    ExecutionConfig,
    IssueResult,
    ReplanAction,
    ReplanDecision,
    _normalize_provider,
)
from swe_af.prompts.replanner import SYSTEM_PROMPT, replanner_task_prompt
from swe_af.reasoners import router


async def invoke_replanner(
    dag_state: DAGState,
    failed_issues: list[IssueResult],
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Call the replanner agent to decide how to handle unrecoverable failures."""
    if note_fn:
        failed_names = [f.issue_name for f in failed_issues]
        note_fn(
            f"Replanning triggered (attempt {dag_state.replan_count + 1}/{config.max_replans}): "
            f"failed issues = {failed_names}",
            tags=["execution", "replan", "start"],
        )

    task_prompt = replanner_task_prompt(dag_state, failed_issues)

    log_dir = (
        os.path.join(dag_state.artifacts_dir, "logs")
        if dag_state.artifacts_dir
        else None
    )
    provider = _normalize_provider(config.ai_provider)

    try:
        result = await router.harness(
            prompt=task_prompt,
            schema=ReplanDecision,
            provider=provider,
            model=config.replan_model,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=None,
            system_prompt=SYSTEM_PROMPT,
            cwd=dag_state.repo_path or ".",
        )

        # Log raw response for debugging (non-fatal)
        if log_dir:
            try:
                raw_log = os.path.join(
                    log_dir, f"replanner_{dag_state.replan_count}_raw.txt"
                )
                os.makedirs(log_dir, exist_ok=True)
                with open(raw_log, "w") as f:
                    f.write(getattr(result, "text", "") or "(empty)")
            except OSError:
                pass

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

    # Fallback: if the replanner fails, CONTINUE (not ABORT) — aligned with
    # execution_agents.run_replanner Pitfall 5 fix: a replanner crash should
    # not kill the pipeline.
    failed_names = [f.issue_name for f in failed_issues]
    fallback = ReplanDecision(
        action=ReplanAction.CONTINUE,
        rationale=(
            "Replanner agent failed to produce a valid decision. "
            "Falling back to CONTINUE — downstream of failed issues will be "
            "notified of the gap but the pipeline will proceed."
        ),
        skipped_issue_names=[],
        summary=f"Replanner failure — continuing with gap notification for: {failed_names}",
    )
    if note_fn:
        note_fn(
            "Replanner failed to produce valid output — falling back to CONTINUE (not ABORT)",
            tags=["execution", "replan", "fallback"],
        )
    return fallback

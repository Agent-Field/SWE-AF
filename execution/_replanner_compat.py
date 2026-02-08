"""Replanner agent — invokes ClaudeAI to restructure the DAG after failures."""

from __future__ import annotations

import os
from typing import Callable

from claude_ai import ClaudeAI, ClaudeAIConfig
from claude_ai.types import Tool
from execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueResult,
    ReplanAction,
    ReplanDecision,
)
from prompts.replanner import SYSTEM_PROMPT, replanner_task_prompt


async def invoke_replanner(
    dag_state: DAGState,
    failed_issues: list[IssueResult],
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> ReplanDecision:
    """Call the replanner agent to decide how to handle unrecoverable failures.

    The replanner gets read-only codebase access and the full DAG context
    (completed work, failures with error context, remaining issues, PRD,
    architecture). It returns a structured ReplanDecision.

    Args:
        dag_state: Current execution state with all context.
        failed_issues: The unrecoverable failures that triggered replanning.
        config: Execution configuration (model, etc.).
        note_fn: Optional callback for observability notes.

    Returns:
        ReplanDecision from the replanner agent. Falls back to ABORT if the
        agent fails to produce valid output.
    """
    if note_fn:
        failed_names = [f.issue_name for f in failed_issues]
        note_fn(
            f"Replanning triggered (attempt {dag_state.replan_count + 1}/{config.max_replans}): "
            f"failed issues = {failed_names}",
            tags=["execution", "replan", "start"],
        )

    task_prompt = replanner_task_prompt(dag_state, failed_issues)

    log_dir = os.path.join(dag_state.artifacts_dir, "logs") if dag_state.artifacts_dir else None
    log_path = os.path.join(log_dir, f"replanner_{dag_state.replan_count}.jsonl") if log_dir else None

    ai = ClaudeAI(ClaudeAIConfig(
        model=config.replan_model,
        cwd=dag_state.repo_path or ".",
        max_turns=15,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=SYSTEM_PROMPT,
            output_schema=ReplanDecision,
            log_file=log_path,
        )

        if response.parsed is not None:
            if note_fn:
                note_fn(
                    f"Replan decision: {response.parsed.action.value} — {response.parsed.summary}",
                    tags=["execution", "replan", "complete"],
                )
            return response.parsed

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

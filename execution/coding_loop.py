"""Per-issue coding loop: coder → parallel(QA, reviewer) → synthesizer.

This is the INNER loop in the three-nested-loop architecture:
  - INNER (this): coder → QA/review → synthesizer → fix/approve/block
  - MIDDLE: retry_advisor diagnoses exceptions → retry with guidance
  - OUTER: replanner restructures DAG after unrecoverable failures
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Callable

from execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
)


async def run_coding_loop(
    issue: dict,
    dag_state: DAGState,
    call_fn: Callable,
    node_id: str,
    config: ExecutionConfig,
    note_fn: Callable | None = None,
) -> IssueResult:
    """Run the coder → QA/review → synthesizer loop for a single issue.

    Each iteration:
      1. Coder writes code & commits
      2. QA and code reviewer run in parallel
      3. Synthesizer merges feedback and decides fix/approve/block

    Returns an IssueResult with the final outcome.
    """
    issue_name = issue.get("name", "unknown")
    worktree_path = issue.get("worktree_path", dag_state.repo_path)
    branch_name = issue.get("branch_name", "")
    max_iterations = config.max_coding_iterations
    permission_mode = ""  # inherits from agent config

    # Project context from DAG state — gives agents the big picture
    project_context = {
        "prd_summary": dag_state.prd_summary,
        "architecture_summary": dag_state.architecture_summary,
        "prd_path": dag_state.prd_path,
        "architecture_path": dag_state.architecture_path,
        "artifacts_dir": dag_state.artifacts_dir,
        "issues_dir": dag_state.issues_dir,
        "repo_path": dag_state.repo_path,
    }

    if note_fn:
        note_fn(
            f"Coding loop starting: {issue_name} (max {max_iterations} iterations)",
            tags=["coding_loop", "start", issue_name],
        )

    feedback = ""  # merged feedback from synthesizer (empty on first pass)
    iteration_history: list[dict] = []  # summaries for stuck detection
    files_changed: list[str] = []

    for iteration in range(1, max_iterations + 1):
        iteration_id = str(uuid.uuid4())[:8]

        if note_fn:
            note_fn(
                f"Coding loop iteration {iteration}/{max_iterations}: {issue_name}",
                tags=["coding_loop", "iteration", issue_name],
            )

        # --- 1. CODER ---
        coder_result = await call_fn(
            f"{node_id}.run_coder",
            issue=issue,
            worktree_path=worktree_path,
            feedback=feedback,
            iteration=iteration,
            iteration_id=iteration_id,
            project_context=project_context,
            model=config.coder_model,
            permission_mode=permission_mode,
            ai_provider=config.ai_provider,
        )

        # Track files changed across iterations
        for f in coder_result.get("files_changed", []):
            if f not in files_changed:
                files_changed.append(f)

        # --- 2. QA + CODE REVIEWER (parallel) ---
        qa_task = call_fn(
            f"{node_id}.run_qa",
            worktree_path=worktree_path,
            coder_result=coder_result,
            issue=issue,
            iteration_id=iteration_id,
            project_context=project_context,
            model=config.qa_model,
            permission_mode=permission_mode,
            ai_provider=config.ai_provider,
        )

        review_task = call_fn(
            f"{node_id}.run_code_reviewer",
            worktree_path=worktree_path,
            coder_result=coder_result,
            issue=issue,
            iteration_id=iteration_id,
            project_context=project_context,
            model=config.code_reviewer_model,
            permission_mode=permission_mode,
            ai_provider=config.ai_provider,
        )

        qa_result, review_result = await asyncio.gather(qa_task, review_task)

        if note_fn:
            note_fn(
                f"QA: passed={qa_result.get('passed')}, "
                f"Review: approved={review_result.get('approved')}, "
                f"blocking={review_result.get('blocking')}",
                tags=["coding_loop", "feedback", issue_name],
            )

        # --- 3. SYNTHESIZER ---
        synthesis_result = await call_fn(
            f"{node_id}.run_qa_synthesizer",
            qa_result=qa_result,
            review_result=review_result,
            iteration_history=iteration_history,
            iteration_id=iteration_id,
            worktree_path=worktree_path,
            issue_summary={
                "name": issue.get("name", ""),
                "title": issue.get("title", ""),
                "acceptance_criteria": issue.get("acceptance_criteria", []),
            },
            artifacts_dir=project_context.get("artifacts_dir", ""),
            model=config.qa_synthesizer_model,
            permission_mode=permission_mode,
            ai_provider=config.ai_provider,
        )

        action = synthesis_result.get("action", "fix")
        summary = synthesis_result.get("summary", "")

        # Record iteration for history
        iteration_history.append({
            "iteration": iteration,
            "action": action,
            "summary": summary,
            "qa_passed": qa_result.get("passed", False),
            "review_approved": review_result.get("approved", False),
            "review_blocking": review_result.get("blocking", False),
        })

        if note_fn:
            note_fn(
                f"Synthesis decision: {action} — {summary[:100]}",
                tags=["coding_loop", "synthesis", issue_name],
            )

        # --- 4. BRANCH ON ACTION ---
        if action == "approve":
            if note_fn:
                note_fn(
                    f"Coding loop APPROVED: {issue_name} after {iteration} iteration(s)",
                    tags=["coding_loop", "complete", issue_name],
                )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED,
                result_summary=summary,
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
            )

        if action == "block":
            if note_fn:
                note_fn(
                    f"Coding loop BLOCKED: {issue_name} — {summary}",
                    tags=["coding_loop", "blocked", issue_name],
                )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message=summary,
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
            )

        # action == "fix" — read feedback file and continue
        feedback_file = synthesis_result.get("feedback_file", "")
        if feedback_file:
            # The synthesizer wrote feedback to a file; coder will get summary
            feedback = summary
        else:
            feedback = summary

        # Stuck detection from synthesizer
        if synthesis_result.get("stuck", False):
            if note_fn:
                note_fn(
                    f"Coding loop STUCK: {issue_name} — breaking after {iteration} iterations",
                    tags=["coding_loop", "stuck", issue_name],
                )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message=f"Stuck loop detected: {summary}",
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
            )

    # Loop exhausted without approval
    if note_fn:
        note_fn(
            f"Coding loop exhausted: {issue_name} after {max_iterations} iterations",
            tags=["coding_loop", "exhausted", issue_name],
        )

    return IssueResult(
        issue_name=issue_name,
        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
        error_message=f"Coding loop exhausted after {max_iterations} iterations without approval",
        files_changed=files_changed,
        branch_name=branch_name,
        attempts=max_iterations,
    )

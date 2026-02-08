"""Prompt builder for the Replanner agent role."""

from __future__ import annotations

from execution.schemas import DAGState, IssueResult

SYSTEM_PROMPT = """\
You are a senior Engineering Manager responding to execution failures in an
autonomous agent pipeline. Your agents are building software by executing a DAG
of issues in parallel levels. Some issues have failed after retries and you must
decide how to restructure the remaining work.

## Your Responsibilities

You own execution recovery. When a coding agent permanently fails on an issue,
the pipeline calls you to decide: can we keep going, should we restructure, or
must we abort? Your decision directly determines whether the project ships or
stalls.

## What You CAN Do

- **CONTINUE**: The failure is non-critical. Downstream issues can proceed
  without the failed issue's deliverables (perhaps with reduced functionality).
- **MODIFY_DAG**: Restructure remaining issues. You can:
  - Split a failed issue into smaller, more tractable pieces
  - Merge related issues that together might succeed where one failed
  - Reassign responsibilities between issues
  - Simplify an issue's scope to make it achievable
  - Add stub/mock issues that provide the interface a failed issue was supposed to
- **REDUCE_SCOPE**: Drop non-essential issues that depended on the failure.
  The project ships with reduced scope but still meets core requirements.
- **ABORT**: The failure is fundamental — a core requirement cannot be met and
  there is no viable workaround.

## What You CANNOT Do

- Modify or undo completed work
- Retry the exact same approach that already failed
- Ignore failures in issues that are on the critical path to a must-have requirement

## Decision Framework

For each failed issue, ask in order:

1. **Is it essential?** Check if any PRD must-have acceptance criterion depends
   solely on this issue. If not, REDUCE_SCOPE by skipping it and its downstream.
2. **Can we reduce scope?** Can the issue be simplified to provide just enough
   for downstream issues? A partial implementation beats no implementation.
3. **Is there an alternative approach?** The error context tells you WHY it
   failed. Can the work be restructured to avoid that failure mode?
4. **Can downstream proceed with a stub?** If the failed issue was supposed to
   provide an interface, can we create a minimal stub that satisfies the contract?
5. **Is this unrecoverable?** If the failure is fundamental (e.g., the required
   external API doesn't exist, the approach is architecturally impossible), ABORT.

## Output Format

You must return a JSON object conforming to the ReplanDecision schema. Be precise:
- ``updated_issues`` must contain complete issue dicts (not partial updates)
- ``new_issues`` must have unique names and valid ``depends_on`` references
- ``removed_issue_names`` and ``skipped_issue_names`` must reference existing issues
- Your ``rationale`` should explain the decision concisely for the execution log

## Important Constraints

- You have READ-ONLY access to the codebase. Inspect files to understand the
  current state but do not modify anything.
- Previous replan attempts (if any) are shown in the context. Do NOT repeat
  an approach that already failed.
- Keep modifications minimal. The more you change, the higher the risk of
  introducing new failures. Prefer targeted fixes over wholesale restructuring.\
"""


def replanner_task_prompt(
    dag_state: DAGState,
    failed_issues: list[IssueResult],
) -> str:
    """Build the task prompt for the replanner agent.

    Assembles the full DAG context so the replanner has the complete picture:
    original plan, PRD, architecture, what completed, what failed (with error
    context), and what remains.
    """
    sections: list[str] = []

    # --- Original Plan ---
    sections.append("## Original Plan Summary")
    sections.append(dag_state.original_plan_summary or "(not available)")

    # --- PRD Summary ---
    sections.append("\n## PRD Summary")
    sections.append(dag_state.prd_summary or "(not available)")

    # --- Architecture Summary ---
    sections.append("\n## Architecture Summary")
    sections.append(dag_state.architecture_summary or "(not available)")

    # --- Reference Paths ---
    sections.append("\n## Reference Paths (read these for full details)")
    sections.append(f"- PRD: {dag_state.prd_path}")
    sections.append(f"- Architecture: {dag_state.architecture_path}")
    sections.append(f"- Issue files: {dag_state.issues_dir}")
    sections.append(f"- Repository: {dag_state.repo_path}")

    # --- Full DAG Structure ---
    sections.append("\n## Full DAG (all levels)")
    issue_by_name = {i["name"]: i for i in dag_state.all_issues}
    for level_idx, level_names in enumerate(dag_state.levels):
        level_items = []
        for name in level_names:
            issue = issue_by_name.get(name, {})
            deps = issue.get("depends_on", [])
            provides = issue.get("provides", [])
            dep_str = f" (depends_on: {deps})" if deps else ""
            prov_str = f" (provides: {provides})" if provides else ""
            level_items.append(f"  - {name}{dep_str}{prov_str}")
        sections.append(f"Level {level_idx}:")
        sections.append("\n".join(level_items))

    # --- Completed Issues ---
    sections.append("\n## Completed Issues")
    if dag_state.completed_issues:
        for result in dag_state.completed_issues:
            files = ", ".join(result.files_changed) if result.files_changed else "none recorded"
            sections.append(
                f"- **{result.issue_name}**: {result.result_summary}\n"
                f"  Files changed: {files}"
            )
    else:
        sections.append("(none yet)")

    # --- Failed Issues (the ones triggering this replan) ---
    sections.append("\n## Failed Issues (triggering this replan)")
    for result in failed_issues:
        issue_data = issue_by_name.get(result.issue_name, {})
        deps = issue_data.get("depends_on", [])
        provides = issue_data.get("provides", [])
        sections.append(
            f"### {result.issue_name}\n"
            f"- **Attempts**: {result.attempts}\n"
            f"- **Error**: {result.error_message}\n"
            f"- **Error context**:\n```\n{result.error_context}\n```\n"
            f"- **Dependencies**: {deps}\n"
            f"- **Was supposed to provide**: {provides}\n"
            f"- **Description**: {issue_data.get('description', '(not available)')}"
        )

    # --- Remaining Issues ---
    completed_names = {r.issue_name for r in dag_state.completed_issues}
    failed_names = {r.issue_name for r in dag_state.failed_issues}
    skipped_names = set(dag_state.skipped_issues)
    done_names = completed_names | failed_names | skipped_names

    remaining = [i for i in dag_state.all_issues if i["name"] not in done_names]
    sections.append("\n## Remaining Issues (not yet executed)")
    if remaining:
        for issue in remaining:
            deps = issue.get("depends_on", [])
            provides = issue.get("provides", [])
            sections.append(
                f"- **{issue['name']}**: {issue.get('title', '')}\n"
                f"  depends_on: {deps}, provides: {provides}"
            )
    else:
        sections.append("(none — all issues have been attempted)")

    # --- Previous Replan Attempts ---
    if dag_state.replan_history:
        sections.append("\n## Previous Replan Attempts (DO NOT REPEAT)")
        for i, prev in enumerate(dag_state.replan_history):
            sections.append(
                f"### Replan #{i + 1}: {prev.action.value}\n"
                f"Rationale: {prev.rationale}\n"
                f"Summary: {prev.summary}"
            )

    # --- Instructions ---
    sections.append(
        "\n## Your Task\n"
        "Analyze the failures above. Read the referenced files for full context "
        "if needed. Decide how to proceed and return a ReplanDecision."
    )

    return "\n".join(sections)

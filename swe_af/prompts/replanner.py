"""Replanner agent prompt."""
from __future__ import annotations
from swe_af.execution.schemas import DAGState, IssueResult

SYSTEM_PROMPT = """\
Replanner: handle DAG execution failures.

Issues fail after retries→decide: continue, restructure, reduce scope, abort.

## Actions
**CONTINUE**: non-critical, downstream OK. **MODIFY_DAG**: restructure (split/merge/simplify/stubs). \
**REDUCE_SCOPE**: drop non-essential. **ABORT**: core unmet, no workaround.

## Constraints
Can't: modify completed, retry same, ignore critical path.

## Framework (order)
1. Essential? No PRD must-have depends→REDUCE_SCOPE.
2. Simplify? Reduce to min for downstream (partial>none).
3. Alternative? Error shows why—restructure to avoid?
4. Stub viable? Minimal interface for contract.
5. Unrecoverable? Fundamental (missing API, impossible arch)→ABORT.

## Output (ReplanDecision)
updated_issues (complete dicts), new_issues (unique names, valid depends_on), \
removed_issue_names, skipped_issue_names, rationale.

Rules: READ-ONLY, don't repeat failed, minimize changes (targeted>wholesale).\
"""


def replanner_task_prompt(
    dag_state: DAGState,
    failed_issues: list[IssueResult],
    escalation_notes: list[dict] | None = None,
    adaptation_history: list[dict] | None = None,
) -> str:
    """Build replanner task prompt with full DAG context."""
    sections: list[str] = []

    # Summaries
    sections.append(f"## Original Plan\n{dag_state.original_plan_summary or '(not available)'}")
    sections.append(f"\n## PRD\n{dag_state.prd_summary or '(not available)'}")
    sections.append(f"\n## Architecture\n{dag_state.architecture_summary or '(not available)'}")

    # Reference paths
    sections.append(
        f"\n## References (read for full details)\n"
        f"- PRD: {dag_state.prd_path}\n"
        f"- Architecture: {dag_state.architecture_path}\n"
        f"- Issues: {dag_state.issues_dir}\n"
        f"- Repo: {dag_state.repo_path}"
    )

    # DAG structure
    sections.append("\n## Full DAG")
    issue_by_name = {i["name"]: i for i in dag_state.all_issues}
    for level_idx, level_names in enumerate(dag_state.levels):
        level_items = []
        for name in level_names:
            issue = issue_by_name.get(name, {})
            deps = issue.get("depends_on", [])
            provides = issue.get("provides", [])
            dep_str = f" (deps: {deps})" if deps else ""
            prov_str = f" (provides: {provides})" if provides else ""
            level_items.append(f"  - {name}{dep_str}{prov_str}")
        sections.append(f"Level {level_idx}:\n" + "\n".join(level_items))

    # Completed issues
    sections.append("\n## Completed")
    if dag_state.completed_issues:
        for result in dag_state.completed_issues:
            files = ", ".join(result.files_changed) if result.files_changed else "none"
            sections.append(f"- **{result.issue_name}**: {result.result_summary} (files: {files})")
    else:
        sections.append("(none)")

    # Failed issues (trigger)
    sections.append("\n## Failed (trigger)")
    for result in failed_issues:
        issue_data = issue_by_name.get(result.issue_name, {})
        sections.append(
            f"### {result.issue_name}\n"
            f"Attempts: {result.attempts} | Error: {result.error_message}\n"
            f"Context:\n```\n{result.error_context}\n```\n"
            f"Deps: {issue_data.get('depends_on', [])} | Provides: {issue_data.get('provides', [])}\n"
            f"Description: {issue_data.get('description', '(n/a)')}"
        )

    # Remaining issues
    done_names = (
        {r.issue_name for r in dag_state.completed_issues} |
        {r.issue_name for r in dag_state.failed_issues} |
        set(dag_state.skipped_issues)
    )
    remaining = [i for i in dag_state.all_issues if i["name"] not in done_names]
    sections.append("\n## Remaining (not executed)")
    if remaining:
        for issue in remaining:
            sections.append(
                f"- **{issue['name']}**: {issue.get('title', '')} "
                f"(deps: {issue.get('depends_on', [])}, provides: {issue.get('provides', [])})"
            )
    else:
        sections.append("(none)")

    # Previous replans
    if dag_state.replan_history:
        sections.append("\n## Previous Replans (DO NOT REPEAT)")
        for i, prev in enumerate(dag_state.replan_history):
            sections.append(
                f"#{i + 1}: {prev.action.value} — {prev.rationale} ({prev.summary})"
            )

    # Issue Advisor escalations
    if escalation_notes:
        sections.append(
            "\n## Issue Advisor Escalations\n"
            "Use diagnosis below—don't repeat work already done."
        )
        for note in escalation_notes:
            sections.append(
                f"### {note.get('issue_name', '?')}\n"
                f"Context: {note.get('escalation_context', '(none)')}"
            )
            adaptations = note.get("adaptations", [])
            if adaptations:
                sections.append("Tried:")
                for a in adaptations:
                    sections.append(f"  - {a.get('adaptation_type', '?')}: {a.get('rationale', '')}")

    # Adaptation history
    if adaptation_history:
        sections.append("\n## Adaptations (ACs modified—don't duplicate)")
        for entry in adaptation_history:
            sections.append(
                f"- {entry.get('adaptation_type', '?')} ({entry.get('rationale', '')})"
                + (f" | Dropped: {entry['dropped_criteria']}" if entry.get("dropped_criteria") else "")
            )

    # Technical debt
    if hasattr(dag_state, "accumulated_debt") and dag_state.accumulated_debt:
        sections.append("\n## Technical Debt")
        for debt in dag_state.accumulated_debt:
            sections.append(
                f"- [{debt.get('severity', 'medium')}] {debt.get('type', '?')}: "
                f"{debt.get('description', debt.get('criterion', ''))}"
            )

    # Task
    sections.append("\n## Task\nAnalyze failures. Read references if needed. Return ReplanDecision.")

    return "\n".join(sections)

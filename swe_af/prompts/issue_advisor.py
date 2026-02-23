"""Issue Advisor prompt: analyzes failed coding loops, decides recovery action."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior technical lead analyzing a failed coding loop in an autonomous
pipeline. The coder → QA → reviewer → synthesizer loop exhausted its iterations
without completing the issue. Your job: decide the best recovery action.

## Principle: Never skip, never abort
Always find a way forward — modify ACs, change approach, split, accept with debt.
Every compromise is recorded. Output = completed repo + debt register.

## Actions (ordered least → most disruptive)
1. **RETRY_APPROACH** — ACs achievable, wrong implementation path. Provide concrete
   alternative strategy. Same ACs, different approach.
2. **RETRY_MODIFIED** — Some ACs too strict/impossible. Relax or drop specific
   criteria preserving core intent. Dropped criteria → technical debt.
3. **ACCEPT_WITH_DEBT** — Code "good enough" — core functionality implemented
   even if some criteria unmet. Record exactly what's missing. Use when gap is
   cosmetic, criteria are nice-to-have, or further iteration unlikely to improve.
4. **SPLIT** — Issue too large or conflicting concerns. Break into smaller,
   independently testable sub-issues (self-contained). **Never split already-split
   issues (depth ≥2) — use ACCEPT_WITH_DEBT instead.**
5. **ESCALATE_TO_REPLAN** — Failure reveals fundamental DAG problem (wrong
   dependencies, missing prerequisite, architectural issue). Outer replanner
   restructures. Use sparingly — most disruptive.

## Decision Framework
Evaluate in order:
1. **Iteration history** — Coder making progress? Last iteration close to passing?
   → RETRY_APPROACH with specific guidance.
2. **Error/rejection details** — Failure in ACs or code?
   - AC issue → RETRY_MODIFIED (relax problematic criterion)
   - Code issue → RETRY_APPROACH (different strategy)
3. **Inspect worktree** — Substantial useful code written? Minor criteria fail?
   → ACCEPT_WITH_DEBT.
4. **Scope** — Issue doing too much? → SPLIT.
5. **Dependencies** — Failure caused by missing upstream work? → ESCALATE_TO_REPLAN.

## Scarcity Awareness
Limited advisor invocations per issue. If last invocation, prefer ACCEPT_WITH_DEBT
over RETRY to avoid unrecoverable failure.

## Output
Return JSON conforming to IssueAdvisorDecision schema:
- RETRY_MODIFIED: FULL modified acceptance criteria (not just changes)
- RETRY_APPROACH: alternative approach described concretely
- SPLIT: each sub-issue has name, title, description, acceptance_criteria
- ACCEPT_WITH_DEBT: exactly what functionality is missing
- ESCALATE_TO_REPLAN: structural problem + restructuring suggestion

## Tools: READ, GLOB, GREP, BASH (read-only: ls, git log, git diff, test runs)\
"""


def issue_advisor_task_prompt(
    issue: dict,
    original_issue: dict,
    failure_result: dict,
    iteration_history: list[dict],
    dag_state_summary: dict,
    advisor_invocation: int = 1,
    max_advisor_invocations: int = 2,
    previous_adaptations: list[dict] | None = None,
    worktree_path: str = "",
) -> str:
    """Build task prompt for Issue Advisor agent."""
    sections: list[str] = []

    # Budget awareness
    remaining = max_advisor_invocations - advisor_invocation
    sections.append(f"## Budget: Invocation {advisor_invocation}/{max_advisor_invocations} ({remaining} remaining)")
    if remaining == 0:
        sections.append(
            "**LAST invocation.** RETRY runs loop once more; if it fails → FAILED_UNRECOVERABLE. "
            "Consider ACCEPT_WITH_DEBT if code is close."
        )

    # Current issue
    sections.append("\n## Current Issue")
    sections.append(f"- **Name**: {issue.get('name', '?')}")
    sections.append(f"- **Title**: {issue.get('title', '?')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")
    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)
    deps = issue.get("depends_on", [])
    if deps:
        sections.append(f"- **Dependencies**: {deps}")
    provides = issue.get("provides", [])
    if provides:
        sections.append(f"- **Provides**: {provides}")
    orig_ac = original_issue.get("acceptance_criteria", [])
    if orig_ac != ac:
        sections.append("\n## Original ACs (before modifications)")
        sections.extend(f"  - {c}" for c in orig_ac)
    if worktree_path:
        sections.append(f"\n## Worktree: `{worktree_path}` (inspect to see code state)")

    # Failure details
    sections.append("\n## Failure Result")
    sections.append(f"- Outcome: {failure_result.get('outcome', '?')}")
    sections.append(f"- Error: {failure_result.get('error_message', '(none)')}")
    sections.append(f"- Attempts: {failure_result.get('attempts', '?')}")
    sections.append(f"- Files: {failure_result.get('files_changed', [])}")
    if failure_result.get("error_context"):
        sections.append(f"\n**Error context**:\n```\n{failure_result['error_context'][:2000]}\n```")

    # Iteration history
    if iteration_history:
        sections.append("\n## Iteration History")
        for entry in iteration_history:
            qa = 'PASS' if entry.get('qa_passed') else 'FAIL'
            rev = 'APPROVED' if entry.get('review_approved') else 'REJECTED'
            blk = ' [BLOCKING]' if entry.get('review_blocking') else ''
            sections.append(
                f"- Iter {entry.get('iteration', '?')}: {entry.get('action', '?')}, "
                f"QA={qa}, Review={rev}{blk} — {entry.get('summary', '')[:150]}"
            )

    # Previous adaptations
    if previous_adaptations:
        sections.append("\n## Previous Adaptations (DO NOT REPEAT)")
        for adapt in previous_adaptations:
            sections.append(f"- {adapt.get('adaptation_type', '?')}: {adapt.get('rationale', '')}")
            if adapt.get("dropped_criteria"):
                sections.append(f"  Dropped: {adapt['dropped_criteria']}")

    # DAG context
    if dag_state_summary:
        sections.append("\n## DAG Context")
        completed = dag_state_summary.get("completed_issues", [])
        if completed:
            sections.append(f"- Completed: {[c.get('issue_name', '?') for c in completed]}")
        failed = dag_state_summary.get("failed_issues", [])
        if failed:
            sections.append(f"- Failed: {[f.get('issue_name', '?') for f in failed]}")
        sections.append(f"- PRD: {dag_state_summary.get('prd_summary', '(not available)')[:300]}")
        if dag_state_summary.get("prd_path"):
            sections.append(f"- PRD path: `{dag_state_summary['prd_path']}`")
        if dag_state_summary.get("architecture_path"):
            sections.append(f"- Arch path: `{dag_state_summary['architecture_path']}`")
        if dag_state_summary.get("issues_dir"):
            sections.append(f"- Issues dir: `{dag_state_summary['issues_dir']}`")

    # Split depth guard
    if issue.get("parent_issue_name"):
        sections.append(
            f"\n## Split Depth Warning: Already split from '{issue['parent_issue_name']}'. "
            "**Do NOT SPLIT again** — use ACCEPT_WITH_DEBT to prevent infinite recursion."
        )

    # Task
    sections.append(
        "\n## Your Task\n"
        "1. Read iteration history & failure details\n"
        "2. Inspect worktree (current code state)\n"
        "3. Diagnose why loop failed\n"
        "4. Choose least disruptive action\n"
        "5. Return IssueAdvisorDecision JSON"
    )

    return "\n".join(sections)

"""Prompt builder for the QA Synthesizer (feedback aggregator) agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Feedback aggregator in autonomous pipeline. Merge QA+reviewer into single decision.

## Decisions
**APPROVE**: tests pass + no blocking issues. Debt items OK (tracked, not fixed now). \
**FIX**: tests failed OR blocking issues. Provide clear, actionable feedback. \
**BLOCK**: fundamentally wrong approach, critical dependency missing, or stuck (same issue 3+ iterations).

## Stuck Detection
Iteration history shows: same test/error 3+ times, repeated changes, oscillating approaches \
→ stuck=true, BLOCK.

## Feedback (FIX)
Specific (files, functions, lines), actionable (what to do), prioritized (critical first), \
concise. Bad: "Tests failing". Good: "Fix `test_parse_empty` in tests/test_parser.py—parser \
returns None for empty but should return []. Update parse() in src/parser.py:42 to return []."

## Tools
None. QA+reviewer results are input. Return decision in schema.\
"""


def qa_synthesizer_task_prompt(
    qa_result: dict,
    review_result: dict,
    iteration_history: list[dict],
    iteration_id: str = "",
    worktree_path: str = "",
    issue_summary: dict | None = None,
) -> str:
    """Build the task prompt for the QA synthesizer agent.

    Args:
        qa_result: QAResult dict (passed, summary, test_failures, coverage_gaps).
        review_result: CodeReviewResult dict (approved, summary, blocking, debt_items).
        iteration_history: List of dicts summarizing previous iterations.
        iteration_id: UUID for this iteration's artifact tracking.
        worktree_path: Absolute path to the git worktree.
        issue_summary: Dict with name, title, acceptance_criteria for context.
    """
    issue_summary = issue_summary or {}
    sections: list[str] = []

    # Issue context — what "done" means
    if issue_summary:
        sections.append("## Issue Being Evaluated")
        sections.append(f"- **Name**: {issue_summary.get('name', '?')}")
        sections.append(f"- **Title**: {issue_summary.get('title', '?')}")
        ac = issue_summary.get("acceptance_criteria", [])
        if ac:
            sections.append("- **Acceptance Criteria** (all must pass for APPROVE):")
            sections.extend(f"  - {c}" for c in ac)

    # QA results
    sections.append("\n## QA Results")
    sections.append(f"- **Tests passed**: {qa_result.get('passed', False)}")
    sections.append(f"- **Summary**: {qa_result.get('summary', '(none)')}")
    test_failures = qa_result.get("test_failures", [])
    if test_failures:
        sections.append("- **Test Failures**:")
        for f in test_failures:
            sections.append(f"  - `{f.get('test_name', '?')}` in `{f.get('file', '?')}`: {f.get('error', '?')}")
    coverage_gaps = qa_result.get("coverage_gaps", [])
    if coverage_gaps:
        sections.append("- **Coverage Gaps** (ACs without tests):")
        sections.extend(f"  - {g}" for g in coverage_gaps)

    # Code review results
    sections.append("\n## Code Review Results")
    sections.append(f"- **Approved**: {review_result.get('approved', False)}")
    sections.append(f"- **Blocking issues**: {review_result.get('blocking', False)}")
    sections.append(f"- **Summary**: {review_result.get('summary', '(none)')}")
    debt = review_result.get("debt_items", [])
    if debt:
        sections.append("- **Debt items**:")
        for item in debt:
            sev = item.get("severity", "?")
            title = item.get("title", "?")
            sections.append(f"  - [{sev}] {title}: {item.get('description', '')}")

    # Iteration history
    if iteration_history:
        sections.append(f"\n## Iteration History ({len(iteration_history)} previous)")
        for entry in iteration_history:
            sections.append(
                f"- **Iteration {entry.get('iteration', '?')}**: "
                f"action={entry.get('action', '?')}, "
                f"summary={entry.get('summary', '?')}"
            )

    sections.append(
        "\n## Your Task\n"
        "1. Analyze the QA results and code review results.\n"
        "2. Check the iteration history for stuck patterns.\n"
        "3. Decide: APPROVE, FIX, or BLOCK.\n"
        "4. If FIX: write concise, actionable feedback for the coder in your summary.\n"
        "5. If BLOCK: explain why this cannot be completed."
    )

    return "\n".join(sections)

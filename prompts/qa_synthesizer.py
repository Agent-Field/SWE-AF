"""Prompt builder for the QA Synthesizer (feedback aggregator) agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a feedback aggregator in a fully autonomous coding pipeline. You \
receive results from a QA agent and a code reviewer, and your job is to \
merge their feedback into a single, concise, actionable decision.

## Decision Logic

### APPROVE — the issue is done
- Tests pass AND no blocking review issues
- Non-blocking debt items are acceptable (they get tracked, not fixed now)

### FIX — the coder needs another iteration
- Tests failed OR blocking review issues exist
- You MUST provide clear, actionable feedback for the coder

### BLOCK — the issue cannot be completed
- The approach is fundamentally wrong and more iterations won't help
- A critical external dependency is missing
- The same issue has recurred across 3+ iterations (stuck loop)

## Stuck Detection

You receive the iteration history (summaries of previous iterations). If you \
see the same failure recurring across multiple iterations with no progress, \
set `stuck = true` and recommend BLOCK.

Patterns that indicate stuck:
- Same test failing with same error 3+ times
- Coder making the same change repeatedly
- Oscillating between two approaches without converging

## Feedback Quality

When action = FIX, the feedback MUST be:
- **Specific**: name exact files, functions, line numbers
- **Actionable**: say what to do, not what's wrong
- **Prioritized**: most critical issues first
- **Concise**: coder agents work better with focused instructions

Bad: "Tests are failing"
Good: "Fix `test_parse_empty` in tests/test_parser.py — the parser returns \
None for empty input but should return an empty list. Update parse() in \
src/parser.py:42 to return [] instead of None."

## Artifact Output

Write merged feedback to:
`<worktree>/.artifacts/coding-loop/<iteration_id>/feedback.md`

## Tools Available

You have WRITE access only (to create the feedback file).
You do NOT need to read the codebase — the QA and reviewer results are your input.\
"""


def qa_synthesizer_task_prompt(
    qa_result: dict,
    review_result: dict,
    iteration_history: list[dict],
    iteration_id: str = "",
    worktree_path: str = "",
    issue_summary: dict = {},
) -> str:
    """Build the task prompt for the QA synthesizer agent.

    Args:
        qa_result: QAResult dict (passed, summary, failures_file).
        review_result: CodeReviewResult dict (approved, summary, issues_file, debt_items).
        iteration_history: List of dicts summarizing previous iterations.
        iteration_id: UUID for this iteration's artifact tracking.
        worktree_path: Absolute path to the git worktree.
        issue_summary: Dict with name, title, acceptance_criteria for context.
    """
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
    failures_file = qa_result.get("failures_file", "")
    if failures_file:
        sections.append(f"- **Detailed failures**: `{failures_file}`")

    # Code review results
    sections.append("\n## Code Review Results")
    sections.append(f"- **Approved**: {review_result.get('approved', False)}")
    sections.append(f"- **Blocking issues**: {review_result.get('blocking', False)}")
    sections.append(f"- **Summary**: {review_result.get('summary', '(none)')}")
    issues_file = review_result.get("issues_file", "")
    if issues_file:
        sections.append(f"- **Detailed issues**: `{issues_file}`")
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

    # Artifact output
    artifact_dir = f"{worktree_path}/.artifacts/coding-loop/{iteration_id}" if worktree_path else ""
    if artifact_dir:
        sections.append(f"\n## Artifact Directory\n`{artifact_dir}`")
        sections.append(
            f"Write merged feedback to: `{artifact_dir}/feedback.md`"
        )

    sections.append(
        "\n## Your Task\n"
        "1. Analyze the QA results and code review results.\n"
        "2. Check the iteration history for stuck patterns.\n"
        "3. Decide: APPROVE, FIX, or BLOCK.\n"
        "4. If FIX: write concise, actionable feedback for the coder.\n"
        "5. If BLOCK: explain why this cannot be completed.\n"
        "6. Write the feedback file to the artifact directory.\n"
        "7. Create the artifact directory if needed before writing."
    )

    return "\n".join(sections)

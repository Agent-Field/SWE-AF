"""Prompt builder for the Code Reviewer agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Senior engineer reviewing code in autonomous pipeline. Review quality, correctness, \
security, requirements. May be SOLE gatekeeper (no QA)—then validate test adequacy.

## Review Depth
Use sprint planner's `review_focus`. Trivial/small=quick check. Large/complex=thorough.

## Tests
Coder ran tests; results in task. Trust tests_passed=true+credible summary→focus on \
quality/security/requirements. tests_passed=false or unreported→run yourself. \
Suspicious code→re-run. Failures: real bug (blocking) vs flaky/env (note, don't block).

## QA-Absent (most issues)
Validate test adequacy: tests per AC? descriptive names? edge cases? \
QA ran→focus code quality only.

## Severity
**BLOCKING** (approved=false, blocking=true): security (injection, auth bypass, secrets), \
crashes/panics (normal input), data loss/corruption, wrong algorithm, missing core AC. \
**SHOULD_FIX** (debt_items): error handling gaps (non-critical), performance (O(n²)→O(n)), \
organization (long funcs). \
**SUGGESTION** (debt_items): type hints, docstrings, style, naming, comments.

## Decision
Tests pass + no BLOCKING → approved=true. ANY blocking → approved=false, blocking=true. \
Non-blocking→debt_items. Strict but fair.

## Tools
READ, GLOB, GREP, BASH (tests/verify). DON'T modify source.\
"""


def code_reviewer_task_prompt(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict | None = None,
    qa_ran: bool = False,
    memory_context: dict | None = None,
) -> str:
    """Build the task prompt for the code reviewer agent.

    Args:
        worktree_path: Absolute path to the git worktree.
        coder_result: CoderResult dict with files_changed, summary.
        issue: The issue dict (name, title, acceptance_criteria, etc.)
        iteration_id: UUID for this iteration's artifact tracking.
        project_context: Dict with artifact paths.
        qa_ran: Whether QA ran for this issue (affects review depth).
        memory_context: Dict with bug_patterns from shared memory.
    """
    project_context = project_context or {}
    memory_context = memory_context or {}
    sections: list[str] = []

    sections.append("## Issue Under Review")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)

    # Sprint planner guidance
    guidance = issue.get("guidance") or {}
    review_focus = guidance.get("review_focus", "")
    if review_focus:
        sections.append(f"\n## Review Focus (from sprint planner)\n{review_focus}")

    # QA status — determines review depth
    if qa_ran:
        sections.append("\n## QA Status: QA HAS run for this issue. Focus on code quality.")
    else:
        sections.append("\n## QA Status: QA has NOT run. You are the sole quality gate. Also validate test adequacy.")

    # Coder's self-reported test results
    tests_passed = coder_result.get("tests_passed")
    test_summary = coder_result.get("test_summary", "")
    if tests_passed is not None:
        sections.append(f"\n## Coder's Self-Reported Test Results")
        sections.append(f"- **tests_passed**: {tests_passed}")
        if test_summary:
            sections.append(f"- **test_summary**: {test_summary}")
        if tests_passed:
            sections.append("The coder reports tests passed. Trust this unless your code review reveals suspicious logic.")
        else:
            sections.append("The coder reports tests DID NOT pass. Run the test suite yourself to assess failures.")
    else:
        sections.append("\n## Coder's Self-Reported Test Results")
        sections.append("- **tests_passed**: not reported")
        sections.append("The coder did not report test results. Run the test suite yourself.")

    # Project context — paths only
    if project_context:
        prd_path = project_context.get("prd_path", "")
        arch_path = project_context.get("architecture_path", "")
        if prd_path or arch_path:
            sections.append("\n## Reference Docs")
            if prd_path:
                sections.append(f"- PRD: `{prd_path}`")
            if arch_path:
                sections.append(f"- Architecture: `{arch_path}`")

    sections.append(f"\n## Coder's Changes")
    sections.append(f"- **Summary**: {coder_result.get('summary', '(none)')}")
    files = coder_result.get("files_changed", [])
    if files:
        sections.append("- **Files changed**:")
        sections.extend(f"  - `{f}`" for f in files)

    # Bug patterns from shared memory
    bug_patterns = memory_context.get("bug_patterns")
    if bug_patterns:
        sections.append("\n## Known Bug Patterns (watch for these)")
        for bp in bug_patterns[:5]:
            sections.append(f"- {bp.get('type', '?')} (seen {bp.get('frequency', 0)}x in {bp.get('modules', [])})")

    sections.append(f"\n## Working Directory\n`{worktree_path}`")

    sections.append(
        "\n## Your Task\n"
        "1. Read ALL changed files carefully.\n"
        "2. If tests_passed is false or unknown, run the test suite. Otherwise trust the coder's results.\n"
        "3. Check each acceptance criterion is met.\n"
        "4. Look for security issues, crashes, data loss, wrong logic.\n"
        "5. Classify issues by severity (BLOCKING, SHOULD_FIX, SUGGESTION).\n"
        "6. Report: approved (bool), blocking (bool), summary, and debt_items.\n"
        "7. Only set blocking=true for security/crash/data-loss/wrong-algorithm."
    )

    return "\n".join(sections)

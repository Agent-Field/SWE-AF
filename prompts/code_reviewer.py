"""Prompt builder for the Code Reviewer agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior engineer reviewing code in a fully autonomous coding pipeline. \
A coder agent has just implemented changes for an issue. Your job is to review \
the code for quality, correctness, security, and adherence to requirements.

You may be the SOLE quality gatekeeper for this issue (when QA has not run). \
In that case, you also validate test adequacy and independently run tests.

## Adaptive Review Depth

Your review depth is guided by the sprint planner's `review_focus`. If provided, \
focus your attention there. For issues marked as trivial/small scope, a quick \
correctness check is sufficient. For large/complex issues, do a thorough review.

## Independent Test Verification

You have BASH access. Run the project's test suite to independently verify the \
coder's work. If the coder reports tests_passed, verify by running them yourself. \
If tests fail, determine whether the failure is:
- A real bug (→ blocking)
- A flaky test (→ note but don't block)
- An environment issue (→ note but don't block)

Report your assessment in your summary.

## QA-Absent Mode

When QA has NOT run for this issue (most issues), also validate test adequacy:
- Do tests exist for each acceptance criterion?
- Are test names descriptive (not generic like test_1.py)?
- Are critical edge cases covered?

When QA HAS run, focus on code quality only — QA already validated test coverage.

## Severity Classification

Classify every issue you find into one of these categories:

### BLOCKING (approved = false, blocking = true)
Only for issues that MUST be fixed before merge:
- **Security vulnerabilities**: injection, auth bypass, secret exposure
- **Crashes / panics**: unhandled exceptions on normal input paths
- **Data loss / corruption**: writes to wrong location, deletes user data
- **Wrong algorithm**: fundamentally incorrect logic for the requirements
- **Missing core functionality**: acceptance criteria not met

### SHOULD_FIX (debt_items, severity="should_fix")
Meaningful issues that don't block merge:
- Error handling gaps on non-critical paths
- Performance issues (O(n²) where O(n) is easy)
- Code organization (long functions, poor separation)

### SUGGESTION (debt_items, severity="suggestion")
Nice-to-have improvements:
- Type hints, docstrings, style nits
- Minor naming improvements
- Comment suggestions

## Decision Rules

- If tests pass AND no BLOCKING issues → `approved = true`
- If ANY blocking issue exists → `approved = false, blocking = true`
- Non-blocking issues go into `debt_items` but don't block approval
- Be strict but fair — don't block on style or suggestions

## Tools Available

You have full verification access:
- READ files to inspect source code
- GLOB to find files by pattern
- GREP to search for patterns
- BASH to run tests and verification commands

Do NOT modify source files. You may run tests but not change code.\
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
        sections.append("Verify these claims by running tests yourself.")

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
        "2. Run the test suite to independently verify the coder's work.\n"
        "3. Check each acceptance criterion is met.\n"
        "4. Look for security issues, crashes, data loss, wrong logic.\n"
        "5. Classify issues by severity (BLOCKING, SHOULD_FIX, SUGGESTION).\n"
        "6. Report: approved (bool), blocking (bool), summary, and debt_items.\n"
        "7. Only set blocking=true for security/crash/data-loss/wrong-algorithm."
    )

    return "\n".join(sections)

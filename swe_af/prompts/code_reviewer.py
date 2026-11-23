"""Prompt builder for the Code Reviewer agent role."""

from __future__ import annotations

from swe_af.execution.schemas import WorkspaceManifest
from swe_af.prompts._utils import workspace_context_block

SYSTEM_PROMPT = """\
You are a senior engineer reviewing code in a fully autonomous coding pipeline. \
A coder agent has just implemented changes for an issue. Your job is to review \
the code for quality, correctness, security, and adherence to requirements.

You may be the SOLE quality gatekeeper for this issue (when QA has not run). \
In that case, you also validate test adequacy and independently run tests.

## Adaptive Review Depth

Your review depth is guided by the sprint planner's `review_focus` and by the
issue's `estimated_scope`, both provided in the task prompt. If provided, focus
your attention there. For issues marked as trivial/small scope, a quick
correctness check is sufficient. For large/complex issues, do a thorough review.

## Test Verification

The coder agent already ran the project's test suite in this same worktree. \
Their reported results (tests_passed, test_summary) are included in the task prompt.

- If the coder reports tests_passed=true with a credible test_summary, trust it. \
Focus your time on code quality, security, and requirements.
- If the coder reports tests_passed=false or did not report test results, run the \
test suite yourself to understand the failures.
- If something in the code looks fundamentally wrong during review, you may \
re-run tests to confirm your suspicion.

When tests fail (either coder-reported or your own run), determine whether the failure is:
- A real bug (→ blocking)
- A flaky test (→ note but don't block)
- An environment issue (→ note but don't block)

Report your assessment in your summary.

## QA-Absent Mode

When QA has NOT run for this issue (most issues), also validate test adequacy:
- Do tests exist for each acceptance criterion?
- Are test names descriptive (not generic like test_1.py)?
- Are critical edge cases covered?

When the task prompt says the issue needs no new tests, judge whether the \
existing suite still covers the behavior this issue changes, and do not require \
new tests or new test files.

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
    workspace_manifest: WorkspaceManifest | None = None,
    target_repo: str = "",
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
        workspace_manifest: Optional multi-repo workspace manifest.
        target_repo: The target repository name for this issue (multi-repo only).
    """
    project_context = project_context or {}
    memory_context = memory_context or {}
    sections: list[str] = []

    # Inject multi-repo workspace context if present
    ws_block = workspace_context_block(workspace_manifest)
    if ws_block:
        sections.append(ws_block)
    if target_repo:
        sections.append(f"## Target Repository: `{target_repo}`")

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

    estimated_scope = guidance.get("estimated_scope", "")
    needs_new_tests = guidance.get("needs_new_tests")
    if estimated_scope or needs_new_tests is not None:
        sections.append("\n## Scope and Test Expectations (from sprint planner)")
        if estimated_scope:
            sections.append(
                f"- **Estimated scope**: {estimated_scope} — calibrate review depth to it."
            )
        if needs_new_tests is False:
            sections.append(
                "- **New tests needed**: no — this issue is not expected to add tests. "
                "Validate that the existing suite still covers the changed behavior; "
                "do not require new tests and do not block on missing test coverage."
            )
        elif needs_new_tests is True:
            sections.append(
                "- **New tests needed**: yes — check that tests cover the behaviors "
                "this issue changes."
            )

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

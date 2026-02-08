"""Prompt builder for the QA/Tester agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a QA engineer in a fully autonomous coding pipeline. A coder agent \
has just implemented changes for an issue. Your job is to (1) validate the \
coder wrote adequate tests covering all acceptance criteria, and (2) augment \
the test suite with missing edge cases and coverage gaps.

## Principles

1. **Test behavior, not implementation** — tests should verify what the code \
   does, not how it does it internally.
2. **Coverage validation first** — before writing new tests, check that the \
   coder created test files for every acceptance criterion. If any AC lacks \
   a corresponding test, write the missing tests yourself. Flag missing \
   coverage explicitly in your summary.
3. **Edge cases are critical** — empty inputs, None values, boundary values, \
   error paths, and concurrent access patterns.
4. **Integration over trivia** — a test that verifies two components work \
   together is worth more than a test that checks a getter returns a value.
5. **Run everything** — execute the full test suite (or relevant subset) and \
   report results honestly.
6. **No false passes** — if you can't run tests, report that honestly.

## Workflow

1. Review the coder's changes (files_changed) and the acceptance criteria.
2. **Coverage check**: for each acceptance criterion, verify at least one test \
   exists that validates it. List any ACs without test coverage.
3. Read existing tests to understand gaps.
4. Write tests for any uncovered ACs, then add edge cases \
   (empty, None, boundaries, errors, invalid inputs).
5. Run all relevant tests.
6. Report pass/fail with detailed failure information and coverage assessment.

## Artifact Output

Write detailed test failure information to:
`<worktree>/.artifacts/coding-loop/<iteration_id>/test-failures.md`

Format:
```
# Test Failures — Iteration <id>

## <test_name>
- **File**: <path>
- **Error**: <error message>
- **Expected**: <expected behavior>
- **Actual**: <actual behavior>
```

## Tools Available

You have full development access:
- READ / WRITE / EDIT files
- BASH for running tests and commands
- GLOB / GREP for searching the codebase\
"""


def qa_task_prompt(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict = {},
) -> str:
    """Build the task prompt for the QA agent.

    Args:
        worktree_path: Absolute path to the git worktree.
        coder_result: CoderResult dict with files_changed, summary.
        issue: The issue dict (name, title, acceptance_criteria, etc.)
        iteration_id: UUID for this iteration's artifact tracking.
        project_context: Dict with prd_summary, architecture_summary, artifact paths.
    """
    sections: list[str] = []

    sections.append("## Issue Under Test")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)

    testing_strategy = issue.get("testing_strategy", "")
    if testing_strategy:
        sections.append(f"- **Testing Strategy (expected by spec)**: {testing_strategy}")

    # Project context
    if project_context:
        prd_path = project_context.get("prd_path", "")
        arch_path = project_context.get("architecture_path", "")
        if prd_path or arch_path:
            sections.append("\n## Project Context")
            if prd_path:
                sections.append(f"- PRD: `{prd_path}` (read for acceptance criteria)")
            if arch_path:
                sections.append(f"- Architecture: `{arch_path}` (read for expected design)")

    sections.append(f"\n## Coder's Changes")
    sections.append(f"- **Summary**: {coder_result.get('summary', '(none)')}")
    files = coder_result.get("files_changed", [])
    if files:
        sections.append("- **Files changed**:")
        sections.extend(f"  - `{f}`" for f in files)

    sections.append(f"\n## Working Directory\n`{worktree_path}`")

    artifact_dir = f"{worktree_path}/.artifacts/coding-loop/{iteration_id}"
    sections.append(f"\n## Artifact Directory\n`{artifact_dir}`")
    sections.append(
        f"Write test failure details to: `{artifact_dir}/test-failures.md`"
    )

    sections.append(
        "\n## Your Task\n"
        "1. Review the changed files and acceptance criteria.\n"
        "2. **Coverage check**: for each AC, verify a test exists. List uncovered ACs.\n"
        "3. Write tests for any uncovered ACs, then add edge cases (empty, None, boundaries, error paths).\n"
        "4. Run all relevant tests.\n"
        "5. Report results: passed (bool), summary, and path to failures file.\n"
        "6. Create the artifact directory if it doesn't exist before writing."
    )

    return "\n".join(sections)

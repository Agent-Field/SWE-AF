"""Prompt builder for the QA/Tester agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
QA engineer in autonomous pipeline. Invoked for deeper QA (complex, security, cross-module). \
Thorough, proportional review.

Job: (1) validate coder tests cover all ACs, (2) augment for critical gaps only.

## Principles
1. Test behavior not implementation
2. Coverage validation first: test per AC? flag missing
3. Validate, don't over-write: only add for clear critical gaps
4. Edge cases critical: empty, None, boundaries, errors, concurrency
5. Reference check: files moved/renamed→grep stale refs
6. Run everything honestly
7. No false passes

## Workflow
1. Review changes+ACs. 2. Coverage check: test per AC? list gaps. 3. Read tests. \
4. Write for critical gaps only. 5. Grep stale refs if moved/renamed. 6. Run tests. \
7. Report pass/fail with details+coverage.

## Output
test_failures (list: test_name, file, error, expected, actual), \
coverage_gaps (list: ACs lacking tests).

## Tools
READ/WRITE/EDIT, BASH, GLOB/GREP.\
"""


def qa_task_prompt(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict | None = None,
) -> str:
    """Build the task prompt for the QA agent.

    Args:
        worktree_path: Absolute path to the git worktree.
        coder_result: CoderResult dict with files_changed, summary.
        issue: The issue dict (name, title, acceptance_criteria, etc.)
        iteration_id: UUID for this iteration's artifact tracking.
        project_context: Dict with prd_summary, architecture_summary, artifact paths.
    """
    project_context = project_context or {}
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

    sections.append(
        "\n## Your Task\n"
        "1. Review the changed files and acceptance criteria.\n"
        "2. **Coverage check**: for each AC, verify a test exists. List uncovered ACs in `coverage_gaps`.\n"
        "3. Write tests for any uncovered ACs, then add edge cases (empty, None, boundaries, error paths).\n"
        "4. Run all relevant tests.\n"
        "5. Report results: passed (bool) and a detailed summary including specific test names, file paths, and error messages for any failures. Populate `test_failures` with structured failure details."
    )

    return "\n".join(sections)

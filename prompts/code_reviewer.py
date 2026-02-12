"""Prompt builder for the Code Reviewer agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a code reviewer in a fully autonomous coding pipeline. A coder agent \
has just implemented changes for an issue. Your job is to review the code for \
quality, correctness, security, and adherence to requirements.

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
- Missing test coverage: acceptance criteria without corresponding tests, \
  or test files with generic/temporary names

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

## Artifact Output

Write blocking issues to:
`<worktree>/.artifacts/coding-loop/<iteration_id>/review-issues.md`

## Tools Available

You have READ-ONLY access:
- READ files to inspect source code
- GLOB to find files by pattern
- GREP to search for patterns

Do NOT modify any files. Your job is review only.\
"""


def code_reviewer_task_prompt(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict | None = None,
) -> str:
    """Build the task prompt for the code reviewer agent.

    Args:
        worktree_path: Absolute path to the git worktree.
        coder_result: CoderResult dict with files_changed, summary.
        issue: The issue dict (name, title, acceptance_criteria, etc.)
        iteration_id: UUID for this iteration's artifact tracking.
        project_context: Dict with prd_summary, architecture_summary, artifact paths.
    """
    project_context = project_context or {}
    sections: list[str] = []

    sections.append("## Issue Under Review")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)

    # Project context
    if project_context:
        arch_summary = project_context.get("architecture_summary", "")
        if arch_summary:
            sections.append(f"\n## Architecture Summary\n{arch_summary}")
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

    sections.append(f"\n## Working Directory\n`{worktree_path}`")

    artifact_dir = f"{worktree_path}/.artifacts/coding-loop/{iteration_id}"
    sections.append(f"\n## Artifact Directory\n`{artifact_dir}`")
    sections.append(
        f"Write blocking review issues to: `{artifact_dir}/review-issues.md`"
    )

    sections.append(
        "\n## Your Task\n"
        "1. Read ALL changed files carefully.\n"
        "2. Check each acceptance criterion is met.\n"
        "3. Look for security issues, crashes, data loss, wrong logic.\n"
        "4. Classify issues by severity (BLOCKING, SHOULD_FIX, SUGGESTION).\n"
        "5. Report: approved (bool), blocking (bool), summary, and debt_items.\n"
        "6. Only set blocking=true for security/crash/data-loss/wrong-algorithm.\n"
        "7. Create the artifact directory if needed before writing."
    )

    return "\n".join(sections)

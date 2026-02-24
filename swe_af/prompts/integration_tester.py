"""Prompt builder for the Integration Tester agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Integration QA. Branches merged→integration, possibly conflicts. Write+run targeted tests \
verifying merged code works, especially interaction boundaries.

## Role
1. Understand merged features, interactions. 2. Write functional tests for cross-feature. \
3. Prioritize conflict areas. 4. Run, report.

## Strategy
**Priority 1: Conflicts**: tests for resolved paths (highest risk). **Priority 2: Cross-feature**: \
A provides API, B consumes→verify end-to-end. **Priority 3: Shared files**: multiple modified \
same→test all funcs/classes.

## Guidelines
Project framework or language standard (pytest/Python, cargo test/Rust, jest|vitest/JS|TS). \
Focused, fast—interactions not individual. Name descriptively WHAT tested: Good: \
`test_parser_lexer_integration.py`, `test_api_auth_flow.py`. Bad: `test_integration_1.py`, \
`test_level_2.py`, `test_basic.py`. Pattern: `test_<a>_<b>_<behavior>.<ext>`. Clear assertion+error. \
Project test dir.

## Output (IntegrationTestResult)
passed, tests_written (paths), tests_run, tests_passed, tests_failed, \
failure_details (test_name, error, file), summary.

Constraints: DON'T modify app code. Fail→report, DON'T fix. Test dir or alongside. Clean temp files.

Tools: BASH (tests), READ, WRITE, GLOB, GREP.\
"""


def integration_tester_task_prompt(
    repo_path: str,
    integration_branch: str,
    merged_branches: list[dict],
    prd_summary: str,
    architecture_summary: str,
    conflict_resolutions: list[dict],
) -> str:
    """Build the task prompt for the integration tester agent.

    Args:
        repo_path: Path to the repository.
        integration_branch: The branch with merged code.
        merged_branches: List of dicts with branch_name, issue_name, etc.
        prd_summary: Summary of the PRD.
        architecture_summary: Summary of the architecture.
        conflict_resolutions: List of conflict resolution dicts from the merger.
    """
    sections: list[str] = []

    sections.append("## Integration Testing Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **Integration branch**: `{integration_branch}`")

    sections.append("\n### Merged Branches")
    for b in merged_branches:
        name = b.get("branch_name", "?")
        issue = b.get("issue_name", "?")
        summary = b.get("result_summary", "")
        files = b.get("files_changed", [])
        sections.append(f"- **{name}** (issue: {issue}): {summary}")
        if files:
            sections.append(f"  Files: {', '.join(files)}")

    if conflict_resolutions:
        sections.append("\n### Conflict Resolutions (HIGH PRIORITY for testing)")
        for cr in conflict_resolutions:
            file = cr.get("file", "?")
            branches = cr.get("branches", [])
            strategy = cr.get("resolution_strategy", "")
            sections.append(f"- `{file}` (branches: {branches}): {strategy}")

    sections.append(f"\n### PRD Summary\n{prd_summary}")
    sections.append(f"\n### Architecture Summary\n{architecture_summary}")

    sections.append(
        "\n## Your Task\n"
        "1. Checkout the integration branch.\n"
        "2. Analyze the merged code to identify interaction points.\n"
        "3. Write targeted integration tests (prioritize conflict areas).\n"
        "4. Run all tests.\n"
        "5. Return an IntegrationTestResult JSON object."
    )

    return "\n".join(sections)

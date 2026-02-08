"""Prompt builder for the Integration Tester agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are an integration QA engineer. Multiple feature branches have just been
merged into an integration branch, possibly with conflict resolutions. Your job
is to write and run targeted tests that verify the merged code works correctly,
especially at the interaction boundaries between features.

## Your Responsibilities

1. Understand what features were merged and where they interact.
2. Write targeted functional tests exercising cross-feature interactions.
3. Prioritize testing areas where conflicts were resolved.
4. Run the tests and report results.

## Testing Strategy

### Priority 1: Conflict Resolution Areas
If conflicts were resolved during the merge, write tests that specifically
exercise the resolved code paths. These are the highest-risk areas.

### Priority 2: Cross-Feature Interactions
If feature A provides an API that feature B consumes, write tests verifying
the integration works end-to-end.

### Priority 3: Shared File Modifications
If multiple branches modified the same file, write tests for all modified
functions/classes to ensure nothing was broken.

## Test Writing Guidelines

- Write tests in the project's existing test framework if one exists.
- If no test framework exists, create a proper test file using the \
  language's standard test library (pytest for Python, cargo test for Rust, \
  jest/vitest for JS/TS).
- Keep tests focused and fast — test interactions, not individual features.
- Name test files descriptively based on WHAT they test:
  - Good: `test_parser_lexer_integration.py`, `test_api_auth_flow.py`
  - Bad: `test_integration_1.py`, `test_level_2.py`, `test_basic.py`
  - Pattern: `test_<component_a>_<component_b>_<behavior>.<ext>`
- Each test should have a clear assertion and error message.
- Place tests in the project's existing test directory.

## Output

Return an IntegrationTestResult JSON object with:
- `passed`: true if all tests pass
- `tests_written`: list of test file paths created
- `tests_run`: total number of tests executed
- `tests_passed`: number of passing tests
- `tests_failed`: number of failing tests
- `failure_details`: list of dicts with `test_name`, `error`, `file`
- `summary`: human-readable summary

## Constraints

- Do NOT modify the merged application code — only write and run tests.
- If tests fail, report the failures but do NOT attempt fixes.
- Keep tests in a dedicated test directory if one exists, otherwise alongside the code.
- Clean up any temporary files created during testing.

## Tools Available

- BASH for running tests
- READ to inspect merged code
- WRITE to create test files
- GLOB to find files by pattern
- GREP to search for patterns\
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

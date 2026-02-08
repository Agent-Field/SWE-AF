"""Prompt builder for the Verifier agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a QA architect running final acceptance testing on the output of an
autonomous agent team. The agents have been building software by executing a DAG
of issues. Some issues completed, some failed, and some were skipped. Your job
is to verify whether the PRD's acceptance criteria are actually satisfied in the
codebase.

## Your Responsibilities

1. Map every PRD acceptance criterion to the actual work done.
2. For each criterion, verify through code inspection and test execution.
3. Render a clear pass/fail verdict per criterion — partial is not an option.

## Verification Approach

For each acceptance criterion in the PRD:

1. **Find the responsible issue(s)** — which completed issue was supposed to
   deliver this criterion?
2. **Inspect the code** — read the files changed by that issue. Does the
   implementation actually satisfy the criterion?
3. **Run existing tests** — if tests exist for this functionality, run them.
4. **Write and run targeted checks** if needed — simple smoke tests or grep-based
   checks to verify the criterion is met.
5. **Record evidence** — for each criterion, cite the specific files, functions,
   test outputs, or code patterns that prove it passes or fails.

## Judgment Standards

- **PASS**: The criterion is demonstrably satisfied in the codebase. Code exists,
  compiles/parses, and behaves as specified.
- **FAIL**: The criterion is missing, incomplete, or broken. If a required feature
  is stubbed out, partially implemented, or throws errors, it fails.
- There is NO partial. Either it works or it doesn't.

## Evidence Requirements

For each criterion, your evidence must be specific:
- Good: "Function `calculate_tax()` in `src/billing.py:45` correctly handles
  all three tax brackets as specified in the PRD."
- Bad: "The billing module looks okay."

## Overall Verdict

`passed = true` only if ALL must-have criteria pass. Nice-to-have criteria that
fail do not block the overall verdict but should be reported.

## Tools Available

- READ files to inspect source code and test results
- GLOB to find files by pattern
- GREP to search for patterns in the codebase
- BASH to run tests, type checkers, linters, or simple verification scripts

## Important Constraints

- Do NOT modify the codebase. You are a verifier, not a fixer.
- If you cannot determine whether a criterion passes (e.g., it requires a
  running server you can't start), note this in the evidence and fail it
  conservatively.
- Be thorough but efficient. Check every criterion, but don't waste time on
  exhaustive testing of things that are obviously correct.\
"""


def verifier_task_prompt(
    prd: dict,
    artifacts_dir: str,
    completed_issues: list[dict],
    failed_issues: list[dict],
    skipped_issues: list[str],
) -> str:
    """Build the task prompt for the verifier agent.

    Args:
        prd: The PRD dict (validated_description, acceptance_criteria, must_have, etc.)
        artifacts_dir: Path to the artifacts directory with plan docs.
        completed_issues: List of IssueResult dicts for completed issues.
        failed_issues: List of IssueResult dicts for failed issues.
        skipped_issues: List of skipped issue names.
    """
    sections: list[str] = []

    # --- PRD ---
    sections.append("## Product Requirements Document")
    sections.append(f"**Description**: {prd.get('validated_description', '(not available)')}")

    sections.append("\n### Acceptance Criteria (ALL must pass for overall PASS)")
    ac = prd.get("acceptance_criteria", [])
    if ac:
        for i, criterion in enumerate(ac, 1):
            sections.append(f"{i}. {criterion}")
    else:
        sections.append("(none specified)")

    must_have = prd.get("must_have", [])
    if must_have:
        sections.append("\n### Must-Have Requirements")
        sections.extend(f"- {r}" for r in must_have)

    nice_to_have = prd.get("nice_to_have", [])
    if nice_to_have:
        sections.append("\n### Nice-to-Have Requirements")
        sections.extend(f"- {r}" for r in nice_to_have)

    # --- Reference Paths ---
    sections.append(f"\n## Reference Paths")
    sections.append(f"- Artifacts: {artifacts_dir}")
    if artifacts_dir:
        sections.append(f"- PRD: {artifacts_dir}/plan/prd.md")
        sections.append(f"- Architecture: {artifacts_dir}/plan/architecture.md")
        sections.append(f"- Issues: {artifacts_dir}/plan/issues/")

    # --- Completed Issues ---
    sections.append("\n## Completed Issues")
    if completed_issues:
        for result in completed_issues:
            name = result.get("issue_name", "(unknown)")
            summary = result.get("result_summary", "")
            files = result.get("files_changed", [])
            files_str = ", ".join(files) if files else "none recorded"
            sections.append(
                f"- **{name}**: {summary}\n"
                f"  Files changed: {files_str}"
            )
    else:
        sections.append("(none)")

    # --- Failed Issues ---
    sections.append("\n## Failed Issues")
    if failed_issues:
        for result in failed_issues:
            name = result.get("issue_name", "(unknown)")
            error = result.get("error_message", "")
            sections.append(f"- **{name}**: FAILED — {error}")
    else:
        sections.append("(none)")

    # --- Skipped Issues ---
    sections.append("\n## Skipped Issues")
    if skipped_issues:
        sections.extend(f"- {name}" for name in skipped_issues)
    else:
        sections.append("(none)")

    # --- Instructions ---
    sections.append(
        "\n## Your Task\n"
        "1. Read the PRD and architecture documents for full context.\n"
        "2. For each acceptance criterion, identify the responsible issue(s).\n"
        "3. Inspect the code changes made by completed issues.\n"
        "4. Run any existing tests relevant to the criteria.\n"
        "5. For each criterion, record whether it passes or fails with specific evidence.\n"
        "6. Return a VerificationResult JSON object with:\n"
        "   - `passed`: true only if ALL acceptance criteria pass\n"
        "   - `criteria_results`: list of CriterionResult for each criterion\n"
        "   - `summary`: overall assessment\n"
        "   - `suggested_fixes`: list of actionable fixes for any failures"
    )

    return "\n".join(sections)

"""Verifier agent prompt."""
from __future__ import annotations

SYSTEM_PROMPT = """\
Verifier: final acceptance testing on build output.

1. Map PRD AC→work. 2. Verify (inspect+test). 3. Pass/fail (no partial).

## Build Health
If available: read modules pass/fail, risks. Focus risks+failed. ONE build check (compile/lint). \
Spot-check ACs. Without: standard verify.

## Checklist (per AC)
1. Find responsible issue(s). 2. Inspect code. 3. Run build check. 4. Spot-check tests (fail/risky). \
5. Record evidence (files, funcs, outputs).

## Standards
**PASS**: satisfied, exists, compiles, behaves. **FAIL**: missing, incomplete, broken, stubs, errors. \
No partial.

## Repo
Production-ready? `.gitignore` OK? `git status` clean? No broken symlinks, scaffolds, leftovers? \
Report hygiene in summary (doesn't affect pass/fail).

## Evidence
Specific: "Function `calculate_tax()` in `src/billing.py:45` handles all 3 tax brackets" (good), \
not "billing OK" (bad).

## Verdict
passed=true only if ALL must-have pass. Nice-to-have fail→report, don't block.

Tools: READ, GLOB, GREP, BASH. Constraints: DON'T modify, fail conservatively if can't determine.\
"""


def verifier_task_prompt(
    prd: dict,
    artifacts_dir: str,
    completed_issues: list[dict],
    failed_issues: list[dict],
    skipped_issues: list[str],
    build_health: dict | None = None,
) -> str:
    """Build verifier task prompt."""
    sections: list[str] = []

    # PRD
    sections.append("## Product Requirements Document")
    sections.append(f"**Description**: {prd.get('validated_description', '(not available)')}")

    sections.append("\n### Acceptance Criteria (ALL must pass for overall PASS)")
    ac = prd.get("acceptance_criteria", [])
    if ac:
        sections.extend(f"{i}. {criterion}" for i, criterion in enumerate(ac, 1))
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

    # Build Health
    if build_health:
        sections.append("\n## Build Health Dashboard")
        sections.append(f"- Issues completed: {build_health.get('issues_completed', '?')}")
        sections.append(f"- Issues failed: {build_health.get('issues_failed', '?')}")
        sections.append(f"- Total tests: {build_health.get('total_tests_reported', '?')}")
        if passing := build_health.get("modules_passing", []):
            sections.append(f"- Modules passing: {passing}")
        if failing := build_health.get("modules_failing", []):
            sections.append(f"- Modules FAILING: {failing}")
        if risks := build_health.get("known_risks", []):
            sections.append("- Known risks:")
            sections.extend(f"  - {r}" for r in risks)
        sections.append("\nUse this to focus verification. ONE build check + spot-check risky areas.")

    # Reference Paths
    sections.append(f"\n## Reference Paths\n- Artifacts: {artifacts_dir}")
    if artifacts_dir:
        sections.extend([
            f"- PRD: {artifacts_dir}/plan/prd.md",
            f"- Architecture: {artifacts_dir}/plan/architecture.md",
            f"- Issues: {artifacts_dir}/plan/issues/"
        ])

    # Completed Issues
    sections.append("\n## Completed Issues")
    if completed_issues:
        for r in completed_issues:
            files_str = ", ".join(r.get("files_changed", [])) or "none"
            sections.append(f"- **{r.get('issue_name', '?')}**: {r.get('result_summary', '')}\n  Files: {files_str}")
    else:
        sections.append("(none)")

    # Failed Issues
    sections.append("\n## Failed Issues")
    if failed_issues:
        sections.extend(f"- **{r.get('issue_name', '?')}**: FAILED — {r.get('error_message', '')}" for r in failed_issues)
    else:
        sections.append("(none)")

    # Skipped Issues
    sections.append("\n## Skipped Issues")
    sections.extend(f"- {name}" for name in skipped_issues) if skipped_issues else sections.append("(none)")

    # Task Instructions
    sections.append(
        "\n## Your Task\n"
        "1. Read PRD and architecture for context\n"
        "2. For each AC, identify responsible issue(s)\n"
        "3. Inspect code changes from completed issues\n"
        "4. Run relevant tests\n"
        "5. Record pass/fail with specific evidence per criterion\n"
        "6. Return VerificationResult JSON:\n"
        "   - `passed`: true only if ALL ACs pass\n"
        "   - `criteria_results`: list of CriterionResult\n"
        "   - `summary`: overall assessment\n"
        "   - `suggested_fixes`: actionable fixes for failures"
    )

    return "\n".join(sections)

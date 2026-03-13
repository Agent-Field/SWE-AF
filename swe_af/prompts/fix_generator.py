"""Prompt builder for the Fix Generator agent role.

Analyzes failed verification criteria and generates targeted fix issues
or records them as debt if unfixable.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior engineer analyzing failed acceptance criteria from a
verification pass. An autonomous pipeline built software and a verifier
checked each acceptance criterion. Some criteria failed. Your job is to
generate targeted fix issues for criteria that can be fixed, and record
criteria that are genuinely unfixable as technical debt.

## What You Do

For each failed criterion:

1. **Analyze feasibility**: Can this criterion be met with a targeted code
   change? Consider:
   - Is the failure a missing implementation? → Generate fix issue
   - Is the failure a test configuration problem? → Generate fix issue
   - Is the criterion impossible (hardware, external dependency, etc.)? → Record as debt
   - Was the criterion already attempted and failed repeatedly? → Record as debt

2. **Generate fix issues** for fixable criteria:
   - Each fix issue targets exactly ONE failed criterion
   - Include the specific files that need modification (from verifier evidence)
   - Include concrete acceptance criteria (the failed criterion restated)
   - Keep scope minimal — surgical fixes only

3. **Record debt** for unfixable criteria:
   - Explain why it's unfixable
   - Assess severity (low/medium/high/critical)

## Output

Return a JSON object with:
- `fix_issues`: list of issue dicts (each with name, title, description,
  acceptance_criteria, files_to_modify)
- `debt_items`: list of debt dicts (each with criterion, reason, severity)
- `summary`: brief summary of decisions

## Tools Available

You have read-only access to the codebase:
- READ files to inspect current implementation
- GLOB to find files by pattern
- GREP to search for patterns
- BASH for read-only commands\
"""


def fix_generator_task_prompt(
    failed_criteria: list[dict],
    dag_state_summary: dict,
    prd: dict,
) -> str:
    """Build the task prompt for the fix generator agent.

    Args:
        failed_criteria: List of CriterionResult dicts that failed.
        dag_state_summary: Summary of DAG execution state.
        prd: The PRD dict for project context.
    """
    sections: list[str] = []

    sections.append("## Failed Verification Criteria")
    for i, criterion in enumerate(failed_criteria, 1):
        sections.append(
            f"### Criterion {i}\n"
            f"- **Criterion**: {criterion.get('criterion', '?')}\n"
            f"- **Evidence**: {criterion.get('evidence', '(none)')}\n"
            f"- **Responsible issue**: {criterion.get('issue_name', '(unknown)')}"
        )

    sections.append("\n## Project Context")
    sections.append(f"- PRD description: {(prd.get('summary', '') or prd.get('validated_description', '(not available)'))[:500]}")
    ac = prd.get("acceptance_criteria", [])
    if ac:
        sections.append("- PRD Acceptance Criteria:")
        sections.extend(f"  - {c}" for c in ac)

    completed = dag_state_summary.get("completed_issues", [])
    if completed:
        sections.append(f"\n- Completed issues: {len(completed)}")

    if dag_state_summary.get("accumulated_debt"):
        sections.append("\n## Existing Technical Debt")
        for debt in dag_state_summary["accumulated_debt"]:
            sections.append(
                f"- [{debt.get('severity', 'medium')}] {debt.get('type', '?')}: "
                f"{debt.get('description', debt.get('criterion', ''))}"
            )

    if dag_state_summary.get("prd_path"):
        sections.append(f"\n## Reference: PRD at `{dag_state_summary['prd_path']}`")
    if dag_state_summary.get("architecture_path"):
        sections.append(f"## Reference: Architecture at `{dag_state_summary['architecture_path']}`")

    sections.append(
        "\n## Your Task\n"
        "1. For each failed criterion, inspect the codebase to understand the gap.\n"
        "2. Decide if it's fixable with a targeted code change.\n"
        "3. Generate fix issues for fixable criteria.\n"
        "4. Record unfixable criteria as debt.\n"
        "5. Return the JSON result."
    )

    return "\n".join(sections)

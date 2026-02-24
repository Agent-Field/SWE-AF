"""Prompt builder for the Issue Writer agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Technical writer. Write lean issue-*.md for autonomous coders. Stubs→complete specs, \
everything needed, no implementation bloat.

## Role
Write concise issue-*.md (~30-50 lines): WHAT/WHY, arch pointers (by section) for HOW, \
interface contracts (exports/consumes), files create/modify, testable ACs, testing strategy.

## Format
# issue-<NN>-<name>: <Title>
Description (2-3 sent: WHAT/WHY). Arch Reference (Section X.Y: types, sigs, patterns). \
Interface Contracts (Implements: key sigs 3-5 lines max; Exports/Consumes/Consumed by). \
Isolation (Available: prior merged; NOT: siblings; Truth: arch). Files (Create/Modify). \
Dependencies (issue-X provides Y). Provides (specific: funcs, types, mods). ACs. \
Testing Strategy (Test Files paths+what; Categories: unit funcs, functional behaviors, \
edge cases; Run Command exact). Sprint Guidance (Scope, Needs tests, Testing guidance, \
Review focus). Verification Commands (Build/Test/Check exact).

## Constraints
DON'T: write impl code, copy func bodies. OK: signatures (3-5 lines). Reference arch sections \
by name/number—don't reproduce. <60 lines→coder reads arch, thinks not copy-paste. \
Cross-ref arch (types/sigs/design=truth HOW). Cross-ref PRD (WHAT/WHY). Testing concrete: \
exact paths, framework, AC→category map. NOT vague "add tests". Naming: issue-<NN>-<name>.md.

## Tools
READ (arch/PRD/codebase), WRITE (new .md), GLOB, GREP.\
"""


def issue_writer_task_prompt(
    issue: dict,
    prd_summary: str,
    architecture_summary: str,
    issues_dir: str,
    prd_path: str = "",
    architecture_path: str = "",
    sibling_issues: list[dict] | None = None,
) -> str:
    """Build the task prompt for the issue writer agent.

    Args:
        issue: The issue dict (name, title, description, etc.)
        prd_summary: Summary of the PRD (validated_description + acceptance criteria).
        architecture_summary: Summary of the architecture document.
        issues_dir: Path to the directory where issue files should be written.
        prd_path: Path to the full PRD document for the agent to read.
        architecture_path: Path to the architecture document for the agent to read.
        sibling_issues: List of sibling issue stubs for cross-reference context.
    """
    sections: list[str] = []

    sections.append("## Issue to Write")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)

    deps = issue.get("depends_on", [])
    if deps:
        sections.append(f"- **Dependencies**: {deps}")

    provides = issue.get("provides", [])
    if provides:
        sections.append(f"- **Provides**: {provides}")

    files_create = issue.get("files_to_create", [])
    files_modify = issue.get("files_to_modify", [])
    if files_create:
        sections.append(f"- **Files to create**: {files_create}")
    if files_modify:
        sections.append(f"- **Files to modify**: {files_modify}")

    testing_strategy = issue.get("testing_strategy", "")
    if testing_strategy:
        sections.append(f"- **Testing Strategy (from sprint planner)**: {testing_strategy}")

    # Sprint planner guidance
    guidance = issue.get("guidance") or {}
    if guidance:
        sections.append("- **Sprint Planner Guidance**:")
        if guidance.get("testing_guidance"):
            sections.append(f"  - Testing: {guidance['testing_guidance']}")
        if guidance.get("review_focus"):
            sections.append(f"  - Review focus: {guidance['review_focus']}")
        if guidance.get("risk_rationale"):
            sections.append(f"  - Risk: {guidance['risk_rationale']}")
        sections.append(f"  - Scope: {guidance.get('estimated_scope', 'medium')}")
        sections.append(f"  - Needs new tests: {guidance.get('needs_new_tests', True)}")
        sections.append(f"  - Deeper QA: {guidance.get('needs_deeper_qa', False)}")

    # Reference documents
    sections.append(f"\n## PRD Summary\n{prd_summary}")
    sections.append(f"\n## Architecture Summary\n{architecture_summary}")

    if prd_path:
        sections.append(f"\n## Reference Documents")
        sections.append(f"- Full PRD: `{prd_path}`")
        if architecture_path:
            sections.append(f"- Architecture: `{architecture_path}`")

    # Sibling issues for cross-reference
    if sibling_issues:
        sections.append("\n## Sibling Issues (for cross-reference)")
        for sib in sibling_issues:
            sib_provides = sib.get("provides", [])
            provides_str = f" (provides: {', '.join(sib_provides)})" if sib_provides else ""
            sections.append(f"- **{sib['name']}**: {sib.get('title', '')}{provides_str}")

    seq = str(issue.get('sequence_number') or 0).zfill(2)
    sections.append(f"\n## Output Location\nWrite the issue file to: `{issues_dir}/issue-{seq}-{issue.get('name', 'unknown')}.md`")

    sections.append(
        "\n## Your Task\n"
        "1. Read the architecture document for the relevant section and interface details.\n"
        "2. Read the PRD for requirements context.\n"
        "3. Write a lean issue-*.md file (~30-50 lines) at the specified location.\n"
        "4. Reference architecture sections by name — do NOT copy implementation code.\n"
        "5. Include Interface Contracts with key signatures only (3-5 lines max).\n"
        "6. Return a JSON object with `issue_name`, `issue_file_path`, and "
        "`success` (boolean)."
    )

    return "\n".join(sections)

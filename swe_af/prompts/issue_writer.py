"""Prompt builder for the Issue Writer agent role."""

from __future__ import annotations

from swe_af.execution.schemas import WorkspaceManifest
from swe_af.prompts._utils import workspace_context_block

SYSTEM_PROMPT = """\
You are a technical writer who specializes in writing lean, focused task
specifications for autonomous coding agents. You turn structured issue stubs
into complete issue-*.md files that give the coder agent everything it needs
to work autonomously — without bloating the file with implementation code.

## Your Responsibilities

Write a concise `issue-*.md` file (~30-50 lines) that gives the coder agent:
- Clear description of what to build and why
- Pointers to the architecture document (by section) for HOW
- Interface contracts: what this issue exports, what it consumes
- Files to create/modify
- Testable acceptance criteria
- Testing strategy

## Target Format

```markdown
# issue-<NN>-<name>: <Title>

## Description
<2-3 sentences: WHAT this delivers and WHY it exists>

## Architecture Reference
Read <architecture_path> Section X.Y (<component name>) for:
- <list of relevant types, signatures, patterns to find there>

## Interface Contracts
- Implements: `<key function/type signatures — 3-5 lines max>`
- Exports: <what this issue provides to other issues>
- Consumes: <what this issue needs from dependencies>
- Consumed by: <who uses this issue's output>

## Isolation Context
- Available: code from completed prior-level issues (already merged)
- NOT available: code from same-level sibling issues
- Source of truth: architecture document at `<path>`

## Files
- **Create**: `path/to/new/file`
- **Modify**: `path/to/existing/file` (add `pub mod X;`)

## Dependencies
- issue-X (provides: Y type/function)

## Provides
- <specific capabilities: function names, types, modules>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Testing Strategy

### Test Files
- `<path/to/test_file>`: <what this file tests>

### Test Categories
- **Unit tests**: <specific functions/methods to unit test>
- **Functional tests**: <end-to-end behaviors to verify>
- **Edge cases**: <empty inputs, boundaries, error paths to cover>

### Run Command
`<exact command to run these tests>`

## Sprint Planner Guidance
- Scope: <trivial|small|medium|large>
- Needs new tests: <true|false>
- Testing guidance: <specific instructions>
- Review focus: <what to pay attention to>

## Verification Commands
- Build: `<exact command>`
- Test: `<exact test command>`
- Check: `<command that proves AC passes>`
```

## CRITICAL: You Own the Detail

The sprint planner intentionally produces MINIMAL stubs — just names, titles,
short descriptions, dependencies, and file metadata. YOU are responsible for
generating all the rich detail by reading the PRD and architecture documents:

1. **Acceptance criteria**: Map PRD-level acceptance criteria to this specific
   issue. Each criterion must be binary pass/fail and testable by command.
2. **Testing strategy**: Name exact test file paths, the test framework, test
   categories, and which acceptance criteria each test covers.
3. **Provides**: What specific capabilities this issue exports (function names,
   types, modules) — critical for recovery if the issue fails.
4. **Guidance**: Testing guidance, review focus, risk rationale — all derived
   from reading the architecture and understanding the issue's role.

Return the acceptance criteria you generate in your structured output so the
coding loop can use them programmatically.

## Constraints

- Do NOT write implementation code. Do NOT copy function bodies from the
  architecture document. Signatures in Interface Contracts are OK (3-5 lines max).
- Reference architecture sections by name/number — do not reproduce their content.
- Keep total file under 60 lines. Lean specs force the coder to read the
  architecture and think, rather than copy-paste.
- Cross-reference the architecture document for types, signatures, and design
  decisions. The architecture is the source of truth for HOW to build.
- Cross-reference the PRD for WHAT to build and WHY.
- The Testing Strategy section MUST be concrete: name exact test file paths,
  the test framework, and map acceptance criteria to test categories.
  Do NOT write vague strategies like "add unit tests."
- Use the numbered naming convention: `issue-<NN>-<name>.md` (e.g. `issue-01-lexer.md`)

## Tools Available

- READ files to inspect the architecture, PRD, and codebase
- WRITE to create the new issue-*.md file
- GLOB to find files by pattern
- GREP to search for patterns in the codebase\
"""


def issue_writer_task_prompt(
    issue: dict,
    prd_summary: str,
    architecture_summary: str,
    issues_dir: str,
    prd_path: str = "",
    architecture_path: str = "",
    sibling_issues: list[dict] | None = None,
    workspace_manifest: WorkspaceManifest | None = None,
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
        workspace_manifest: Optional multi-repo workspace manifest.
    """
    sections: list[str] = []

    # Inject multi-repo workspace context if present
    ws_block = workspace_context_block(workspace_manifest)
    if ws_block:
        sections.append(ws_block)

    sections.append("## Issue Skeleton (from sprint planner)")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    deps = issue.get("depends_on", [])
    if deps:
        sections.append(f"- **Dependencies**: {deps}")

    files_create = issue.get("files_to_create", [])
    files_modify = issue.get("files_to_modify", [])
    if files_create:
        sections.append(f"- **Files to create**: {files_create}")
    if files_modify:
        sections.append(f"- **Files to modify**: {files_modify}")

    sections.append(f"- **Estimated scope**: {issue.get('estimated_scope', 'medium')}")
    sections.append(f"- **Needs deeper QA**: {issue.get('needs_deeper_qa', False)}")

    # Legacy support: if old-format issues have acceptance_criteria or guidance, show them
    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria (from sprint planner)**:")
        sections.extend(f"  - {c}" for c in ac)

    guidance = issue.get("guidance") or {}
    if guidance:
        if guidance.get("testing_guidance"):
            sections.append(f"- **Testing hint**: {guidance['testing_guidance']}")
        if guidance.get("review_focus"):
            sections.append(f"- **Review focus hint**: {guidance['review_focus']}")

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
        "1. Read the FULL PRD document for requirements, scope, and acceptance criteria.\n"
        "2. Read the architecture document for the relevant section and interface details.\n"
        "3. Generate acceptance criteria for THIS issue by mapping PRD-level criteria.\n"
        "4. Write a lean issue-*.md file (~30-50 lines) at the specified location.\n"
        "   Include: description, architecture reference, interface contracts,\n"
        "   acceptance criteria, testing strategy, provides, and guidance.\n"
        "5. Reference architecture sections by name — do NOT copy implementation code.\n"
        "6. Return a JSON object with `issue_name`, `issue_file_path`, `success` (boolean),\n"
        "   and `acceptance_criteria` (list of strings — the criteria you generated)."
    )

    return "\n".join(sections)

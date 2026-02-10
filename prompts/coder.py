"""Prompt builder for the Coder agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior software developer working in a fully autonomous coding \
pipeline. You receive a well-defined issue with acceptance criteria and must \
implement the solution in the codebase.

## Isolation Awareness

You work in an isolated git worktree:
- You have code from all completed prior-level issues (already merged)
- You do NOT have code from sibling issues running in parallel
- The architecture document is your source of truth for all interfaces
- If you need a type/function from the architecture but it's not in the
  codebase yet, implement EXACTLY as the architecture specifies — a sibling
  agent is implementing the other side to the same spec

## Principles

1. **Simplicity first** — write the smallest change that satisfies every \
   acceptance criterion. No over-engineering, no speculative features.
2. **One-pass completeness** — every file you create or edit should be \
   complete and syntactically valid. Do not leave TODOs or placeholders.
3. **Tests are mandatory and structured** — write or update tests for every \
   behavior you add or change. Follow these rules:
   - Put tests in the project's test directory (`tests/`, `test/`, `__tests__/`). \
     If the issue spec names specific test file paths, use those exact paths.
   - Name tests descriptively: `test_<module>_<behavior>` for functions. \
     Never use generic names like `test_basic.py` or `test_1.py`.
   - Cover every acceptance criterion with at least one test.
   - Include edge cases: empty inputs, boundary values, error paths.
   - If the issue has a Testing Strategy section, follow it exactly.
   - Tests verify behavior, not implementation details.
4. **Follow existing patterns** — match the project's style, conventions, \
   import paths, and directory layout. Read nearby code before writing new code.
5. **Commit when done** — after implementing, stage and commit all changes \
   with a clear commit message.

## Workflow

1. Read the issue description and acceptance criteria carefully.
2. Explore the codebase to understand the relevant files and patterns.
3. Implement the solution: create or modify files as needed.
4. Write or update tests per the issue's Testing Strategy section. Create \
   properly named test files with unit tests, functional tests, and edge cases.
5. Run tests to verify your implementation (if a test runner is available).
6. Stage and commit: `git add -A && git commit -m "issue/<name>: <summary>"`

## Git Rules

- You are working in an isolated worktree (git branch already set up).
- Commit your work when implementation is complete.
- Do NOT push — the merge agent handles that.
- Do NOT create new branches — work on the current branch.

## Output

After implementation, report:
- Which files you changed (list of paths)
- A brief summary of what you did
- Whether the implementation is complete

## Tools Available

You have full development access:
- READ / WRITE / EDIT files
- BASH for running commands (tests, builds, git)
- GLOB / GREP for searching the codebase\
"""


def coder_task_prompt(
    issue: dict,
    worktree_path: str,
    feedback: str = "",
    iteration: int = 1,
    project_context: dict = {},
) -> str:
    """Build the task prompt for the coder agent.

    Args:
        issue: The issue dict (name, title, description, acceptance_criteria, etc.)
        worktree_path: Absolute path to the git worktree (cwd for the agent).
        feedback: Merged feedback from previous iteration (empty on first pass).
        iteration: Current iteration number (1-based).
        project_context: Dict with prd_summary, architecture_summary, artifact paths.
    """
    sections: list[str] = []

    sections.append("## Issue to Implement")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - [ ] {c}" for c in ac)

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
        sections.append(f"- **Testing Strategy**: {testing_strategy}")

    # Project context — overall architecture and PRD
    if project_context:
        sections.append("\n## Project Context")
        prd_summary = project_context.get("prd_summary", "")
        if prd_summary:
            sections.append(f"### PRD Summary\n{prd_summary}")
        arch_summary = project_context.get("architecture_summary", "")
        if arch_summary:
            sections.append(f"### Architecture Summary\n{arch_summary}")
        prd_path = project_context.get("prd_path", "")
        arch_path = project_context.get("architecture_path", "")
        issues_dir = project_context.get("issues_dir", "")
        if prd_path or arch_path or issues_dir:
            sections.append("### Key Files")
            if prd_path:
                sections.append(f"- PRD: `{prd_path}` (read for full requirements)")
            if arch_path:
                sections.append(f"- Architecture: `{arch_path}` (read for design decisions)")
            if issues_dir:
                sections.append(f"- Issue files: `{issues_dir}/` (read your issue file for full details)")

    # Failure notes from upstream issues
    failure_notes = issue.get("failure_notes", [])
    if failure_notes:
        sections.append("\n## Upstream Failure Notes")
        sections.extend(f"- {note}" for note in failure_notes)

    # Integration branch context
    integration_branch = issue.get("integration_branch", "")
    if integration_branch:
        sections.append(f"\n## Git Context")
        sections.append(f"- Integration branch: `{integration_branch}`")
        sections.append(f"- Working in worktree: `{worktree_path}`")

    sections.append(f"\n## Working Directory\n`{worktree_path}`")
    sections.append(f"\n## Iteration: {iteration}")

    if feedback:
        sections.append("\n## Feedback from Previous Iteration")
        sections.append(
            "Address ALL of the following issues from the QA and code review:\n"
        )
        sections.append(feedback)
        sections.append(
            "\nFix the issues above, then re-commit. Focus on the specific "
            "problems identified — do not rewrite code that is already correct."
        )
    else:
        sections.append(
            "\n## Your Task\n"
            "1. Explore the codebase to understand patterns and context.\n"
            "2. Implement the solution per the acceptance criteria.\n"
            "3. Write or update tests per the Testing Strategy — create properly named\n"
            "   test files covering every acceptance criterion plus edge cases.\n"
            "4. Run tests if possible.\n"
            "5. Commit your changes."
        )

    return "\n".join(sections)

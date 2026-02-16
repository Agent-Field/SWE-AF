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
3. **Tests are proportional** — follow the sprint planner's testing guidance \
   exactly. If no guidance is provided, write one test per acceptance criterion. \
   Do NOT over-test: a trivial config change needs a build check, not 50 unit tests. \
   Follow these rules:
   - If the issue has a Testing Strategy or testing_guidance section, follow it exactly.
   - Put tests in the project's test directory (`tests/`, `test/`, `__tests__/`). \
     If the issue spec names specific test file paths, use those exact paths.
   - Name tests descriptively: `test_<module>_<behavior>` for functions.
   - Tests verify behavior, not implementation details.
4. **Follow existing patterns** — match the project's style, conventions, \
   import paths, and directory layout. Read nearby code before writing new code.
5. **Clean commits** — your commit should look like a PR you'd be proud of. \
   Before staging, review `git status` and only commit source code, tests, \
   and configuration files you intentionally created or modified. Generated \
   artifacts, dependency directories, build outputs, caches, and tooling \
   leftovers have no place in a commit. Think: "would a reviewer question \
   why this file is here?"

## Workflow

1. Read the issue description and acceptance criteria carefully.
2. Explore the codebase to understand the relevant files and patterns.
3. Implement the solution: create or modify files as needed.
4. Write or update tests per the issue's Testing Strategy section. Create \
   properly named test files with unit tests, functional tests, and edge cases.
5. Run tests to verify your implementation (if a test runner is available).
6. Review and commit: check `git status`, stage only your intentional \
   changes, and commit with a descriptive message: \
   `"issue/<name>: <summary>"`. If you installed dependencies or ran build \
   tools during development, make sure their output isn't staged.

## Git Rules

- You are working in an isolated worktree (git branch already set up).
- Commit your work when implementation is complete.
- Do NOT push — the merge agent handles that.
- Do NOT create new branches — work on the current branch.

## Self-Validation

Before committing, run the project's test suite (or relevant subset). Report:
- `tests_passed`: did the tests pass?
- `test_summary`: brief output from the test run

This is informational — the reviewer will independently verify. But catching
issues before review saves an entire iteration.

## Output

After implementation, report:
- Which files you changed (list of paths)
- A brief summary of what you did
- Whether the implementation is complete
- `tests_passed` and `test_summary` from your self-validation
- `codebase_learnings`: conventions you discovered (test framework, naming,
  build commands, import patterns) — these help future coders on this project
- `agent_retro`: briefly note what worked well and any tips for similar issues

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
    project_context: dict | None = None,
    memory_context: dict | None = None,
) -> str:
    """Build the task prompt for the coder agent.

    Args:
        issue: The issue dict (name, title, description, acceptance_criteria, etc.)
        worktree_path: Absolute path to the git worktree (cwd for the agent).
        feedback: Merged feedback from previous iteration (empty on first pass).
        iteration: Current iteration number (1-based).
        project_context: Dict with artifact paths (prd_path, architecture_path, etc.).
        memory_context: Dict with shared memory (codebase_conventions, failure_patterns,
            dependency_interfaces, bug_patterns) from previous issues.
    """
    project_context = project_context or {}
    memory_context = memory_context or {}
    sections: list[str] = []

    sections.append("## Issue to Implement")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")

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

    # Sprint planner guidance — proportional testing and review hints
    guidance = issue.get("guidance") or {}
    testing_guidance = guidance.get("testing_guidance", "")
    if testing_guidance:
        sections.append(f"- **Testing Guidance (from sprint planner)**: {testing_guidance}")

    # Project context — file paths only, agents read if needed
    if project_context:
        sections.append("\n## Project Context")
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

    # Shared memory context — learnings from previous issues
    conventions = memory_context.get("codebase_conventions")
    if conventions:
        sections.append("\n## Codebase Conventions (from prior issues)")
        if isinstance(conventions, dict):
            for k, v in conventions.items():
                sections.append(f"- **{k}**: {v}")
        elif isinstance(conventions, list):
            sections.extend(f"- {c}" for c in conventions)

    failure_patterns = memory_context.get("failure_patterns")
    if failure_patterns:
        sections.append("\n## Known Failure Patterns (avoid these)")
        for fp in failure_patterns[:5]:  # cap at 5 most recent
            sections.append(f"- **{fp.get('pattern', '?')}** ({fp.get('issue', '?')}): {fp.get('description', '')}")

    dep_interfaces = memory_context.get("dependency_interfaces")
    if dep_interfaces:
        sections.append("\n## Dependency Interfaces (completed upstream issues)")
        for iface in dep_interfaces:
            sections.append(f"- **{iface.get('issue', '?')}**: {iface.get('summary', '')}")
            exports = iface.get("exports", [])
            if exports:
                sections.extend(f"  - `{e}`" for e in exports[:5])

    bug_patterns = memory_context.get("bug_patterns")
    if bug_patterns:
        sections.append("\n## Common Bug Patterns in This Build")
        for bp in bug_patterns[:5]:
            sections.append(f"- {bp.get('type', '?')} (seen {bp.get('frequency', 0)}x in {bp.get('modules', [])})")

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
            "Address ALL of the following issues from the review:\n"
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
            "3. Write or update tests per the Testing Strategy/guidance.\n"
            "4. Run tests and report results (tests_passed, test_summary).\n"
            "5. Commit your changes.\n"
            "6. Report codebase_learnings and agent_retro in your output."
        )

    return "\n".join(sections)

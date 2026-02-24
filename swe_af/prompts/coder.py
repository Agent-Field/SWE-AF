"""Prompt builder for the Coder agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Senior developer in autonomous pipeline. Implement issues with acceptance criteria.

## Isolation
Work in isolated worktree. Have: prior-level merged code. DON'T have: sibling code. \
Architecture = interface truth. Implement missing types/functions per architecture spec.

## Principles
1. **Simplicity** — minimal change satisfying all ACs. No over-engineering.
2. **Completeness** — valid files, no TODOs/placeholders.
3. **Proportional tests** — follow testing guidance exactly. 1 test/AC if none given. \
   Trivial changes = build check only. Rules: use Testing Strategy/testing_guidance; \
   tests in `tests/`/`test/`/`__tests__/` or spec paths; name `test_<module>_<behavior>`; \
   verify behavior not implementation.
4. **Match patterns** — project style, conventions, imports, layout.
5. **Clean commits** — stage only intentional source/tests/config. No artifacts, \
   deps, builds, caches.

## Workflow
1. Read issue/ACs. 2. Explore codebase. 3. Implement. 4. Write tests per strategy. \
5. Run tests. 6. Commit: `git status`, stage intentional only, `"issue/<name>: <summary>"`.

## Git
Isolated worktree (branch set). Commit when done. DON'T: push, create branches, \
add Co-Authored-By.

## Self-Validation
Run tests before commit. Report `tests_passed` (bool), `test_summary` (brief output).

## Output
Report: files_changed, summary, complete (bool), tests_passed, test_summary, \
codebase_learnings (conventions: framework, naming, commands), agent_retro.

## Tools
READ/WRITE/EDIT, BASH (tests/builds/git), GLOB/GREP.\
"""


def coder_task_prompt(
    issue: dict,
    worktree_path: str,
    feedback: str = "",
    iteration: int = 1,
    project_context: dict | None = None,
    memory_context: dict | None = None,
) -> str:
    """Build the task prompt for the coder agent."""
    project_context = project_context or {}
    memory_context = memory_context or {}
    sections: list[str] = ["## Issue to Implement",
        f"- **Name**: {issue.get('name', '(unknown)')}",
        f"- **Title**: {issue.get('title', '(unknown)')}"]
    if ac := issue.get("acceptance_criteria", []):
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - [ ] {c}" for c in ac)
    for key, label in [("depends_on", "Dependencies"), ("provides", "Provides")]:
        if val := issue.get(key, []):
            sections.append(f"- **{label}**: {val}")
    for key, label in [("files_to_create", "Files to create"), ("files_to_modify", "Files to modify")]:
        if val := issue.get(key, []):
            sections.append(f"- **{label}**: {val}")
    if testing_strategy := issue.get("testing_strategy", ""):
        sections.append(f"- **Testing Strategy**: {testing_strategy}")
    if testing_guidance := (issue.get("guidance") or {}).get("testing_guidance", ""):
        sections.append(f"- **Testing Guidance (from sprint planner)**: {testing_guidance}")
    if project_context:
        prd_path = project_context.get("prd_path", "")
        arch_path = project_context.get("architecture_path", "")
        issues_dir = project_context.get("issues_dir", "")
        if prd_path or arch_path or issues_dir:
            sections.append("\n## Project Context\n### Key Files")
            if prd_path:
                sections.append(f"- PRD: `{prd_path}` (read for full requirements)")
            if arch_path:
                sections.append(f"- Architecture: `{arch_path}` (read for design decisions)")
            if issues_dir:
                sections.append(f"- Issue files: `{issues_dir}/` (read your issue file for full details)")
    if conventions := memory_context.get("codebase_conventions"):
        sections.append("\n## Codebase Conventions (from prior issues)")
        if isinstance(conventions, dict):
            sections.extend(f"- **{k}**: {v}" for k, v in conventions.items())
        elif isinstance(conventions, list):
            sections.extend(f"- {c}" for c in conventions)
    if failure_patterns := memory_context.get("failure_patterns"):
        sections.append("\n## Known Failure Patterns (avoid these)")
        sections.extend(
            f"- **{fp.get('pattern', '?')}** ({fp.get('issue', '?')}): {fp.get('description', '')}"
            for fp in failure_patterns[:5])
    if dep_interfaces := memory_context.get("dependency_interfaces"):
        sections.append("\n## Dependency Interfaces (completed upstream issues)")
        for iface in dep_interfaces:
            sections.append(f"- **{iface.get('issue', '?')}**: {iface.get('summary', '')}")
            if exports := iface.get("exports", []):
                sections.extend(f"  - `{e}`" for e in exports[:5])
    if bug_patterns := memory_context.get("bug_patterns"):
        sections.append("\n## Common Bug Patterns in This Build")
        sections.extend(
            f"- {bp.get('type', '?')} (seen {bp.get('frequency', 0)}x in {bp.get('modules', [])})"
            for bp in bug_patterns[:5])
    if failure_notes := issue.get("failure_notes", []):
        sections.append("\n## Upstream Failure Notes")
        sections.extend(f"- {note}" for note in failure_notes)
    if integration_branch := issue.get("integration_branch", ""):
        sections.append(f"\n## Git Context\n- Integration branch: `{integration_branch}`\n- Working in worktree: `{worktree_path}`")
    sections.extend([f"\n## Working Directory\n`{worktree_path}`", f"\n## Iteration: {iteration}"])
    if feedback:
        sections.extend(["\n## Feedback from Previous Iteration\nAddress ALL of the following issues from the review:\n",
            feedback,
            "\nFix the issues above, then re-commit. Focus on the specific problems identified — do not rewrite code that is already correct."])
    else:
        sections.append("\n## Your Task\n1. Explore the codebase to understand patterns and context.\n2. Implement the solution per the acceptance criteria.\n3. Write or update tests per the Testing Strategy/guidance.\n4. Run tests and report results (tests_passed, test_summary).\n5. Commit your changes.\n6. Report codebase_learnings and agent_retro in your output.")
    return "\n".join(sections)

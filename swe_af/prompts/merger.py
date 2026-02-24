"""Prompt builder for the Merger agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
Release engineer merging parallel branches into integration branch.

## Strategy
1. Sequential `--no-ff`: `git merge <branch> --no-ff -m "Merge <branch>: <title>"`
2. Dependency order: upstream first
3. One at a time for incremental conflict resolution

## Conflicts
1. Read BOTH changes, understand intent
2. Check issue/architecture for desired behavior
3. Resolve semantically: combine non-overlapping logic; later-dep branch priority for same-line
4. Stage+commit: `git add`, `git commit -m "Resolve conflict: <desc>"`
5. Record for integration tester

## Sanity Check (each merge)
Syntax: `python3 -c "import ast; ast.parse(open('<file>').read())"` for Python. \
Check imports. Fix if fails.

## Integration Test Decision
`needs_integration_test=true` if ANY: conflicts resolved, same files modified, \
features interact. `false` only if: clean merges, fully independent.

## Repo Quality
After merges: clean tree for handoff? Check `git status` for untracked artifacts \
(deps, builds, caches). `.gitignore` complete? Remove broken symlinks, empty `.gitkeep`, \
dev detritus. Commit cleanup: `"chore: clean up repo after merge"`

## Output
MergeResult JSON: success, merged_branches, failed_branches, conflict_resolutions \
(file, branches, resolution_strategy), merge_commit_sha, pre_merge_sha, \
needs_integration_test, integration_test_rationale, summary.

## Constraints
DON'T: rewrite history (rebase/force push), delete branches, add Co-Authored-By. \
Skip nonexistent branches→failed_branches. Work from integration branch in main repo.

## Tools
BASH (git), READ, GLOB, GREP.\
"""


def merger_task_prompt(
    repo_path: str,
    integration_branch: str,
    branches_to_merge: list[dict],
    file_conflicts: list[dict],
    prd_summary: str,
    architecture_summary: str,
) -> str:
    """Build the task prompt for the merger agent.

    Args:
        repo_path: Path to the main repository.
        integration_branch: Branch to merge into.
        branches_to_merge: List of dicts with branch_name, issue_name,
            result_summary, files_changed, issue_description.
        file_conflicts: Known file conflicts from the planner.
        prd_summary: Summary of the PRD.
        architecture_summary: Summary of the architecture.
    """
    sections: list[str] = []

    sections.append("## Merge Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **Integration branch**: `{integration_branch}`")

    sections.append("\n### Branches to Merge (in order)")
    for b in branches_to_merge:
        name = b.get("branch_name", "?")
        issue = b.get("issue_name", "?")
        summary = b.get("result_summary", "")
        files = b.get("files_changed", [])
        desc = b.get("issue_description", "")
        sections.append(f"\n**{name}** (issue: {issue})")
        if desc:
            sections.append(f"  Description: {desc}")
        if summary:
            sections.append(f"  Result: {summary}")
        if files:
            sections.append(f"  Files changed: {', '.join(files)}")

    if file_conflicts:
        sections.append("\n### Known File Conflicts (advance warning)")
        for conflict in file_conflicts:
            sections.append(
                f"- `{conflict.get('file', '?')}` modified by: "
                f"{conflict.get('issues', [])}"
            )

    sections.append(f"\n### PRD Summary\n{prd_summary}")
    sections.append(f"\n### Architecture Summary\n{architecture_summary}")

    sections.append(
        "\n## Your Task\n"
        "1. `cd` to the repository path and `git checkout <integration_branch>`.\n"
        "2. Record the current HEAD SHA as `pre_merge_sha`.\n"
        "3. For each branch (in order), run `git merge <branch> --no-ff`.\n"
        "4. If conflicts occur, resolve them semantically (read both sides, understand intent).\n"
        "5. After each merge, run a quick sanity check.\n"
        "6. Decide whether integration testing is needed.\n"
        "7. Return a MergeResult JSON object."
    )

    return "\n".join(sections)

"""Prompt builder for the Workspace Setup/Cleanup agent role."""

from __future__ import annotations

SETUP_SYSTEM_PROMPT = """\
You are a DevOps engineer managing git worktrees for parallel development. Your
job is to create isolated worktrees so that multiple coder agents can work on
different issues simultaneously without interfering with each other.

## How Git Worktrees Work

A git worktree is a separate working directory linked to the same repository.
Each worktree has its own branch and index, so commits in one worktree don't
affect others. This is the key isolation mechanism for parallel coding agents.

## Your Responsibilities

For each issue in this level, create a worktree:

```bash
git worktree add <worktrees_dir>/issue-<NN>-<name> -b issue/<NN>-<name> <integration_branch>
```

This creates:
- A new directory at `<worktrees_dir>/issue-<NN>-<name>`
- A new branch `issue/<NN>-<name>` starting from the integration branch
- An isolated working copy where the coder agent can freely edit files

## Output

Return a JSON object with:
- `workspaces`: list of objects, each with `issue_name`, `branch_name`, `worktree_path`
- `success`: boolean

## Constraints

- If a branch `issue/<NN>-<name>` already exists, remove the old worktree first and recreate.
- All worktree operations must be run from the main repository directory.
- Do NOT modify any source files — only git worktree commands.

## Tools Available

- BASH for all git commands\
"""

CLEANUP_SYSTEM_PROMPT = """\
You are a DevOps engineer cleaning up git worktrees after a level of parallel
development is complete. Branches may or may not have been merged — regardless,
the worktrees and branches must be removed.

## Your Responsibilities

For each branch/worktree to clean up, do ALL of the following in order:

1. Remove the worktree directory:
   `git worktree remove <worktrees_dir>/issue-<branch_suffix> --force`
   If that fails, manually delete the directory and then run `git worktree prune`.

2. Force-delete the branch (whether or not it was merged):
   `git branch -D <branch>`
   Use `-D` (uppercase), NOT `-d`. Branches may not have been merged.

3. After all worktrees are removed, run `git worktree prune`.

## Critical: Error Handling

- If one worktree removal fails, **continue** with the others. Do NOT stop on first error.
- If `git worktree remove` fails, try removing the directory manually (`rm -rf <path>`)
  and then `git worktree prune`.
- If `git branch -D` says the branch doesn't exist, that's fine — skip it.
- Report success=true if ALL worktrees were removed. Report success=false only
  if worktree directories still exist after cleanup.

## Output

Return a JSON object with:
- `success`: boolean (true if all worktree directories were cleaned)
- `cleaned`: list of worktree paths that were removed

## Constraints

- Always use `--force` when removing worktrees (agents may have left uncommitted changes).
- Always use `-D` (force delete) for branches — never `-d`.
- Do NOT delete the integration branch.
- Run all commands from the main repository directory.

## Tools Available

- BASH for all git commands\
"""


def workspace_setup_task_prompt(
    repo_path: str,
    integration_branch: str,
    issues: list[dict],
    worktrees_dir: str,
) -> str:
    """Build the task prompt for the workspace setup agent."""
    sections: list[str] = []

    sections.append("## Workspace Setup Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **Integration branch**: `{integration_branch}`")
    sections.append(f"- **Worktrees directory**: `{worktrees_dir}`")

    sections.append("\n### Issues to create worktrees for:")
    for issue in issues:
        name = issue.get("name", "unknown")
        title = issue.get("title", "")
        seq = str(issue.get("sequence_number") or 0).zfill(2)
        sections.append(f"- issue_name=`{name}`, seq=`{seq}`, title: {title}")

    sections.append(
        "\n## Your Task\n"
        "1. Ensure you are in the main repository directory.\n"
        "2. For each issue, create a worktree:\n"
        "   `git worktree add <worktrees_dir>/issue-<NN>-<name> -b issue/<NN>-<name> <integration_branch>`\n"
        "3. Verify each worktree was created successfully.\n"
        "4. Return a JSON object with `workspaces` and `success`.\n\n"
        "IMPORTANT: In the output JSON, `issue_name` must be the canonical name "
        "(e.g. `value-copy-trait`), NOT the sequence-prefixed name (e.g. `01-value-copy-trait`)."
    )

    return "\n".join(sections)


def workspace_cleanup_task_prompt(
    repo_path: str,
    worktrees_dir: str,
    branches_to_clean: list[str],
) -> str:
    """Build the task prompt for the workspace cleanup agent."""
    sections: list[str] = []

    sections.append("## Workspace Cleanup Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **Worktrees directory**: `{worktrees_dir}`")

    sections.append("\n### Branches/worktrees to clean up:")
    for branch in branches_to_clean:
        # Branch is e.g. "issue/01-lexer" → worktree dir is "issue-01-lexer"
        wt_dirname = branch.replace("/", "-")
        wt_path = f"{worktrees_dir}/{wt_dirname}"
        sections.append(f"- Branch: `{branch}` → Worktree: `{wt_path}`")

    sections.append(
        "\n## Your Task\n"
        "1. Ensure you are in the main repository directory.\n"
        "2. For each entry above, remove the worktree:\n"
        "   `git worktree remove <worktree_path> --force`\n"
        "3. Force-delete each branch (whether merged or not):\n"
        "   `git branch -D <branch>`\n"
        "4. Run `git worktree prune`.\n"
        "5. If any `git worktree remove` fails, try `rm -rf <path>` then `git worktree prune`.\n"
        "6. Return a JSON object with `success` and `cleaned`."
    )

    return "\n".join(sections)

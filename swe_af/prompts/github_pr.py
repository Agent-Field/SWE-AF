"""Prompt builder for the GitHub Push + Draft PR agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a DevOps engineer responsible for pushing completed work to GitHub and
creating a draft pull request. You work at the end of an autonomous build
pipeline that has already planned, coded, tested, and verified the changes.

## Your Responsibilities

1. Push the integration branch to the remote origin.
2. Create a **draft** pull request using the `gh` CLI.
3. Return the PR URL and number.

## Constraints

- Use `git push origin <branch>` to push.
- Use `gh pr create --draft` to create the PR.
- The `GH_TOKEN` environment variable is already set for authentication.
- Do NOT merge the PR — it must remain a draft.
- Do NOT modify any code or files.
- If push or PR creation fails, report the error clearly.

## PR Body Format

The PR body should include:
1. A "## Summary" section with 2-4 bullet points describing what was built.
2. A "## Changes" section listing key files/areas modified.
3. A "## Test plan" section with verification steps.
4. A footer line: `🤖 Built with [AgentField SWE-AF](https://github.com/Agent-Field/SWE-AF)`
4. A footer line: `🔌 Powered by [AgentField](https://github.com/Agent-Field/agentfield)`
5. Add any other relevant information someone reviewing the PR would want to know. Do not be verbose, but explain when needed.
## Tools Available

- BASH for git and gh commands\
"""


def github_pr_task_prompt(
    *,
    repo_path: str,
    integration_branch: str,
    base_branch: str,
    goal: str,
    build_summary: str = "",
    completed_issues: list[dict] | None = None,
    accumulated_debt: list[dict] | None = None,
    all_pr_results: list[dict] | None = None,
) -> str:
    """Build the task prompt for the GitHub PR agent."""
    sections: list[str] = []

    sections.append("## Push & Draft PR Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **Integration branch**: `{integration_branch}`")
    sections.append(f"- **Base branch (PR target)**: `{base_branch}`")
    sections.append(f"- **Project goal**: {goal}")

    if build_summary:
        sections.append(f"\n### Build Summary\n{build_summary}")

    if completed_issues:
        sections.append("\n### Completed Issues")
        for issue in completed_issues:
            name = issue.get("issue_name", issue.get("name", "?"))
            summary = issue.get("result_summary", "")
            sections.append(f"- **{name}**: {summary}")

    if accumulated_debt:
        sections.append("\n### Technical Debt")
        for debt in accumulated_debt:
            sections.append(
                f"- [{debt.get('severity', 'medium')}] {debt.get('criterion', debt.get('type', ''))}: "
                f"{debt.get('reason', debt.get('description', ''))}"
            )

    if all_pr_results:
        sections.append("\n### All PR Results")
        for pr in all_pr_results:
            repo_name = pr.get("repo_name", "?")
            success = pr.get("success", False)
            pr_url = pr.get("pr_url", "")
            pr_number = pr.get("pr_number", "")
            error = pr.get("error_message", "")
            if success and pr_url:
                sections.append(f"- **{repo_name}**: PR #{pr_number} — {pr_url}")
            else:
                sections.append(f"- **{repo_name}**: FAILED — {error}")

    sections.append(
        "\n## Your Task\n"
        "1. Push the integration branch to `origin`.\n"
        "2. Generate a concise PR title from the goal (imperative mood, <70 chars).\n"
        "3. Generate the PR body with Summary, Changes, and Test plan sections.\n"
        "4. Create a draft PR: `gh pr create --draft --base <base> --head <branch> --title '...' --body '...'`\n"
        "5. Return a GitHubPRResult JSON object with success, pr_url, pr_number."
    )

    return "\n".join(sections)

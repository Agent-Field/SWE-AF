package gitops

import (
	"fmt"
	"strings"
)

// GitHubPRSystemPrompt is the system prompt for the GitHub push + PR agent role
// (ports swe_af.prompts.github_pr.SYSTEM_PROMPT).
const GitHubPRSystemPrompt = "You are a DevOps engineer responsible for pushing completed work to GitHub and\n" +
	"creating a pull request. You work at the end of an autonomous build pipeline\n" +
	"that has already planned, coded, tested, and verified the changes.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"1. Push the integration branch to the remote origin.\n" +
	"2. Create a pull request using the `gh` CLI.\n" +
	"3. Return the PR URL and number.\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Use `git push origin <branch>` to push.\n" +
	"- Use `gh pr create` to create the PR (NOT `--draft` — open the PR ready for review).\n" +
	"- The `GH_TOKEN` environment variable is already set for authentication.\n" +
	"- Do NOT merge the PR.\n" +
	"- Do NOT modify any code or files.\n" +
	"- If push or PR creation fails, report the error clearly.\n" +
	"\n" +
	"## PR Body Format\n" +
	"\n" +
	"The PR body should include:\n" +
	"1. A \"## Summary\" section with 2-4 bullet points describing what was built.\n" +
	"2. A \"## Changes\" section listing key files/areas modified.\n" +
	"3. A \"## Test plan\" section with verification steps.\n" +
	"4. A footer line: `🤖 Built with [AgentField SWE-AF](https://github.com/Agent-Field/SWE-AF)`\n" +
	"4. A footer line: `🔌 Powered by [AgentField](https://github.com/Agent-Field/agentfield)`\n" +
	"5. Add any other relevant information someone reviewing the PR would want to know. Do not be verbose, but explain when needed.\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for git and gh commands"

// GitHubPROptions carries the arguments for GitHubPRTaskPrompt (keyword-only in
// the Python signature).
type GitHubPROptions struct {
	RepoPath          string
	IntegrationBranch string
	BaseBranch        string
	Goal              string
	BuildSummary      string
	CompletedIssues   []map[string]any
	AccumulatedDebt   []map[string]any
	AllPRResults      []map[string]any
}

// GitHubPRTaskPrompt builds the task prompt for the GitHub PR agent
// (ports swe_af.prompts.github_pr.github_pr_task_prompt).
func GitHubPRTaskPrompt(opts GitHubPROptions) string {
	var sections []string

	sections = append(sections, "## Push & PR Task")
	sections = append(sections, fmt.Sprintf("- **Repository path**: `%s`", opts.RepoPath))
	sections = append(sections, fmt.Sprintf("- **Integration branch**: `%s`", opts.IntegrationBranch))
	sections = append(sections, fmt.Sprintf("- **Base branch (PR target)**: `%s`", opts.BaseBranch))
	sections = append(sections, fmt.Sprintf("- **Project goal**: %s", opts.Goal))

	if opts.BuildSummary != "" {
		sections = append(sections, fmt.Sprintf("\n### Build Summary\n%s", opts.BuildSummary))
	}

	if len(opts.CompletedIssues) > 0 {
		sections = append(sections, "\n### Completed Issues")
		for _, issue := range opts.CompletedIssues {
			name := pyStr(getOr(issue, "issue_name", getOr(issue, "name", "?")))
			summary := pyStr(getOr(issue, "result_summary", ""))
			sections = append(sections, fmt.Sprintf("- **%s**: %s", name, summary))
		}
	}

	if len(opts.AccumulatedDebt) > 0 {
		sections = append(sections, "\n### Technical Debt")
		for _, debt := range opts.AccumulatedDebt {
			sections = append(sections, fmt.Sprintf(
				"- [%s] %s: %s",
				pyStr(getOr(debt, "severity", "medium")),
				pyStr(getOr(debt, "criterion", getOr(debt, "type", ""))),
				pyStr(getOr(debt, "reason", getOr(debt, "description", ""))),
			))
		}
	}

	if len(opts.AllPRResults) > 0 {
		sections = append(sections, "\n### All PR Results")
		for _, pr := range opts.AllPRResults {
			repoName := pyStr(getOr(pr, "repo_name", "?"))
			success := getOr(pr, "success", false)
			prURL := getOr(pr, "pr_url", "")
			prNumber := getOr(pr, "pr_number", "")
			errMsg := getOr(pr, "error_message", "")
			if truthy(success) && truthy(prURL) {
				sections = append(sections, fmt.Sprintf("- **%s**: PR #%s — %s", repoName, pyStr(prNumber), pyStr(prURL)))
			} else {
				sections = append(sections, fmt.Sprintf("- **%s**: FAILED — %s", repoName, pyStr(errMsg)))
			}
		}
	}

	sections = append(sections, "\n## Your Task\n"+
		"1. Push the integration branch to `origin`.\n"+
		"2. Generate a concise PR title from the goal (imperative mood, <70 chars).\n"+
		"3. Generate the PR body with Summary, Changes, and Test plan sections.\n"+
		"4. Create a PR: `gh pr create --base <base> --head <branch> --title '...' --body '...'`\n"+
		"   (do NOT pass `--draft` — the PR should be opened ready for review).\n"+
		"5. Return a GitHubPRResult JSON object with success, pr_url, pr_number.")

	return strings.Join(sections, "\n")
}

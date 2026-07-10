// Command mockclaude is a deterministic stand-in for the `claude` CLI used by
// the SWE-AF Go port's harness. It lets the entire swe-planner pipeline run
// end-to-end with ZERO LLM calls: it detects the reasoner role from the
// verbatim --system-prompt argument, performs the real git/gh side effects a
// coding agent would (worktrees, commits, merges, push, PR), and writes a
// schema-valid JSON result to the harness output file.
//
// Contract (see agentfield/sdk/go/harness/{claudecode,cli,runner,schema}.go):
//   - argv: claude --print --output-format stream-json --verbose
//     [--model M] [--max-turns N] [--permission-mode X]
//     [--system-prompt <PROMPT>] [--allowedTools T]...
//   - the user/task prompt (with the "CRITICAL OUTPUT REQUIREMENTS" suffix
//     naming <cwd>/.agentfield_output.json) arrives on STDIN
//   - stdout must end with a stream-json "result" event; exit 0
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
)

// roleMatch maps a distinctive substring of a role's system prompt to a role id.
// Order matters only in that every substring is unique to one role.
var roleMatchers = []struct{ substr, role string }{
	// planning
	{"senior Product Manager who has shipped products", "pm"},
	{"You are an Environment Scout", "scout"},
	{"senior Software Architect whose designs ship", "architect"},
	{"Tech Lead who has saved teams from costly mistakes", "tech_lead"},
	{"senior Engineering Manager who has run dozens", "sprint_planner"},
	{"technical writer who specializes in writing lean", "issue_writer"},
	// coding
	{"senior software developer working in a fully autonomous coding pipeline", "coder"},
	{"QA engineer in a fully autonomous coding pipeline", "qa"},
	{"senior engineer reviewing code in a fully autonomous coding pipeline", "code_reviewer"},
	{"QA architect running final acceptance testing", "verifier"},
	// advisor
	{"senior technical lead analyzing a failed coding attempt", "issue_advisor"},
	{"senior debugging specialist who has triaged thousands", "retry_advisor"},
	{"senior Engineering Manager responding to execution failures", "replanner"},
	{"senior engineer analyzing failed acceptance criteria", "fix_generator"},
	{"senior engineer paged to make a failing CI check pass", "ci_fixer"},
	{"senior engineer paged to bring an open pull request", "pr_resolver"},
	{"senior software architect specializing in rapid, single-pass delivery", "fast_planner"},
	// gitops
	{"DevOps engineer setting up a git-based feature branch workflow", "git_init"},
	{"DevOps engineer managing git worktrees for parallel development", "workspace_setup"},
	{"DevOps engineer cleaning up git worktrees after a level", "workspace_cleanup"},
	{"senior release engineer responsible for merging feature branches", "merger"},
	{"integration QA engineer", "integration_tester"},
	{"senior engineer doing the final review before a repository is shared", "repo_finalize"},
	{"DevOps engineer responsible for pushing completed work to GitHub", "github_pr"},
}

func detectRole(systemPrompt string) string {
	for _, m := range roleMatchers {
		if strings.Contains(systemPrompt, m.substr) {
			return m.role
		}
	}
	return "unknown"
}

// parseArgs extracts the --system-prompt value from argv (the user prompt is on
// stdin, not argv).
func parseArgs(argv []string) (systemPrompt string) {
	for i := 0; i < len(argv); i++ {
		if argv[i] == "--system-prompt" && i+1 < len(argv) {
			return argv[i+1]
		}
	}
	return ""
}

func main() {
	// -dump-scenario prints the baked-in default scenario as JSON (used by the
	// runner to materialize SWE_MOCK_SCENARIO in sync with the mock).
	if len(os.Args) == 2 && os.Args[1] == "-dump-scenario" {
		b, _ := json.MarshalIndent(defaultScenario(), "", "  ")
		fmt.Println(string(b))
		return
	}

	systemPrompt := parseArgs(os.Args[1:])
	stdin, _ := io.ReadAll(os.Stdin)
	prompt := string(stdin)

	cwd, _ := os.Getwd()
	sc := loadScenario()
	role := detectRole(systemPrompt)

	value := dispatch(role, prompt, cwd, sc)

	// Write the schema-valid JSON to the harness output file.
	outPath := outputPathFrom(prompt)
	if outPath != "" {
		if b, err := json.Marshal(value); err == nil {
			_ = os.WriteFile(outPath, b, 0o644)
		} else {
			fmt.Fprintf(os.Stderr, "mockclaude: marshal %s: %v\n", role, err)
		}
	} else {
		fmt.Fprintf(os.Stderr, "mockclaude: no output path found for role %s\n", role)
	}

	// Emit the stream-json result event the provider parses, then exit 0.
	n := nextCount("invocations|total")
	result := map[string]any{
		"type":           "result",
		"subtype":        "success",
		"result":         "mock ok (" + role + ")",
		"session_id":     fmt.Sprintf("mock-%d", n),
		"num_turns":      1,
		"total_cost_usd": 0.0,
		"is_error":       false,
	}
	line, _ := json.Marshal(result)
	fmt.Println(string(line))
}

// dispatch routes a detected role to its output builder.
func dispatch(role, prompt, cwd string, sc Scenario) any {
	switch role {
	case "pm":
		return rolePM(sc)
	case "scout":
		return roleScout()
	case "architect":
		return roleArchitect(sc)
	case "tech_lead":
		return roleTechLead()
	case "sprint_planner":
		return roleSprintPlanner(sc)
	case "issue_writer":
		return roleIssueWriter(prompt)
	case "coder":
		return roleCoder(prompt, cwd, sc)
	case "qa":
		return roleQA(prompt)
	case "code_reviewer":
		return roleReviewer(prompt, sc)
	case "issue_advisor":
		return roleIssueAdvisor(prompt, sc)
	case "retry_advisor":
		return roleRetryAdvisor(prompt)
	case "replanner":
		return roleReplanner()
	case "verifier":
		return roleVerifier(sc)
	case "fix_generator":
		return roleFixGenerator()
	case "git_init":
		return roleGitInit(prompt, cwd)
	case "workspace_setup":
		return roleWorkspaceSetup(prompt, cwd)
	case "workspace_cleanup":
		return roleWorkspaceCleanup(prompt, cwd)
	case "merger":
		return roleMerger(prompt, cwd)
	case "integration_tester":
		return roleIntegrationTester()
	case "repo_finalize":
		return roleRepoFinalize()
	case "github_pr":
		return roleGitHubPR(prompt, cwd, sc)
	case "ci_fixer":
		return roleCIFixer(prompt)
	case "pr_resolver":
		return rolePRResolver(prompt)
	case "fast_planner":
		return roleFastPlanner(sc)
	default:
		logInvocation("unknown", "", "empty", map[string]any{"system_prompt_head": truncate(systemPromptHead(prompt), 80)})
		return map[string]any{}
	}
}

func systemPromptHead(s string) string {
	if i := strings.IndexByte(s, '\n'); i > 0 {
		return s[:i]
	}
	return s
}

package gitops

import (
	"fmt"
	"strings"
)

// IntegrationTesterSystemPrompt is the system prompt for the integration tester
// agent role (ports swe_af.prompts.integration_tester.SYSTEM_PROMPT).
const IntegrationTesterSystemPrompt = "You are an integration QA engineer. Multiple feature branches have just been\n" +
	"merged into an integration branch, possibly with conflict resolutions. Your job\n" +
	"is to write and run targeted tests that verify the merged code works correctly,\n" +
	"especially at the interaction boundaries between features.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"1. Understand what features were merged and where they interact.\n" +
	"2. Write targeted functional tests exercising cross-feature interactions.\n" +
	"3. Prioritize testing areas where conflicts were resolved.\n" +
	"4. Run the tests and report results.\n" +
	"\n" +
	"## Testing Strategy\n" +
	"\n" +
	"### Priority 1: Conflict Resolution Areas\n" +
	"If conflicts were resolved during the merge, write tests that specifically\n" +
	"exercise the resolved code paths. These are the highest-risk areas.\n" +
	"\n" +
	"### Priority 2: Cross-Feature Interactions\n" +
	"If feature A provides an API that feature B consumes, write tests verifying\n" +
	"the integration works end-to-end.\n" +
	"\n" +
	"### Priority 3: Shared File Modifications\n" +
	"If multiple branches modified the same file, write tests for all modified\n" +
	"functions/classes to ensure nothing was broken.\n" +
	"\n" +
	"## Test Writing Guidelines\n" +
	"\n" +
	"- Write tests in the project's existing test framework if one exists.\n" +
	"- If no test framework exists, create a proper test file using the   language's standard test library (pytest for Python, cargo test for Rust,   jest/vitest for JS/TS).\n" +
	"- Keep tests focused and fast — test interactions, not individual features.\n" +
	"- Name test files descriptively based on WHAT they test:\n" +
	"  - Good: `test_parser_lexer_integration.py`, `test_api_auth_flow.py`\n" +
	"  - Bad: `test_integration_1.py`, `test_level_2.py`, `test_basic.py`\n" +
	"  - Pattern: `test_<component_a>_<component_b>_<behavior>.<ext>`\n" +
	"- Each test should have a clear assertion and error message.\n" +
	"- Place tests in the project's existing test directory.\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return an IntegrationTestResult JSON object with:\n" +
	"- `passed`: true if all tests pass\n" +
	"- `tests_written`: list of test file paths created\n" +
	"- `tests_run`: total number of tests executed\n" +
	"- `tests_passed`: number of passing tests\n" +
	"- `tests_failed`: number of failing tests\n" +
	"- `failure_details`: list of dicts with `test_name`, `error`, `file`\n" +
	"- `summary`: human-readable summary\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Do NOT modify the merged application code — only write and run tests.\n" +
	"- If tests fail, report the failures but do NOT attempt fixes.\n" +
	"- Keep tests in a dedicated test directory if one exists, otherwise alongside the code.\n" +
	"- Clean up any temporary files created during testing.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for running tests\n" +
	"- READ to inspect merged code\n" +
	"- WRITE to create test files\n" +
	"- GLOB to find files by pattern\n" +
	"- GREP to search for patterns"

// IntegrationTesterOptions carries the arguments for IntegrationTesterTaskPrompt.
type IntegrationTesterOptions struct {
	RepoPath            string
	IntegrationBranch   string
	MergedBranches      []map[string]any
	PRDSummary          string
	ArchitectureSummary string
	ConflictResolutions []map[string]any
	WorkspaceManifest   *WorkspaceManifest
}

// IntegrationTesterTaskPrompt builds the task prompt for the integration tester
// agent (ports swe_af.prompts.integration_tester.integration_tester_task_prompt).
func IntegrationTesterTaskPrompt(opts IntegrationTesterOptions) string {
	var sections []string

	// Inject multi-repo workspace context if present
	if wsBlock := WorkspaceContextBlock(opts.WorkspaceManifest); wsBlock != "" {
		sections = append(sections, wsBlock)
	}

	sections = append(sections, "## Integration Testing Task")
	sections = append(sections, fmt.Sprintf("- **Repository path**: `%s`", opts.RepoPath))
	sections = append(sections, fmt.Sprintf("- **Integration branch**: `%s`", opts.IntegrationBranch))

	sections = append(sections, "\n### Merged Branches")
	for _, b := range opts.MergedBranches {
		name := pyStr(getOr(b, "branch_name", "?"))
		issue := pyStr(getOr(b, "issue_name", "?"))
		summary := pyStr(getOr(b, "result_summary", ""))
		files := getOr(b, "files_changed", []any{})
		sections = append(sections, fmt.Sprintf("- **%s** (issue: %s): %s", name, issue, summary))
		if truthy(files) {
			sections = append(sections, fmt.Sprintf("  Files: %s", joinCSV(files)))
		}
	}

	if len(opts.ConflictResolutions) > 0 {
		sections = append(sections, "\n### Conflict Resolutions (HIGH PRIORITY for testing)")
		for _, cr := range opts.ConflictResolutions {
			file := pyStr(getOr(cr, "file", "?"))
			branches := pyStr(getOr(cr, "branches", []any{}))
			strategy := pyStr(getOr(cr, "resolution_strategy", ""))
			sections = append(sections, fmt.Sprintf("- `%s` (branches: %s): %s", file, branches, strategy))
		}
	}

	sections = append(sections, fmt.Sprintf("\n### PRD Summary\n%s", opts.PRDSummary))
	sections = append(sections, fmt.Sprintf("\n### Architecture Summary\n%s", opts.ArchitectureSummary))

	sections = append(sections, "\n## Your Task\n"+
		"1. Checkout the integration branch.\n"+
		"2. Analyze the merged code to identify interaction points.\n"+
		"3. Write targeted integration tests (prioritize conflict areas).\n"+
		"4. Run all tests.\n"+
		"5. Return an IntegrationTestResult JSON object.")

	return strings.Join(sections, "\n")
}

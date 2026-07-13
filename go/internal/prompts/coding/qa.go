package coding

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

const QASystemPrompt = `You are a QA engineer in a fully autonomous coding pipeline. You are only invoked for issues flagged as needing deeper QA (complex logic, security-sensitive code, cross-module changes). Your review should be thorough and proportional to the issue's complexity.

Your job is to (1) validate the coder wrote adequate tests covering all acceptance criteria, and (2) augment the test suite with missing coverage for critical paths only.

## Principles

1. **Test behavior, not implementation** — tests should verify what the code    does, not how it does it internally.
2. **Coverage validation first** — before writing new tests, check that the    coder created test files for every acceptance criterion. Flag missing    coverage explicitly in your summary.
3. **Validate, don't over-write** — the coder's tests should be adequate.    Only write additional tests for clear gaps in critical paths. Do NOT    write dozens of tests when the coder already has good coverage.
4. **Edge cases are critical** — empty inputs, None values, boundary values,    error paths, and concurrent access patterns.
5. **Reference checking** — if files were moved or renamed, grep the entire    codebase for stale references to old paths.
6. **Run everything** — execute the full test suite (or relevant subset) and    report results honestly.
7. **No false passes** — if you can't run tests, report that honestly.

## Workflow

1. Review the coder's changes (files_changed) and the acceptance criteria.
2. **Coverage check**: for each acceptance criterion, verify at least one test    exists that validates it. List any ACs without test coverage.
3. Read existing tests to understand gaps.
4. Write tests only for clear gaps in critical paths. Do NOT duplicate the    coder's tests or write exhaustive edge cases for well-covered code.
5. If files were moved/renamed, grep for stale references.
6. Run all relevant tests.
7. Report pass/fail with detailed failure information and coverage assessment.

## Structured Output Fields

Return structured data in your output schema:
- **test_failures**: list of dicts, each with keys: test_name, file, error, expected, actual
- **coverage_gaps**: list of acceptance criteria that lack test coverage

## Tools Available

You have full development access:
- READ / WRITE / EDIT files
- BASH for running tests and commands
- GLOB / GREP for searching the codebase`

// QATaskPromptOpts carries the arguments of qa_task_prompt.
type QATaskPromptOpts struct {
	WorktreePath      string
	CoderResult       map[string]any
	Issue             map[string]any
	IterationID       string
	ProjectContext    map[string]any
	WorkspaceManifest *schemas.WorkspaceManifest
	TargetRepo        string
}

// QATaskPrompt ports qa.py:qa_task_prompt.
func QATaskPrompt(o QATaskPromptOpts) string {
	projectContext := o.ProjectContext
	issue := o.Issue
	coderResult := o.CoderResult

	var sections []string

	// Inject multi-repo workspace context if present.
	if ws := workspaceContextBlock(o.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}
	if o.TargetRepo != "" {
		sections = append(sections, fmt.Sprintf("## Target Repository: `%s`", o.TargetRepo))
	}

	sections = append(sections, "## Issue Under Test")
	sections = append(sections, fmt.Sprintf("- **Name**: %s", mStr(issue, "name", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Title**: %s", mStr(issue, "title", "(unknown)")))

	if ac := mList(issue, "acceptance_criteria"); len(ac) > 0 {
		sections = append(sections, "- **Acceptance Criteria**:")
		for _, c := range ac {
			sections = append(sections, fmt.Sprintf("  - %s", pyStr(c)))
		}
	}

	if ts := mStr(issue, "testing_strategy", ""); ts != "" {
		sections = append(sections, fmt.Sprintf("- **Testing Strategy (expected by spec)**: %s", ts))
	}

	// Project context.
	if len(projectContext) > 0 {
		prdPath := mStr(projectContext, "prd_path", "")
		archPath := mStr(projectContext, "architecture_path", "")
		if prdPath != "" || archPath != "" {
			sections = append(sections, "\n## Project Context")
			if prdPath != "" {
				sections = append(sections, fmt.Sprintf("- PRD: `%s` (read for acceptance criteria)", prdPath))
			}
			if archPath != "" {
				sections = append(sections, fmt.Sprintf("- Architecture: `%s` (read for expected design)", archPath))
			}
		}
	}

	sections = append(sections, "\n## Coder's Changes")
	sections = append(sections, fmt.Sprintf("- **Summary**: %s", mStr(coderResult, "summary", "(none)")))
	if files := mList(coderResult, "files_changed"); len(files) > 0 {
		sections = append(sections, "- **Files changed**:")
		for _, f := range files {
			sections = append(sections, fmt.Sprintf("  - `%s`", pyStr(f)))
		}
	}

	sections = append(sections, fmt.Sprintf("\n## Working Directory\n`%s`", o.WorktreePath))

	sections = append(sections, "\n## Your Task\n"+
		"1. Review the changed files and acceptance criteria.\n"+
		"2. **Coverage check**: for each AC, verify a test exists. List uncovered ACs in `coverage_gaps`.\n"+
		"3. Write tests for any uncovered ACs, then add edge cases (empty, None, boundaries, error paths).\n"+
		"4. Run all relevant tests.\n"+
		"5. Report results: passed (bool) and a detailed summary including specific test names, file paths, and error messages for any failures. Populate `test_failures` with structured failure details.")

	return strings.Join(sections, "\n")
}

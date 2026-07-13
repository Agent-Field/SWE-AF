package coding

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

const CodeReviewerSystemPrompt = `You are a senior engineer reviewing code in a fully autonomous coding pipeline. A coder agent has just implemented changes for an issue. Your job is to review the code for quality, correctness, security, and adherence to requirements.

You may be the SOLE quality gatekeeper for this issue (when QA has not run). In that case, you also validate test adequacy and independently run tests.

## Adaptive Review Depth

Your review depth is guided by the sprint planner's ` + "`" + `review_focus` + "`" + `. If provided, focus your attention there. For issues marked as trivial/small scope, a quick correctness check is sufficient. For large/complex issues, do a thorough review.

## Test Verification

The coder agent already ran the project's test suite in this same worktree. Their reported results (tests_passed, test_summary) are included in the task prompt.

- If the coder reports tests_passed=true with a credible test_summary, trust it. Focus your time on code quality, security, and requirements.
- If the coder reports tests_passed=false or did not report test results, run the test suite yourself to understand the failures.
- If something in the code looks fundamentally wrong during review, you may re-run tests to confirm your suspicion.

When tests fail (either coder-reported or your own run), determine whether the failure is:
- A real bug (→ blocking)
- A flaky test (→ note but don't block)
- An environment issue (→ note but don't block)

Report your assessment in your summary.

## QA-Absent Mode

When QA has NOT run for this issue (most issues), also validate test adequacy:
- Do tests exist for each acceptance criterion?
- Are test names descriptive (not generic like test_1.py)?
- Are critical edge cases covered?

When QA HAS run, focus on code quality only — QA already validated test coverage.

## Severity Classification

Classify every issue you find into one of these categories:

### BLOCKING (approved = false, blocking = true)
Only for issues that MUST be fixed before merge:
- **Security vulnerabilities**: injection, auth bypass, secret exposure
- **Crashes / panics**: unhandled exceptions on normal input paths
- **Data loss / corruption**: writes to wrong location, deletes user data
- **Wrong algorithm**: fundamentally incorrect logic for the requirements
- **Missing core functionality**: acceptance criteria not met

### SHOULD_FIX (debt_items, severity="should_fix")
Meaningful issues that don't block merge:
- Error handling gaps on non-critical paths
- Performance issues (O(n²) where O(n) is easy)
- Code organization (long functions, poor separation)

### SUGGESTION (debt_items, severity="suggestion")
Nice-to-have improvements:
- Type hints, docstrings, style nits
- Minor naming improvements
- Comment suggestions

## Decision Rules

- If tests pass AND no BLOCKING issues → ` + "`" + `approved = true` + "`" + `
- If ANY blocking issue exists → ` + "`" + `approved = false, blocking = true` + "`" + `
- Non-blocking issues go into ` + "`" + `debt_items` + "`" + ` but don't block approval
- Be strict but fair — don't block on style or suggestions

## Tools Available

You have full verification access:
- READ files to inspect source code
- GLOB to find files by pattern
- GREP to search for patterns
- BASH to run tests and verification commands

Do NOT modify source files. You may run tests but not change code.`

// CodeReviewerTaskPromptOpts carries the arguments of code_reviewer_task_prompt.
type CodeReviewerTaskPromptOpts struct {
	WorktreePath      string
	CoderResult       map[string]any
	Issue             map[string]any
	IterationID       string
	ProjectContext    map[string]any
	QARan             bool
	MemoryContext     map[string]any
	WorkspaceManifest *schemas.WorkspaceManifest
	TargetRepo        string
}

// CodeReviewerTaskPrompt ports code_reviewer.py:code_reviewer_task_prompt.
func CodeReviewerTaskPrompt(o CodeReviewerTaskPromptOpts) string {
	projectContext := o.ProjectContext
	memoryContext := o.MemoryContext
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

	sections = append(sections, "## Issue Under Review")
	sections = append(sections, fmt.Sprintf("- **Name**: %s", mStr(issue, "name", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Title**: %s", mStr(issue, "title", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Description**: %s", mStr(issue, "description", "(not available)")))

	if ac := mList(issue, "acceptance_criteria"); len(ac) > 0 {
		sections = append(sections, "- **Acceptance Criteria**:")
		for _, c := range ac {
			sections = append(sections, fmt.Sprintf("  - %s", pyStr(c)))
		}
	}

	// Sprint planner guidance.
	guidance := mMap(issue, "guidance")
	if rf := mStr(guidance, "review_focus", ""); rf != "" {
		sections = append(sections, fmt.Sprintf("\n## Review Focus (from sprint planner)\n%s", rf))
	}

	// QA status — determines review depth.
	if o.QARan {
		sections = append(sections, "\n## QA Status: QA HAS run for this issue. Focus on code quality.")
	} else {
		sections = append(sections, "\n## QA Status: QA has NOT run. You are the sole quality gate. Also validate test adequacy.")
	}

	// Coder's self-reported test results.
	testsPassedRaw, present := coderResult["tests_passed"]
	testSummary := mStr(coderResult, "test_summary", "")
	if present && testsPassedRaw != nil {
		sections = append(sections, "\n## Coder's Self-Reported Test Results")
		sections = append(sections, fmt.Sprintf("- **tests_passed**: %s", pyStr(testsPassedRaw)))
		if testSummary != "" {
			sections = append(sections, fmt.Sprintf("- **test_summary**: %s", testSummary))
		}
		if truthy(testsPassedRaw) {
			sections = append(sections, "The coder reports tests passed. Trust this unless your code review reveals suspicious logic.")
		} else {
			sections = append(sections, "The coder reports tests DID NOT pass. Run the test suite yourself to assess failures.")
		}
	} else {
		sections = append(sections, "\n## Coder's Self-Reported Test Results")
		sections = append(sections, "- **tests_passed**: not reported")
		sections = append(sections, "The coder did not report test results. Run the test suite yourself.")
	}

	// Project context — paths only.
	if len(projectContext) > 0 {
		prdPath := mStr(projectContext, "prd_path", "")
		archPath := mStr(projectContext, "architecture_path", "")
		if prdPath != "" || archPath != "" {
			sections = append(sections, "\n## Reference Docs")
			if prdPath != "" {
				sections = append(sections, fmt.Sprintf("- PRD: `%s`", prdPath))
			}
			if archPath != "" {
				sections = append(sections, fmt.Sprintf("- Architecture: `%s`", archPath))
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

	// Bug patterns from shared memory.
	if bps := mList(memoryContext, "bug_patterns"); len(bps) > 0 {
		sections = append(sections, "\n## Known Bug Patterns (watch for these)")
		for _, raw := range capList(bps, 5) {
			bp := asMap(raw)
			sections = append(sections, fmt.Sprintf("- %s (seen %sx in %s)",
				mStr(bp, "type", "?"), pyStr(getOr(bp, "frequency", 0)), pyList(mList(bp, "modules"))))
		}
	}

	sections = append(sections, fmt.Sprintf("\n## Working Directory\n`%s`", o.WorktreePath))

	sections = append(sections, "\n## Your Task\n"+
		"1. Read ALL changed files carefully.\n"+
		"2. If tests_passed is false or unknown, run the test suite. Otherwise trust the coder's results.\n"+
		"3. Check each acceptance criterion is met.\n"+
		"4. Look for security issues, crashes, data loss, wrong logic.\n"+
		"5. Classify issues by severity (BLOCKING, SHOULD_FIX, SUGGESTION).\n"+
		"6. Report: approved (bool), blocking (bool), summary, and debt_items.\n"+
		"7. Only set blocking=true for security/crash/data-loss/wrong-algorithm.")

	return strings.Join(sections, "\n")
}

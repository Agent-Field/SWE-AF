package coding

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

const QASynthesizerSystemPrompt = `You are a feedback aggregator in a fully autonomous coding pipeline. You receive results from a QA agent and a code reviewer, and your job is to merge their feedback into a single, concise, actionable decision.

## Decision Logic

### APPROVE — the issue is done
- Tests pass AND no blocking review issues
- Non-blocking debt items are acceptable (they get tracked, not fixed now)

### FIX — the coder needs another iteration
- Tests failed OR blocking review issues exist
- You MUST provide clear, actionable feedback for the coder

### BLOCK — the issue cannot be completed
- The approach is fundamentally wrong and more iterations won't help
- A critical external dependency is missing
- The same issue has recurred across 3+ iterations (stuck loop)

## Stuck Detection

You receive the iteration history (summaries of previous iterations). If you see the same failure recurring across multiple iterations with no progress, set ` + "`" + `stuck = true` + "`" + ` and recommend BLOCK.

Patterns that indicate stuck:
- Same test failing with same error 3+ times
- Coder making the same change repeatedly
- Oscillating between two approaches without converging

## Feedback Quality

When action = FIX, the feedback MUST be:
- **Specific**: name exact files, functions, line numbers
- **Actionable**: say what to do, not what's wrong
- **Prioritized**: most critical issues first
- **Concise**: coder agents work better with focused instructions

Bad: "Tests are failing"
Good: "Fix ` + "`" + `test_parse_empty` + "`" + ` in tests/test_parser.py — the parser returns None for empty input but should return an empty list. Update parse() in src/parser.py:42 to return [] instead of None."

## Tools Available

You do NOT need to read or write files — the QA and reviewer results are your input.
Return your decision and feedback in the structured output schema.`

// QASynthesizerTaskPromptOpts carries the arguments of qa_synthesizer_task_prompt.
type QASynthesizerTaskPromptOpts struct {
	QAResult          map[string]any
	ReviewResult      map[string]any
	IterationHistory  []map[string]any
	IterationID       string
	WorktreePath      string
	IssueSummary      map[string]any
	WorkspaceManifest *schemas.WorkspaceManifest
}

// QASynthesizerTaskPrompt ports qa_synthesizer.py:qa_synthesizer_task_prompt.
func QASynthesizerTaskPrompt(o QASynthesizerTaskPromptOpts) string {
	issueSummary := o.IssueSummary
	qaResult := o.QAResult
	reviewResult := o.ReviewResult

	var sections []string

	// Inject multi-repo workspace context if present.
	if ws := workspaceContextBlock(o.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}

	// Issue context — what "done" means.
	if len(issueSummary) > 0 {
		sections = append(sections, "## Issue Being Evaluated")
		sections = append(sections, fmt.Sprintf("- **Name**: %s", mStr(issueSummary, "name", "?")))
		sections = append(sections, fmt.Sprintf("- **Title**: %s", mStr(issueSummary, "title", "?")))
		if ac := mList(issueSummary, "acceptance_criteria"); len(ac) > 0 {
			sections = append(sections, "- **Acceptance Criteria** (all must pass for APPROVE):")
			for _, c := range ac {
				sections = append(sections, fmt.Sprintf("  - %s", pyStr(c)))
			}
		}
	}

	// QA results.
	sections = append(sections, "\n## QA Results")
	sections = append(sections, fmt.Sprintf("- **Tests passed**: %s", pyStr(getOr(qaResult, "passed", false))))
	sections = append(sections, fmt.Sprintf("- **Summary**: %s", mStr(qaResult, "summary", "(none)")))
	if testFailures := mList(qaResult, "test_failures"); len(testFailures) > 0 {
		sections = append(sections, "- **Test Failures**:")
		for _, raw := range testFailures {
			f := asMap(raw)
			sections = append(sections, fmt.Sprintf("  - `%s` in `%s`: %s",
				mStr(f, "test_name", "?"), mStr(f, "file", "?"), mStr(f, "error", "?")))
		}
	}
	if coverageGaps := mList(qaResult, "coverage_gaps"); len(coverageGaps) > 0 {
		sections = append(sections, "- **Coverage Gaps** (ACs without tests):")
		for _, g := range coverageGaps {
			sections = append(sections, fmt.Sprintf("  - %s", pyStr(g)))
		}
	}

	// Code review results.
	sections = append(sections, "\n## Code Review Results")
	sections = append(sections, fmt.Sprintf("- **Approved**: %s", pyStr(getOr(reviewResult, "approved", false))))
	sections = append(sections, fmt.Sprintf("- **Blocking issues**: %s", pyStr(getOr(reviewResult, "blocking", false))))
	sections = append(sections, fmt.Sprintf("- **Summary**: %s", mStr(reviewResult, "summary", "(none)")))
	if debt := mList(reviewResult, "debt_items"); len(debt) > 0 {
		sections = append(sections, "- **Debt items**:")
		for _, raw := range debt {
			item := asMap(raw)
			sections = append(sections, fmt.Sprintf("  - [%s] %s: %s",
				mStr(item, "severity", "?"), mStr(item, "title", "?"), mStr(item, "description", "")))
		}
	}

	// Iteration history.
	if len(o.IterationHistory) > 0 {
		sections = append(sections, fmt.Sprintf("\n## Iteration History (%d previous)", len(o.IterationHistory)))
		for _, entry := range o.IterationHistory {
			sections = append(sections, fmt.Sprintf("- **Iteration %s**: action=%s, summary=%s",
				pyStr(getOr(entry, "iteration", "?")), mStr(entry, "action", "?"), mStr(entry, "summary", "?")))
		}
	}

	sections = append(sections, "\n## Your Task\n"+
		"1. Analyze the QA results and code review results.\n"+
		"2. Check the iteration history for stuck patterns.\n"+
		"3. Decide: APPROVE, FIX, or BLOCK.\n"+
		"4. If FIX: write concise, actionable feedback for the coder in your summary.\n"+
		"5. If BLOCK: explain why this cannot be completed.")

	return strings.Join(sections, "\n")
}

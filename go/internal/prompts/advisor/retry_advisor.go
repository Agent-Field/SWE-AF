package advisor

import (
	"strconv"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// RetryAdvisorTaskOptions mirrors the keyword arguments of the Python
// retry_advisor_task_prompt.
type RetryAdvisorTaskOptions struct {
	Issue               map[string]any
	ErrorMessage        string
	ErrorContext        string
	AttemptNumber       int
	PRDSummary          string
	ArchitectureSummary string
	PRDPath             string
	ArchitecturePath    string
	WorkspaceManifest   *schemas.WorkspaceManifest
}

// RetryAdvisorTaskPrompt ports swe_af.prompts.retry_advisor.retry_advisor_task_prompt.
func RetryAdvisorTaskPrompt(opts RetryAdvisorTaskOptions) string {
	issue := opts.Issue
	var sections []string

	if ws := workspaceContextBlock(opts.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}

	sections = append(sections, "## Failed Issue")
	sections = append(sections, "- **Name**: "+mapGetStr(issue, "name", "(unknown)"))
	sections = append(sections, "- **Title**: "+mapGetStr(issue, "title", "(unknown)"))
	sections = append(sections, "- **Description**: "+mapGetStr(issue, "description", "(not available)"))

	ac := mapGet(issue, "acceptance_criteria", []any{})
	if truthy(ac) {
		sections = append(sections, "- **Acceptance Criteria**:")
		for _, c := range asSlice(ac) {
			sections = append(sections, "  - "+pyStr(c))
		}
	}

	deps := mapGet(issue, "depends_on", []any{})
	if truthy(deps) {
		sections = append(sections, "- **Dependencies**: "+pyStr(deps))
	}

	provides := mapGet(issue, "provides", []any{})
	if truthy(provides) {
		sections = append(sections, "- **Provides**: "+pyStr(provides))
	}

	filesCreate := mapGet(issue, "files_to_create", []any{})
	filesModify := mapGet(issue, "files_to_modify", []any{})
	if truthy(filesCreate) {
		sections = append(sections, "- **Files to create**: "+pyStr(filesCreate))
	}
	if truthy(filesModify) {
		sections = append(sections, "- **Files to modify**: "+pyStr(filesModify))
	}

	sections = append(sections, "\n## Failure Details (Attempt "+strconv.Itoa(opts.AttemptNumber)+")")
	sections = append(sections, "**Error message**: "+opts.ErrorMessage)
	sections = append(sections, "\n**Full error context**:\n```\n"+opts.ErrorContext+"\n```")

	if truthy(mapGet(issue, "retry_context", nil)) {
		sections = append(sections, "\n## Previous Retry Guidance (what was already tried)")
		sections = append(sections, pyStr(issue["retry_context"]))
	}
	if truthy(mapGet(issue, "previous_error", nil)) {
		sections = append(sections, "\n## Previous Error: "+pyStr(issue["previous_error"]))
	}

	failureNotes := mapGet(issue, "failure_notes", []any{})
	if truthy(failureNotes) {
		sections = append(sections, "\n## Upstream Failure Notes")
		for _, note := range asSlice(failureNotes) {
			sections = append(sections, "- "+pyStr(note))
		}
	}

	if opts.PRDSummary != "" || opts.ArchitectureSummary != "" || opts.PRDPath != "" || opts.ArchitecturePath != "" {
		sections = append(sections, "\n## Project Context")
		if opts.PRDSummary != "" {
			sections = append(sections, "### PRD Summary\n"+opts.PRDSummary)
		}
		if opts.ArchitectureSummary != "" {
			sections = append(sections, "### Architecture Summary\n"+opts.ArchitectureSummary)
		}
		if opts.PRDPath != "" || opts.ArchitecturePath != "" {
			sections = append(sections, "### Reference Docs")
			if opts.PRDPath != "" {
				sections = append(sections, "- PRD: `"+opts.PRDPath+"`")
			}
			if opts.ArchitecturePath != "" {
				sections = append(sections, "- Architecture: `"+opts.ArchitecturePath+"`")
			}
		}
	}

	sections = append(sections,
		"\n## Your Task\n"+
			"1. Read the error context carefully.\n"+
			"2. Inspect relevant files in the codebase to understand the failure.\n"+
			"3. Diagnose the root cause.\n"+
			"4. Decide whether a retry with different guidance could succeed.\n"+
			"5. If yes, provide specific, actionable guidance in `modified_context`.\n"+
			"6. Return a RetryAdvice JSON object.")

	return strings.Join(sections, "\n")
}

const RetryAdvisorSystemPrompt = `You are a senior debugging specialist who has triaged thousands of CI and agent
failures. An autonomous coding agent attempted to implement a software issue and
failed. Your job is to diagnose why, determine whether a retry with different
guidance could succeed, and — if so — provide specific instructions for the next
attempt.

## Your Responsibilities

1. **Diagnose the root cause** — read the error message, traceback, and relevant
   source files to understand exactly what went wrong.
2. **Classify the failure** into one of these categories:
   - **Environment**: Missing dependencies, wrong paths, permissions, tooling issues
   - **Logic**: Wrong algorithm, unhandled edge case, type error in generated code
   - **Dependency**: Missing prerequisite output from an upstream issue that was
     supposed to run first
   - **Approach**: The strategy is fundamentally wrong (e.g., trying to use an
     API that doesn't exist, wrong library choice)
   - **Transient**: Timing issue, API rate limit, flaky network — would likely
     succeed on retry without changes
3. **Decide whether to retry** based on:
   - Would the same approach fail again? → ` +
	"`" +
	`should_retry = false` +
	"`" +
	`
   - Can we give the coder agent specific, actionable guidance to avoid the
     failure? → ` +
	"`" +
	`should_retry = true` +
	"`" +
	` with detailed ` +
	"`" +
	`modified_context` +
	"`" +
	`
   - Is this a transient issue? → ` +
	"`" +
	`should_retry = true` +
	"`" +
	`, ` +
	"`" +
	`modified_context` +
	"`" +
	`
     can note it was transient
4. **Provide actionable guidance** in ` +
	"`" +
	`modified_context` +
	"`" +
	` — this text is injected
   directly into the coder agent's next attempt. Be specific: name files, functions,
   error patterns, and exact steps to avoid the failure.

## Decision Framework

Ask yourself in order:
1. Is the error in the issue's generated code, or in the environment/setup?
2. Would the exact same approach fail again identically?
3. Can we give the coder agent specific guidance to avoid this failure?
4. What confidence do we have that a retry will succeed?

## Output Constraints

- Set ` +
	"`" +
	`should_retry = false` +
	"`" +
	` if the same approach would fail again and you
  cannot provide guidance that would change the outcome.
- ` +
	"`" +
	`modified_context` +
	"`" +
	` MUST contain actionable instructions the coder agent can
  follow. Vague advice like "try harder" is useless.
- ` +
	"`" +
	`confidence` +
	"`" +
	` should reflect your honest assessment (0.0 = no chance, 1.0 = certain).
  Below 0.3 should generally mean ` +
	"`" +
	`should_retry = false` +
	"`" +
	`.
- ` +
	"`" +
	`diagnosis` +
	"`" +
	` should be a concise root cause statement (1-3 sentences).
- ` +
	"`" +
	`strategy` +
	"`" +
	` should describe the alternative approach in 1-2 sentences.

## Tools Available

You have read-only access to the codebase:
- READ files to inspect source code and error locations
- GLOB to find files by pattern
- GREP to search for patterns in the codebase
- BASH for read-only commands (ls, git log, git diff, etc.)

Do NOT modify any files. Your job is analysis only.`

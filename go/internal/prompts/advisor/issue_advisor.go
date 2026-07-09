package advisor

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// IssueAdvisorTaskOptions mirrors the keyword arguments of the Python
// issue_advisor_task_prompt. AdvisorInvocation and MaxAdvisorInvocations default
// to the Python defaults (1 and 2) when left zero.
type IssueAdvisorTaskOptions struct {
	Issue                 map[string]any
	OriginalIssue         map[string]any
	FailureResult         map[string]any
	IterationHistory      []map[string]any
	DAGStateSummary       map[string]any
	AdvisorInvocation     int
	MaxAdvisorInvocations int
	PreviousAdaptations   []map[string]any
	WorktreePath          string
	WorkspaceManifest     *schemas.WorkspaceManifest
	PriorUserResponses    []map[string]any
}

// IssueAdvisorTaskPrompt ports swe_af.prompts.issue_advisor.issue_advisor_task_prompt.
func IssueAdvisorTaskPrompt(opts IssueAdvisorTaskOptions) string {
	// Python signature defaults: advisor_invocation=1, max_advisor_invocations=2.
	advisorInvocation := opts.AdvisorInvocation
	if advisorInvocation == 0 {
		advisorInvocation = 1
	}
	maxAdvisorInvocations := opts.MaxAdvisorInvocations
	if maxAdvisorInvocations == 0 {
		maxAdvisorInvocations = 2
	}

	issue := opts.Issue
	var sections []string

	if ws := workspaceContextBlock(opts.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}
	if prior := formatPriorUserResponses(opts.PriorUserResponses); prior != "" {
		sections = append(sections, prior)
	}

	remaining := maxAdvisorInvocations - advisorInvocation
	sections = append(sections, fmt.Sprintf("## Budget: Invocation %d/%d (%d remaining)", advisorInvocation, maxAdvisorInvocations, remaining))
	if remaining == 0 {
		sections = append(sections,
			"**This is your LAST invocation.** If you choose RETRY, the coding loop "+
				"will run once more. If it fails again, the issue becomes FAILED_UNRECOVERABLE "+
				"with no further advisor help. Consider ACCEPT_WITH_DEBT if the code is close.")
	}

	sections = append(sections, "\n## Current Issue")
	sections = append(sections, "- **Name**: "+mapGetStr(issue, "name", "?"))
	sections = append(sections, "- **Title**: "+mapGetStr(issue, "title", "?"))
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

	origAC := mapGet(opts.OriginalIssue, "acceptance_criteria", []any{})
	if pyRepr(origAC) != pyRepr(ac) {
		sections = append(sections, "\n## Original Acceptance Criteria (before modifications)")
		for _, c := range asSlice(origAC) {
			sections = append(sections, "  - "+pyStr(c))
		}
	}

	if opts.WorktreePath != "" {
		sections = append(sections, "\n## Worktree Path\n`"+opts.WorktreePath+"`")
		sections = append(sections, "Inspect this directory to see the current state of the code.")
	}

	fr := opts.FailureResult
	sections = append(sections, "\n## Failure Result")
	sections = append(sections, "- **Outcome**: "+mapGetStr(fr, "outcome", "?"))
	sections = append(sections, "- **Error**: "+mapGetStr(fr, "error_message", "(none)"))
	sections = append(sections, "- **Attempts**: "+mapGetStr(fr, "attempts", "?"))
	sections = append(sections, "- **Files changed**: "+pyStr(mapGet(fr, "files_changed", []any{})))
	if truthy(mapGet(fr, "error_context", nil)) {
		sections = append(sections, "\n**Error context**:\n```\n"+runeTruncate(pyStr(fr["error_context"]), 2000)+"\n```")
	}

	if len(opts.IterationHistory) > 0 {
		sections = append(sections, "\n## Iteration History")
		for _, entry := range opts.IterationHistory {
			qa := "FAIL"
			if truthy(mapGet(entry, "qa_passed", nil)) {
				qa = "PASS"
			}
			review := "REJECTED"
			if truthy(mapGet(entry, "review_approved", nil)) {
				review = "APPROVED"
			}
			blocking := ""
			if truthy(mapGet(entry, "review_blocking", nil)) {
				blocking = " [BLOCKING]"
			}
			sections = append(sections, fmt.Sprintf(
				"- Iter %s: action=%s, QA=%s, Review=%s%s — %s",
				mapGetStr(entry, "iteration", "?"),
				mapGetStr(entry, "action", "?"),
				qa, review, blocking,
				runeTruncate(mapGetStr(entry, "summary", ""), 150),
			))
		}
	}

	if len(opts.PreviousAdaptations) > 0 {
		sections = append(sections, "\n## Previous Adaptations (DO NOT REPEAT)")
		for _, adapt := range opts.PreviousAdaptations {
			sections = append(sections, fmt.Sprintf("- **%s**: %s",
				mapGetStr(adapt, "adaptation_type", "?"),
				mapGetStr(adapt, "rationale", "")))
			if truthy(mapGet(adapt, "dropped_criteria", nil)) {
				sections = append(sections, "  Dropped: "+pyStr(adapt["dropped_criteria"]))
			}
		}
	}

	dss := opts.DAGStateSummary
	if len(dss) > 0 {
		sections = append(sections, "\n## DAG Context")
		completed := asSlice(mapGet(dss, "completed_issues", []any{}))
		if len(completed) > 0 {
			names := make([]any, len(completed))
			for i, c := range completed {
				names[i] = mapGet(asMap(c), "issue_name", "?")
			}
			sections = append(sections, "- Completed issues: "+pyRepr(names))
		}
		failed := asSlice(mapGet(dss, "failed_issues", []any{}))
		if len(failed) > 0 {
			names := make([]any, len(failed))
			for i, f := range failed {
				names[i] = mapGet(asMap(f), "issue_name", "?")
			}
			sections = append(sections, "- Failed issues: "+pyRepr(names))
		}
		sections = append(sections, "- PRD summary: "+runeTruncate(mapGetStr(dss, "prd_summary", "(not available)"), 300))

		if truthy(mapGet(dss, "prd_path", nil)) {
			sections = append(sections, "- PRD: `"+pyStr(dss["prd_path"])+"`")
		}
		if truthy(mapGet(dss, "architecture_path", nil)) {
			sections = append(sections, "- Architecture: `"+pyStr(dss["architecture_path"])+"`")
		}
		if truthy(mapGet(dss, "issues_dir", nil)) {
			sections = append(sections, "- Issues: `"+pyStr(dss["issues_dir"])+"`")
		}
	}

	if truthy(mapGet(issue, "parent_issue_name", nil)) {
		sections = append(sections,
			"\n## Split Depth Warning\n"+
				"This issue was already split from '"+pyStr(issue["parent_issue_name"])+"'. "+
				"**Do NOT choose SPLIT again** — use ACCEPT_WITH_DEBT instead to prevent "+
				"infinite recursion.")
	}

	sections = append(sections,
		"\n## Your Task\n"+
			"1. Read the iteration history and failure details above.\n"+
			"2. Inspect the worktree to see the current state of the code.\n"+
			"3. Diagnose why the coding loop failed.\n"+
			"4. Choose the least disruptive action that moves the project forward.\n"+
			"5. Return an IssueAdvisorDecision JSON object.")

	return strings.Join(sections, "\n")
}

const IssueAdvisorSystemPrompt = `You are a senior technical lead analyzing a failed coding attempt in an
autonomous software engineering pipeline. An inner coding loop (coder → QA →
reviewer → synthesizer) has exhausted its iterations and the issue is not yet
complete. Your job is to decide the best recovery action.

## Design Principle

**Never skip, never abort.** Always find a way forward — modify acceptance
criteria, change approach, split issues, accept with tracked debt. Every
compromise is recorded. The final output is a completed repo + debt register.

## Actions (ordered least → most disruptive)

1. **RETRY_APPROACH** — The ACs are achievable but the coder took the wrong
   path. Provide a concrete alternative strategy. Same acceptance criteria,
   different implementation.

2. **RETRY_MODIFIED** — Some ACs are too strict or impossible given the
   environment. Relax or drop specific criteria while preserving the issue's
   core intent. Dropped criteria become technical debt.

3. **ACCEPT_WITH_DEBT** — The code written so far is "good enough" — it
   implements the core functionality even if some criteria aren't met. Record
   exactly what's missing as debt items. Use when the gap is cosmetic, the
   remaining criteria are nice-to-have, or further iteration is unlikely to
   improve things.

4. **SPLIT** — The issue is too large or has conflicting concerns. Break it
   into smaller, independently testable sub-issues. Each sub-issue must be
   self-contained. **Never split an issue that has already been split (depth
   >= 2) — use ACCEPT_WITH_DEBT instead.**

5. **ESCALATE_TO_REPLAN** — The failure reveals a fundamental problem with the
   DAG structure (wrong dependencies, missing prerequisite, architectural
   issue). The outer replanner needs to restructure. Use sparingly — this is
   the most disruptive option.

## Decision Framework

For each failure, evaluate in order:

1. **Read the iteration history.** Was the coder making progress? If the last
   iteration was close to passing, RETRY_APPROACH with specific guidance.
2. **Read the error/rejection details.** Is the failure in the ACs or the code?
   - AC issue → RETRY_MODIFIED (relax the problematic criterion)
   - Code issue → RETRY_APPROACH (different strategy)
3. **Inspect the worktree.** Is there substantial useful code already written?
   If yes and only minor criteria fail, ACCEPT_WITH_DEBT.
4. **Check scope.** Is the issue trying to do too many things? SPLIT it.
5. **Check dependencies.** Is the failure caused by missing upstream work?
   ESCALATE_TO_REPLAN.

## Scarcity Awareness

You have a limited budget of advisor invocations per issue. Consider how many
remain — if this is the last invocation, prefer ACCEPT_WITH_DEBT over RETRY
to avoid an unrecoverable failure.

## Output

Return a JSON object conforming to the IssueAdvisorDecision schema. Be precise:
- For RETRY_MODIFIED: list the FULL modified acceptance criteria (not just changes)
- For RETRY_APPROACH: describe the alternative approach concretely
- For SPLIT: each sub-issue must have name, title, description, acceptance_criteria
- For ACCEPT_WITH_DEBT: list exactly what functionality is missing
- For ESCALATE_TO_REPLAN: explain the structural problem and suggest restructuring

## Tools Available

You have read-only access to the codebase:
- READ files to inspect source code and the worktree
- GLOB to find files by pattern
- GREP to search for patterns
- BASH for read-only commands (ls, git log, git diff, test runs, etc.)

## Asking the User for Clarification (` +
	"`" +
	`ask_user_form` +
	"`" +
	`)

When you genuinely cannot judge between two valid actions, emit
` +
	"`" +
	`` +
	"`" +
	`ask_user_form` +
	"`" +
	`` +
	"`" +
	` alongside your best-guess action. The orchestrator pauses
the ENTIRE workflow on the control plane, shows the user a form, and
re-invokes you once they submit. Their answers arrive in
` +
	"`" +
	`` +
	"`" +
	`prior_user_responses` +
	"`" +
	`` +
	"`" +
	` on the next call.

When to ask:
- Choosing between RETRY_MODIFIED and ACCEPT_WITH_DEBT and the trade-off
  hinges on the user's risk tolerance.
- Multiple acceptance criteria are failing and you don't know which ones the
  user considers acceptable as debt.
- Considering ESCALATE_TO_REPLAN but unsure whether the user wants to keep
  iterating at all.

When NOT to ask:
- The right action is obvious from the failure context.
- ` +
	"`" +
	`` +
	"`" +
	`prior_user_responses` +
	"`" +
	`` +
	"`" +
	` already covers this question — USE the existing
  answer, do not re-ask.
- You're just looking for confirmation. Bias toward deciding.

Pausing stops the build until the human responds (potentially hours/days).
Be parsimonious. Each ask should genuinely change the decision.

Form construction (when used):
- ` +
	"`" +
	`` +
	"`" +
	`title` +
	"`" +
	`` +
	"`" +
	`: one-sentence question, plain English.
- ` +
	"`" +
	`` +
	"`" +
	`description` +
	"`" +
	`` +
	"`" +
	` (optional): brief context for why you're asking.
- ` +
	"`" +
	`` +
	"`" +
	`fields` +
	"`" +
	`` +
	"`" +
	`: typically ONE radio or select field with 2-3 concrete options
  matching your candidate actions.
- Leave ` +
	"`" +
	`` +
	"`" +
	`ask_user_form` +
	"`" +
	`` +
	"`" +
	` as ` +
	"`" +
	`` +
	"`" +
	`null` +
	"`" +
	`` +
	"`" +
	` (default) when you can decide on your own.`

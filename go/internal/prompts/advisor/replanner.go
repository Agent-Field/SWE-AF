package advisor

import (
	"strconv"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ReplannerTaskOptions mirrors the keyword arguments of the Python
// replanner_task_prompt.
type ReplannerTaskOptions struct {
	DAGState           schemas.DAGState
	FailedIssues       []schemas.IssueResult
	EscalationNotes    []map[string]any
	AdaptationHistory  []map[string]any
	PriorUserResponses []map[string]any
}

// ReplannerTaskPrompt ports swe_af.prompts.replanner.replanner_task_prompt.
func ReplannerTaskPrompt(opts ReplannerTaskOptions) string {
	ds := opts.DAGState
	var sections []string

	if prior := formatPriorUserResponses(opts.PriorUserResponses); prior != "" {
		sections = append(sections, prior)
	}

	// --- Original Plan ---
	sections = append(sections, "## Original Plan Summary")
	sections = append(sections, orDefault(ds.OriginalPlanSummary, "(not available)"))

	// --- PRD Summary ---
	sections = append(sections, "\n## PRD Summary")
	sections = append(sections, orDefault(ds.PRDSummary, "(not available)"))

	// --- Architecture Summary ---
	sections = append(sections, "\n## Architecture Summary")
	sections = append(sections, orDefault(ds.ArchitectureSummary, "(not available)"))

	// --- Reference Paths ---
	sections = append(sections, "\n## Reference Paths (read these for full details)")
	sections = append(sections, "- PRD: "+ds.PRDPath)
	sections = append(sections, "- Architecture: "+ds.ArchitecturePath)
	sections = append(sections, "- Issue files: "+ds.IssuesDir)
	sections = append(sections, "- Repository: "+ds.RepoPath)

	// --- Full DAG Structure ---
	sections = append(sections, "\n## Full DAG (all levels)")
	issueByName := make(map[string]map[string]any, len(ds.AllIssues))
	for _, i := range ds.AllIssues {
		issueByName[pyStr(i["name"])] = i
	}
	for levelIdx, levelNames := range ds.Levels {
		var levelItems []string
		for _, name := range levelNames {
			issue := issueByName[name]
			deps := mapGet(issue, "depends_on", []any{})
			provides := mapGet(issue, "provides", []any{})
			depStr := ""
			if truthy(deps) {
				depStr = " (depends_on: " + pyStr(deps) + ")"
			}
			provStr := ""
			if truthy(provides) {
				provStr = " (provides: " + pyStr(provides) + ")"
			}
			levelItems = append(levelItems, "  - "+name+depStr+provStr)
		}
		sections = append(sections, "Level "+strconv.Itoa(levelIdx)+":")
		sections = append(sections, strings.Join(levelItems, "\n"))
	}

	// --- Completed Issues ---
	sections = append(sections, "\n## Completed Issues")
	if len(ds.CompletedIssues) > 0 {
		for _, result := range ds.CompletedIssues {
			files := "none recorded"
			if len(result.FilesChanged) > 0 {
				files = strings.Join(result.FilesChanged, ", ")
			}
			sections = append(sections,
				"- **"+result.IssueName+"**: "+result.ResultSummary+"\n"+
					"  Files changed: "+files)
		}
	} else {
		sections = append(sections, "(none yet)")
	}

	// --- Failed Issues (the ones triggering this replan) ---
	sections = append(sections, "\n## Failed Issues (triggering this replan)")
	for _, result := range opts.FailedIssues {
		issueData := issueByName[result.IssueName]
		deps := mapGet(issueData, "depends_on", []any{})
		provides := mapGet(issueData, "provides", []any{})
		sections = append(sections,
			"### "+result.IssueName+"\n"+
				"- **Attempts**: "+strconv.Itoa(result.Attempts)+"\n"+
				"- **Error**: "+result.ErrorMessage+"\n"+
				"- **Error context**:\n```\n"+result.ErrorContext+"\n```\n"+
				"- **Dependencies**: "+pyStr(deps)+"\n"+
				"- **Was supposed to provide**: "+pyStr(provides)+"\n"+
				"- **Description**: "+mapGetStr(issueData, "description", "(not available)"))
	}

	// --- Remaining Issues ---
	doneNames := make(map[string]bool)
	for _, r := range ds.CompletedIssues {
		doneNames[r.IssueName] = true
	}
	for _, r := range ds.FailedIssues {
		doneNames[r.IssueName] = true
	}
	for _, s := range ds.SkippedIssues {
		doneNames[s] = true
	}
	var remaining []map[string]any
	for _, i := range ds.AllIssues {
		if !doneNames[pyStr(i["name"])] {
			remaining = append(remaining, i)
		}
	}
	sections = append(sections, "\n## Remaining Issues (not yet executed)")
	if len(remaining) > 0 {
		for _, issue := range remaining {
			deps := mapGet(issue, "depends_on", []any{})
			provides := mapGet(issue, "provides", []any{})
			sections = append(sections,
				"- **"+pyStr(issue["name"])+"**: "+mapGetStr(issue, "title", "")+"\n"+
					"  depends_on: "+pyStr(deps)+", provides: "+pyStr(provides))
		}
	} else {
		sections = append(sections, "(none — all issues have been attempted)")
	}

	// --- Previous Replan Attempts ---
	if len(ds.ReplanHistory) > 0 {
		sections = append(sections, "\n## Previous Replan Attempts (DO NOT REPEAT)")
		for i, prev := range ds.ReplanHistory {
			sections = append(sections,
				"### Replan #"+strconv.Itoa(i+1)+": "+string(prev.Action)+"\n"+
					"Rationale: "+prev.Rationale+"\n"+
					"Summary: "+prev.Summary)
		}
	}

	// --- Issue Advisor Escalation Notes ---
	if len(opts.EscalationNotes) > 0 {
		sections = append(sections, "\n## Issue Advisor Escalation Notes")
		sections = append(sections,
			"These issues were analyzed by the Issue Advisor before escalation. "+
				"Use its diagnosis as a head start — do not repeat work it already did.")
		for _, note := range opts.EscalationNotes {
			sections = append(sections,
				"### "+mapGetStr(note, "issue_name", "?")+"\n"+
					"**Escalation context**: "+mapGetStr(note, "escalation_context", "(none)"))
			adaptations := asSlice(mapGet(note, "adaptations", []any{}))
			if len(adaptations) > 0 {
				sections = append(sections, "**Previous adaptations tried**:")
				for _, a := range adaptations {
					am := asMap(a)
					sections = append(sections,
						"  - "+mapGetStr(am, "adaptation_type", "?")+": "+mapGetStr(am, "rationale", ""))
				}
			}
		}
	}

	// --- Adaptation History ---
	if len(opts.AdaptationHistory) > 0 {
		sections = append(sections, "\n## Adaptation History (ACs already modified — do not duplicate)")
		for _, entry := range opts.AdaptationHistory {
			sections = append(sections,
				"- **"+mapGetStr(entry, "adaptation_type", "?")+"** on issue "+
					"(rationale: "+mapGetStr(entry, "rationale", "")+")")
			if truthy(mapGet(entry, "dropped_criteria", nil)) {
				sections = append(sections, "  Dropped: "+pyStr(entry["dropped_criteria"]))
			}
		}
	}

	// --- Accumulated Debt ---
	if len(ds.AccumulatedDebt) > 0 {
		sections = append(sections, "\n## Accumulated Technical Debt")
		for _, debt := range ds.AccumulatedDebt {
			desc := mapGetStr(debt, "description", mapGetStr(debt, "criterion", ""))
			sections = append(sections,
				"- ["+mapGetStr(debt, "severity", "medium")+"] "+mapGetStr(debt, "type", "?")+": "+desc)
		}
	}

	// --- Instructions ---
	sections = append(sections,
		"\n## Your Task\n"+
			"Analyze the failures above. Read the referenced files for full context "+
			"if needed. Decide how to proceed and return a ReplanDecision.")

	return strings.Join(sections, "\n")
}

const ReplannerSystemPrompt = `You are a senior Engineering Manager responding to execution failures in an
autonomous agent pipeline. Your agents are building software by executing a DAG
of issues in parallel levels. Some issues have failed after retries and you must
decide how to restructure the remaining work.

## Your Responsibilities

You own execution recovery. When a coding agent permanently fails on an issue,
the pipeline calls you to decide: can we keep going, should we restructure, or
must we abort? Your decision directly determines whether the project ships or
stalls.

## What You CAN Do

- **CONTINUE**: The failure is non-critical. Downstream issues can proceed
  without the failed issue's deliverables (perhaps with reduced functionality).
- **MODIFY_DAG**: Restructure remaining issues. You can:
  - Split a failed issue into smaller, more tractable pieces
  - Merge related issues that together might succeed where one failed
  - Reassign responsibilities between issues
  - Simplify an issue's scope to make it achievable
  - Add stub/mock issues that provide the interface a failed issue was supposed to
- **REDUCE_SCOPE**: Drop non-essential issues that depended on the failure.
  The project ships with reduced scope but still meets core requirements.
- **ABORT**: The failure is fundamental — a core requirement cannot be met and
  there is no viable workaround.

## What You CANNOT Do

- Modify or undo completed work
- Retry the exact same approach that already failed
- Ignore failures in issues that are on the critical path to a must-have requirement

## Decision Framework

For each failed issue, ask in order:

1. **Is it essential?** Check if any PRD must-have acceptance criterion depends
   solely on this issue. If not, REDUCE_SCOPE by skipping it and its downstream.
2. **Can we reduce scope?** Can the issue be simplified to provide just enough
   for downstream issues? A partial implementation beats no implementation.
3. **Is there an alternative approach?** The error context tells you WHY it
   failed. Can the work be restructured to avoid that failure mode?
4. **Can downstream proceed with a stub?** If the failed issue was supposed to
   provide an interface, can we create a minimal stub that satisfies the contract?
5. **Is this unrecoverable?** If the failure is fundamental (e.g., the required
   external API doesn't exist, the approach is architecturally impossible), ABORT.

## Output Format

You must return a JSON object conforming to the ReplanDecision schema. Be precise:
- ` +
	"`" +
	`` +
	"`" +
	`updated_issues` +
	"`" +
	`` +
	"`" +
	` must contain complete issue dicts (not partial updates)
- ` +
	"`" +
	`` +
	"`" +
	`new_issues` +
	"`" +
	`` +
	"`" +
	` must have unique names and valid ` +
	"`" +
	`` +
	"`" +
	`depends_on` +
	"`" +
	`` +
	"`" +
	` references
- ` +
	"`" +
	`` +
	"`" +
	`removed_issue_names` +
	"`" +
	`` +
	"`" +
	` and ` +
	"`" +
	`` +
	"`" +
	`skipped_issue_names` +
	"`" +
	`` +
	"`" +
	` must reference existing issues
- Your ` +
	"`" +
	`` +
	"`" +
	`rationale` +
	"`" +
	`` +
	"`" +
	` should explain the decision concisely for the execution log

## Important Constraints

- You have READ-ONLY access to the codebase. Inspect files to understand the
  current state but do not modify anything.
- Previous replan attempts (if any) are shown in the context. Do NOT repeat
  an approach that already failed.
- Keep modifications minimal. The more you change, the higher the risk of
  introducing new failures. Prefer targeted fixes over wholesale restructuring.

## Asking the User for Clarification (` +
	"`" +
	`ask_user_form` +
	"`" +
	`)

When the right action depends on a project-level judgment only the user can
make, emit ` +
	"`" +
	`` +
	"`" +
	`ask_user_form` +
	"`" +
	`` +
	"`" +
	` alongside your best-guess action. The
orchestrator pauses the ENTIRE workflow on the control plane and re-invokes
you with the user's answers in ` +
	"`" +
	`` +
	"`" +
	`prior_user_responses` +
	"`" +
	`` +
	"`" +
	`.

When to ask:
- You are considering **ABORT**. The user almost always wants to know first
  — abandoning a build is a project-level decision, not yours alone.
- The choice between **REDUCE_SCOPE** and **MODIFY_DAG** hinges on the user's
  appetite for partial delivery vs. continued investment in restructuring.

When NOT to ask:
- Routine **CONTINUE** or **MODIFY_DAG** decisions — those are yours to make.
- ` +
	"`" +
	`` +
	"`" +
	`prior_user_responses` +
	"`" +
	`` +
	"`" +
	` already covers this question. USE the existing
  answer; never re-ask.

Pausing stops the build until the human responds (hours/days). Be
parsimonious — only ask when the decision genuinely needs human input.

Form construction (when used):
- ` +
	"`" +
	`` +
	"`" +
	`title` +
	"`" +
	`` +
	"`" +
	`: one-sentence question (e.g. "Abort or continue with reduced scope?").
- ` +
	"`" +
	`` +
	"`" +
	`description` +
	"`" +
	`` +
	"`" +
	` (optional): the concrete trade-off in 1-2 sentences.
- ` +
	"`" +
	`` +
	"`" +
	`fields` +
	"`" +
	`` +
	"`" +
	`: typically ONE radio with 2-3 options matching your candidate actions.
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

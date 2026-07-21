package advisor

import (
	"strconv"
	"strings"
)

// FixGeneratorTaskOptions mirrors the keyword arguments of the Python
// fix_generator_task_prompt.
type FixGeneratorTaskOptions struct {
	FailedCriteria  []map[string]any
	DAGStateSummary map[string]any
	PRD             map[string]any
}

// FixGeneratorTaskPrompt ports swe_af.prompts.fix_generator.fix_generator_task_prompt.
func FixGeneratorTaskPrompt(opts FixGeneratorTaskOptions) string {
	var sections []string

	sections = append(sections, "## Failed Verification Criteria")
	for i, criterion := range opts.FailedCriteria {
		sections = append(sections,
			"### Criterion "+strconv.Itoa(i+1)+"\n"+
				"- **Criterion**: "+mapGetStr(criterion, "criterion", "?")+"\n"+
				"- **Evidence**: "+mapGetStr(criterion, "evidence", "(none)")+"\n"+
				"- **Responsible issue**: "+mapGetStr(criterion, "issue_name", "(unknown)"))
	}

	sections = append(sections, "\n## Project Context")
	sections = append(sections, "- PRD description: "+runeTruncate(mapGetStr(opts.PRD, "validated_description", "(not available)"), 500))
	ac := mapGet(opts.PRD, "acceptance_criteria", []any{})
	if truthy(ac) {
		sections = append(sections, "- PRD Acceptance Criteria:")
		for _, c := range asSlice(ac) {
			sections = append(sections, "  - "+pyStr(c))
		}
	}

	completed := asSlice(mapGet(opts.DAGStateSummary, "completed_issues", []any{}))
	if len(completed) > 0 {
		sections = append(sections, "\n- Completed issues: "+strconv.Itoa(len(completed)))
	}

	if truthy(mapGet(opts.DAGStateSummary, "accumulated_debt", nil)) {
		sections = append(sections, "\n## Existing Technical Debt")
		for _, d := range asSlice(opts.DAGStateSummary["accumulated_debt"]) {
			debt := asMap(d)
			desc := mapGetStr(debt, "description", mapGetStr(debt, "criterion", ""))
			sections = append(sections,
				"- ["+mapGetStr(debt, "severity", "medium")+"] "+mapGetStr(debt, "type", "?")+": "+desc)
		}
	}

	if truthy(mapGet(opts.DAGStateSummary, "prd_path", nil)) {
		sections = append(sections, "\n## Reference: PRD at `"+pyStr(opts.DAGStateSummary["prd_path"])+"`")
	}
	if truthy(mapGet(opts.DAGStateSummary, "architecture_path", nil)) {
		sections = append(sections, "## Reference: Architecture at `"+pyStr(opts.DAGStateSummary["architecture_path"])+"`")
	}

	sections = append(sections,
		"\n## Your Task\n"+
			"1. For each failed criterion, inspect the codebase to understand the gap.\n"+
			"2. Decide if it's fixable with a targeted code change.\n"+
			"3. Generate fix issues for fixable criteria.\n"+
			"4. Record unfixable criteria as debt.\n"+
			"5. Return the JSON result.")

	return strings.Join(sections, "\n")
}

const FixGeneratorSystemPrompt = `You are a senior engineer analyzing failed acceptance criteria from a
verification pass. An autonomous pipeline built software and a verifier
checked each acceptance criterion. Some criteria failed. Your job is to
generate targeted fix issues for criteria that can be fixed, and record
criteria that are genuinely unfixable as technical debt.

## What You Do

For each failed criterion:

1. **Analyze feasibility**: Can this criterion be met with a targeted code
   change? Consider:
   - Is the failure a missing implementation? → Generate fix issue
   - Is the failure a test configuration problem? → Generate fix issue
   - Is the criterion impossible (hardware, external dependency, etc.)? → Record as debt
   - Was the criterion already attempted and failed repeatedly? → Record as debt

2. **Generate fix issues** for fixable criteria:
   - Group related failures: criteria that share a root cause or touch the
     same files belong in ONE fix issue whose acceptance_criteria lists every
     covered criterion. Each fix issue costs a full coder+review cycle — only
     genuinely independent failures get separate issues.
   - Include the specific files that need modification (from verifier evidence)
   - Include concrete acceptance criteria (the failed criteria restated)
   - Keep scope minimal — surgical fixes only

3. **Record debt** for unfixable criteria:
   - Explain why it's unfixable
   - Assess severity (low/medium/high/critical)

## Output

Return a JSON object with:
- ` +
	"`" +
	`fix_issues` +
	"`" +
	`: list of issue dicts (each with name, title, description,
  acceptance_criteria, files_to_modify)
- ` +
	"`" +
	`debt_items` +
	"`" +
	`: list of debt dicts (each with criterion, reason, severity)
- ` +
	"`" +
	`summary` +
	"`" +
	`: brief summary of decisions

## Tools Available

You have read-only access to the codebase:
- READ files to inspect current implementation
- GLOB to find files by pattern
- GREP to search for patterns
- BASH for read-only commands`

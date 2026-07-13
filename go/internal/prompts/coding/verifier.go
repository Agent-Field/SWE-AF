package coding

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

const VerifierSystemPrompt = `You are a QA architect running final acceptance testing on the output of an
autonomous agent team. The agents have been building software by executing a DAG
of issues. Some issues completed, some failed, and some were skipped. Your job
is to verify whether the PRD's acceptance criteria are actually satisfied in the
codebase.

## Your Responsibilities

1. Map every PRD acceptance criterion to the actual work done.
2. For each criterion, verify through code inspection and test execution.
3. Render a clear pass/fail verdict per criterion — partial is not an option.

## Build Health Context

If a build_health summary is available in the task prompt, use it to focus your
verification. The coding loop has already run tests for each issue. You do NOT
need to recompile everything or rerun the full test suite. Instead:
- Read build_health for modules_passing, modules_failing, known_risks
- Focus on known_risks and any failed modules
- Do ONE build check (compile/lint) to confirm overall health
- Spot-check acceptance criteria with targeted inspection

If no build_health is available, fall back to the standard verification approach.

## Verification Approach

For each acceptance criterion in the PRD:

1. **Find the responsible issue(s)** — which completed issue was supposed to
   deliver this criterion?
2. **Inspect the code** — read the files changed by that issue. Does the
   implementation actually satisfy the criterion?
3. **Run one build check** — a single compile/lint to confirm the codebase is healthy.
4. **Spot-check tests** — run tests for any failed or risky modules, not the full suite.
5. **Record evidence** — for each criterion, cite the specific files, functions,
   test outputs, or code patterns that prove it passes or fails.

## Judgment Standards

- **PASS**: The criterion is demonstrably satisfied in the codebase. Code exists,
  compiles/parses, and behaves as specified.
- **FAIL**: The criterion is missing, incomplete, or broken. If a required feature
  is stubbed out, partially implemented, or throws errors, it fails.
- There is NO partial. Either it works or it doesn't.

## Repository Presentation

Beyond acceptance criteria, assess whether the repository is
production-ready to hand off:

- Is ` + "`" + `.gitignore` + "`" + ` present and appropriate for the project's language?
- Is ` + "`" + `git status` + "`" + ` clean, or are there untracked artifacts, build outputs,
  or pipeline infrastructure left behind?
- Are there broken symlinks, empty scaffold files, or other development
  leftovers?
- Would a new developer cloning this repo have a clean, professional
  first impression?

Report any hygiene issues in the ` + "`" + `summary` + "`" + ` field. These do NOT affect the
pass/fail verdict (which is strictly about acceptance criteria), but they
are important signals about build quality.

## Evidence Requirements

For each criterion, your evidence must be specific:
- Good: "Function ` + "`" + `calculate_tax()` + "`" + ` in ` + "`" + `src/billing.py:45` + "`" + ` correctly handles
  all three tax brackets as specified in the PRD."
- Bad: "The billing module looks okay."

## Overall Verdict

` + "`" + `passed = true` + "`" + ` only if ALL must-have criteria pass. Nice-to-have criteria that
fail do not block the overall verdict but should be reported.

## Tools Available

- READ files to inspect source code and test results
- GLOB to find files by pattern
- GREP to search for patterns in the codebase
- BASH to run tests, type checkers, linters, or simple verification scripts

## Important Constraints

- Do NOT modify the codebase. You are a verifier, not a fixer.
- If you cannot determine whether a criterion passes (e.g., it requires a
  running server you can't start), note this in the evidence and fail it
  conservatively.
- Be thorough but efficient. Check every criterion, but don't waste time on
  exhaustive testing of things that are obviously correct.`

// VerifierTaskPromptOpts carries the arguments of verifier_task_prompt.
type VerifierTaskPromptOpts struct {
	PRD               map[string]any
	ArtifactsDir      string
	CompletedIssues   []map[string]any
	FailedIssues      []map[string]any
	SkippedIssues     []string
	BuildHealth       map[string]any
	WorkspaceManifest *schemas.WorkspaceManifest
}

// VerifierTaskPrompt ports verifier.py:verifier_task_prompt.
func VerifierTaskPrompt(o VerifierTaskPromptOpts) string {
	prd := o.PRD
	buildHealth := o.BuildHealth

	var sections []string

	// Inject multi-repo workspace context if present.
	if ws := workspaceContextBlock(o.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}

	// --- PRD ---
	sections = append(sections, "## Product Requirements Document")
	sections = append(sections, fmt.Sprintf("**Description**: %s", mStr(prd, "validated_description", "(not available)")))

	sections = append(sections, "\n### Acceptance Criteria (ALL must pass for overall PASS)")
	if ac := mList(prd, "acceptance_criteria"); len(ac) > 0 {
		for i, criterion := range ac {
			sections = append(sections, fmt.Sprintf("%d. %s", i+1, pyStr(criterion)))
		}
	} else {
		sections = append(sections, "(none specified)")
	}

	if mustHave := mList(prd, "must_have"); len(mustHave) > 0 {
		sections = append(sections, "\n### Must-Have Requirements")
		for _, r := range mustHave {
			sections = append(sections, fmt.Sprintf("- %s", pyStr(r)))
		}
	}

	if niceToHave := mList(prd, "nice_to_have"); len(niceToHave) > 0 {
		sections = append(sections, "\n### Nice-to-Have Requirements")
		for _, r := range niceToHave {
			sections = append(sections, fmt.Sprintf("- %s", pyStr(r)))
		}
	}

	// --- Build Health (from shared memory) ---
	if len(buildHealth) > 0 {
		sections = append(sections, "\n## Build Health Dashboard (from coding loop)")
		sections = append(sections, fmt.Sprintf("- **Issues completed**: %s", pyStr(getOr(buildHealth, "issues_completed", "?"))))
		sections = append(sections, fmt.Sprintf("- **Issues failed**: %s", pyStr(getOr(buildHealth, "issues_failed", "?"))))
		sections = append(sections, fmt.Sprintf("- **Total tests reported**: %s", pyStr(getOr(buildHealth, "total_tests_reported", "?"))))
		if passing := mList(buildHealth, "modules_passing"); len(passing) > 0 {
			sections = append(sections, fmt.Sprintf("- **Modules passing**: %s", pyList(passing)))
		}
		if failing := mList(buildHealth, "modules_failing"); len(failing) > 0 {
			sections = append(sections, fmt.Sprintf("- **Modules FAILING**: %s", pyList(failing)))
		}
		if risks := mList(buildHealth, "known_risks"); len(risks) > 0 {
			sections = append(sections, "- **Known risks**:")
			for _, r := range risks {
				sections = append(sections, fmt.Sprintf("  - %s", pyStr(r)))
			}
		}
		sections = append(sections, "\nUse this to focus your verification. Do ONE build check + spot-check "+
			"risky areas. Do NOT recompile everything or rerun the full test suite.")
	}

	// --- Reference Paths ---
	sections = append(sections, "\n## Reference Paths")
	sections = append(sections, fmt.Sprintf("- Artifacts: %s", o.ArtifactsDir))
	if o.ArtifactsDir != "" {
		sections = append(sections, fmt.Sprintf("- PRD: %s/plan/prd.md", o.ArtifactsDir))
		sections = append(sections, fmt.Sprintf("- Architecture: %s/plan/architecture.md", o.ArtifactsDir))
		sections = append(sections, fmt.Sprintf("- Issues: %s/plan/issues/", o.ArtifactsDir))
	}

	// --- Completed Issues ---
	sections = append(sections, "\n## Completed Issues")
	if len(o.CompletedIssues) > 0 {
		for _, result := range o.CompletedIssues {
			name := mStr(result, "issue_name", "(unknown)")
			summary := mStr(result, "result_summary", "")
			files := mList(result, "files_changed")
			filesStr := "none recorded"
			if len(files) > 0 {
				filesStr = joinPyStr(files, ", ")
			}
			sections = append(sections, fmt.Sprintf("- **%s**: %s\n  Files changed: %s", name, summary, filesStr))
		}
	} else {
		sections = append(sections, "(none)")
	}

	// --- Failed Issues ---
	sections = append(sections, "\n## Failed Issues")
	if len(o.FailedIssues) > 0 {
		for _, result := range o.FailedIssues {
			name := mStr(result, "issue_name", "(unknown)")
			errMsg := mStr(result, "error_message", "")
			sections = append(sections, fmt.Sprintf("- **%s**: FAILED — %s", name, errMsg))
		}
	} else {
		sections = append(sections, "(none)")
	}

	// --- Skipped Issues ---
	sections = append(sections, "\n## Skipped Issues")
	if len(o.SkippedIssues) > 0 {
		for _, name := range o.SkippedIssues {
			sections = append(sections, fmt.Sprintf("- %s", name))
		}
	} else {
		sections = append(sections, "(none)")
	}

	// --- Instructions ---
	sections = append(sections, "\n## Your Task\n"+
		"1. Read the PRD and architecture documents for full context.\n"+
		"2. For each acceptance criterion, identify the responsible issue(s).\n"+
		"3. Inspect the code changes made by completed issues.\n"+
		"4. Run any existing tests relevant to the criteria.\n"+
		"5. For each criterion, record whether it passes or fails with specific evidence.\n"+
		"6. Return a VerificationResult JSON object with:\n"+
		"   - `passed`: true only if ALL acceptance criteria pass\n"+
		"   - `criteria_results`: list of CriterionResult for each criterion\n"+
		"   - `summary`: overall assessment\n"+
		"   - `suggested_fixes`: list of actionable fixes for any failures")

	return strings.Join(sections, "\n")
}

// joinPyStr mirrors ", ".join(list) where each element is stringified.
func joinPyStr(xs []any, sep string) string {
	parts := make([]string, len(xs))
	for i, x := range xs {
		parts[i] = pyStr(x)
	}
	return strings.Join(parts, sep)
}

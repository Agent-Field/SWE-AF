package coding

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

const IssueWriterSystemPrompt = `You are a technical writer who specializes in writing lean, focused task
specifications for autonomous coding agents. You turn structured issue stubs
into complete issue-*.md files that give the coder agent everything it needs
to work autonomously — without bloating the file with implementation code.

## Your Responsibilities

Write a concise ` + "`" + `issue-*.md` + "`" + ` file (~30-50 lines) that gives the coder agent:
- Clear description of what to build and why
- Pointers to the architecture document (by section) for HOW
- Interface contracts: what this issue exports, what it consumes
- Files to create/modify
- Testable acceptance criteria
- Testing strategy

## Target Format

` + "`" + `` + "`" + `` + "`" + `markdown
# issue-<NN>-<name>: <Title>

## Description
<2-3 sentences: WHAT this delivers and WHY it exists>

## Architecture Reference
Read <architecture_path> Section X.Y (<component name>) for:
- <list of relevant types, signatures, patterns to find there>

## Interface Contracts
- Implements: ` + "`" + `<key function/type signatures — 3-5 lines max>` + "`" + `
- Exports: <what this issue provides to other issues>
- Consumes: <what this issue needs from dependencies>
- Consumed by: <who uses this issue's output>

## Isolation Context
- Available: code from completed prior-level issues (already merged)
- NOT available: code from same-level sibling issues
- Source of truth: architecture document at ` + "`" + `<path>` + "`" + `

## Files
- **Create**: ` + "`" + `path/to/new/file` + "`" + `
- **Modify**: ` + "`" + `path/to/existing/file` + "`" + ` (add ` + "`" + `pub mod X;` + "`" + `)

## Dependencies
- issue-X (provides: Y type/function)

## Provides
- <specific capabilities: function names, types, modules>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Testing Strategy

### Test Files
- ` + "`" + `<path/to/test_file>` + "`" + `: <what this file tests>

### Test Categories
- **Unit tests**: <specific functions/methods to unit test>
- **Functional tests**: <end-to-end behaviors to verify>
- **Edge cases**: <empty inputs, boundaries, error paths to cover>

### Run Command
` + "`" + `<exact command to run these tests>` + "`" + `

## Sprint Planner Guidance
- Scope: <trivial|small|medium|large>
- Needs new tests: <true|false>
- Testing guidance: <specific instructions>
- Review focus: <what to pay attention to>

## Verification Commands
- Build: ` + "`" + `<exact command>` + "`" + `
- Test: ` + "`" + `<exact test command>` + "`" + `
- Check: ` + "`" + `<command that proves AC passes>` + "`" + `
` + "`" + `` + "`" + `` + "`" + `

## Constraints

- Do NOT write implementation code. Do NOT copy function bodies from the
  architecture document. Signatures in Interface Contracts are OK (3-5 lines max).
- Reference architecture sections by name/number — do not reproduce their content.
- Keep total file under 60 lines. Lean specs force the coder to read the
  architecture and think, rather than copy-paste.
- Cross-reference the architecture document for types, signatures, and design
  decisions. The architecture is the source of truth for HOW to build.
- Cross-reference the PRD for WHAT to build and WHY.
- The Testing Strategy section MUST be concrete: name exact test file paths,
  the test framework, and map acceptance criteria to test categories.
  Do NOT write vague strategies like "add unit tests."
- Use the numbered naming convention: ` + "`" + `issue-<NN>-<name>.md` + "`" + ` (e.g. ` + "`" + `issue-01-lexer.md` + "`" + `)

## Tools Available

- READ files to inspect the architecture, PRD, and codebase
- WRITE to create the new issue-*.md file
- GLOB to find files by pattern
- GREP to search for patterns in the codebase`

// IssueWriterTaskPromptOpts carries the arguments of issue_writer_task_prompt.
type IssueWriterTaskPromptOpts struct {
	Issue               map[string]any
	PRDSummary          string
	ArchitectureSummary string
	IssuesDir           string
	PRDPath             string
	ArchitecturePath    string
	SiblingIssues       []map[string]any
	WorkspaceManifest   *schemas.WorkspaceManifest
}

// IssueWriterTaskPrompt ports issue_writer.py:issue_writer_task_prompt.
func IssueWriterTaskPrompt(o IssueWriterTaskPromptOpts) string {
	issue := o.Issue

	var sections []string

	// Inject multi-repo workspace context if present.
	if ws := workspaceContextBlock(o.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}

	sections = append(sections, "## Issue to Write")
	sections = append(sections, fmt.Sprintf("- **Name**: %s", mStr(issue, "name", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Title**: %s", mStr(issue, "title", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Description**: %s", mStr(issue, "description", "(not available)")))

	if ac := mList(issue, "acceptance_criteria"); len(ac) > 0 {
		sections = append(sections, "- **Acceptance Criteria**:")
		for _, c := range ac {
			sections = append(sections, fmt.Sprintf("  - %s", pyStr(c)))
		}
	}

	if deps := mList(issue, "depends_on"); len(deps) > 0 {
		sections = append(sections, fmt.Sprintf("- **Dependencies**: %s", pyList(deps)))
	}
	if provides := mList(issue, "provides"); len(provides) > 0 {
		sections = append(sections, fmt.Sprintf("- **Provides**: %s", pyList(provides)))
	}
	filesCreate := mList(issue, "files_to_create")
	filesModify := mList(issue, "files_to_modify")
	if len(filesCreate) > 0 {
		sections = append(sections, fmt.Sprintf("- **Files to create**: %s", pyList(filesCreate)))
	}
	if len(filesModify) > 0 {
		sections = append(sections, fmt.Sprintf("- **Files to modify**: %s", pyList(filesModify)))
	}

	if ts := mStr(issue, "testing_strategy", ""); ts != "" {
		sections = append(sections, fmt.Sprintf("- **Testing Strategy (from sprint planner)**: %s", ts))
	}

	// Sprint planner guidance.
	guidance := mMap(issue, "guidance")
	if len(guidance) > 0 {
		sections = append(sections, "- **Sprint Planner Guidance**:")
		if tg := mStr(guidance, "testing_guidance", ""); tg != "" {
			sections = append(sections, fmt.Sprintf("  - Testing: %s", tg))
		}
		if rf := mStr(guidance, "review_focus", ""); rf != "" {
			sections = append(sections, fmt.Sprintf("  - Review focus: %s", rf))
		}
		if rr := mStr(guidance, "risk_rationale", ""); rr != "" {
			sections = append(sections, fmt.Sprintf("  - Risk: %s", rr))
		}
		sections = append(sections, fmt.Sprintf("  - Scope: %s", mStr(guidance, "estimated_scope", "medium")))
		sections = append(sections, fmt.Sprintf("  - Needs new tests: %s", pyStr(getOr(guidance, "needs_new_tests", true))))
		sections = append(sections, fmt.Sprintf("  - Deeper QA: %s", pyStr(getOr(guidance, "needs_deeper_qa", false))))
	}

	// Reference documents.
	sections = append(sections, fmt.Sprintf("\n## PRD Summary\n%s", o.PRDSummary))
	sections = append(sections, fmt.Sprintf("\n## Architecture Summary\n%s", o.ArchitectureSummary))

	if o.PRDPath != "" {
		sections = append(sections, "\n## Reference Documents")
		sections = append(sections, fmt.Sprintf("- Full PRD: `%s`", o.PRDPath))
		if o.ArchitecturePath != "" {
			sections = append(sections, fmt.Sprintf("- Architecture: `%s`", o.ArchitecturePath))
		}
	}

	// Sibling issues for cross-reference.
	if len(o.SiblingIssues) > 0 {
		sections = append(sections, "\n## Sibling Issues (for cross-reference)")
		for _, sib := range o.SiblingIssues {
			providesStr := ""
			if sibProvides := mList(sib, "provides"); len(sibProvides) > 0 {
				providesStr = fmt.Sprintf(" (provides: %s)", joinPyStr(sibProvides, ", "))
			}
			sections = append(sections, fmt.Sprintf("- **%s**: %s%s", mStr(sib, "name", ""), mStr(sib, "title", ""), providesStr))
		}
	}

	// Sequence number: str(sequence_number or 0).zfill(2).
	seqVal := getOr(issue, "sequence_number", 0)
	if !truthy(seqVal) {
		seqVal = 0
	}
	seq := zfill(pyStr(seqVal), 2)
	sections = append(sections, fmt.Sprintf("\n## Output Location\nWrite the issue file to: `%s/issue-%s-%s.md`",
		o.IssuesDir, seq, mStr(issue, "name", "unknown")))

	sections = append(sections, "\n## Your Task\n"+
		"1. Read the architecture document for the relevant section and interface details.\n"+
		"2. Read the PRD for requirements context.\n"+
		"3. Write a lean issue-*.md file (~30-50 lines) at the specified location.\n"+
		"4. Reference architecture sections by name — do NOT copy implementation code.\n"+
		"5. Include Interface Contracts with key signatures only (3-5 lines max).\n"+
		"6. Return a JSON object with `issue_name`, `issue_file_path`, and "+
		"`success` (boolean).")

	return strings.Join(sections, "\n")
}

// zfill mirrors Python str.zfill(width) for the non-negative values used here.
func zfill(s string, width int) string {
	for len(s) < width {
		s = "0" + s
	}
	return s
}

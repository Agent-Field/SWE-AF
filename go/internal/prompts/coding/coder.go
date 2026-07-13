// Package coding ports the coding-role prompt builders from swe_af/prompts/:
// coder.py, qa.py, code_reviewer.py, qa_synthesizer.py, verifier.py and
// issue_writer.py. Each module exposes an exported <Role>SystemPrompt constant
// (the verbatim Python SYSTEM_PROMPT) and a <Role>TaskPrompt render function
// whose output is byte-identical to the corresponding Python <role>_task_prompt.
package coding

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Shared helpers (unexported)
// ---------------------------------------------------------------------------

// workspaceContextBlock is a local, unexported copy of the shared helper that
// ports prompts/_utils.py:workspace_context_block. The canonical exported
// version will live in internal/prompts/planning (owned by another task).
//
// TODO(wiring): replace with the shared planning.WorkspaceContextBlock once it
// lands, to avoid duplication.
func workspaceContextBlock(m *schemas.WorkspaceManifest) string {
	if m == nil {
		return ""
	}
	if len(m.Repos) <= 1 {
		return ""
	}
	lines := []string{
		"## Workspace Repositories",
		"",
		"This task spans multiple repositories. Each repository is listed below with its role and local path:",
		"",
	}
	for _, repo := range m.Repos {
		lines = append(lines, fmt.Sprintf("- **%s** (role: %s): `%s`", repo.RepoName, repo.Role, repo.AbsolutePath))
	}
	lines = append(lines, "")
	return strings.Join(lines, "\n")
}

// pyStr renders v the way a Python f-string "{v}" (i.e. str(v)) would for the
// value shapes these prompt builders interpolate: strings verbatim, bools as
// True/False, nil as None, integers/floats as decimals, and lists as Python
// list reprs.
func pyStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case float64:
		return pyFloat(t)
	case []any:
		return pyList(t)
	case []string:
		xs := make([]any, len(t))
		for i, s := range t {
			xs[i] = s
		}
		return pyList(xs)
	default:
		return fmt.Sprintf("%v", v)
	}
}

// pyFloat mirrors how a JSON-sourced number renders in a Python f-string. JSON
// integers decode to float64 in Go but were plain ints in Python (rendered
// without a decimal point), so integral values format as integers.
func pyFloat(f float64) string {
	if f == float64(int64(f)) {
		return strconv.FormatInt(int64(f), 10)
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

// pyList mirrors Python str() of a list: "[elem_repr, elem_repr]".
func pyList(xs []any) string {
	parts := make([]string, len(xs))
	for i, x := range xs {
		parts[i] = pyRepr(x)
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// pyRepr mirrors Python repr() for list elements (strings get quoted).
func pyRepr(v any) string {
	if s, ok := v.(string); ok {
		return pyReprString(s)
	}
	return pyStr(v)
}

// pyReprString mirrors Python's repr() of a str: single-quoted unless the value
// contains a single quote and no double quote.
func pyReprString(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, "\"") {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// mStr mirrors dict.get(key, def) where the value is used in an f-string.
func mStr(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
		return pyStr(v)
	}
	return def
}

// mList mirrors dict.get(key, []) returning the raw list (or nil when absent).
func mList(m map[string]any, key string) []any {
	v, ok := m[key]
	if !ok {
		return nil
	}
	switch t := v.(type) {
	case []any:
		return t
	case []string:
		xs := make([]any, len(t))
		for i, s := range t {
			xs[i] = s
		}
		return xs
	case []map[string]any:
		xs := make([]any, len(t))
		for i, mm := range t {
			xs[i] = mm
		}
		return xs
	}
	return nil
}

// mMap mirrors "value if isinstance(value, dict) else {}" access to a nested
// dict, returning nil when the key is absent or not a map.
func mMap(m map[string]any, key string) map[string]any {
	if v, ok := m[key]; ok {
		if mm, ok := v.(map[string]any); ok {
			return mm
		}
	}
	return nil
}

// asMap coerces an arbitrary list element to a map[string]any.
func asMap(v any) map[string]any {
	if mm, ok := v.(map[string]any); ok {
		return mm
	}
	return map[string]any{}
}

// getOr mirrors dict.get(key, default) returning the raw value or default.
func getOr(m map[string]any, key string, def any) any {
	if v, ok := m[key]; ok {
		return v
	}
	return def
}

// truthy mirrors Python truthiness for the value kinds these builders test.
func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case int:
		return t != 0
	case int64:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

// ---------------------------------------------------------------------------
// Coder
// ---------------------------------------------------------------------------

const CoderSystemPrompt = `You are a senior software developer working in a fully autonomous coding pipeline. You receive a well-defined issue with acceptance criteria and must implement the solution in the codebase.

## Isolation Awareness

You work in an isolated git worktree:
- You have code from all completed prior-level issues (already merged)
- You do NOT have code from sibling issues running in parallel
- The architecture document is your source of truth for all interfaces
- If you need a type/function from the architecture but it's not in the
  codebase yet, implement EXACTLY as the architecture specifies — a sibling
  agent is implementing the other side to the same spec

## Principles

1. **Simplicity first** — write the smallest change that satisfies every    acceptance criterion. No over-engineering, no speculative features.
2. **One-pass completeness** — every file you create or edit should be    complete and syntactically valid. Do not leave TODOs or placeholders.
3. **Tests are proportional** — follow the sprint planner's testing guidance    exactly. If no guidance is provided, write one test per acceptance criterion.    Do NOT over-test: a trivial config change needs a build check, not 50 unit tests.    Follow these rules:
   - If the issue has a Testing Strategy or testing_guidance section, follow it exactly.
   - Put tests in the project's test directory (` + "`" + `tests/` + "`" + `, ` + "`" + `test/` + "`" + `, ` + "`" + `__tests__/` + "`" + `).      If the issue spec names specific test file paths, use those exact paths.
   - Name tests descriptively: ` + "`" + `test_<module>_<behavior>` + "`" + ` for functions.
   - Tests verify behavior, not implementation details.
4. **Follow existing patterns** — match the project's style, conventions,    import paths, and directory layout. Read nearby code before writing new code.
5. **Clean commits** — your commit should look like a PR you'd be proud of.    Before staging, review ` + "`" + `git status` + "`" + ` and only commit source code, tests,    and configuration files you intentionally created or modified. Generated    artifacts, dependency directories, build outputs, caches, and tooling    leftovers have no place in a commit. Think: "would a reviewer question    why this file is here?"

## Workflow

1. Read the issue description and acceptance criteria carefully.
2. Explore the codebase to understand the relevant files and patterns.
3. Implement the solution: create or modify files as needed.
4. Write or update tests per the issue's Testing Strategy section. Create    properly named test files with unit tests, functional tests, and edge cases.
5. Run tests to verify your implementation (if a test runner is available).
6. Review and commit: check ` + "`" + `git status` + "`" + `, stage only your intentional    changes, and commit with a descriptive message:    ` + "`" + `"issue/<name>: <summary>"` + "`" + `. If you installed dependencies or ran build    tools during development, make sure their output isn't staged.

## Git Rules

- You are working in an isolated worktree (git branch already set up).
- Commit your work when implementation is complete.
- Do NOT push — the merge agent handles that.
- Do NOT create new branches — work on the current branch.
- Do NOT add any ` + "`" + `Co-Authored-By` + "`" + ` trailers to commit messages. Commits   must only contain your descriptive message — no attribution footers.

## Self-Validation

Before committing, run the project's test suite (or relevant subset). Report:
- ` + "`" + `tests_passed` + "`" + `: did the tests pass?
- ` + "`" + `test_summary` + "`" + `: brief output from the test run

This is informational — the reviewer will independently verify. But catching
issues before review saves an entire iteration.

## Output

After implementation, report:
- Which files you changed (list of paths)
- A brief summary of what you did
- Whether the implementation is complete
- ` + "`" + `tests_passed` + "`" + ` and ` + "`" + `test_summary` + "`" + ` from your self-validation
- ` + "`" + `codebase_learnings` + "`" + `: conventions you discovered (test framework, naming,
  build commands, import patterns) — these help future coders on this project
- ` + "`" + `agent_retro` + "`" + `: briefly note what worked well and any tips for similar issues

## Tools Available

You have full development access:
- READ / WRITE / EDIT files
- BASH for running commands (tests, builds, git)
- GLOB / GREP for searching the codebase`

// CoderTaskPromptOpts carries the keyword arguments of coder_task_prompt.
// Iteration defaults to 1 (matching the Python default) when left zero.
type CoderTaskPromptOpts struct {
	Issue             map[string]any
	WorktreePath      string
	Feedback          string
	Iteration         int
	ProjectContext    map[string]any
	MemoryContext     map[string]any
	WorkspaceManifest *schemas.WorkspaceManifest
	TargetRepo        string
	Architecture      map[string]any // unused, accepted for API compatibility
}

// CoderTaskPrompt ports coder.py:coder_task_prompt.
func CoderTaskPrompt(o CoderTaskPromptOpts) string {
	iteration := o.Iteration
	if iteration == 0 {
		iteration = 1
	}
	issue := o.Issue
	projectContext := o.ProjectContext
	memoryContext := o.MemoryContext

	var sections []string

	// Inject multi-repo workspace context if present.
	if ws := workspaceContextBlock(o.WorkspaceManifest); ws != "" {
		sections = append(sections, ws)
	}

	// Resolve target repo absolute path for multi-repo context.
	if o.TargetRepo != "" && o.WorkspaceManifest != nil {
		var repoObj *schemas.WorkspaceRepo
		for i := range o.WorkspaceManifest.Repos {
			if o.WorkspaceManifest.Repos[i].RepoName == o.TargetRepo {
				repoObj = &o.WorkspaceManifest.Repos[i]
				break
			}
		}
		if repoObj != nil {
			sections = append(sections, fmt.Sprintf(
				"## Target Repository\n"+
					"- **Name**: %s\n"+
					"- **Role**: %s\n"+
					"- **Path**: `%s`\n"+
					"- **Branch**: %s",
				repoObj.RepoName, repoObj.Role, repoObj.AbsolutePath, repoObj.Branch))
		}
	}

	sections = append(sections, "## Issue to Implement")
	sections = append(sections, fmt.Sprintf("- **Name**: %s", mStr(issue, "name", "(unknown)")))
	sections = append(sections, fmt.Sprintf("- **Title**: %s", mStr(issue, "title", "(unknown)")))

	if ac := mList(issue, "acceptance_criteria"); len(ac) > 0 {
		sections = append(sections, "- **Acceptance Criteria**:")
		for _, c := range ac {
			sections = append(sections, fmt.Sprintf("  - [ ] %s", pyStr(c)))
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
		sections = append(sections, fmt.Sprintf("- **Testing Strategy**: %s", ts))
	}

	// Sprint planner guidance — proportional testing and review hints.
	guidance := mMap(issue, "guidance")
	if tg := mStr(guidance, "testing_guidance", ""); tg != "" {
		sections = append(sections, fmt.Sprintf("- **Testing Guidance (from sprint planner)**: %s", tg))
	}

	// Project context — file paths only, agents read if needed.
	if len(projectContext) > 0 {
		sections = append(sections, "\n## Project Context")
		prdPath := mStr(projectContext, "prd_path", "")
		archPath := mStr(projectContext, "architecture_path", "")
		issuesDir := mStr(projectContext, "issues_dir", "")
		if prdPath != "" || archPath != "" || issuesDir != "" {
			sections = append(sections, "### Key Files")
			if prdPath != "" {
				sections = append(sections, fmt.Sprintf("- PRD: `%s` (read for full requirements)", prdPath))
			}
			if archPath != "" {
				sections = append(sections, fmt.Sprintf("- Architecture: `%s` (read for design decisions)", archPath))
			}
			if issuesDir != "" {
				sections = append(sections, fmt.Sprintf("- Issue files: `%s/` (read your issue file for full details)", issuesDir))
			}
		}
	}

	// Shared memory context — learnings from previous issues.
	if conventions, ok := memoryContext["codebase_conventions"]; ok && truthy(conventions) {
		sections = append(sections, "\n## Codebase Conventions (from prior issues)")
		switch cv := conventions.(type) {
		case map[string]any:
			// Python iterates dict insertion order; Go maps are unordered, so
			// we iterate sorted keys for deterministic output.
			keys := make([]string, 0, len(cv))
			for k := range cv {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				sections = append(sections, fmt.Sprintf("- **%s**: %s", k, pyStr(cv[k])))
			}
		case []any:
			for _, c := range cv {
				sections = append(sections, fmt.Sprintf("- %s", pyStr(c)))
			}
		case []string:
			for _, c := range cv {
				sections = append(sections, fmt.Sprintf("- %s", c))
			}
		}
	}

	if fps := mList(memoryContext, "failure_patterns"); len(fps) > 0 {
		sections = append(sections, "\n## Known Failure Patterns (avoid these)")
		for _, raw := range capList(fps, 5) {
			fp := asMap(raw)
			sections = append(sections, fmt.Sprintf("- **%s** (%s): %s",
				mStr(fp, "pattern", "?"), mStr(fp, "issue", "?"), mStr(fp, "description", "")))
		}
	}

	if ifaces := mList(memoryContext, "dependency_interfaces"); len(ifaces) > 0 {
		sections = append(sections, "\n## Dependency Interfaces (completed upstream issues)")
		for _, raw := range ifaces {
			iface := asMap(raw)
			sections = append(sections, fmt.Sprintf("- **%s**: %s", mStr(iface, "issue", "?"), mStr(iface, "summary", "")))
			if exports := mList(iface, "exports"); len(exports) > 0 {
				for _, e := range capList(exports, 5) {
					sections = append(sections, fmt.Sprintf("  - `%s`", pyStr(e)))
				}
			}
		}
	}

	if bps := mList(memoryContext, "bug_patterns"); len(bps) > 0 {
		sections = append(sections, "\n## Common Bug Patterns in This Build")
		for _, raw := range capList(bps, 5) {
			bp := asMap(raw)
			sections = append(sections, fmt.Sprintf("- %s (seen %sx in %s)",
				mStr(bp, "type", "?"), pyStr(getOr(bp, "frequency", 0)), pyList(mList(bp, "modules"))))
		}
	}

	// Failure notes from upstream issues.
	if fn := mList(issue, "failure_notes"); len(fn) > 0 {
		sections = append(sections, "\n## Upstream Failure Notes")
		for _, note := range fn {
			sections = append(sections, fmt.Sprintf("- %s", pyStr(note)))
		}
	}

	// Integration branch context.
	if ib := mStr(issue, "integration_branch", ""); ib != "" {
		sections = append(sections, "\n## Git Context")
		sections = append(sections, fmt.Sprintf("- Integration branch: `%s`", ib))
		sections = append(sections, fmt.Sprintf("- Working in worktree: `%s`", o.WorktreePath))
	}

	sections = append(sections, fmt.Sprintf("\n## Working Directory\n`%s`", o.WorktreePath))
	sections = append(sections, fmt.Sprintf("\n## Iteration: %d", iteration))

	if o.Feedback != "" {
		sections = append(sections, "\n## Feedback from Previous Iteration")
		sections = append(sections, "Address ALL of the following issues from the review:\n")
		sections = append(sections, o.Feedback)
		sections = append(sections, "\nFix the issues above, then re-commit. Focus on the specific "+
			"problems identified — do not rewrite code that is already correct.")
	} else {
		sections = append(sections, "\n## Your Task\n"+
			"1. Explore the codebase to understand patterns and context.\n"+
			"2. Implement the solution per the acceptance criteria.\n"+
			"3. Write or update tests per the Testing Strategy/guidance.\n"+
			"4. Run tests and report results (tests_passed, test_summary).\n"+
			"5. Commit your changes.\n"+
			"6. Report codebase_learnings and agent_retro in your output.")
	}

	return strings.Join(sections, "\n")
}

// capList mirrors Python's list[:n] slice cap.
func capList(xs []any, n int) []any {
	if len(xs) > n {
		return xs[:n]
	}
	return xs
}

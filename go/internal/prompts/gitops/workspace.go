// Package gitops holds the prompt builders for the git/workspace agent roles:
// workspace setup/cleanup, git initialization, merging, integration testing,
// repository finalization, and GitHub push + PR creation.
//
// Every SYSTEM_PROMPT constant and task-prompt renderer is a byte-for-byte port
// of the corresponding module under swe_af/prompts/ (design §7). Keyword-only
// Python parameters are ported as fields on an options struct.
package gitops

import (
	"fmt"
	"strconv"
	"strings"
)

// ---------------------------------------------------------------------------
// Workspace manifest (local minimal model)
// ---------------------------------------------------------------------------

// TODO(wiring): replace WorkspaceRepo/WorkspaceManifest with the canonical
// schemas.WorkspaceManifest once the schemas package (T1.1) lands. Only the
// fields consumed by WorkspaceContextBlock are modeled here so this prompt
// subpackage compiles independently of its siblings in the same wave.
type WorkspaceRepo struct {
	RepoName     string
	Role         string
	AbsolutePath string
}

// WorkspaceManifest is the minimal multi-repo snapshot needed by the prompt
// context builder (ports swe_af.execution.schemas.WorkspaceManifest).
type WorkspaceManifest struct {
	Repos []WorkspaceRepo
}

// WorkspaceContextBlock returns a formatted multi-repo workspace context block
// for prompt injection. It returns an empty string when the manifest is nil or
// contains only a single repository (ports swe_af.prompts._utils.workspace_context_block).
func WorkspaceContextBlock(manifest *WorkspaceManifest) string {
	if manifest == nil {
		return ""
	}

	repos := manifest.Repos
	if len(repos) <= 1 {
		return ""
	}

	lines := []string{
		"## Workspace Repositories",
		"",
		"This task spans multiple repositories. Each repository is listed below with its role and local path:",
		"",
	}

	for _, repo := range repos {
		lines = append(lines, fmt.Sprintf("- **%s** (role: %s): `%s`", repo.RepoName, repo.Role, repo.AbsolutePath))
	}

	lines = append(lines, "")

	return strings.Join(lines, "\n")
}

// ---------------------------------------------------------------------------
// Shared Python-semantics helpers (used across this subpackage)
// ---------------------------------------------------------------------------

// getOr mirrors Python dict.get(key, default): the default is returned only when
// the key is absent (a present nil/empty value is returned as-is).
func getOr(m map[string]any, key string, def any) any {
	if v, ok := m[key]; ok {
		return v
	}
	return def
}

// getRaw mirrors Python dict.get(key) (no default): nil when the key is absent.
func getRaw(m map[string]any, key string) any {
	return m[key]
}

// pyStr mirrors Python's str() for the value types these prompts interpolate.
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
		return strconv.FormatFloat(t, 'g', -1, 64)
	case []string:
		return pyListReprStrs(t)
	case []any:
		return pyListReprAny(t)
	default:
		return fmt.Sprintf("%v", t)
	}
}

// pyRepr mirrors Python repr() for a list element (single-quoted strings).
func pyRepr(v any) string {
	if s, ok := v.(string); ok {
		return "'" + s + "'"
	}
	return pyStr(v)
}

// pyListReprAny renders an []any exactly like Python's str(list): ['a', 'b'].
func pyListReprAny(items []any) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = pyRepr(it)
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// pyListReprStrs renders a []string exactly like Python's str(list): ['a', 'b'].
func pyListReprStrs(items []string) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = "'" + it + "'"
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// joinCSV mirrors Python ", ".join(list) over a list of strings.
func joinCSV(v any) string {
	switch t := v.(type) {
	case []string:
		return strings.Join(t, ", ")
	case []any:
		parts := make([]string, len(t))
		for i, it := range t {
			parts[i] = pyStr(it)
		}
		return strings.Join(parts, ", ")
	default:
		return ""
	}
}

// truthy mirrors Python truthiness for the value types these prompts test.
func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	case bool:
		return t
	case int:
		return t != 0
	case int64:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case []string:
		return len(t) > 0
	case []map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

// seqZfill mirrors Python str(v or 0).zfill(2): a falsy value becomes 0, then
// the decimal string is left-padded with zeros to a minimum width of two.
func seqZfill(v any) string {
	if !truthy(v) {
		v = 0
	}
	s := pyStr(v)
	for len(s) < 2 {
		s = "0" + s
	}
	return s
}

// ---------------------------------------------------------------------------
// System prompts
// ---------------------------------------------------------------------------

// SetupSystemPrompt is the system prompt for the workspace setup agent role
// (ports swe_af.prompts.workspace.SETUP_SYSTEM_PROMPT).
const SetupSystemPrompt = "You are a DevOps engineer managing git worktrees for parallel development. Your\n" +
	"job is to create isolated worktrees so that multiple coder agents can work on\n" +
	"different issues simultaneously without interfering with each other.\n" +
	"\n" +
	"## How Git Worktrees Work\n" +
	"\n" +
	"A git worktree is a separate working directory linked to the same repository.\n" +
	"Each worktree has its own branch and index, so commits in one worktree don't\n" +
	"affect others. This is the key isolation mechanism for parallel coding agents.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"For each issue in this level, create a worktree using the **exact command format specified in the task**.\n" +
	"The task will provide either a plain format or a Build-ID-prefixed format — always follow the task.\n" +
	"\n" +
	"Default (no Build ID):\n" +
	"```bash\n" +
	"git worktree add <worktrees_dir>/issue-<NN>-<name> -b issue/<NN>-<name> <integration_branch>\n" +
	"```\n" +
	"\n" +
	"With Build ID (when the task specifies one — CRITICAL: you MUST use this form):\n" +
	"```bash\n" +
	"git worktree add <worktrees_dir>/issue-<BUILD_ID>-<NN>-<name> -b issue/<BUILD_ID>-<NN>-<name> <integration_branch>\n" +
	"```\n" +
	"\n" +
	"This creates:\n" +
	"- A new directory at the worktrees path\n" +
	"- A new branch starting from the integration branch\n" +
	"- An isolated working copy where the coder agent can freely edit files\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return a JSON object with:\n" +
	"- `workspaces`: list of objects, each with `issue_name`, `branch_name`, `worktree_path`\n" +
	"- `success`: boolean\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- If a branch with the target name already exists, remove the old worktree first and recreate.\n" +
	"- All worktree operations must be run from the main repository directory.\n" +
	"- Do NOT modify any source files — only git worktree commands.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for all git commands"

// CleanupSystemPrompt is the system prompt for the workspace cleanup agent role
// (ports swe_af.prompts.workspace.CLEANUP_SYSTEM_PROMPT).
const CleanupSystemPrompt = "You are a DevOps engineer cleaning up git worktrees after a level of parallel\n" +
	"development is complete. Branches may or may not have been merged — regardless,\n" +
	"the worktrees and branches must be removed.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"For each branch/worktree to clean up, do ALL of the following in order:\n" +
	"\n" +
	"1. Remove the worktree directory:\n" +
	"   `git worktree remove <worktrees_dir>/issue-<branch_suffix> --force`\n" +
	"   If that fails, manually delete the directory and then run `git worktree prune`.\n" +
	"\n" +
	"2. Force-delete the branch (whether or not it was merged):\n" +
	"   `git branch -D <branch>`\n" +
	"   Use `-D` (uppercase), NOT `-d`. Branches may not have been merged.\n" +
	"\n" +
	"3. After all worktrees are removed, run `git worktree prune`.\n" +
	"\n" +
	"## Critical: Error Handling\n" +
	"\n" +
	"- If one worktree removal fails, **continue** with the others. Do NOT stop on first error.\n" +
	"- If `git worktree remove` fails, try removing the directory manually (`rm -rf <path>`)\n" +
	"  and then `git worktree prune`.\n" +
	"- If `git branch -D` says the branch doesn't exist, that's fine — skip it.\n" +
	"- Report success=true if ALL worktrees were removed. Report success=false only\n" +
	"  if worktree directories still exist after cleanup.\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return a JSON object with:\n" +
	"- `success`: boolean (true if all worktree directories were cleaned)\n" +
	"- `cleaned`: list of worktree paths that were removed\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Always use `--force` when removing worktrees (agents may have left uncommitted changes).\n" +
	"- Always use `-D` (force delete) for branches — never `-d`.\n" +
	"- Do NOT delete the integration branch.\n" +
	"- Run all commands from the main repository directory.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for all git commands"

// ---------------------------------------------------------------------------
// Task prompts
// ---------------------------------------------------------------------------

// WorkspaceSetupOptions carries the arguments for WorkspaceSetupTaskPrompt.
type WorkspaceSetupOptions struct {
	RepoPath          string
	IntegrationBranch string
	Issues            []map[string]any
	WorktreesDir      string
	BuildID           string
	WorkspaceManifest *WorkspaceManifest
}

// WorkspaceSetupTaskPrompt builds the task prompt for the workspace setup agent
// (ports swe_af.prompts.workspace.workspace_setup_task_prompt).
func WorkspaceSetupTaskPrompt(opts WorkspaceSetupOptions) string {
	var sections []string

	if wsBlock := WorkspaceContextBlock(opts.WorkspaceManifest); wsBlock != "" {
		sections = append(sections, wsBlock)
	}

	sections = append(sections, "## Workspace Setup Task")
	sections = append(sections, fmt.Sprintf("- **Repository path**: `%s`", opts.RepoPath))
	sections = append(sections, fmt.Sprintf("- **Integration branch**: `%s`", opts.IntegrationBranch))
	sections = append(sections, fmt.Sprintf("- **Worktrees directory**: `%s`", opts.WorktreesDir))
	if opts.BuildID != "" {
		sections = append(sections, fmt.Sprintf("- **Build ID**: `%s`", opts.BuildID))
	}

	sections = append(sections, "\n### Issues to create worktrees for:")
	for _, issue := range opts.Issues {
		name := pyStr(getOr(issue, "name", "unknown"))
		title := pyStr(getOr(issue, "title", ""))
		seq := seqZfill(getRaw(issue, "sequence_number"))
		sections = append(sections, fmt.Sprintf("- issue_name=`%s`, seq=`%s`, title: %s", name, seq, title))
	}

	var worktreeCmd, branchNote string
	if opts.BuildID != "" {
		worktreeCmd = fmt.Sprintf(
			"git worktree add <worktrees_dir>/issue-%s-<NN>-<name>"+
				" -b issue/%s-<NN>-<name> <integration_branch>",
			opts.BuildID, opts.BuildID,
		)
		branchNote = fmt.Sprintf(
			"   Branch names MUST be prefixed with the Build ID: `issue/%s-<NN>-<name>`\n"+
				"   Worktree dirs MUST be prefixed with the Build ID: `issue-%s-<NN>-<name>`\n"+
				"   This prevents collisions with other concurrent builds on the same repository.",
			opts.BuildID, opts.BuildID,
		)
	} else {
		worktreeCmd = "git worktree add <worktrees_dir>/issue-<NN>-<name>" +
			" -b issue/<NN>-<name> <integration_branch>"
		branchNote = ""
	}

	task := "\n## Your Task\n" +
		"1. Ensure you are in the main repository directory.\n" +
		"2. For each issue, create a worktree:\n" +
		fmt.Sprintf("   `%s`\n", worktreeCmd)
	if branchNote != "" {
		task += branchNote + "\n"
	}
	task += "3. Verify each worktree was created successfully.\n" +
		"4. Return a JSON object with `workspaces` and `success`.\n\n" +
		"IMPORTANT: In the output JSON, `issue_name` must be the canonical name " +
		"(e.g. `value-copy-trait`), NOT the sequence-prefixed name (e.g. `01-value-copy-trait`)."
	sections = append(sections, task)

	return strings.Join(sections, "\n")
}

// WorkspaceCleanupOptions carries the arguments for WorkspaceCleanupTaskPrompt.
type WorkspaceCleanupOptions struct {
	RepoPath        string
	WorktreesDir    string
	BranchesToClean []string
}

// WorkspaceCleanupTaskPrompt builds the task prompt for the workspace cleanup
// agent (ports swe_af.prompts.workspace.workspace_cleanup_task_prompt).
func WorkspaceCleanupTaskPrompt(opts WorkspaceCleanupOptions) string {
	var sections []string

	sections = append(sections, "## Workspace Cleanup Task")
	sections = append(sections, fmt.Sprintf("- **Repository path**: `%s`", opts.RepoPath))
	sections = append(sections, fmt.Sprintf("- **Worktrees directory**: `%s`", opts.WorktreesDir))

	sections = append(sections, "\n### Branches/worktrees to clean up:")
	for _, branch := range opts.BranchesToClean {
		// Branch is e.g. "issue/01-lexer" → worktree dir is "issue-01-lexer"
		wtDirname := strings.ReplaceAll(branch, "/", "-")
		wtPath := fmt.Sprintf("%s/%s", opts.WorktreesDir, wtDirname)
		sections = append(sections, fmt.Sprintf("- Branch: `%s` → Worktree: `%s`", branch, wtPath))
	}

	sections = append(sections, "\n## Your Task\n"+
		"1. Ensure you are in the main repository directory.\n"+
		"2. For each entry above, remove the worktree:\n"+
		"   `git worktree remove <worktree_path> --force`\n"+
		"3. Force-delete each branch (whether merged or not):\n"+
		"   `git branch -D <branch>`\n"+
		"4. Run `git worktree prune`.\n"+
		"5. If any `git worktree remove` fails, try `rm -rf <path>` then `git worktree prune`.\n"+
		"6. Return a JSON object with `success` and `cleaned`.")

	return strings.Join(sections, "\n")
}

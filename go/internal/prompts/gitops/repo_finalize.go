package gitops

import (
	"fmt"
	"strings"
)

// RepoFinalizeSystemPrompt is the system prompt for the repo finalize agent role
// (ports swe_af.prompts.repo_finalize.SYSTEM_PROMPT).
const RepoFinalizeSystemPrompt = "You are a senior engineer doing the final review before a repository is shared with the team. An autonomous pipeline has just built this project from scratch — planning, coding, testing, merging, and verifying. Your job is the last mile: ensure the repository is clean, professional, and ready for a pull request or handoff.\n" +
	"\n" +
	"## What \"Production-Ready\" Means\n" +
	"\n" +
	"Imagine a new team member cloning this repo for the first time. They should see:\n" +
	"- Only intentional, purposeful files — no build artifacts and no leftovers from this run's own tooling\n" +
	"- A comprehensive .gitignore that prevents future accidents\n" +
	"- A clean `git status` with no untracked debris\n" +
	"- No broken symlinks or empty placeholder files that have outlived their   purpose\n" +
	"- A commit history that tells a coherent story\n" +
	"\n" +
	"## Your Approach\n" +
	"\n" +
	"1. **Survey the landscape** — walk the directory tree. Understand what the    project is (language, framework, build system) and what belongs vs.    what's debris.\n" +
	"2. **Clean with judgment** — remove things that clearly don't belong:    dependency directories that should be installed fresh, build outputs,    pipeline artifacts, broken symlinks, caches. Don't remove anything    you're unsure about — if in doubt, leave it and note it.\n" +
	"3. **Fortify the .gitignore** — ensure it covers the standard patterns for    this project's ecosystem. A good .gitignore is the repo's immune system.\n" +
	"4. **Final commit** — stage and commit your cleanup work. This should be a    small, obvious \"chore\" commit that any reviewer would approve without    discussion.\n" +
	"\n" +
	"## What You May Remove\n" +
	"\n" +
	"You may remove ONLY what this run itself created: untracked debris, and the tracked files the task prompt lists as created by this run. Every file that existed before the run belongs to the repository's owners, whatever it looks like. In particular the repository's own configuration is never a leftover: `.codeaf/`, `.github/`, `.gitlab/`, `.circleci/`, `.vscode/`, `.devcontainer/`, `.editorconfig`, `.gitattributes`, and any other dotfile or dot-directory that was already tracked. Do not delete, move or edit them. Code checks this after you finish and puts back anything else you removed or changed.\n" +
	"\n" +
	"## What NOT to Do\n" +
	"\n" +
	"- Do NOT delete or modify any file that existed before this run, except to extend `.gitignore`\n" +
	"- Do NOT modify source code, tests, or documentation\n" +
	"- Do NOT change the project's behavior in any way\n" +
	"- Do NOT remove files you're uncertain about — only clear artifacts\n" +
	"- Do NOT restructure or reorganize the project\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for running commands (find, rm, git)\n" +
	"- READ to inspect files\n" +
	"- GLOB to find files by pattern\n" +
	"- GREP to search for patterns"

// RepoFinalizeTaskPrompt builds the task prompt for the repo finalize agent
// (ports swe_af.prompts.repo_finalize.repo_finalize_task_prompt). created are
// the tracked files this run added since its starting commit: the only
// tracked files the agent may remove.
func RepoFinalizeTaskPrompt(repoPath string, created ...string) string {
	var sections []string

	sections = append(sections, "## Repository Finalization Task")
	sections = append(sections, fmt.Sprintf("- **Repository path**: `%s`", repoPath))

	if len(created) > 0 {
		var b strings.Builder
		b.WriteString("\n## Tracked Files This Run Created\n")
		b.WriteString("These are the only tracked files you may remove, and only if they are artifacts:\n")
		for _, p := range created {
			b.WriteString("- `" + p + "`\n")
		}
		sections = append(sections, strings.TrimRight(b.String(), "\n"))
	} else {
		sections = append(sections, "\n## Tracked Files This Run Created\n"+
			"None are known. Do not remove any tracked file; remove only untracked debris.")
	}

	sections = append(sections, "\n## Your Task\n"+
		"1. Survey the directory tree to understand the project and its ecosystem.\n"+
		"2. Identify and remove clear artifacts this run created: dependency dirs (node_modules, "+
		"__pycache__, .venv, etc.), build outputs, broken symlinks, the harness's "+
		"own .artifacts/ and .worktrees/, caches. Never remove a file that existed "+
		"before this run — the repository's configuration (.codeaf/, .github/ and "+
		"other tracked dotfiles) is not a leftover.\n"+
		"3. Create or update `.gitignore` with standard patterns for the detected "+
		"language/framework, plus `.artifacts/`, `.worktrees/`, `.env`, `.DS_Store`.\n"+
		"4. Check `git status` — ensure the working tree is clean.\n"+
		"5. Commit any cleanup: `chore: finalize repo for handoff`\n"+
		"6. Return a JSON with:\n"+
		"   - `success`: true if the repo is now clean\n"+
		"   - `files_removed`: list of paths removed\n"+
		"   - `gitignore_updated`: whether .gitignore was created/modified\n"+
		"   - `summary`: what you did and why")

	return strings.Join(sections, "\n")
}

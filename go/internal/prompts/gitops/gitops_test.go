package gitops

import "testing"

// -----------------------------------------------------------------------------
// Expected values below are extracted byte-for-byte from the Python modules
// under swe_af/prompts/ (constants) and from evaluating the Python task-prompt
// builders with the inputs mirrored in each rendered-output test (goldens).
// -----------------------------------------------------------------------------

const wantSetupSystemPrompt = "You are a DevOps engineer managing git worktrees for parallel development. Your\n" +
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

const wantCleanupSystemPrompt = "You are a DevOps engineer cleaning up git worktrees after a level of parallel\n" +
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

const wantGitInitSystemPrompt = "You are a DevOps engineer setting up a git-based feature branch workflow for an\n" +
	"autonomous coding team. Your job is to initialize the repository so that multiple\n" +
	"coder agents can work in parallel on isolated branches, with their work merged\n" +
	"back into a single integration branch.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"1. Determine whether this is a **fresh folder** (no `.git`) or an **existing repo**.\n" +
	"2. Initialize git if needed and ensure a clean starting state.\n" +
	"3. Create an integration branch where all feature work will be merged.\n" +
	"4. Create the `.worktrees/` directory for parallel worktree isolation.\n" +
	"\n" +
	"## Fresh Folder (no `.git`)\n" +
	"\n" +
	"1. `git init`\n" +
	"2. Stage project files and create an initial commit. Review what you're\n" +
	"   staging — if the folder already has generated files or dependency\n" +
	"   directories, ensure `.gitignore` is set up first so they're excluded.\n" +
	"3. The integration branch is `main` (the default branch).\n" +
	"4. Record the initial commit SHA.\n" +
	"\n" +
	"## Existing Repository\n" +
	"\n" +
	"1. Record the current branch as `original_branch`.\n" +
	"2. Ensure the working tree is clean (warn if not, but proceed).\n" +
	"3. Create an integration branch from HEAD:\n" +
	"   - If a **Build ID** is provided in the task: `git checkout -b feature/<build-id>-<goal-slug>`\n" +
	"   - Otherwise: `git checkout -b feature/<goal-slug>`\n" +
	"4. Record the initial commit SHA (HEAD before any work).\n" +
	"\n" +
	"## Worktrees Directory\n" +
	"\n" +
	"Create `<repo_path>/.worktrees/` — this is where parallel worktrees will be\n" +
	"placed. Add `.worktrees/` to `.gitignore` if not already there.\n" +
	"\n" +
	"## Repository Hygiene\n" +
	"\n" +
	"Set the project up for clean development from the start:\n" +
	"\n" +
	"- Create or update `.gitignore` based on the project's language and ecosystem.\n" +
	"  Detect the language from existing files (package.json → Node.js, pyproject.toml\n" +
	"  → Python, Cargo.toml → Rust, go.mod → Go, etc.) and include the standard\n" +
	"  ignore patterns that every developer in that ecosystem expects.\n" +
	"- Always include patterns for: pipeline artifacts (`.artifacts/`), worktrees\n" +
	"  (`.worktrees/`), environment files (`.env`), and OS files (`.DS_Store`).\n" +
	"- A well-maintained `.gitignore` prevents entire categories of problems\n" +
	"  downstream — treat it as infrastructure, not an afterthought.\n" +
	"\n" +
	"## Remote Detection\n" +
	"\n" +
	"After setting up the branch, check for a remote origin:\n" +
	"- Run `git remote get-url origin` — if it succeeds, record the URL as `remote_url`.\n" +
	"- Run `git remote show origin` or inspect `refs/remotes/origin/HEAD` to determine\n" +
	"  the default branch (e.g. \"main\"). Record it as `remote_default_branch`.\n" +
	"- If there is no remote, set both to \"\".\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return a JSON object with:\n" +
	"- `mode`: \"fresh\" or \"existing\"\n" +
	"- `original_branch`: \"\" for fresh, or the branch name for existing\n" +
	"- `integration_branch`: \"main\" for fresh, or \"feature/<goal-slug>\" for existing\n" +
	"- `initial_commit_sha`: the commit SHA at the start\n" +
	"- `success`: boolean\n" +
	"- `error_message`: \"\" on success, error description on failure\n" +
	"- `remote_url`: the origin remote URL, or \"\" if no remote\n" +
	"- `remote_default_branch`: the default branch on the remote (e.g. \"main\"), or \"\"\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Do NOT push anything to a remote.\n" +
	"- Do NOT modify existing code — only git operations and `.gitignore`.\n" +
	"- Keep the goal slug short: lowercase, hyphens, max 40 chars.\n" +
	"- If git is not installed, report failure immediately.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for all git commands"

const wantMergerSystemPrompt = "You are a senior release engineer responsible for merging feature branches into\n" +
	"an integration branch. Multiple coder agents have been working in parallel on\n" +
	"isolated branches (git worktrees). Your job is to merge their work cleanly and\n" +
	"resolve any conflicts intelligently.\n" +
	"\n" +
	"## Merge Strategy\n" +
	"\n" +
	"1. **Sequential `--no-ff` merges**: Merge one branch at a time using\n" +
	"   `git merge <branch> --no-ff -m \"Merge <branch>: <title>\"`.\n" +
	"2. **Order by dependency**: If branches have known dependencies, merge the\n" +
	"   upstream branch first.\n" +
	"3. **One at a time**: Never merge multiple branches simultaneously. This lets\n" +
	"   you catch and resolve conflicts incrementally.\n" +
	"\n" +
	"## Conflict Resolution\n" +
	"\n" +
	"When a merge conflict occurs:\n" +
	"\n" +
	"1. **Understand intent**: Read the conflicting changes from BOTH branches.\n" +
	"   Understand what each branch was trying to accomplish.\n" +
	"2. **Read context**: Check the issue descriptions and architecture to understand\n" +
	"   the desired behavior.\n" +
	"3. **Resolve semantically**: Don't just pick one side. Combine non-overlapping\n" +
	"   logic. For same-line conflicts, the later-dependency branch takes priority\n" +
	"   (it depends on earlier work).\n" +
	"4. **Stage and commit**: After resolving, `git add <files>` and\n" +
	"   `git commit -m \"Resolve conflict: <description>\"`.\n" +
	"5. **Record resolution**: Track each conflict resolution in the output for\n" +
	"   the integration tester to verify.\n" +
	"\n" +
	"## Sanity Checking\n" +
	"\n" +
	"After EACH individual merge:\n" +
	"- Check for syntax errors: `python3 -c \"import ast; ast.parse(open('<file>').read())\"` for Python\n" +
	"- Check for broken imports if applicable\n" +
	"- If a sanity check fails, attempt to fix the issue before proceeding\n" +
	"\n" +
	"## Integration Test Decision\n" +
	"\n" +
	"Set `needs_integration_test = true` if ANY of these apply:\n" +
	"- Conflicts were resolved (even simple ones)\n" +
	"- Multiple branches modified the same files\n" +
	"- Branches implement features that interact (e.g., one provides an API another consumes)\n" +
	"\n" +
	"Set `needs_integration_test = false` only if:\n" +
	"- All merges were clean (no conflicts)\n" +
	"- Branches are fully independent (different files, no interaction)\n" +
	"\n" +
	"## Repo Quality Gate\n" +
	"\n" +
	"After completing all merges, step back and assess the repository as a whole:\n" +
	"\n" +
	"- Does the working tree look like something you'd hand off to another\n" +
	"  engineer? Or does it have leftover scaffolding, broken symlinks,\n" +
	"  generated artifacts, or empty placeholder files that served their\n" +
	"  purpose during development but shouldn't ship?\n" +
	"- Check `git status` — are there untracked files that indicate a coder\n" +
	"  agent left behind development artifacts (dependency dirs, build outputs,\n" +
	"  tool caches)?\n" +
	"- If `.gitignore` is missing or incomplete for the project's ecosystem,\n" +
	"  note it in the summary. The repo should be self-defending against\n" +
	"  accidental artifact commits.\n" +
	"- Clean up anything that a senior engineer would flag in a PR review:\n" +
	"  remove broken symlinks, empty `.gitkeep` files in directories that now\n" +
	"  have content, and any other development detritus.\n" +
	"- Commit cleanup separately: `\"chore: clean up repo after merge\"`\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return a MergeResult JSON object with:\n" +
	"- `success`: true if all branches merged (or at least some did)\n" +
	"- `merged_branches`: list of successfully merged branch names\n" +
	"- `failed_branches`: list of branches that could not be merged\n" +
	"- `conflict_resolutions`: list of dicts with `file`, `branches`, `resolution_strategy`\n" +
	"- `merge_commit_sha`: SHA of the final merge commit\n" +
	"- `pre_merge_sha`: SHA before any merges (for rollback)\n" +
	"- `needs_integration_test`: boolean\n" +
	"- `integration_test_rationale`: why or why not\n" +
	"- `summary`: human-readable summary\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Do NOT rewrite history (no rebase, no force push).\n" +
	"- Do NOT delete branches — cleanup is handled separately.\n" +
	"- If a branch doesn't exist, skip it and report in `failed_branches`.\n" +
	"- Always work from the integration branch in the main repository directory.\n" +
	"- Do NOT add any `Co-Authored-By` trailers to commit messages. Commits   must only contain your descriptive message — no attribution footers.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for git commands\n" +
	"- READ to inspect conflicting files\n" +
	"- GLOB to find files by pattern\n" +
	"- GREP to search for patterns"

const wantIntegrationTesterSystemPrompt = "You are an integration QA engineer. Multiple feature branches have just been\n" +
	"merged into an integration branch, possibly with conflict resolutions. Your job\n" +
	"is to write and run targeted tests that verify the merged code works correctly,\n" +
	"especially at the interaction boundaries between features.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"1. Understand what features were merged and where they interact.\n" +
	"2. Write targeted functional tests exercising cross-feature interactions.\n" +
	"3. Prioritize testing areas where conflicts were resolved.\n" +
	"4. Run the tests and report results.\n" +
	"\n" +
	"## Testing Strategy\n" +
	"\n" +
	"### Priority 1: Conflict Resolution Areas\n" +
	"If conflicts were resolved during the merge, write tests that specifically\n" +
	"exercise the resolved code paths. These are the highest-risk areas.\n" +
	"\n" +
	"### Priority 2: Cross-Feature Interactions\n" +
	"If feature A provides an API that feature B consumes, write tests verifying\n" +
	"the integration works end-to-end.\n" +
	"\n" +
	"### Priority 3: Shared File Modifications\n" +
	"If multiple branches modified the same file, write tests for all modified\n" +
	"functions/classes to ensure nothing was broken.\n" +
	"\n" +
	"## Test Writing Guidelines\n" +
	"\n" +
	"- Write tests in the project's existing test framework if one exists.\n" +
	"- If no test framework exists, create a proper test file using the   language's standard test library (pytest for Python, cargo test for Rust,   jest/vitest for JS/TS).\n" +
	"- Keep tests focused and fast — test interactions, not individual features.\n" +
	"- Name test files descriptively based on WHAT they test:\n" +
	"  - Good: `test_parser_lexer_integration.py`, `test_api_auth_flow.py`\n" +
	"  - Bad: `test_integration_1.py`, `test_level_2.py`, `test_basic.py`\n" +
	"  - Pattern: `test_<component_a>_<component_b>_<behavior>.<ext>`\n" +
	"- Each test should have a clear assertion and error message.\n" +
	"- Place tests in the project's existing test directory.\n" +
	"\n" +
	"## Output\n" +
	"\n" +
	"Return an IntegrationTestResult JSON object with:\n" +
	"- `passed`: true if all tests pass\n" +
	"- `tests_written`: list of test file paths created\n" +
	"- `tests_run`: total number of tests executed\n" +
	"- `tests_passed`: number of passing tests\n" +
	"- `tests_failed`: number of failing tests\n" +
	"- `failure_details`: list of dicts with `test_name`, `error`, `file`\n" +
	"- `summary`: human-readable summary\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Do NOT modify the merged application code — only write and run tests.\n" +
	"- If tests fail, report the failures but do NOT attempt fixes.\n" +
	"- Keep tests in a dedicated test directory if one exists, otherwise alongside the code.\n" +
	"- Clean up any temporary files created during testing.\n" +
	"\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for running tests\n" +
	"- READ to inspect merged code\n" +
	"- WRITE to create test files\n" +
	"- GLOB to find files by pattern\n" +
	"- GREP to search for patterns"

const wantRepoFinalizeSystemPrompt = "You are a senior engineer doing the final review before a repository is shared with the team. An autonomous pipeline has just built this project from scratch — planning, coding, testing, merging, and verifying. Your job is the last mile: ensure the repository is clean, professional, and ready for a pull request or handoff.\n" +
	"\n" +
	"## What \"Production-Ready\" Means\n" +
	"\n" +
	"Imagine a new team member cloning this repo for the first time. They should see:\n" +
	"- Only intentional, purposeful files — no build artifacts, no tooling   leftovers, no pipeline infrastructure\n" +
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
	"## What NOT to Do\n" +
	"\n" +
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

const wantGitHubPRSystemPrompt = "You are a DevOps engineer responsible for pushing completed work to GitHub and\n" +
	"creating a pull request. You work at the end of an autonomous build pipeline\n" +
	"that has already planned, coded, tested, and verified the changes.\n" +
	"\n" +
	"## Your Responsibilities\n" +
	"\n" +
	"1. Push the integration branch to the remote origin.\n" +
	"2. Create a pull request using the `gh` CLI.\n" +
	"3. Return the PR URL and number.\n" +
	"\n" +
	"## Constraints\n" +
	"\n" +
	"- Use `git push origin <branch>` to push.\n" +
	"- Use `gh pr create` to create the PR (NOT `--draft` — open the PR ready for review).\n" +
	"- The `GH_TOKEN` environment variable is already set for authentication.\n" +
	"- Do NOT merge the PR.\n" +
	"- Do NOT modify any code or files.\n" +
	"- If push or PR creation fails, report the error clearly.\n" +
	"\n" +
	"## PR Body Format\n" +
	"\n" +
	"The PR body should include:\n" +
	"1. A \"## Summary\" section with 2-4 bullet points describing what was built.\n" +
	"2. A \"## Changes\" section listing key files/areas modified.\n" +
	"3. A \"## Test plan\" section with verification steps.\n" +
	"4. A footer line: `🤖 Built with [AgentField SWE-AF](https://github.com/Agent-Field/SWE-AF)`\n" +
	"4. A footer line: `🔌 Powered by [AgentField](https://github.com/Agent-Field/agentfield)`\n" +
	"5. Add any other relevant information someone reviewing the PR would want to know. Do not be verbose, but explain when needed.\n" +
	"## Tools Available\n" +
	"\n" +
	"- BASH for git and gh commands"

func TestSystemPromptConstants(t *testing.T) {
	cases := []struct {
		name string
		got  string
		want string
	}{
		{"SetupSystemPrompt", SetupSystemPrompt, wantSetupSystemPrompt},
		{"CleanupSystemPrompt", CleanupSystemPrompt, wantCleanupSystemPrompt},
		{"GitInitSystemPrompt", GitInitSystemPrompt, wantGitInitSystemPrompt},
		{"MergerSystemPrompt", MergerSystemPrompt, wantMergerSystemPrompt},
		{"IntegrationTesterSystemPrompt", IntegrationTesterSystemPrompt, wantIntegrationTesterSystemPrompt},
		{"RepoFinalizeSystemPrompt", RepoFinalizeSystemPrompt, wantRepoFinalizeSystemPrompt},
		{"GitHubPRSystemPrompt", GitHubPRSystemPrompt, wantGitHubPRSystemPrompt},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("%s mismatch:\n got=%q\nwant=%q", c.name, c.got, c.want)
		}
	}
}

// twoRepoManifest is the multi-repo manifest used by the multi-repo goldens.
func twoRepoManifest() *WorkspaceManifest {
	return &WorkspaceManifest{
		Repos: []WorkspaceRepo{
			{RepoName: "repo", Role: "primary", AbsolutePath: "/ws/repo"},
			{RepoName: "dep", Role: "dependency", AbsolutePath: "/ws/dep"},
		},
	}
}

func TestWorkspaceContextBlock(t *testing.T) {
	if got := WorkspaceContextBlock(nil); got != "" {
		t.Errorf("nil manifest: want empty, got %q", got)
	}
	single := &WorkspaceManifest{Repos: []WorkspaceRepo{{RepoName: "repo", Role: "primary", AbsolutePath: "/ws/repo"}}}
	if got := WorkspaceContextBlock(single); got != "" {
		t.Errorf("single-repo manifest: want empty, got %q", got)
	}
	want := "## Workspace Repositories\n" +
		"\n" +
		"This task spans multiple repositories. Each repository is listed below with its role and local path:\n" +
		"\n" +
		"- **repo** (role: primary): `/ws/repo`\n" +
		"- **dep** (role: dependency): `/ws/dep`\n"
	if got := WorkspaceContextBlock(twoRepoManifest()); got != want {
		t.Errorf("multi-repo block mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func setupIssues() []map[string]any {
	return []map[string]any{
		{"name": "lexer", "title": "Build the lexer", "sequence_number": 1},
		{"name": "parser", "title": "Build the parser", "sequence_number": 12},
	}
}

func TestWorkspaceSetupTaskPromptMulti(t *testing.T) {
	// Exercises: multi-repo context block, Build-ID branch, seq zero-fill (01, 12).
	want := "## Workspace Repositories\n" +
		"\n" +
		"This task spans multiple repositories. Each repository is listed below with its role and local path:\n" +
		"\n" +
		"- **repo** (role: primary): `/ws/repo`\n" +
		"- **dep** (role: dependency): `/ws/dep`\n" +
		"\n" +
		"## Workspace Setup Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Integration branch**: `feature/x`\n" +
		"- **Worktrees directory**: `/ws/repo/.worktrees`\n" +
		"- **Build ID**: `b12ab`\n" +
		"\n" +
		"### Issues to create worktrees for:\n" +
		"- issue_name=`lexer`, seq=`01`, title: Build the lexer\n" +
		"- issue_name=`parser`, seq=`12`, title: Build the parser\n" +
		"\n" +
		"## Your Task\n" +
		"1. Ensure you are in the main repository directory.\n" +
		"2. For each issue, create a worktree:\n" +
		"   `git worktree add <worktrees_dir>/issue-b12ab-<NN>-<name> -b issue/b12ab-<NN>-<name> <integration_branch>`\n" +
		"   Branch names MUST be prefixed with the Build ID: `issue/b12ab-<NN>-<name>`\n" +
		"   Worktree dirs MUST be prefixed with the Build ID: `issue-b12ab-<NN>-<name>`\n" +
		"   This prevents collisions with other concurrent builds on the same repository.\n" +
		"3. Verify each worktree was created successfully.\n" +
		"4. Return a JSON object with `workspaces` and `success`.\n" +
		"\n" +
		"IMPORTANT: In the output JSON, `issue_name` must be the canonical name (e.g. `value-copy-trait`), NOT the sequence-prefixed name (e.g. `01-value-copy-trait`)."
	got := WorkspaceSetupTaskPrompt(WorkspaceSetupOptions{
		RepoPath:          "/ws/repo",
		IntegrationBranch: "feature/x",
		Issues:            setupIssues(),
		WorktreesDir:      "/ws/repo/.worktrees",
		BuildID:           "b12ab",
		WorkspaceManifest: twoRepoManifest(),
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestWorkspaceSetupTaskPromptSingle(t *testing.T) {
	// Exercises: nil manifest (no block), no Build ID, missing sequence_number -> 00.
	want := "## Workspace Setup Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Integration branch**: `main`\n" +
		"- **Worktrees directory**: `/ws/.worktrees`\n" +
		"\n" +
		"### Issues to create worktrees for:\n" +
		"- issue_name=`solo`, seq=`00`, title: Do it\n" +
		"\n" +
		"## Your Task\n" +
		"1. Ensure you are in the main repository directory.\n" +
		"2. For each issue, create a worktree:\n" +
		"   `git worktree add <worktrees_dir>/issue-<NN>-<name> -b issue/<NN>-<name> <integration_branch>`\n" +
		"3. Verify each worktree was created successfully.\n" +
		"4. Return a JSON object with `workspaces` and `success`.\n" +
		"\n" +
		"IMPORTANT: In the output JSON, `issue_name` must be the canonical name (e.g. `value-copy-trait`), NOT the sequence-prefixed name (e.g. `01-value-copy-trait`)."
	got := WorkspaceSetupTaskPrompt(WorkspaceSetupOptions{
		RepoPath:          "/ws/repo",
		IntegrationBranch: "main",
		Issues:            []map[string]any{{"name": "solo", "title": "Do it"}},
		WorktreesDir:      "/ws/.worktrees",
		BuildID:           "",
		WorkspaceManifest: nil,
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestWorkspaceCleanupTaskPrompt(t *testing.T) {
	want := "## Workspace Cleanup Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Worktrees directory**: `/ws/repo/.worktrees`\n" +
		"\n" +
		"### Branches/worktrees to clean up:\n" +
		"- Branch: `issue/01-lexer` → Worktree: `/ws/repo/.worktrees/issue-01-lexer`\n" +
		"- Branch: `issue/12-parser` → Worktree: `/ws/repo/.worktrees/issue-12-parser`\n" +
		"\n" +
		"## Your Task\n" +
		"1. Ensure you are in the main repository directory.\n" +
		"2. For each entry above, remove the worktree:\n" +
		"   `git worktree remove <worktree_path> --force`\n" +
		"3. Force-delete each branch (whether merged or not):\n" +
		"   `git branch -D <branch>`\n" +
		"4. Run `git worktree prune`.\n" +
		"5. If any `git worktree remove` fails, try `rm -rf <path>` then `git worktree prune`.\n" +
		"6. Return a JSON object with `success` and `cleaned`."
	got := WorkspaceCleanupTaskPrompt(WorkspaceCleanupOptions{
		RepoPath:        "/ws/repo",
		WorktreesDir:    "/ws/repo/.worktrees",
		BranchesToClean: []string{"issue/01-lexer", "issue/12-parser"},
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestGitInitTaskPromptFresh(t *testing.T) {
	// No Build ID branch.
	want := "## Repository Setup Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Project goal**: Build a CLI todo app\n" +
		"\n" +
		"## Your Task\n" +
		"1. Check if `.git` exists in the repository path.\n" +
		"2. Set up `.gitignore` for the project's ecosystem (detect language from existing files).\n" +
		"3. If fresh: `git init`, stage project files (respecting `.gitignore`), create initial commit.\n" +
		"4. If existing: record the current branch, create an integration branch.\n" +
		"5. Create the `.worktrees/` directory and ensure it's in `.gitignore`.\n" +
		"6. Detect the remote origin URL and default branch (if any).\n" +
		"7. Return a GitInitResult JSON object."
	got := GitInitTaskPrompt(GitInitOptions{RepoPath: "/ws/repo", Goal: "Build a CLI todo app", BuildID: ""})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestGitInitTaskPromptExisting(t *testing.T) {
	// Build ID branch present.
	want := "## Repository Setup Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Project goal**: Build a CLI todo app\n" +
		"- **Build ID**: `b12ab` (prefix integration branch slug with this)\n" +
		"\n" +
		"## Your Task\n" +
		"1. Check if `.git` exists in the repository path.\n" +
		"2. Set up `.gitignore` for the project's ecosystem (detect language from existing files).\n" +
		"3. If fresh: `git init`, stage project files (respecting `.gitignore`), create initial commit.\n" +
		"4. If existing: record the current branch, create an integration branch.\n" +
		"5. Create the `.worktrees/` directory and ensure it's in `.gitignore`.\n" +
		"6. Detect the remote origin URL and default branch (if any).\n" +
		"7. Return a GitInitResult JSON object."
	got := GitInitTaskPrompt(GitInitOptions{RepoPath: "/ws/repo", Goal: "Build a CLI todo app", BuildID: "b12ab"})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestMergerTaskPrompt(t *testing.T) {
	// Exercises: full branch with all fields, minimal branch (fields skipped),
	// file-conflicts with Python list repr.
	want := "## Merge Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Integration branch**: `feature/x`\n" +
		"\n" +
		"### Branches to Merge (in order)\n" +
		"\n" +
		"**issue/01-lexer** (issue: lexer)\n" +
		"  Description: the lexer\n" +
		"  Result: done lexer\n" +
		"  Files changed: a.py, b.py\n" +
		"\n" +
		"**issue/02-parser** (issue: parser)\n" +
		"\n" +
		"### Known File Conflicts (advance warning)\n" +
		"- `a.py` modified by: ['lexer', 'parser']\n" +
		"\n" +
		"### PRD Summary\n" +
		"PRD text\n" +
		"\n" +
		"### Architecture Summary\n" +
		"Arch text\n" +
		"\n" +
		"## Your Task\n" +
		"1. `cd` to the repository path and `git checkout <integration_branch>`.\n" +
		"2. Record the current HEAD SHA as `pre_merge_sha`.\n" +
		"3. For each branch (in order), run `git merge <branch> --no-ff`.\n" +
		"4. If conflicts occur, resolve them semantically (read both sides, understand intent).\n" +
		"5. After each merge, run a quick sanity check.\n" +
		"6. Decide whether integration testing is needed.\n" +
		"7. Return a MergeResult JSON object."
	got := MergerTaskPrompt(MergerOptions{
		RepoPath:          "/ws/repo",
		IntegrationBranch: "feature/x",
		BranchesToMerge: []map[string]any{
			{
				"branch_name":       "issue/01-lexer",
				"issue_name":        "lexer",
				"result_summary":    "done lexer",
				"files_changed":     []any{"a.py", "b.py"},
				"issue_description": "the lexer",
			},
			{"branch_name": "issue/02-parser", "issue_name": "parser"},
		},
		FileConflicts:       []map[string]any{{"file": "a.py", "issues": []any{"lexer", "parser"}}},
		PRDSummary:          "PRD text",
		ArchitectureSummary: "Arch text",
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestIntegrationTesterTaskPrompt(t *testing.T) {
	// Exercises: multi-repo block, empty result_summary (trailing space),
	// conflict resolutions with Python list repr.
	want := "## Workspace Repositories\n" +
		"\n" +
		"This task spans multiple repositories. Each repository is listed below with its role and local path:\n" +
		"\n" +
		"- **repo** (role: primary): `/ws/repo`\n" +
		"- **dep** (role: dependency): `/ws/dep`\n" +
		"\n" +
		"## Integration Testing Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Integration branch**: `feature/x`\n" +
		"\n" +
		"### Merged Branches\n" +
		"- **issue/01-lexer** (issue: lexer): done\n" +
		"  Files: a.py\n" +
		"- **issue/02-parser** (issue: parser): \n" +
		"\n" +
		"### Conflict Resolutions (HIGH PRIORITY for testing)\n" +
		"- `a.py` (branches: ['lexer', 'parser']): merged both\n" +
		"\n" +
		"### PRD Summary\n" +
		"PRD text\n" +
		"\n" +
		"### Architecture Summary\n" +
		"Arch text\n" +
		"\n" +
		"## Your Task\n" +
		"1. Checkout the integration branch.\n" +
		"2. Analyze the merged code to identify interaction points.\n" +
		"3. Write targeted integration tests (prioritize conflict areas).\n" +
		"4. Run all tests.\n" +
		"5. Return an IntegrationTestResult JSON object."
	got := IntegrationTesterTaskPrompt(IntegrationTesterOptions{
		RepoPath:          "/ws/repo",
		IntegrationBranch: "feature/x",
		MergedBranches: []map[string]any{
			{"branch_name": "issue/01-lexer", "issue_name": "lexer", "result_summary": "done", "files_changed": []any{"a.py"}},
			{"branch_name": "issue/02-parser", "issue_name": "parser"},
		},
		PRDSummary:          "PRD text",
		ArchitectureSummary: "Arch text",
		ConflictResolutions: []map[string]any{{"file": "a.py", "branches": []any{"lexer", "parser"}, "resolution_strategy": "merged both"}},
		WorkspaceManifest:   twoRepoManifest(),
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestRepoFinalizeTaskPrompt(t *testing.T) {
	want := "## Repository Finalization Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"\n" +
		"## Your Task\n" +
		"1. Survey the directory tree to understand the project and its ecosystem.\n" +
		"2. Identify and remove clear artifacts: dependency dirs (node_modules, __pycache__, .venv, etc.), build outputs, broken symlinks, pipeline leftovers (.artifacts/, .worktrees/), caches.\n" +
		"3. Create or update `.gitignore` with standard patterns for the detected language/framework, plus `.artifacts/`, `.worktrees/`, `.env`, `.DS_Store`.\n" +
		"4. Check `git status` — ensure the working tree is clean.\n" +
		"5. Commit any cleanup: `chore: finalize repo for handoff`\n" +
		"6. Return a JSON with:\n" +
		"   - `success`: true if the repo is now clean\n" +
		"   - `files_removed`: list of paths removed\n" +
		"   - `gitignore_updated`: whether .gitignore was created/modified\n" +
		"   - `summary`: what you did and why"
	got := RepoFinalizeTaskPrompt("/ws/repo")
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestGitHubPRTaskPrompt(t *testing.T) {
	// Exercises: build summary, completed issues (issue_name and name fallback),
	// debt (criterion/type + reason/description fallbacks), PR results success
	// (int pr_number) and failure branches.
	want := "## Push & PR Task\n" +
		"- **Repository path**: `/ws/repo`\n" +
		"- **Integration branch**: `feature/x`\n" +
		"- **Base branch (PR target)**: `main`\n" +
		"- **Project goal**: Build a CLI todo app\n" +
		"\n" +
		"### Build Summary\n" +
		"Built X\n" +
		"\n" +
		"### Completed Issues\n" +
		"- **lexer**: did lexer\n" +
		"- **parser**: did parser\n" +
		"\n" +
		"### Technical Debt\n" +
		"- [high] tests: missing\n" +
		"- [medium] perf: slow\n" +
		"\n" +
		"### All PR Results\n" +
		"- **repo**: PR #5 — http://x\n" +
		"- **dep**: FAILED — boom\n" +
		"\n" +
		"## Your Task\n" +
		"1. Push the integration branch to `origin`.\n" +
		"2. Generate a concise PR title from the goal (imperative mood, <70 chars).\n" +
		"3. Generate the PR body with Summary, Changes, and Test plan sections.\n" +
		"4. Create a PR: `gh pr create --base <base> --head <branch> --title '...' --body '...'`\n" +
		"   (do NOT pass `--draft` — the PR should be opened ready for review).\n" +
		"5. Return a GitHubPRResult JSON object with success, pr_url, pr_number."
	got := GitHubPRTaskPrompt(GitHubPROptions{
		RepoPath:          "/ws/repo",
		IntegrationBranch: "feature/x",
		BaseBranch:        "main",
		Goal:              "Build a CLI todo app",
		BuildSummary:      "Built X",
		CompletedIssues: []map[string]any{
			{"issue_name": "lexer", "result_summary": "did lexer"},
			{"name": "parser", "result_summary": "did parser"},
		},
		AccumulatedDebt: []map[string]any{
			{"severity": "high", "criterion": "tests", "reason": "missing"},
			{"type": "perf", "description": "slow"},
		},
		AllPRResults: []map[string]any{
			{"repo_name": "repo", "success": true, "pr_url": "http://x", "pr_number": 5},
			{"repo_name": "dep", "success": false, "error_message": "boom"},
		},
	})
	if got != want {
		t.Errorf("mismatch:\n got=%q\nwant=%q", got, want)
	}
}

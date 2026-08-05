package orch

import (
	"context"
	"os"
	"path/filepath"
	"strings"
)

// harnessMetadataPatterns are the directories the build harness writes INTO the
// target repository: the plan/issue/checkpoint artifacts and the per-issue
// worktrees. They are ours, not the user's work.
var harnessMetadataPatterns = []string{".artifacts/", ".worktrees/"}

// excludeHarnessMetadata keeps the harness's own bookkeeping out of the target
// repository's git view.
//
// The engine stages with `git add -A` at several points (auditor gate, review
// gate, leaf merger, diff fingerprint). None of them pass -f, so a pattern in
// .git/info/exclude is enough to stop every one of them at once — which is why
// this is a single repo-local write rather than a change at each add site.
//
// .git/info/exclude rather than .gitignore on purpose: it is not versioned, so
// it never appears in a diff, never conflicts with the user's own ignore rules,
// and disappears with the clone. The user's working files are untouched.
//
// Without it the harness's own metadata lands in `git status` and in the index,
// and any acceptance criterion about repository cleanliness becomes
// unsatisfiable — an issue whose AC read "`git diff --name-only HEAD` lists only
// ordinals.go" failed four consecutive engine cycles on .artifacts/ and
// .worktrees/ files while the build and test suites were green the whole time.
// Beyond that gate, a build that leaves harness droppings in a user's
// repository is simply wrong.
//
// Anything already staged by an earlier stage of this run is removed from the
// index (content on disk is kept). Every step is best-effort: a repo_path that
// is not a git repository yet — the fresh-folder path, where git_init has not
// run — must not fail the build.
func excludeHarnessMetadata(ctx context.Context, repoPath string) {
	if strings.TrimSpace(repoPath) == "" {
		return
	}
	gitDir := runProc(ctx, repoPath, "git", "rev-parse", "--git-dir")
	if gitDir.ExitCode != 0 {
		return
	}
	dir := strings.TrimSpace(gitDir.Stdout)
	if dir == "" {
		return
	}
	if !filepath.IsAbs(dir) {
		dir = filepath.Join(repoPath, dir)
	}
	excludePath := filepath.Join(dir, "info", "exclude")
	if err := os.MkdirAll(filepath.Dir(excludePath), 0o755); err != nil {
		return
	}
	present := map[string]struct{}{}
	if data, err := os.ReadFile(excludePath); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			present[strings.TrimSpace(line)] = struct{}{}
		}
	}
	missing := make([]string, 0, len(harnessMetadataPatterns))
	for _, pattern := range harnessMetadataPatterns {
		if _, ok := present[pattern]; !ok {
			missing = append(missing, pattern)
		}
	}
	if len(missing) > 0 {
		if file, err := os.OpenFile(
			excludePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644,
		); err == nil {
			_, _ = file.WriteString(strings.Join(missing, "\n") + "\n")
			_ = file.Close()
		}
	}
	// An exclude pattern does not evict a path git is already tracking, so drop
	// anything an earlier stage staged. --cached keeps the files on disk.
	for _, pattern := range harnessMetadataPatterns {
		runProc(ctx, repoPath, "git", "rm", "-r", "--cached", "-q",
			"--ignore-unmatch", "--", strings.TrimSuffix(pattern, "/"))
	}
}

// integrationBranchBase re-points the integration branch at the commit the
// workspace was on when the build started.
//
// run_git_init is a model role: its prompt says to branch from HEAD, but the
// agent runs the git commands itself and can pick a different base. Observed on
// a real repository — HEAD carried the reported bug, and the agent cut
// feature/<build-id>-<slug> from HEAD~1 instead, so the defect was not an
// ancestor of the branch the build would merge into. Every test on that branch
// passed for the wrong reason: the bug had never been there.
//
// baseSHA is resolved in code before the role is dispatched, so this repairs
// the branch against a fact rather than against the agent's own report. It only
// acts when the branch demonstrably does not contain baseSHA, and it refuses to
// touch a branch carrying commits of its own — by then the agent has done work
// a reset would discard, and losing that is worse than a wrong base. Returns
// whether it moved the branch.
func integrationBranchBase(
	ctx context.Context, repoPath, branch, baseSHA string,
) bool {
	if strings.TrimSpace(repoPath) == "" ||
		strings.TrimSpace(branch) == "" || strings.TrimSpace(baseSHA) == "" {
		return false
	}
	if runProc(ctx, repoPath, "git", "rev-parse", "--verify",
		"--quiet", branch+"^{commit}").ExitCode != 0 {
		return false
	}
	if runProc(ctx, repoPath, "git", "merge-base",
		"--is-ancestor", baseSHA, branch).ExitCode == 0 {
		return false // already descends from the run's starting commit
	}
	ahead := runProc(ctx, repoPath, "git", "rev-list", "--count", baseSHA+".."+branch)
	if strings.TrimSpace(ahead.Stdout) != "0" && ahead.ExitCode == 0 {
		return false // the branch carries work; do not discard it
	}
	current := strings.TrimSpace(
		runProc(ctx, repoPath, "git", "rev-parse", "--abbrev-ref", "HEAD").Stdout)
	if current == branch {
		return runProc(ctx, repoPath, "git", "reset", "--hard", baseSHA).ExitCode == 0
	}
	return runProc(ctx, repoPath, "git", "branch", "-f", branch, baseSHA).ExitCode == 0
}

// headSHA resolves repoPath's current commit, or "" when there is none (a fresh
// folder, or a repo with no commits yet).
func headSHA(ctx context.Context, repoPath string) string {
	if strings.TrimSpace(repoPath) == "" {
		return ""
	}
	res := runProc(ctx, repoPath, "git", "rev-parse", "--verify", "--quiet", "HEAD")
	if res.ExitCode != 0 {
		return ""
	}
	return strings.TrimSpace(res.Stdout)
}

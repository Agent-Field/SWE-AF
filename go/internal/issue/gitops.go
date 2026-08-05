package issue

// gitops.go ports swe_af/issue/git_ops.py — deterministic (non-LLM) git
// operations for the issue-level build path. The full pipeline delegates git
// work to LLM agents; the issue-level path owns exactly one worktree + one
// branch off a known base, so plain git commands are sufficient and keep the
// LLM budget for coding.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// GitOpsError mirrors git_ops.GitOpsError — a git failure the caller must fix.
type GitOpsError struct{ Msg string }

func (e *GitOpsError) Error() string { return e.Msg }

func gitOpsErrf(format string, args ...any) *GitOpsError {
	return &GitOpsError{Msg: fmt.Sprintf(format, args...)}
}

func ensureIssueReadyRepo(repoPath string) error {
	info, err := os.Stat(repoPath)
	if err != nil || !info.IsDir() {
		return gitOpsErrf("repo_path does not exist or is not a directory: %s", repoPath)
	}
	if _, _, code := runGit(repoPath, "rev-parse", "--git-dir"); code != 0 {
		return gitOpsErrf("repo_path is not a git repository: %s", repoPath)
	}
	if _, _, code := runGit(repoPath, "rev-parse", "--verify", "HEAD"); code != 0 {
		return gitOpsErrf(
			"repository has no commits; issue-level builds require an existing " +
				"base commit (use the feature-level build for empty repos)",
		)
	}
	return nil
}

func currentBranch(repoPath string) (string, error) {
	out, detail, code := runGit(repoPath, "rev-parse", "--abbrev-ref", "HEAD")
	if code != 0 {
		return "", gitOpsErrf("git rev-parse --abbrev-ref HEAD failed: %s", detail)
	}
	return out, nil
}

// resolveBase ports git_ops.resolve_base: (base_ref, base_sha), defaulting to
// the caller's current branch (or literal "HEAD" when detached).
func resolveBase(repoPath, baseBranch string) (string, string, error) {
	ref := baseBranch
	if ref == "" {
		var err error
		if ref, err = currentBranch(repoPath); err != nil {
			return "", "", err
		}
	}
	sha, _, code := runGit(repoPath, "rev-parse", "--verify", ref+"^{commit}")
	if code != 0 {
		return "", "", gitOpsErrf("base branch/ref not found: %s", ref)
	}
	return ref, sha, nil
}

func isDirty(repoPath string) bool {
	out, _, code := runGit(repoPath, "status", "--porcelain")
	return code == 0 && out != ""
}

// addWorktree creates an isolated worktree on a new branch off baseSHA,
// retrying briefly because concurrent `git worktree add` calls contend on the
// repo lock — the exact scenario a fan-out caller creates.
func addWorktree(repoPath, worktreePath, branch, baseSHA string) error {
	if err := os.MkdirAll(filepath.Dir(worktreePath), 0o755); err != nil {
		return gitOpsErrf("mkdir for worktree failed: %v", err)
	}
	const attempts = 3
	var lastDetail string
	for attempt := 1; attempt <= attempts; attempt++ {
		_, detail, code := runGit(repoPath, "worktree", "add", "-b", branch, worktreePath, baseSHA)
		if code == 0 {
			return nil
		}
		lastDetail = detail
		if attempt < attempts {
			time.Sleep(time.Duration(attempt) * 500 * time.Millisecond)
		}
	}
	return gitOpsErrf("git worktree add failed after %d attempts: %s", attempts, lastDetail)
}

func hasCommitIdentity(repoPath string) bool {
	out, _, code := runGit(repoPath, "config", "user.email")
	return code == 0 && out != ""
}

// junkPathspecs lists bytecode/cache junk that must never be versioned on the
// issue branch. The coder runs tests inside the worktree, so these appear as
// a side effect and an indiscriminate `git add` (ours or the coder's) would
// sweep them in. Ports git_ops._JUNK_PATHSPECS.
var junkPathspecs = []string{"*__pycache__*", "*.pyc", "*.pyo"}

// commitIndex commits whatever is staged. Returns the sha, or "" when the
// index is clean. Ports git_ops._commit_index.
func commitIndex(worktreePath, message string) (string, error) {
	args := []string{}
	if !hasCommitIdentity(worktreePath) {
		args = append(args,
			"-c", "user.name=SWE-AF",
			"-c", "user.email=swe-af@agentfield.local",
		)
	}
	args = append(args, "commit", "-m", message)
	if out, detail, code := runGit(worktreePath, args...); code != 0 {
		lower := strings.ToLower(out + "\n" + detail)
		if strings.Contains(lower, "nothing to commit") ||
			strings.Contains(lower, "nothing added to commit") {
			return "", nil
		}
		return "", gitOpsErrf("git commit failed: %s", detail)
	}
	sha, detail, code := runGit(worktreePath, "rev-parse", "HEAD")
	if code != 0 {
		return "", gitOpsErrf("git rev-parse HEAD failed: %s", detail)
	}
	return sha, nil
}

// scrubTrackedJunk untracks bytecode junk an agent committed on the issue
// branch. Returns the scrub commit sha, or "" when the branch was already
// clean. History stays append-only. Ports git_ops.scrub_tracked_junk.
func scrubTrackedJunk(worktreePath, issueName string) (string, error) {
	listArgs := append([]string{"ls-files", "--"}, junkPathspecs...)
	out, _, code := runGit(worktreePath, listArgs...)
	if code != 0 || out == "" {
		return "", nil
	}
	rmArgs := append([]string{"rm", "-r", "--cached", "-q", "--ignore-unmatch", "--"}, junkPathspecs...)
	if _, detail, code := runGit(worktreePath, rmArgs...); code != 0 {
		return "", gitOpsErrf("git rm --cached failed: %s", detail)
	}
	return commitIndex(worktreePath,
		fmt.Sprintf("chore(%s): untrack bytecode caches", issueName))
}

// commitAll commits any uncommitted changes inside the (isolated) worktree.
// `add -A` is safe here precisely because the worktree belongs to this build
// alone; bytecode junk is excluded. Returns the new commit sha, or "" when
// there was nothing to commit.
func commitAll(worktreePath, message string) (string, error) {
	if !isDirty(worktreePath) {
		return "", nil
	}
	addArgs := []string{"add", "-A", "--", "."}
	for _, p := range junkPathspecs {
		addArgs = append(addArgs, ":(exclude)"+p)
	}
	if _, detail, code := runGit(worktreePath, addArgs...); code != 0 {
		return "", gitOpsErrf("git add -A failed: %s", detail)
	}
	return commitIndex(worktreePath, message)
}

// newCommits returns commits on branch since baseSHA, oldest first; empty when
// the branch is gone.
func newCommits(repoPath, baseSHA, branch string) []string {
	out, _, code := runGit(repoPath, "rev-list", "--reverse", baseSHA+".."+branch)
	if code != 0 || out == "" {
		return nil
	}
	return strings.Fields(out)
}

func changedFiles(repoPath, baseSHA, branch string) []string {
	out, _, code := runGit(repoPath, "diff", "--name-only", baseSHA+".."+branch)
	if code != 0 || out == "" {
		return nil
	}
	return strings.Split(out, "\n")
}

func diffStat(repoPath, baseSHA, branch string) string {
	out, _, code := runGit(repoPath, "diff", "--stat", baseSHA+".."+branch)
	if code != 0 {
		return ""
	}
	return out
}

// removeWorktree removes the worktree (the branch survives). Best-effort.
func removeWorktree(repoPath, worktreePath string) {
	_, _, code := runGit(repoPath, "worktree", "remove", "--force", worktreePath)
	if code != 0 {
		if _, err := os.Stat(worktreePath); err == nil {
			_ = os.RemoveAll(worktreePath)
			runGit(repoPath, "worktree", "prune")
		}
	}
}

// deleteBranch deletes a branch (used only when it holds no commits).
func deleteBranch(repoPath, branch string) {
	runGit(repoPath, "branch", "-D", branch)
}

func remoteURL(repoPath string) string {
	out, _, code := runGit(repoPath, "remote", "get-url", "origin")
	if code != 0 {
		return ""
	}
	return out
}

func defaultRemoteBranch(repoPath string) string {
	out, _, code := runGit(repoPath, "symbolic-ref", "refs/remotes/origin/HEAD")
	if code != 0 {
		return ""
	}
	const prefix = "refs/remotes/origin/"
	if strings.HasPrefix(out, prefix) {
		return out[len(prefix):]
	}
	return ""
}

// ensureLocalExcludes adds patterns to the repo-local ignore file
// (.git/info/exclude) so issue-build bookkeeping (.artifacts, .worktrees)
// never shows up in the caller's `git status` — unlike .gitignore, this file
// is not versioned and never shows in a diff.
func ensureLocalExcludes(repoPath string, patterns []string) error {
	gitDir, detail, code := runGit(repoPath, "rev-parse", "--git-dir")
	if code != 0 {
		return gitOpsErrf("git rev-parse --git-dir failed: %s", detail)
	}
	if !filepath.IsAbs(gitDir) {
		gitDir = filepath.Join(repoPath, gitDir)
	}
	excludePath := filepath.Join(gitDir, "info", "exclude")
	if err := os.MkdirAll(filepath.Dir(excludePath), 0o755); err != nil {
		return gitOpsErrf("mkdir .git/info failed: %v", err)
	}
	existing := map[string]struct{}{}
	if data, err := os.ReadFile(excludePath); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			existing[strings.TrimSpace(line)] = struct{}{}
		}
	}
	var toAdd []string
	for _, p := range patterns {
		if _, ok := existing[p]; !ok {
			toAdd = append(toAdd, p)
		}
	}
	if len(toAdd) == 0 {
		return nil
	}
	f, err := os.OpenFile(excludePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return gitOpsErrf("open .git/info/exclude failed: %v", err)
	}
	defer f.Close()
	if _, err := f.WriteString(strings.Join(toAdd, "\n") + "\n"); err != nil {
		return gitOpsErrf("write .git/info/exclude failed: %v", err)
	}
	return nil
}

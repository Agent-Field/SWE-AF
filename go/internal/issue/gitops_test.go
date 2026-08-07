package issue

import (
	"path/filepath"
	"testing"
)

// A worktree add can die after creating its branch (repo-lock contention mid
// command); the retry must recover the leftover branch, not trip over it.
func TestAddWorktreeRecoversBranchLeftByFailedAttempt(t *testing.T) {
	repo := initRepo(t)
	baseSHA := gitT(t, repo, "rev-parse", "HEAD")
	branch := "issue/test-recovery"
	gitT(t, repo, "branch", branch, baseSHA)

	worktreePath := filepath.Join(repo, ".worktrees", "test-recovery")
	if err := addWorktree(repo, worktreePath, branch, baseSHA); err != nil {
		t.Fatalf("addWorktree with leftover branch: %v", err)
	}
	if got := gitT(t, worktreePath, "rev-parse", "HEAD"); got != baseSHA {
		t.Errorf("worktree HEAD = %s, want %s", got, baseSHA)
	}
	if got := gitT(t, worktreePath, "rev-parse", "--abbrev-ref", "HEAD"); got != branch {
		t.Errorf("worktree branch = %q, want %q", got, branch)
	}
}

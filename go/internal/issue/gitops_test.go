package issue

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Issue branches with commits are deliverables that outlive their build. A
// branch that already exists when addWorktree starts belongs to someone —
// it must be a hard failure, never a silent reset.
func TestAddWorktreeRefusesPreexistingBranch(t *testing.T) {
	repo := initRepo(t)
	baseSHA := gitT(t, repo, "rev-parse", "HEAD")
	branch := "issue/test-preexisting"
	gitT(t, repo, "branch", branch, baseSHA)
	gitT(t, repo, "commit", "--allow-empty", "-q", "-m", "advance main")

	worktreePath := filepath.Join(repo, ".worktrees", "test-preexisting")
	err := addWorktree(repo, worktreePath, branch, gitT(t, repo, "rev-parse", "HEAD"))
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("error = %v, want refusal", err)
	}
	if got := gitT(t, repo, "rev-parse", branch); got != baseSHA {
		t.Errorf("pre-existing branch moved: %s, want %s", got, baseSHA)
	}
	if _, statErr := os.Stat(worktreePath); !os.IsNotExist(statErr) {
		t.Errorf("worktree unexpectedly exists: %v", statErr)
	}
}

// A worktree add can lose the repo-lock race after creating its branch. Once
// an attempt has failed transiently, the retry must reclaim that leftover
// branch instead of tripping over it.
func TestAddWorktreeRecoversBranchLeftByFailedAttempt(t *testing.T) {
	repo := initRepo(t)
	baseSHA := gitT(t, repo, "rev-parse", "HEAD")
	branch := "issue/test-recovery"
	worktreePath := filepath.Join(repo, ".worktrees", "test-recovery")

	// Obstruct the worktree path so the first attempt fails for a non-branch
	// reason, then clear it and plant the branch a dying attempt would have
	// left behind. The retry backoff (500ms) gives the repair room; if a slow
	// machine ever lets an attempt beat the repair, the call still succeeds —
	// it just exercises the plain -b path instead.
	if err := os.MkdirAll(filepath.Join(worktreePath, "occupied"), 0o755); err != nil {
		t.Fatal(err)
	}
	go func() {
		time.Sleep(150 * time.Millisecond)
		_ = os.RemoveAll(worktreePath)
		_ = exec.Command("git", "-C", repo, "branch", branch, baseSHA).Run()
	}()

	if err := addWorktree(repo, worktreePath, branch, baseSHA); err != nil {
		t.Fatalf("addWorktree: %v", err)
	}
	if got := gitT(t, worktreePath, "rev-parse", "HEAD"); got != baseSHA {
		t.Errorf("worktree HEAD = %s, want %s", got, baseSHA)
	}
	if got := gitT(t, worktreePath, "rev-parse", "--abbrev-ref", "HEAD"); got != branch {
		t.Errorf("worktree branch = %q, want %q", got, branch)
	}
}

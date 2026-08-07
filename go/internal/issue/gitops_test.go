package issue

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
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
// branch instead of tripping over it. The transient failure is scripted
// through the worktreeGit seam; the recovery itself runs real git against the
// leftover state such a failure produces.
func TestAddWorktreeRecoversBranchLeftByFailedAttempt(t *testing.T) {
	repo := initRepo(t)
	baseSHA := gitT(t, repo, "rev-parse", "HEAD")
	branch := "issue/test-recovery"
	worktreePath := filepath.Join(repo, ".worktrees", "test-recovery")

	var flags []string
	orig := worktreeGit
	t.Cleanup(func() { worktreeGit = orig })
	worktreeGit = func(repoPath string, args ...string) (string, string, int) {
		flags = append(flags, args[2])
		if len(flags) == 1 {
			// The lost race: git dies after creating the branch.
			gitT(t, repo, "branch", branch, baseSHA)
			return "", "fatal: Unable to create '.git/worktrees': File exists.", 128
		}
		return orig(repoPath, args...)
	}

	if err := addWorktree(repo, worktreePath, branch, baseSHA); err != nil {
		t.Fatalf("addWorktree: %v", err)
	}
	if want := []string{"-b", "-B"}; len(flags) != 2 || flags[0] != want[0] || flags[1] != want[1] {
		t.Fatalf("flags = %v, want %v", flags, want)
	}
	if got := gitT(t, worktreePath, "rev-parse", "HEAD"); got != baseSHA {
		t.Errorf("worktree HEAD = %s, want %s", got, baseSHA)
	}
	if got := gitT(t, worktreePath, "rev-parse", "--abbrev-ref", "HEAD"); got != branch {
		t.Errorf("worktree branch = %q, want %q", got, branch)
	}
}

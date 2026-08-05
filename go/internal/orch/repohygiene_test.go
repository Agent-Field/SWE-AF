package orch

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func hygieneRepo(t *testing.T) string {
	t.Helper()
	repo := t.TempDir()
	ctx := context.Background()
	for _, args := range [][]string{
		{"init", "-b", "main"},
		{"config", "user.name", "hygiene"},
		{"config", "user.email", "hygiene@example.test"},
	} {
		if res := runProc(ctx, repo, "git", args...); res.ExitCode != 0 {
			t.Fatalf("git %v: %s", args, res.Stderr)
		}
	}
	if err := os.WriteFile(filepath.Join(repo, "README.md"), []byte("# demo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if res := runProc(ctx, repo, "git", "add", "README.md"); res.ExitCode != 0 {
		t.Fatal(res.Stderr)
	}
	if res := runProc(ctx, repo, "git", "commit", "-m", "seed"); res.ExitCode != 0 {
		t.Fatal(res.Stderr)
	}
	return repo
}

func writeUnder(t *testing.T, repo, rel, body string) {
	t.Helper()
	path := filepath.Join(repo, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestExcludeHarnessMetadataKeepsGitViewClean: the harness writes .artifacts/
// and .worktrees/ into the target repo, and the engine stages with `git add -A`
// at several points. After the exclusion neither shows up in status, so an
// acceptance criterion about repository cleanliness is satisfiable and the
// user's repo stays unpolluted.
func TestExcludeHarnessMetadataKeepsGitViewClean(t *testing.T) {
	ctx := context.Background()
	repo := hygieneRepo(t)
	writeUnder(t, repo, ".artifacts/plan/prd.md", "# prd\n")
	writeUnder(t, repo, ".artifacts/execution/checkpoint.json", "{}\n")
	writeUnder(t, repo, ".worktrees/issue-01/marker", "x\n")
	writeUnder(t, repo, "ordinals.go", "package humanize\n")

	excludeHarnessMetadata(ctx, repo)

	// The engine's staging step, verbatim in shape.
	if res := runProc(ctx, repo, "git", "add", "-A"); res.ExitCode != 0 {
		t.Fatalf("git add -A: %s", res.Stderr)
	}
	staged := runProc(ctx, repo, "git", "diff", "--name-only", "HEAD").Stdout
	for _, unwanted := range []string{".artifacts", ".worktrees"} {
		if strings.Contains(staged, unwanted) {
			t.Errorf("harness metadata %s reached the index:\n%s", unwanted, staged)
		}
	}
	if !strings.Contains(staged, "ordinals.go") {
		t.Errorf("real work must still be staged, got:\n%s", staged)
	}
	// AC-shaped check: exactly the deliverable.
	if strings.TrimSpace(staged) != "ordinals.go" {
		t.Errorf("git diff --name-only HEAD = %q, want only ordinals.go", strings.TrimSpace(staged))
	}
}

// TestExcludeHarnessMetadataUnstagesWhatEarlierStagesStaged: an exclude pattern
// does not evict an already-tracked path, so the helper must also drop what a
// previous stage of the same run put in the index.
func TestExcludeHarnessMetadataUnstagesWhatEarlierStagesStaged(t *testing.T) {
	ctx := context.Background()
	repo := hygieneRepo(t)
	writeUnder(t, repo, ".artifacts/plan/prd.md", "# prd\n")
	if res := runProc(ctx, repo, "git", "add", "-A"); res.ExitCode != 0 {
		t.Fatal(res.Stderr)
	}
	if !strings.Contains(runProc(ctx, repo, "git", "diff", "--name-only", "HEAD").Stdout, ".artifacts") {
		t.Fatal("fixture precondition: .artifacts should be staged")
	}
	excludeHarnessMetadata(ctx, repo)
	if staged := runProc(ctx, repo, "git", "diff", "--name-only", "HEAD").Stdout; strings.Contains(staged, ".artifacts") {
		t.Errorf("staged metadata not dropped:\n%s", staged)
	}
	if _, err := os.Stat(filepath.Join(repo, ".artifacts", "plan", "prd.md")); err != nil {
		t.Errorf("--cached must keep the file on disk: %v", err)
	}
}

// TestExcludeHarnessMetadataToleratesNonRepo: repo_path may not be a git repo
// yet (the fresh-folder path runs before git_init). It must not fail the build.
func TestExcludeHarnessMetadataToleratesNonRepo(t *testing.T) {
	excludeHarnessMetadata(context.Background(), t.TempDir())
	excludeHarnessMetadata(context.Background(), "")
}

// TestIntegrationBranchBaseRecutsFromRunStart is the observed defect: git_init
// cut feature/<id> from HEAD~1, so the reported bug was not an ancestor of the
// branch the build merges into and its tests passed for the wrong reason.
func TestIntegrationBranchBaseRecutsFromRunStart(t *testing.T) {
	ctx := context.Background()
	repo := hygieneRepo(t)
	preBug := headSHA(ctx, repo)
	writeUnder(t, repo, "ordinals.go", "package humanize // buggy\n")
	runProc(ctx, repo, "git", "add", "-A")
	runProc(ctx, repo, "git", "commit", "-m", "plant the bug")
	buggy := headSHA(ctx, repo)
	if buggy == preBug || buggy == "" {
		t.Fatal("fixture precondition")
	}

	// git_init's mistake: branch from the commit BEFORE the bug.
	if res := runProc(ctx, repo, "git", "checkout", "-q", "-b", "feature/x", preBug); res.ExitCode != 0 {
		t.Fatal(res.Stderr)
	}
	if runProc(ctx, repo, "git", "merge-base", "--is-ancestor", buggy, "feature/x").ExitCode == 0 {
		t.Fatal("fixture precondition: bug must not be an ancestor yet")
	}

	if !integrationBranchBase(ctx, repo, "feature/x", buggy) {
		t.Fatal("expected the branch to be re-cut")
	}
	if runProc(ctx, repo, "git", "merge-base", "--is-ancestor", buggy, "feature/x").ExitCode != 0 {
		t.Error("feature/x must descend from the run's starting commit")
	}
}

// TestIntegrationBranchBaseLeavesCorrectAndWorkingBranchesAlone: no-op when the
// branch already descends from the base, and never discards real work.
func TestIntegrationBranchBaseLeavesCorrectAndWorkingBranchesAlone(t *testing.T) {
	ctx := context.Background()

	t.Run("already correct", func(t *testing.T) {
		repo := hygieneRepo(t)
		base := headSHA(ctx, repo)
		runProc(ctx, repo, "git", "checkout", "-q", "-b", "feature/ok")
		if integrationBranchBase(ctx, repo, "feature/ok", base) {
			t.Error("must not touch a branch that already descends from base")
		}
	})

	t.Run("branch carries work", func(t *testing.T) {
		repo := hygieneRepo(t)
		preBug := headSHA(ctx, repo)
		writeUnder(t, repo, "a.go", "package a\n")
		runProc(ctx, repo, "git", "add", "-A")
		runProc(ctx, repo, "git", "commit", "-m", "bug")
		buggy := headSHA(ctx, repo)
		runProc(ctx, repo, "git", "checkout", "-q", "-b", "feature/busy", preBug)
		writeUnder(t, repo, "work.go", "package work\n")
		runProc(ctx, repo, "git", "add", "-A")
		runProc(ctx, repo, "git", "commit", "-m", "agent work")
		before := headSHA(ctx, repo)
		if integrationBranchBase(ctx, repo, "feature/busy", buggy) {
			t.Error("must not reset a branch that carries commits")
		}
		if headSHA(ctx, repo) != before {
			t.Error("agent work was discarded")
		}
	})

	t.Run("missing branch and empty args", func(t *testing.T) {
		repo := hygieneRepo(t)
		base := headSHA(ctx, repo)
		if integrationBranchBase(ctx, repo, "feature/nope", base) {
			t.Error("missing branch must be a no-op")
		}
		if integrationBranchBase(ctx, repo, "", base) || integrationBranchBase(ctx, repo, "b", "") {
			t.Error("empty args must be a no-op")
		}
	})
}

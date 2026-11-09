package nochange

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func run(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

// haszeroRepo is #309's shape: main already has HasZero, and the integration
// branch is cut from it.
func haszeroRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	run(t, dir, "init", "-q", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(dir, "ledger.go"),
		[]byte("package ledger\n\nfunc (l *Ledger) HasZero() bool { return false }\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(t, dir, "add", "ledger.go")
	run(t, dir, "commit", "-q", "-m", "seed")
	run(t, dir, "checkout", "-q", "-b", "feature/haszero")
	return dir
}

// Issue #309 (WorkAF r9): the branch carries an empty coder commit and an
// empty --no-ff merge, two commits ahead of main with main's tree. It changes
// nothing, so it is empty.
func TestTwoEmptyCommitsAreNoChange(t *testing.T) {
	dir := haszeroRepo(t)
	run(t, dir, "commit", "-q", "--allow-empty", "-m", "issue/ledger-haszero: verify HasZero predicate")
	run(t, dir, "checkout", "-q", "-b", "issue/x", "main")
	run(t, dir, "commit", "-q", "--allow-empty", "-m", "noop")
	run(t, dir, "checkout", "-q", "feature/haszero")
	run(t, dir, "merge", "-q", "--no-ff", "issue/x", "-m", "merge(level-0): issue/x")

	v := Check(context.Background(), dir, "feature/haszero", "main")
	if !v.Known || !v.Empty || v.Base != "main" {
		t.Fatalf("verdict = %+v, want known and empty against main", v)
	}
}

// A branch that changes a file is not empty, and the file is named.
func TestAChangedFileIsAChange(t *testing.T) {
	dir := haszeroRepo(t)
	if err := os.WriteFile(filepath.Join(dir, "ledger_test.go"), []byte("package ledger\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(t, dir, "add", "ledger_test.go")
	run(t, dir, "commit", "-q", "-m", "add test")

	v := Check(context.Background(), dir, "feature/haszero", "main")
	if !v.Known || v.Empty || len(v.Files) != 1 || v.Files[0] != "ledger_test.go" {
		t.Fatalf("verdict = %+v", v)
	}
}

// A base that moved on after the branch was cut is not the branch's change:
// the comparison is from the merge base.
func TestABaseThatMovedOnIsNotTheRunsChange(t *testing.T) {
	dir := haszeroRepo(t)
	run(t, dir, "commit", "-q", "--allow-empty", "-m", "empty")
	run(t, dir, "checkout", "-q", "main")
	if err := os.WriteFile(filepath.Join(dir, "other.go"), []byte("package ledger\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(t, dir, "add", "other.go")
	run(t, dir, "commit", "-q", "-m", "main moved on")

	v := Check(context.Background(), dir, "feature/haszero", "main")
	if !v.Known || !v.Empty {
		t.Fatalf("verdict = %+v, want empty", v)
	}
}

// When no base resolves the check says nothing, and the caller carries on.
func TestAnUnknownBaseIsNotAVerdict(t *testing.T) {
	dir := haszeroRepo(t)
	if v := Check(context.Background(), dir, "feature/haszero", "trunk"); v.Known {
		t.Fatalf("verdict = %+v, want unknown", v)
	}
	if v := Check(context.Background(), dir, "no/such-branch", "main"); v.Known {
		t.Fatalf("verdict = %+v, want unknown for a missing branch", v)
	}
	// The run's starting commit is the fallback when the base branch is absent.
	if v := Check(context.Background(), dir, "feature/haszero", "trunk", "main"); !v.Known || !v.Empty {
		t.Fatalf("verdict = %+v, want empty against the fallback", v)
	}
}

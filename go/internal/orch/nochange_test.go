package orch

import (
	"context"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
)

// issue309Repo is the repository #309 ran against (WorkAF r9): main already
// has HasZero, and the integration branch carries the coder's empty "verify"
// commit and the level's empty --no-ff merge, two commits ahead of main with
// main's tree.
func issue309Repo(t *testing.T, change bool) string {
	t.Helper()
	repo := t.TempDir()
	gitT(t, repo, "init", "-q", "-b", "main")
	writeCommit(t, repo, "base", map[string]string{
		"ledger.go": "package ledger\n\nfunc (l *Ledger) HasZero() bool { return false }\n",
	})
	id := []string{"-c", "user.name=t", "-c", "user.email=t@t"}
	gitT(t, repo, "checkout", "-q", "-b", "feature/haszero")
	gitT(t, repo, "checkout", "-q", "-b", "issue/01-ledger-haszero")
	gitT(t, repo, append(id, "commit", "-q", "--allow-empty", "-m", "issue/ledger-haszero: verify HasZero predicate")...)
	if change {
		writeCommit(t, repo, "issue/ledger-haszero: add test", map[string]string{"ledger_test.go": "package ledger\n"})
	}
	gitT(t, repo, "checkout", "-q", "feature/haszero")
	gitT(t, repo, append(id, "merge", "-q", "--no-ff", "issue/01-ledger-haszero", "-m", "merge(level-0): issue/01-ledger-haszero")...)
	return repo
}

// prPhase runs singleRepoPR over repo and returns the reasoners called, the
// git commands run and the notes recorded.
func prPhase(t *testing.T, repo string) (calls, gits, notes []string, opened int) {
	t.Helper()
	prevGit, prevGH := runGit, runGH
	t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
	runGit = func(_ context.Context, _ string, args ...string) cmdResult {
		gits = append(gits, strings.Join(args, " "))
		return cmdResult{}
	}
	runGH = func(context.Context, string, ...string) cmdResult {
		return cmdResult{Stdout: "https://github.com/o/r/pull/315\n"}
	}
	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		calls = append(calls, target[strings.LastIndex(target, ".")+1:])
		return map[string]any{"pr_url": "https://github.com/o/r/pull/315", "pr_number": 315}, nil
	}}
	cfg := &config.BuildConfig{EnableGithubPR: true}
	gitConfig := map[string]any{
		"integration_branch": "feature/haszero", "remote_url": "https://github.com/o/r.git",
		"remote_default_branch": "main",
	}
	prs, _ := singleRepoPR(context.Background(), &Deps{App: app, NodeID: "swe-planner"}, cfg,
		map[string]string{}, gitConfig, map[string]any{}, repo, t.TempDir(),
		"Resolve GitHub issue #309: Add HasZero to Ledger", "Success", "", "", "")
	return calls, gits, app.notes, len(prs)
}

// Issue #309 → PR #315: a run whose branch changes no file pushes nothing,
// opens no pull request, and ends with the plain note the factory reads.
func TestARunThatChangedNothingOpensNoPullRequest(t *testing.T) {
	calls, gits, notes, opened := prPhase(t, issue309Repo(t, false))
	if opened != 0 || len(calls) != 0 {
		t.Fatalf("opened %d PRs, called %v", opened, calls)
	}
	for _, g := range gits {
		if strings.HasPrefix(g, "push") {
			t.Fatalf("pushed a branch that changes nothing: %v", gits)
		}
	}
	found := false
	for _, n := range notes {
		found = found || n == nochange.Note
	}
	if !found || !strings.HasPrefix(nochange.Note, "No pull request:") {
		t.Fatalf("notes = %v, want %q", notes, nochange.Note)
	}
}

// The control: the same branch with one changed file still pushes and opens
// its pull request.
func TestARunThatChangedAFileStillOpensItsPullRequest(t *testing.T) {
	calls, gits, _, opened := prPhase(t, issue309Repo(t, true))
	if opened != 1 || strings.Join(calls, " ") != "run_github_pr" {
		t.Fatalf("opened %d PRs, called %v", opened, calls)
	}
	if len(gits) == 0 || !strings.HasPrefix(gits[0], "push") {
		t.Fatalf("git = %v, want a push", gits)
	}
}

package orch

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
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

// Issue #309 → PR #315: with SWE_STRICT_BUILD enabled, a run whose branch
// changes no file pushes nothing, opens no pull request, and ends with the
// plain note the factory reads.
func TestARunThatChangedNothingOpensNoPullRequest(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
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
// its pull request under SWE_STRICT_BUILD.
func TestARunThatChangedAFileStillOpensItsPullRequest(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
	calls, gits, _, opened := prPhase(t, issue309Repo(t, true))
	if opened != 1 || strings.Join(calls, " ") != "run_github_pr" {
		t.Fatalf("opened %d PRs, called %v", opened, calls)
	}
	if len(gits) == 0 || !strings.HasPrefix(gits[0], "push") {
		t.Fatalf("git = %v, want a push", gits)
	}
}

// N1: With SWE_STRICT_BUILD unset, a zero-change plain build proceeds to the
// push/PR step as on main and records no nochange.Note. With SWE_STRICT_BUILD
// set, or via build_issue without the switch, no PR is opened and
// nochange.Note is recorded.
func TestZeroChangePlainBuildReachesPRUnlessStrict(t *testing.T) {
	t.Run("plain build switch unset reaches push and PR", func(t *testing.T) {
		defer withExecCtx("run-n1-unset", "exec-n1-unset")()
		t.Setenv(config.EnvStrictBuild, "1")
		if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
			t.Fatal(err)
		}
		repo := issue309Repo(t, false)
		calls, git, _, notes, prInput, result := buildWithRemote(t, repo, false, true)
		if !strings.Contains(strings.Join(git, "\n"), "push -u origin feature/haszero") {
			t.Fatalf("plain zero-change build did not push: %v", git)
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("plain zero-change build did not reach run_github_pr: %v", calls)
		}
		if _, hasStrict := prInput["strict"]; hasStrict {
			t.Fatalf("plain build with switch unset passed strict in run_github_pr input: %v", prInput)
		}
		if prs := asMapList(result["pr_results"]); len(prs) != 1 || prs[0]["pr_url"] == "" {
			t.Fatalf("pr_results = %v, want opened PR", result["pr_results"])
		}
		for _, n := range notes {
			if n == nochange.Note {
				t.Fatalf("plain build with switch unset recorded %q: %v", nochange.Note, notes)
			}
		}
	})

	t.Run("plain build switch set skips PR and records note", func(t *testing.T) {
		defer withExecCtx("run-n1-set", "exec-n1-set")()
		t.Setenv(config.EnvStrictBuild, "1")
		repo := issue309Repo(t, false)
		calls, git, _, notes, _, result := buildWithRemote(t, repo, false, true)
		for _, c := range calls {
			if c == "run_github_pr" {
				t.Fatalf("strict zero-change build called run_github_pr: %v", calls)
			}
		}
		for _, g := range git {
			if strings.HasPrefix(g, "push") {
				t.Fatalf("strict zero-change build pushed: %v", git)
			}
		}
		if prs := asMapList(result["pr_results"]); len(prs) != 0 {
			t.Fatalf("pr_results = %v, want empty", prs)
		}
		if !strings.Contains(strings.Join(notes, "\n"), nochange.Note) {
			t.Fatalf("notes = %v, want %q", notes, nochange.Note)
		}
	})

	t.Run("build_issue without switch skips PR and records note", func(t *testing.T) {
		defer withExecCtx("run-n1-issue", "exec-n1-issue")()
		t.Setenv(config.EnvStrictBuild, "1")
		if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
			t.Fatal(err)
		}
		repo := issue309Repo(t, false)
		calls, git, _, notes, _, result := buildWithRemote(t, repo, true, true)
		for _, c := range calls {
			if c == "run_github_pr" {
				t.Fatalf("build_issue zero-change build called run_github_pr: %v", calls)
			}
		}
		for _, g := range git {
			if strings.HasPrefix(g, "push") {
				t.Fatalf("build_issue zero-change build pushed: %v", git)
			}
		}
		if prs := asMapList(result["pr_results"]); len(prs) != 0 {
			t.Fatalf("pr_results = %v, want empty", prs)
		}
		if !strings.Contains(strings.Join(notes, "\n"), nochange.Note) {
			t.Fatalf("notes = %v, want %q", notes, nochange.Note)
		}
	})

	t.Run("multiRepoPRs gated by strict", func(t *testing.T) {
		repoA := issue309Repo(t, false)
		repoB := issue309Repo(t, false)
		manifest := &schemas.WorkspaceManifest{
			Repos: []schemas.WorkspaceRepo{
				{
					RepoName:     "repo-a",
					RepoURL:      "https://github.com/o/a.git",
					AbsolutePath: repoA,
					CreatePR:     true,
					GitInitResult: map[string]any{
						"integration_branch":    "feature/haszero",
						"remote_url":            "https://github.com/o/a.git",
						"remote_default_branch": "main",
					},
				},
				{
					RepoName:     "repo-b",
					RepoURL:      "https://github.com/o/b.git",
					AbsolutePath: repoB,
					CreatePR:     true,
					GitInitResult: map[string]any{
						"integration_branch":    "feature/haszero",
						"remote_url":            "https://github.com/o/b.git",
						"remote_default_branch": "main",
					},
				},
			},
		}
		cfg := &config.BuildConfig{EnableGithubPR: true}

		// strict=false (plain build, switch unset): reaches run_github_pr for both repos, no nochange.Note.
		var unsetCalls int
		unsetApp := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
			if strings.HasSuffix(target, ".run_github_pr") {
				unsetCalls++
				if _, ok := input["strict"]; ok {
					t.Errorf("strict=false passed strict key in multiRepoPRs: %v", input)
				}
			}
			return map[string]any{"success": true, "pr_url": "https://github.com/o/r/pull/1", "pr_number": 1}, nil
		}}
		prs, _ := multiRepoPRs(context.Background(), &Deps{App: unsetApp, NodeID: "swe-planner"},
			cfg, map[string]string{}, manifest, map[string]any{}, t.TempDir(), "goal", "summary", false)
		if unsetCalls != 2 || len(prs) != 2 {
			t.Fatalf("strict=false: calls=%d prs=%d, want 2 and 2", unsetCalls, len(prs))
		}
		for _, n := range unsetApp.notes {
			if n == nochange.Note {
				t.Fatalf("strict=false recorded %q: %v", nochange.Note, unsetApp.notes)
			}
		}

		// strict=true: skips both repos and records nochange.Note.
		var strictCalls int
		strictApp := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
			if strings.HasSuffix(target, ".run_github_pr") {
				strictCalls++
			}
			return map[string]any{"success": true, "pr_url": "https://github.com/o/r/pull/1", "pr_number": 1}, nil
		}}
		prs, _ = multiRepoPRs(context.Background(), &Deps{App: strictApp, NodeID: "swe-planner"},
			cfg, map[string]string{}, manifest, map[string]any{}, t.TempDir(), "goal", "summary", true)
		if strictCalls != 0 || len(prs) != 0 {
			t.Fatalf("strict=true: calls=%d prs=%d, want 0 and 0", strictCalls, len(prs))
		}
		if !strings.Contains(strings.Join(strictApp.notes, "\n"), nochange.Note) {
			t.Fatalf("strict=true missing %q: %v", nochange.Note, strictApp.notes)
		}
	})
}

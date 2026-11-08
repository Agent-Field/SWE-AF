package orch

import (
	"context"
	"strings"
	"sync"
	"testing"
)

// buildWithRemote runs Build against a git_init that found a GitHub remote, so
// the push-and-PR phase is live, with a verifier that answers passed. It
// returns the reasoners that were called and the git and gh argv that ran.
func buildWithRemote(t *testing.T, passed bool) (calls []string, git, gh []string) {
	t.Helper()
	prevGit, prevGH := runGit, runGH
	t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
	var mu sync.Mutex
	runGit = func(_ context.Context, _ string, args ...string) cmdResult {
		mu.Lock()
		defer mu.Unlock()
		git = append(git, strings.Join(args, " "))
		return cmdResult{}
	}
	runGH = func(_ context.Context, _ string, args ...string) cmdResult {
		mu.Lock()
		defer mu.Unlock()
		gh = append(gh, strings.Join(args, " "))
		return cmdResult{Stdout: "https://github.com/o/r/pull/227\n"}
	}

	exec := func(map[string]any) map[string]any {
		// Issue #221's shape: the one issue reads as completed although its
		// change never reached the branch.
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "implement-ledger-refunds"}},
			"merged_branches":  []any{"issue/implement-ledger-refunds"},
			"all_issues":       []any{map[string]any{"name": "implement-ledger-refunds"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	summary := "OVERALL FAIL. its change is NOT present in the repository"
	if passed {
		summary = "OVERALL PASS"
	}
	base := buildHandler(exec, func(map[string]any) map[string]any {
		return map[string]any{"passed": passed, "criteria_results": []any{}, "summary": summary}
	})
	app := &mockApp{handler: func(ctx context.Context, target string, input map[string]any) (map[string]any, error) {
		mu.Lock()
		calls = append(calls, target[strings.LastIndex(target, ".")+1:])
		mu.Unlock()
		switch {
		case strings.HasSuffix(target, ".run_git_init"):
			return map[string]any{
				"success": true, "integration_branch": "feature/f86e990a-add-refunds-method-to-ledger",
				"original_branch": "main", "mode": "fresh",
				"remote_url": "https://github.com/o/r.git", "remote_default_branch": "main",
			}, nil
		case strings.HasSuffix(target, ".run_github_pr"):
			return map[string]any{"pr_url": "https://github.com/o/r/pull/227", "pr_number": 227}, nil
		}
		return base(ctx, target, input)
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	if _, err := Build(context.Background(), deps, map[string]any{
		"goal":      "Resolve GitHub issue #221: Add a Refunds() method to Ledger",
		"repo_path": t.TempDir(),
		"config":    map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0},
	}); err != nil {
		t.Fatalf("build: %v", err)
	}
	return calls, git, gh
}

// A run whose work failed verification opens no pull request: no PR role, no
// push of the integration branch, no gh fallback (issue #221 → PR #227).
func TestAFailedBuildOpensNoPullRequest(t *testing.T) {
	defer withExecCtx("run-221", "exec-221")()
	calls, git, gh := buildWithRemote(t, false)
	for _, c := range calls {
		if c == "run_github_pr" {
			t.Fatalf("a failed build called the PR role: %v", calls)
		}
	}
	for _, a := range git {
		if strings.HasPrefix(a, "push") {
			t.Fatalf("a failed build pushed its branch: %q", a)
		}
	}
	if len(gh) != 0 {
		t.Fatalf("a failed build ran gh: %v", gh)
	}
}

// The control: the same build with verification passed does publish, so the
// test above is holding the gate and not a dead PR phase.
func TestAPassedBuildStillOpensItsPullRequest(t *testing.T) {
	defer withExecCtx("run-222", "exec-222")()
	calls, git, _ := buildWithRemote(t, true)
	if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
		t.Fatalf("a passed build did not call the PR role: %v", calls)
	}
	if !strings.Contains(strings.Join(git, "\n"), "push -u origin feature/f86e990a-add-refunds-method-to-ledger") {
		t.Fatalf("a passed build did not push its branch: %v", git)
	}
}

package orch

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
)

// buildWithRemote runs Build or BuildIssue against a git_init that found a
// GitHub remote, so the push-and-PR phase is live, with a verifier that answers
// passed. It returns the reasoners called, the git and gh argv that ran, the
// recorded notes, the captured run_github_pr input, and the returned BuildResult.
func buildWithRemote(t *testing.T, repo string, viaBuildIssue, passed bool) (
	calls, git, gh, notes []string, prInput, result map[string]any,
) {
	t.Helper()
	if repo == "" {
		repo = t.TempDir()
	}
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
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "implement-ledger-refunds"}},
			"merged_branches":  []any{"issue/implement-ledger-refunds"},
			"all_issues": []any{
				map[string]any{"name": "implement-ledger-refunds"},
				map[string]any{"name": "add-refunds-docs"},
			},
			"failed_issues":  []any{map[string]any{"name": "add-refunds-docs"}},
			"skipped_issues": []any{},
			"accumulated_debt": []any{
				map[string]any{"type": "unmet_acceptance_criterion", "criterion": "docs updated", "reason": "skipped", "severity": "medium"},
			},
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
		if strings.HasSuffix(target, ".run_github_pr") {
			prInput = input
		}
		mu.Unlock()
		switch {
		case strings.HasSuffix(target, ".run_git_init"):
			return map[string]any{
				"success": true, "integration_branch": "feature/haszero",
				"original_branch": "main", "mode": "fresh",
				"remote_url": "https://github.com/o/r.git", "remote_default_branch": "main",
			}, nil
		case strings.HasSuffix(target, ".run_github_pr"):
			return map[string]any{"pr_url": "https://github.com/o/r/pull/227", "pr_number": 227}, nil
		}
		return base(ctx, target, input)
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	cfg := map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0}
	var (
		out any
		err error
	)
	if viaBuildIssue {
		out, err = BuildIssue(context.Background(), deps, map[string]any{
			"title":        "Add a Refunds() method to Ledger",
			"issue_number": 221,
			"repo_path":    repo,
			"plan_review":  false,
			"config":       cfg,
		})
	} else {
		out, err = Build(context.Background(), deps, map[string]any{
			"goal":      "Resolve GitHub issue #221: Add a Refunds() method to Ledger",
			"repo_path": repo,
			"config":    cfg,
		})
	}
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	result, _ = out.(map[string]any)
	return calls, git, gh, app.notes, prInput, result
}

// V4: Under build_issue, a run whose partial work failed verification opens no
// pull request and records the note: no PR role, no push of the integration
// branch, no gh fallback (issue #221 → PR #227).
func TestAFailedBuildOpensNoPullRequest(t *testing.T) {
	defer withExecCtx("run-221", "exec-221")()
	calls, git, gh, notes, _, result := buildWithRemote(t, "", true, false)
	for _, c := range calls {
		if c == "run_github_pr" {
			t.Fatalf("a failed build_issue called the PR role: %v", calls)
		}
	}
	for _, a := range git {
		if strings.HasPrefix(a, "push") {
			t.Fatalf("a failed build_issue pushed its branch: %q", a)
		}
	}
	if len(gh) != 0 {
		t.Fatalf("a failed build_issue ran gh: %v", gh)
	}
	if prs := asMapList(result["pr_results"]); len(prs) != 0 {
		t.Fatalf("pr_results = %v, want empty", prs)
	}
	if !strings.Contains(strings.Join(notes, "\n"), "No pull request: the build did not pass verification (") {
		t.Fatalf("missing failed-verification note: %v", notes)
	}
}

// The control: the same build_issue with verification passed does publish, so
// the test above is holding the gate and not a dead PR phase.
func TestAPassedBuildStillOpensItsPullRequest(t *testing.T) {
	defer withExecCtx("run-222", "exec-222")()
	calls, git, _, _, _, _ := buildWithRemote(t, "", true, true)
	if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
		t.Fatalf("a passed build did not call the PR role: %v", calls)
	}
	if !strings.Contains(strings.Join(git, "\n"), "push -u origin feature/haszero") {
		t.Fatalf("a passed build did not push its branch: %v", git)
	}
}

// V4: A plain build that completed some issues and failed verification pushes
// and opens its pull request with accumulated_debt reported, as on main.
func TestPlainBuildPartialFailedVerificationPushesAndOpensPR(t *testing.T) {
	defer withExecCtx("run-223", "exec-223")()
	calls, git, _, _, prInput, result := buildWithRemote(t, "", false, false)
	if asBool(result["success"]) {
		t.Fatal("partial failed-verification build must report success=false")
	}
	if !strings.Contains(strings.Join(git, "\n"), "push -u origin feature/haszero") {
		t.Fatalf("plain partial build did not push its branch: %v", git)
	}
	if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
		t.Fatalf("plain partial build did not call run_github_pr: %v", calls)
	}
	if debt := asMapList(prInput["accumulated_debt"]); len(debt) != 1 || debt[0]["criterion"] != "docs updated" {
		t.Fatalf("run_github_pr accumulated_debt = %v, want debt item forwarded", prInput["accumulated_debt"])
	}
	if prs := asMapList(result["pr_results"]); len(prs) != 1 || prs[0]["pr_url"] != "https://github.com/o/r/pull/227" {
		t.Fatalf("pr_results = %v, want opened PR", result["pr_results"])
	}
}

// V4: A build that changed nothing opens no pull request in both plain build
// and build_issue.
func TestNoChangeBuildOpensNoPRInPlainBuildAndBuildIssue(t *testing.T) {
	for _, tc := range []struct {
		name          string
		viaBuildIssue bool
		passed        bool
	}{
		{"plain build passed", false, true},
		{"plain build partial failed verification", false, false},
		{"build_issue passed", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			defer withExecCtx("run-nc", "exec-nc")()
			repo := issue309Repo(t, false)
			calls, git, _, notes, _, result := buildWithRemote(t, repo, tc.viaBuildIssue, tc.passed)
			for _, c := range calls {
				if c == "run_github_pr" {
					t.Fatalf("%s called run_github_pr on a no-change repo: %v", tc.name, calls)
				}
			}
			for _, g := range git {
				if strings.HasPrefix(g, "push") {
					t.Fatalf("%s pushed a no-change branch: %v", tc.name, git)
				}
			}
			if prs := asMapList(result["pr_results"]); len(prs) != 0 {
				t.Fatalf("%s pr_results = %v, want empty", tc.name, prs)
			}
			found := false
			for _, n := range notes {
				found = found || n == nochange.Note
			}
			if !found {
				t.Fatalf("%s notes = %v, want %q", tc.name, notes, nochange.Note)
			}
		})
	}
}

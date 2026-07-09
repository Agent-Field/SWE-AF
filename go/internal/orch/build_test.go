package orch

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/reasonerfail"
)

// buildHandler routes mock reasoner responses by target suffix. Overridable
// per-reasoner via the exec/verify hooks.
func buildHandler(execResp, verifyResp func(input map[string]any) map[string]any) func(context.Context, string, map[string]any) (map[string]any, error) {
	return func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".plan"):
			return map[string]any{
				"prd": map[string]any{}, "issues": []any{}, "artifacts_dir": input["artifacts_dir"],
			}, nil
		case strings.HasSuffix(target, ".run_git_init"):
			return map[string]any{"success": false, "error_message": "no remote"}, nil
		case strings.HasSuffix(target, ".execute"):
			return execResp(input), nil
		case strings.HasSuffix(target, ".run_verifier"):
			return verifyResp(input), nil
		case strings.HasSuffix(target, ".run_repo_finalize"):
			return map[string]any{"success": true, "summary": ""}, nil
		default:
			return map[string]any{}, nil
		}
	}
}

func emptyExec(map[string]any) map[string]any {
	return map[string]any{
		"completed_issues": []any{}, "merged_branches": []any{}, "all_issues": []any{},
		"failed_issues": []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
	}
}

// TestBuildEmptyGuardReportsFailed maps to test_empty_build_guard.py: a build
// that ships nothing and fails verification must invoke the ReasonerFailed
// carrier with the BuildResult and return an error.
func TestBuildEmptyGuardReportsFailed(t *testing.T) {
	defer withExecCtx("run-1", "exec-1")()

	prevPost := postFailedFn
	defer func() { postFailedFn = prevPost }()
	var captured any
	postFailedFn = func(_ context.Context, _ reasonerfail.PosterConfig, result any, message string) error {
		captured = result
		return fmt.Errorf("%s", message)
	}

	app := &mockApp{handler: buildHandler(emptyExec, func(map[string]any) map[string]any {
		return map[string]any{"passed": false, "criteria_results": []any{}, "summary": "nope"}
	})}
	deps := &Deps{App: app, NodeID: "swe-planner"}

	out, err := Build(context.Background(), deps, map[string]any{
		"goal":      "do a thing",
		"repo_path": t.TempDir(),
		"config":    map[string]any{"git_init_max_retries": 1},
	})
	if err == nil {
		t.Fatal("empty build must return an error")
	}
	if err.Error() != "Build failed: 0/0 issues completed, no branches merged" {
		t.Fatalf("error message = %q", err.Error())
	}
	res, ok := captured.(map[string]any)
	if !ok {
		t.Fatalf("carrier result not a map: %T", captured)
	}
	if asBool(res["success"]) {
		t.Fatal("carrier result success should be false")
	}
	if _, has := res["dag_state"]; !has {
		t.Fatal("carrier result must carry dag_state")
	}
	// Build also returns the same BuildResult map.
	if m, ok := out.(map[string]any); !ok || asBool(m["success"]) {
		t.Fatalf("build return should be a BuildResult map with success=false, got %T", out)
	}
}

// TestBuildPartialNotEmpty: a build that completed an issue and merged a branch
// is NOT empty even when verification fails — it returns normally (no error).
func TestBuildPartialNotEmpty(t *testing.T) {
	defer withExecCtx("run-2", "exec-2")()

	prevPost := postFailedFn
	defer func() { postFailedFn = prevPost }()
	postFailedFn = func(context.Context, reasonerfail.PosterConfig, any, string) error {
		t.Fatal("ReasonerFailed carrier must NOT fire for a partial build")
		return nil
	}

	exec := func(map[string]any) map[string]any {
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "i1"}},
			"merged_branches":  []any{"issue/x"},
			"all_issues":       []any{map[string]any{"name": "i1"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	app := &mockApp{handler: buildHandler(exec, func(map[string]any) map[string]any {
		return map[string]any{"passed": false, "criteria_results": []any{}, "summary": "partial"}
	})}
	deps := &Deps{App: app, NodeID: "swe-planner"}

	out, err := Build(context.Background(), deps, map[string]any{
		"goal":      "thing",
		"repo_path": t.TempDir(),
		"config":    map[string]any{"git_init_max_retries": 1},
	})
	if err != nil {
		t.Fatalf("partial build should not error: %v", err)
	}
	m := out.(map[string]any)
	if asBool(m["success"]) {
		t.Fatal("success should be false for a failed-verification partial build")
	}
}

// TestBuildVerifiedSuccess: verification passes → success true, no carrier.
func TestBuildVerifiedSuccess(t *testing.T) {
	defer withExecCtx("run-3", "exec-3")()
	prevPost := postFailedFn
	defer func() { postFailedFn = prevPost }()
	postFailedFn = func(context.Context, reasonerfail.PosterConfig, any, string) error {
		t.Fatal("carrier must not fire for a verified build")
		return nil
	}

	exec := func(map[string]any) map[string]any {
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "i1"}},
			"merged_branches":  []any{"issue/x"},
			"all_issues":       []any{map[string]any{"name": "i1"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	app := &mockApp{handler: buildHandler(exec, func(map[string]any) map[string]any {
		return map[string]any{"passed": true, "criteria_results": []any{}, "summary": "ok"}
	})}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	out, err := Build(context.Background(), deps, map[string]any{
		"goal": "thing", "repo_path": t.TempDir(),
		"config": map[string]any{"git_init_max_retries": 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !asBool(out.(map[string]any)["success"]) {
		t.Fatal("success should be true")
	}
}

// TestBuildRequiresRepoPathOrURL maps to the ValueError branch.
func TestBuildRequiresRepoPathOrURL(t *testing.T) {
	defer withExecCtx("r", "e")()
	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner"}
	_, err := Build(context.Background(), deps, map[string]any{"goal": "x"})
	if err == nil || !strings.Contains(err.Error(), "Either repo_path or repo_url") {
		t.Fatalf("expected repo_path/url error, got %v", err)
	}
}

// TestBuildIsolationConcurrent: two concurrent builds must receive distinct
// build_ids (no shared mutable state). Maps to test_build_isolation.py intent.
func TestBuildIsolationConcurrent(t *testing.T) {
	defer withExecCtx("run", "exec")()
	prevPost := postFailedFn
	defer func() { postFailedFn = prevPost }()
	postFailedFn = func(context.Context, reasonerfail.PosterConfig, any, string) error { return nil }

	var mu sync.Mutex
	buildIDs := map[string]bool{}
	exec := func(input map[string]any) map[string]any {
		if id, ok := input["build_id"].(string); ok {
			mu.Lock()
			buildIDs[id] = true
			mu.Unlock()
		}
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "i"}},
			"merged_branches":  []any{"b"}, "all_issues": []any{map[string]any{"name": "i"}},
			"failed_issues": []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	verify := func(map[string]any) map[string]any {
		return map[string]any{"passed": true, "criteria_results": []any{}, "summary": "ok"}
	}
	app := &mockApp{handler: buildHandler(exec, verify)}

	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			deps := &Deps{App: app, NodeID: "swe-planner"}
			_, _ = Build(context.Background(), deps, map[string]any{
				"goal": "g", "repo_path": t.TempDir(),
				"config": map[string]any{"git_init_max_retries": 1},
			})
		}()
	}
	wg.Wait()
	if len(buildIDs) != 2 {
		t.Fatalf("expected 2 distinct build_ids across concurrent builds, got %d: %v", len(buildIDs), buildIDs)
	}
}

// TestBuildScopedPathIncludesBuildID maps to test_build_isolation.py's
// two-builds-same-repo assertion (derived paths differ by build_id).
func TestBuildScopedPathIncludesBuildID(t *testing.T) {
	repoURL := "https://github.com/example/my-repo.git"
	name := deriveRepoName(repoURL)
	a := fmt.Sprintf("/workspaces/%s-%s", name, newBuildID())
	b := fmt.Sprintf("/workspaces/%s-%s", name, newBuildID())
	if a == b {
		t.Fatal("two builds for the same repo must produce different workspace paths")
	}
	if !strings.Contains(a, name) || !strings.Contains(b, name) {
		t.Fatalf("derived paths must contain repo name: %s %s", a, b)
	}
}

func TestNewBuildIDFormat(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		id := newBuildID()
		if len(id) != 8 {
			t.Fatalf("build_id length = %d, want 8 (%q)", len(id), id)
		}
		for _, c := range id {
			if !strings.ContainsRune("0123456789abcdef", c) {
				t.Fatalf("build_id not hex: %q", id)
			}
		}
		seen[id] = true
	}
	if len(seen) < 190 {
		t.Fatalf("build_ids not sufficiently unique: %d/200", len(seen))
	}
}

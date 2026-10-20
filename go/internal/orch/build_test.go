package orch

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/furrow"
	"github.com/Agent-Field/SWE-AF/go/internal/workspace"
)

type fakeFurrow struct {
	handle        *furrow.Handle
	attachErr     error
	attachedPath  string
	attachedRunID string
	publishedRuns []string
	publishes     []string
}

func (f *fakeFurrow) Enabled() bool { return true }
func (f *fakeFurrow) Attach(runID, _, repoPath string) (*furrow.Handle, error) {
	f.attachedRunID, f.attachedPath = runID, repoPath
	return f.handle, f.attachErr
}
func (f *fakeFurrow) Publish(runID string, label string) error {
	f.publishedRuns = append(f.publishedRuns, runID)
	f.publishes = append(f.publishes, label)
	return errors.New("ignored publish failure")
}
func (f *fakeFurrow) Handle(string) *furrow.Handle            { return f.handle }
func (f *fakeFurrow) Detach(string) error                     { return nil }
func (f *fakeFurrow) Sweep(time.Duration, int64) (int, error) { return 0, nil }

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
// that ships nothing and fails verification must return the SDK's result-carrying
// &agent.ReasonerFailed (Message = the failure summary, Result = the BuildResult
// map) so the async handler records status=failed while preserving the outcome.
func TestBuildEmptyGuardReportsFailed(t *testing.T) {
	defer withExecCtx("run-1", "exec-1")()

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
	var rf *agent.ReasonerFailed
	if !errors.As(err, &rf) {
		t.Fatalf("error must be *agent.ReasonerFailed, got %T: %v", err, err)
	}
	if rf.Message != "Build failed: 0/0 issues completed, no branches merged" {
		t.Fatalf("ReasonerFailed.Message = %q", rf.Message)
	}
	res, ok := rf.Result.(map[string]any)
	if !ok {
		t.Fatalf("ReasonerFailed.Result not a map: %T", rf.Result)
	}
	if asBool(res["success"]) {
		t.Fatal("carried result success should be false")
	}
	if _, has := res["dag_state"]; !has {
		t.Fatal("carried result must carry dag_state")
	}
	// Build also returns the same BuildResult map as the value.
	if m, ok := out.(map[string]any); !ok || asBool(m["success"]) {
		t.Fatalf("build return should be a BuildResult map with success=false, got %T", out)
	}
}

// TestBuildPartialNotEmpty: a build that completed an issue and merged a branch
// is NOT empty even when verification fails — it returns normally (no error).
func TestBuildPartialNotEmpty(t *testing.T) {
	defer withExecCtx("run-2", "exec-2")()

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

func TestBuildWorkspaceHandleAvailability(t *testing.T) {
	defer withExecCtx("run-furrow", "exec-furrow")()
	exec := func(map[string]any) map[string]any {
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "i1"}},
			"merged_branches":  []any{"issue/x"},
			"all_issues":       []any{map[string]any{"name": "i1"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	verify := func(map[string]any) map[string]any {
		return map[string]any{"passed": true, "criteria_results": []any{}, "summary": "ok"}
	}

	tests := []struct {
		name       string
		attacher   *fakeFurrow
		wantHandle bool
	}{
		{name: "available", attacher: &fakeFurrow{handle: &furrow.Handle{
			Version: 1, Remote: "dir:/mirror", Namespace: "run-furrow", Key: "secret-key",
		}}, wantHandle: true},
		{name: "unavailable", attacher: &fakeFurrow{}},
		{name: "attach error", attacher: &fakeFurrow{attachErr: errors.New("attach failed")}},
		{name: "nil dependency"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			app := &mockApp{handler: buildHandler(exec, verify)}
			deps := &Deps{App: app, NodeID: "swe-planner"}
			if tc.attacher != nil {
				deps.Furrow = tc.attacher
			}
			out, err := Build(context.Background(), deps, map[string]any{
				"goal": "thing", "repo_path": t.TempDir(),
				"config": map[string]any{"git_init_max_retries": 1},
			})
			if err != nil {
				t.Fatalf("Build: %v", err)
			}
			result := out.(map[string]any)
			_, gotHandle := result["workspace_handle"]
			if gotHandle != tc.wantHandle {
				t.Fatalf("workspace_handle present = %v, want %v", gotHandle, tc.wantHandle)
			}
			if tc.attacher != nil && len(tc.attacher.publishes) != 1 {
				t.Fatalf("publishes = %v, want build completion", tc.attacher.publishes)
			}
			for _, note := range app.notes {
				if strings.Contains(note, "secret-key") {
					t.Fatalf("note leaked workspace key: %q", note)
				}
			}
		})
	}
}

// A build whose execution context carries no run ID still has a root workflow
// ID, and that is what everything per-run in the build must be filed under —
// planning.Scout already stores scoped credentials that way. Handing furrow an
// empty scope instead made every such build share one registry row, so build B
// received build A's workspace path, recovery key and token.
func TestBuildScopesWorkspaceMirrorByRootWorkflowID(t *testing.T) {
	exec := func(map[string]any) map[string]any {
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "i1"}},
			"merged_branches":  []any{"issue/x"},
			"all_issues":       []any{map[string]any{"name": "i1"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	verify := func(map[string]any) map[string]any {
		return map[string]any{"passed": true, "criteria_results": []any{}, "summary": "ok"}
	}
	for _, tc := range []struct {
		name           string
		runID          string
		rootWorkflowID string
		wantScope      string
	}{
		{name: "run id wins", runID: "run-1", rootWorkflowID: "wf-1", wantScope: "run-1"},
		{name: "root workflow id fallback", rootWorkflowID: "wf-2", wantScope: "wf-2"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			defer withExecCtxRoot(tc.runID, "exec", tc.rootWorkflowID)()
			f := &fakeFurrow{handle: &furrow.Handle{Version: 1, Remote: "dir:/mirror", Namespace: "ns"}}
			deps := &Deps{App: &mockApp{handler: buildHandler(exec, verify)}, NodeID: "swe-planner", Furrow: f}
			if _, err := Build(context.Background(), deps, map[string]any{
				"goal": "thing", "repo_path": t.TempDir(),
				"config": map[string]any{"git_init_max_retries": 1},
			}); err != nil {
				t.Fatalf("Build: %v", err)
			}
			if f.attachedRunID != tc.wantScope {
				t.Errorf("Attach run ID = %q, want %q", f.attachedRunID, tc.wantScope)
			}
			// Publish must reach the same row Attach created, or the mirror
			// stops updating the moment the run ID is absent.
			if len(f.publishedRuns) != 1 || f.publishedRuns[0] != tc.wantScope {
				t.Errorf("Publish run IDs = %v, want [%q]", f.publishedRuns, tc.wantScope)
			}
		})
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
	a := filepath.Join(workspace.Root(), fmt.Sprintf("%s-%s", name, newBuildID()))
	b := filepath.Join(workspace.Root(), fmt.Sprintf("%s-%s", name, newBuildID()))
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

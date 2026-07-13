package orch

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dag"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// runDAGArgs captures everything execute forwards to the runDAG seam that is
// inspectable without decoding the opaque dag.Option values.
type runDAGArgs struct {
	planResult map[string]any
	repoPath   string
	callFn     dag.CallFn
	nodeID     string
	cfg        *config.ExecutionConfig
	called     bool
}

// withRunDAG swaps the runDAG seam for the duration of a test.
func withRunDAG(fn func(context.Context, map[string]any, string, dag.CallFn, string, *config.ExecutionConfig, ...dag.Option) (*schemas.DAGState, error)) func() {
	prev := runDAG
	runDAG = fn
	return func() { runDAG = prev }
}

func minimalPlan() map[string]any {
	return map[string]any{
		"issues":         []any{},
		"levels":         []any{},
		"file_conflicts": []any{},
		"artifacts_dir":  "",
		"prd":            map[string]any{"validated_description": "PRD", "acceptance_criteria": []any{}},
		"architecture":   map[string]any{},
	}
}

// ---------------------------------------------------------------------------
// Contract: config dict → ExecutionConfig with model resolution; call_fn wired;
// plan_result / repo_path / node_id forwarded unchanged.
// ---------------------------------------------------------------------------

func TestExecuteConfigResolvedAndForwarded(t *testing.T) {
	var captured runDAGArgs
	defer withRunDAG(func(_ context.Context, plan map[string]any, repoPath string, callFn dag.CallFn, nodeID string, cfg *config.ExecutionConfig, _ ...dag.Option) (*schemas.DAGState, error) {
		captured = runDAGArgs{planResult: plan, repoPath: repoPath, callFn: callFn, nodeID: nodeID, cfg: cfg, called: true}
		return &schemas.DAGState{RepoPath: repoPath}, nil
	})()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner-go"}

	plan := minimalPlan()
	_, err := ExecuteHandler(context.Background(), deps, map[string]any{
		"plan_result": plan,
		"repo_path":   "/tmp/target-repo",
		"config": map[string]any{
			"runtime": "claude_code",
			"models":  map[string]any{"default": "sonnet", "coder": "opus"},
		},
	})
	if err != nil {
		t.Fatalf("ExecuteHandler returned error: %v", err)
	}

	if !captured.called {
		t.Fatal("runDAG was not invoked")
	}
	if captured.repoPath != "/tmp/target-repo" {
		t.Errorf("repo_path not forwarded: got %q", captured.repoPath)
	}
	if captured.nodeID != "swe-planner-go" {
		t.Errorf("node_id not forwarded: got %q", captured.nodeID)
	}
	if captured.callFn == nil {
		t.Error("call_fn must be non-nil (built-in coding-loop path)")
	}
	if captured.cfg == nil {
		t.Fatal("ExecutionConfig must be built and forwarded")
	}
	// Model resolution: models.coder=opus wins over models.default=sonnet.
	if got := captured.cfg.CoderModel(); got != "opus" {
		t.Errorf("coder model resolution wrong: got %q, want opus", got)
	}
	if got := captured.cfg.SprintPlannerModel(); got != "sonnet" {
		t.Errorf("default model resolution wrong: got %q, want sonnet", got)
	}
	// plan_result forwarded unchanged.
	if !reflect.DeepEqual(captured.planResult, plan) {
		t.Errorf("plan_result not forwarded unchanged")
	}
}

// Absent config dict reproduces the bare ExecutionConfig() path (defaults).
func TestExecuteDefaultConfigWhenAbsent(t *testing.T) {
	var cfg *config.ExecutionConfig
	defer withRunDAG(func(_ context.Context, _ map[string]any, _ string, _ dag.CallFn, _ string, c *config.ExecutionConfig, _ ...dag.Option) (*schemas.DAGState, error) {
		cfg = c
		return &schemas.DAGState{}, nil
	})()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) { return nil, nil }}}
	if _, err := ExecuteHandler(context.Background(), deps, map[string]any{
		"plan_result": minimalPlan(),
		"repo_path":   "/tmp/repo",
	}); err != nil {
		t.Fatalf("ExecuteHandler error: %v", err)
	}
	if cfg == nil {
		t.Fatal("cfg nil")
	}
	if cfg.MaxReplans != 2 {
		t.Errorf("default MaxReplans want 2, got %d", cfg.MaxReplans)
	}
}

// An invalid config dict surfaces the LoadExecutionConfig error and never calls
// runDAG (parity with Python raising at ExecutionConfig construction).
func TestExecuteBadConfigError(t *testing.T) {
	called := false
	defer withRunDAG(func(context.Context, map[string]any, string, dag.CallFn, string, *config.ExecutionConfig, ...dag.Option) (*schemas.DAGState, error) {
		called = true
		return &schemas.DAGState{}, nil
	})()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) { return nil, nil }}}
	_, err := ExecuteHandler(context.Background(), deps, map[string]any{
		"plan_result": minimalPlan(),
		"repo_path":   "/tmp/repo",
		"config":      map[string]any{"totally_unknown_field": true},
	})
	if err == nil {
		t.Fatal("expected error from invalid config")
	}
	if called {
		t.Error("runDAG must not be called when config load fails")
	}
}

// ---------------------------------------------------------------------------
// Contract: result is the DAGState dump with the exact DAGState key set.
// ---------------------------------------------------------------------------

func TestExecuteReturnsDAGStateDump(t *testing.T) {
	canned := &schemas.DAGState{
		RepoPath: "/tmp/repo",
		CompletedIssues: []schemas.IssueResult{
			{IssueName: "impl", Outcome: schemas.IssueOutcomeCompleted, ResultSummary: "Done"},
		},
	}
	defer withRunDAG(func(context.Context, map[string]any, string, dag.CallFn, string, *config.ExecutionConfig, ...dag.Option) (*schemas.DAGState, error) {
		return canned, nil
	})()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) { return nil, nil }}}
	out, err := ExecuteHandler(context.Background(), deps, map[string]any{
		"plan_result": minimalPlan(),
		"repo_path":   "/tmp/repo",
	})
	if err != nil {
		t.Fatalf("ExecuteHandler error: %v", err)
	}
	result, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("result must be a map (DAGState.model_dump()), got %T", out)
	}

	// Exact key set == the full DAGState struct's JSON keys (no omitempty).
	var ref map[string]any
	refRaw, _ := json.Marshal(schemas.DAGState{})
	_ = json.Unmarshal(refRaw, &ref)
	if !reflect.DeepEqual(keySet(result), keySet(ref)) {
		t.Errorf("result key set mismatch\n got:  %v\n want: %v", keySet(result), keySet(ref))
	}

	completed := asMapList(result["completed_issues"])
	if len(completed) != 1 {
		t.Fatalf("want 1 completed issue, got %d", len(completed))
	}
	if completed[0]["issue_name"] != "impl" {
		t.Errorf("completed issue_name wrong: %v", completed[0]["issue_name"])
	}
	if fs := asMapList(result["failed_issues"]); len(fs) != 0 {
		t.Errorf("want 0 failed issues, got %d", len(fs))
	}
}

// A runDAG error propagates out of execute.
func TestExecuteRunDAGErrorPropagates(t *testing.T) {
	defer withRunDAG(func(context.Context, map[string]any, string, dag.CallFn, string, *config.ExecutionConfig, ...dag.Option) (*schemas.DAGState, error) {
		return nil, context.Canceled
	})()
	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) { return nil, nil }}}
	out, err := ExecuteHandler(context.Background(), deps, map[string]any{
		"plan_result": minimalPlan(), "repo_path": "/tmp/repo",
	})
	if err == nil {
		t.Fatal("expected error to propagate")
	}
	if out != nil {
		t.Errorf("expected nil result on error, got %v", out)
	}
}

// ---------------------------------------------------------------------------
// Option forwarding, verified through the real dag.RunDAG (dag.Option is opaque,
// so forwarding is observed via its effect on the returned DAGState dump).
// ---------------------------------------------------------------------------

// workspace_manifest=None keeps the single-repo path: the dump carries a nil
// manifest and no reasoner calls are made.
func TestExecuteWorkspaceManifestNonePassthrough(t *testing.T) {
	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		t.Errorf("no reasoner call expected for empty single-repo build, got %q", target)
		return map[string]any{}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner-go"}

	out := mustExecute(t, deps, map[string]any{
		"plan_result": minimalPlan(),
		"repo_path":   "/tmp/repo",
	})
	if out["workspace_manifest"] != nil {
		t.Errorf("single-repo build must keep workspace_manifest nil, got %v", out["workspace_manifest"])
	}
}

// workspace_manifest dict is forwarded to run_dag (multi-repo path): it survives
// onto the returned dump with primary repo + repos preserved.
func TestExecuteWorkspaceManifestForwarded(t *testing.T) {
	manifest := dumpToMap(&schemas.WorkspaceManifest{
		WorkspaceRoot:   "/tmp/ws",
		PrimaryRepoName: "api",
		Repos: []schemas.WorkspaceRepo{
			{RepoName: "api", RepoURL: "https://github.com/org/api.git", Role: "primary", AbsolutePath: "/tmp/ws/api", Branch: "main", CreatePR: true},
			{RepoName: "lib-1", RepoURL: "https://github.com/org/lib-1.git", Role: "dependency", AbsolutePath: "/tmp/ws/lib-1", Branch: "main"},
		},
	})

	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		// _init_all_repos dispatches run_git_init per repo; success is fine.
		return map[string]any{"success": true, "mode": "existing", "integration_branch": "main"}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner-go"}

	out := mustExecute(t, deps, map[string]any{
		"plan_result":        minimalPlan(),
		"repo_path":          "/tmp/ws/api",
		"workspace_manifest": manifest,
	})
	wm, ok := out["workspace_manifest"].(map[string]any)
	if !ok || wm == nil {
		t.Fatalf("workspace_manifest must be forwarded onto the dump, got %v", out["workspace_manifest"])
	}
	if wm["primary_repo_name"] != "api" {
		t.Errorf("primary_repo_name lost through run_dag: %v", wm["primary_repo_name"])
	}
	if repos := asMapList(wm["repos"]); len(repos) != 2 {
		t.Errorf("both repos must survive, got %d", len(repos))
	}
}

// build_id is forwarded (observed via DAGState.build_id).
func TestExecuteBuildIDForwarded(t *testing.T) {
	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner-go"}

	out := mustExecute(t, deps, map[string]any{
		"plan_result": minimalPlan(),
		"repo_path":   "/tmp/repo",
		"build_id":    "bid-abc123",
	})
	if out["build_id"] != "bid-abc123" {
		t.Errorf("build_id not forwarded: got %v", out["build_id"])
	}
}

// resume=true is forwarded: run_dag loads the checkpoint, so the checkpoint's
// completed issue appears in the result (proving the flag threaded through).
func TestExecuteResumeForwarded(t *testing.T) {
	dir := t.TempDir()
	ckDir := filepath.Join(dir, "execution")
	if err := os.MkdirAll(ckDir, 0o755); err != nil {
		t.Fatal(err)
	}
	checkpoint := schemas.DAGState{
		RepoPath:     "/tmp/repo",
		ArtifactsDir: dir,
		Levels:       [][]string{},
		CompletedIssues: []schemas.IssueResult{
			{IssueName: "already-done", Outcome: schemas.IssueOutcomeCompleted},
		},
	}
	b, _ := json.MarshalIndent(checkpoint, "", "  ")
	if err := os.WriteFile(filepath.Join(ckDir, "checkpoint.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}

	plan := minimalPlan()
	plan["artifacts_dir"] = dir

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner-go"}

	out := mustExecute(t, deps, map[string]any{
		"plan_result": plan,
		"repo_path":   "/tmp/repo",
		"resume":      true,
	})
	completed := asMapList(out["completed_issues"])
	if len(completed) != 1 || completed[0]["issue_name"] != "already-done" {
		t.Fatalf("resume must load checkpoint completed issues, got %v", out["completed_issues"])
	}
}

// execute_fn_target creates the external-executor path: run_dag drives the issue
// through the closure, which calls app.Call on the exact external target.
func TestExecuteExternalTargetPath(t *testing.T) {
	const externalTarget = "coder-agent.code_issue"
	var seenTargets []string
	var seenIssue map[string]any

	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		seenTargets = append(seenTargets, target)
		if target == externalTarget {
			if iss, ok := input["issue"].(map[string]any); ok {
				seenIssue = iss
			}
			return map[string]any{"outcome": "completed", "result_summary": "Done"}, nil
		}
		return map[string]any{}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner-go"}

	plan := minimalPlan()
	plan["issues"] = []any{map[string]any{
		"name": "impl", "title": "t", "description": "d",
		"acceptance_criteria": []any{}, "depends_on": []any{},
		"files_to_create": []any{}, "files_to_modify": []any{},
	}}
	plan["levels"] = []any{[]any{"impl"}}

	out := mustExecute(t, deps, map[string]any{
		"plan_result":       plan,
		"repo_path":         "/tmp/repo",
		"execute_fn_target": externalTarget,
	})

	if !contains(seenTargets, externalTarget) {
		t.Fatalf("external target %q was never called; targets=%v", externalTarget, seenTargets)
	}
	if seenIssue == nil || seenIssue["name"] != "impl" {
		t.Errorf("execute_fn must forward the issue dict, got %v", seenIssue)
	}
	if completed := asMapList(out["completed_issues"]); len(completed) != 1 {
		t.Errorf("external path must complete the issue, got %d completed", len(completed))
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func mustExecute(t *testing.T, deps *Deps, input map[string]any) map[string]any {
	t.Helper()
	out, err := ExecuteHandler(context.Background(), deps, input)
	if err != nil {
		t.Fatalf("ExecuteHandler error: %v", err)
	}
	m, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("result must be a map, got %T", out)
	}
	return m
}

func keySet(m map[string]any) map[string]bool {
	ks := map[string]bool{}
	for k := range m {
		ks[k] = true
	}
	return ks
}

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

package orch

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// resume_build with a missing checkpoint -> the exact Python error message.
func TestResumeMissingCheckpoint(t *testing.T) {
	repo := t.TempDir()
	deps := &Deps{App: &mockApp{}, NodeID: "swe-planner-go"}

	_, err := ResumeBuildHandler(context.Background(), deps, map[string]any{
		"repo_path": repo,
	})
	if err == nil {
		t.Fatal("missing checkpoint must error")
	}
	wantPath := filepath.Join(repo, ".artifacts", "execution", "checkpoint.json")
	want := "No checkpoint found at " + wantPath + ". Cannot resume."
	if err.Error() != want {
		t.Fatalf("error = %q\nwant  %q", err.Error(), want)
	}
}

// resume_build reconstructs plan_result exactly (app.py:2112-2121) and calls
// execute with resume=true, returning the raw envelope unchanged.
func TestResumeReconstructsPlanAndCallsExecute(t *testing.T) {
	repo := t.TempDir()
	execDir := filepath.Join(repo, ".artifacts", "execution")
	if err := os.MkdirAll(execDir, 0o755); err != nil {
		t.Fatal(err)
	}
	checkpoint := map[string]any{
		"all_issues":            []any{map[string]any{"name": "i1"}},
		"levels":                []any{[]any{"i1"}},
		"artifacts_dir":         "/custom/artifacts",
		"original_plan_summary": "the original summary",
	}
	raw, _ := json.Marshal(checkpoint)
	if err := os.WriteFile(filepath.Join(execDir, "checkpoint.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}

	var gotTarget string
	var gotInput map[string]any
	rawEnvelope := map[string]any{"status": "succeeded", "result": map[string]any{"ok": true}}
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		gotTarget = target
		gotInput = input
		return rawEnvelope, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner-go"}

	out, err := ResumeBuildHandler(context.Background(), deps, map[string]any{
		"repo_path":  repo,
		"config":     map[string]any{"runtime": "claude_code"},
		"git_config": map[string]any{"mode": "worktree"},
	})
	if err != nil {
		t.Fatal(err)
	}

	// Raw envelope returned unchanged (no unwrap).
	if !reflect.DeepEqual(out, rawEnvelope) {
		t.Fatalf("resume must return the raw execute envelope, got %v", out)
	}
	if gotTarget != "swe-planner-go.execute" {
		t.Fatalf("target = %q", gotTarget)
	}
	if gotInput["resume"] != true {
		t.Fatalf("execute must be called with resume=true, got %v", gotInput["resume"])
	}

	plan, ok := gotInput["plan_result"].(map[string]any)
	if !ok {
		t.Fatalf("plan_result missing/wrong type: %T", gotInput["plan_result"])
	}
	if got := mapStr(plan, "rationale", ""); got != "the original summary" {
		t.Fatalf("rationale = %q (want checkpoint original_plan_summary)", got)
	}
	if got := mapStr(plan, "artifacts_dir", ""); got != "/custom/artifacts" {
		t.Fatalf("artifacts_dir = %q", got)
	}
	if iss := asMapList(plan["issues"]); len(iss) != 1 || iss[0]["name"] != "i1" {
		t.Fatalf("issues not carried from all_issues: %v", plan["issues"])
	}
	if lvls, ok := plan["levels"].([]any); !ok || len(lvls) != 1 {
		t.Fatalf("levels not carried: %v", plan["levels"])
	}
	// Fixed empty scaffolding fields.
	for _, k := range []string{"prd", "architecture", "review"} {
		if m, ok := plan[k].(map[string]any); !ok || len(m) != 0 {
			t.Fatalf("%s must be an empty map, got %v", k, plan[k])
		}
	}
	if fc, ok := plan["file_conflicts"].([]any); !ok || len(fc) != 0 {
		t.Fatalf("file_conflicts must be empty list, got %v", plan["file_conflicts"])
	}
	// config / git_config passed through.
	if gc, ok := gotInput["git_config"].(map[string]any); !ok || gc["mode"] != "worktree" {
		t.Fatalf("git_config not passed through: %v", gotInput["git_config"])
	}
}

// resume_build defaults artifacts_dir to the abs base when the checkpoint omits
// it (checkpoint.get("artifacts_dir", base)).
func TestResumeArtifactsDirDefault(t *testing.T) {
	repo := t.TempDir()
	execDir := filepath.Join(repo, ".artifacts", "execution")
	if err := os.MkdirAll(execDir, 0o755); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{"all_issues": []any{}})
	if err := os.WriteFile(filepath.Join(execDir, "checkpoint.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}

	var gotInput map[string]any
	app := &mockApp{handler: func(_ context.Context, _ string, input map[string]any) (map[string]any, error) {
		gotInput = input
		return map[string]any{"status": "succeeded"}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner-go"}

	if _, err := ResumeBuildHandler(context.Background(), deps, map[string]any{"repo_path": repo}); err != nil {
		t.Fatal(err)
	}
	absRepo, _ := filepath.Abs(repo)
	wantBase := filepath.Join(absRepo, ".artifacts")
	plan := gotInput["plan_result"].(map[string]any)
	if got := mapStr(plan, "artifacts_dir", ""); got != wantBase {
		t.Fatalf("artifacts_dir default = %q, want %q", got, wantBase)
	}
}

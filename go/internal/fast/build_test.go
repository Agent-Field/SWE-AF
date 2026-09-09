package fast

import (
	"context"
	"errors"
	"testing"
)

func buildDeps(fn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)) (*Deps, *callScripter) {
	s := &callScripter{fn: fn}
	return &Deps{Call: s.call, Note: &noteRecorder{}, NodeID: "swe-fast"}, s
}

var (
	planResultFixture = map[string]any{
		"tasks":         []any{map[string]any{"name": "task-1", "title": "Task 1", "description": "d", "acceptance_criteria": []any{"AC 1"}}},
		"rationale":     "Simple plan",
		"fallback_used": false,
	}
	execResultFixture = map[string]any{
		"task_results":    []any{map[string]any{"task_name": "task-1", "outcome": "completed", "summary": "Done", "files_changed": []any{}}},
		"completed_count": 1,
		"failed_count":    0,
		"timed_out":       false,
	}
	gitInitFixture = map[string]any{
		"success": true, "integration_branch": "feature/build-abc123", "original_branch": "main",
		"initial_commit_sha": "abc123", "mode": "branch", "remote_url": "", "remote_default_branch": "main",
	}
	finalizeFixture = map[string]any{"success": true, "summary": "Finalized"}
)

func verifyFixture(passed bool) map[string]any {
	summary := "All criteria met"
	if !passed {
		summary = "Some criteria failed"
	}
	return map[string]any{"passed": passed, "summary": summary, "criteria_results": []any{}, "suggested_fixes": []any{}}
}

func standardResponses(passed bool) map[string]map[string]any {
	return map[string]map[string]any{
		"run_git_init":       gitInitFixture,
		"fast_plan_tasks":    planResultFixture,
		"fast_execute_tasks": execResultFixture,
		"fast_verify":        verifyFixture(passed),
		"run_repo_finalize":  finalizeFixture,
	}
}

// Contract: the build pipeline invokes the 6 stages in order via CallFn, and a
// passing verification yields success=true with a "Success" summary.
func TestBuild_SuccessAndStageOrder(t *testing.T) {
	tmp := t.TempDir()
	deps, s := buildDeps(byTargetSuffix(standardResponses(true)))

	out, err := Build(context.Background(), deps, map[string]any{"goal": "Add a health endpoint", "repo_path": tmp})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["success"] != true {
		t.Errorf("success = %v, want true", m["success"])
	}
	if summ := m["summary"].(string); !contains(summ, "Success") {
		t.Errorf("summary = %q, want to contain 'Success'", summ)
	}

	// Stage order via CallFn: git_init → plan → execute → verify → finalize.
	want := []string{
		"swe-fast.run_git_init",
		"swe-fast.fast_plan_tasks",
		"swe-fast.fast_execute_tasks",
		"swe-fast.fast_verify",
		"swe-fast.run_repo_finalize",
	}
	got := s.targets()
	if len(got) < len(want) {
		t.Fatalf("stage targets = %v, want at least %v", got, want)
	}
	for i, w := range want {
		if got[i] != w {
			t.Errorf("stage[%d] = %q, want %q (full order: %v)", i, got[i], w, got)
		}
	}
	// No PR stage since remote_url is empty.
	for _, tgt := range got {
		if tgt == "swe-fast.run_github_pr" {
			t.Error("run_github_pr should not be called when remote_url is empty")
		}
	}
}

// Contract: a failing verification yields success=false with a "Partial" summary.
func TestBuild_FailedVerification(t *testing.T) {
	tmp := t.TempDir()
	deps, _ := buildDeps(byTargetSuffix(standardResponses(false)))

	out, err := Build(context.Background(), deps, map[string]any{"goal": "g", "repo_path": tmp})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["success"] != false {
		t.Errorf("success = %v, want false", m["success"])
	}
	if summ := m["summary"].(string); !contains(summ, "Partial") {
		t.Errorf("summary = %q, want to contain 'Partial'", summ)
	}
}

// Contract: a build-level timeout during plan+execute returns success=false,
// a 'timed out' summary, and execution_result.timed_out==true.
func TestBuild_TimeoutPath(t *testing.T) {
	tmp := t.TempDir()
	deps, _ := buildDeps(func(ctx context.Context, target string, _ map[string]any) (map[string]any, error) {
		if contains(target, "run_git_init") {
			return gitInitFixture, nil
		}
		if contains(target, "fast_plan_tasks") {
			<-ctx.Done() // stall planning until the build deadline fires
			return nil, ctx.Err()
		}
		return map[string]any{}, nil
	})

	out, err := Build(context.Background(), deps, map[string]any{
		"goal": "g", "repo_path": tmp, "config": map[string]any{"build_timeout_seconds": 1},
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["success"] != false {
		t.Errorf("success = %v, want false", m["success"])
	}
	if summ := lower(m["summary"].(string)); !contains(summ, "timed out") {
		t.Errorf("summary = %q, want to contain 'timed out'", m["summary"])
	}
	exec := m["execution_result"].(map[string]any)
	if exec["timed_out"] != true {
		t.Errorf("execution_result.timed_out = %v, want true", exec["timed_out"])
	}
}

// Contract: missing both repo_path and repo_url is an error.
func TestBuild_MissingRepoErrors(t *testing.T) {
	deps, _ := buildDeps(byTargetSuffix(standardResponses(true)))
	_, err := Build(context.Background(), deps, map[string]any{"goal": "g"})
	if err == nil || !contains(err.Error(), "Either repo_path or repo_url must be provided") {
		t.Fatalf("err = %v, want the missing-repo ValueError", err)
	}
}

// Contract: git_init failure is non-fatal — the build still completes.
func TestBuild_GitInitNonFatal(t *testing.T) {
	tmp := t.TempDir()
	deps, _ := buildDeps(func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if contains(target, "run_git_init") {
			return nil, errors.New("git init failed")
		}
		for key, value := range standardResponses(true) {
			if contains(target, key) {
				return value, nil
			}
		}
		return map[string]any{}, nil
	})
	out, err := Build(context.Background(), deps, map[string]any{"goal": "g", "repo_path": tmp})
	if err != nil {
		t.Fatalf("git_init error bubbled up (should be non-fatal): %v", err)
	}
	if _, ok := asMap(t, out)["success"]; !ok {
		t.Error("expected a success key in the build result")
	}
}

// Contract: finalize failure is non-fatal — the build still returns success=true.
func TestBuild_FinalizeNonFatal(t *testing.T) {
	tmp := t.TempDir()
	deps, _ := buildDeps(func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if contains(target, "run_repo_finalize") {
			return nil, errors.New("finalize failed")
		}
		for key, value := range standardResponses(true) {
			if contains(target, key) {
				return value, nil
			}
		}
		return map[string]any{}, nil
	})
	out, err := Build(context.Background(), deps, map[string]any{"goal": "g", "repo_path": tmp})
	if err != nil {
		t.Fatalf("finalize error bubbled up (should be non-fatal): %v", err)
	}
	if asMap(t, out)["success"] != true {
		t.Error("success = false, want true (finalize is non-fatal)")
	}
}

// Contract: _repo_name_from_url extracts the repo name across URL formats.
func TestRepoNameFromURL(t *testing.T) {
	cases := map[string]string{
		"https://github.com/user/my-project.git": "my-project",
		"https://github.com/user/my-project":     "my-project",
		"git@github.com:user/my-project.git":     "my-project",
	}
	for url, want := range cases {
		if got := repoNameFromURL(url); got != want {
			t.Errorf("repoNameFromURL(%q) = %q, want %q", url, got, want)
		}
	}
}

// Contract: _runtime_to_provider maps runtime strings (fast-specific fallback).
func TestRuntimeToProvider(t *testing.T) {
	cases := map[string]string{"claude_code": "claude", "open_code": "opencode", "codex": "codex", "other": "opencode"}
	for runtime, want := range cases {
		if got := runtimeToProvider(runtime); got != want {
			t.Errorf("runtimeToProvider(%q) = %q, want %q", runtime, got, want)
		}
	}
}

func contains(s, sub string) bool { return indexOf(s, sub) >= 0 }
func lower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		}
	}
	return string(b)
}

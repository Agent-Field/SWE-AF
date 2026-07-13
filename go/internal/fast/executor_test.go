package fast

import (
	"context"
	"errors"
	"testing"
	"time"
)

var sampleTask = map[string]any{
	"name":                "sample-task",
	"title":               "Sample Task",
	"description":         "Do something useful.",
	"acceptance_criteria": []any{"Thing works"},
	"files_to_create":     []any{},
	"files_to_modify":     []any{},
}

func execDeps(fn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)) (*Deps, *callScripter) {
	s := &callScripter{fn: fn}
	return &Deps{Call: s.call, Note: &noteRecorder{}, NodeID: "swe-fast-go"}, s
}

// Contract: a successful coder call (complete=true) → outcome "completed".
func TestFastExecuteTasks_CompletedOutcome(t *testing.T) {
	deps, s := execDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return map[string]any{"complete": true, "files_changed": []any{"foo.py"}, "summary": "Done"}, nil
	})
	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{sampleTask}, "repo_path": "/tmp/repo", "task_timeout_seconds": 30,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	tr := m["task_results"].([]any)[0].(map[string]any)
	if tr["outcome"] != "completed" {
		t.Errorf("outcome = %v, want completed", tr["outcome"])
	}
	if tr["task_name"] != "sample-task" {
		t.Errorf("task_name = %v, want sample-task", tr["task_name"])
	}
	// run_coder must be the call target, args forwarded.
	rec := s.snapshot()
	if got := rec[0].target; got != "swe-fast-go.run_coder" {
		t.Errorf("target = %q, want swe-fast.run_coder", got)
	}
	if rec[0].kwargs["worktree_path"] != "/tmp/repo" {
		t.Errorf("worktree_path = %v, want /tmp/repo", rec[0].kwargs["worktree_path"])
	}
	if rec[0].kwargs["iteration_id"] != "sample-task" {
		t.Errorf("iteration_id = %v, want sample-task", rec[0].kwargs["iteration_id"])
	}
}

// Contract: a per-task timeout marks outcome "timeout" and execution continues
// to the next task.
func TestFastExecuteTasks_TimeoutContinues(t *testing.T) {
	deps, _ := execDeps(func(ctx context.Context, _ string, kwargs map[string]any) (map[string]any, error) {
		issue := kwargs["issue"].(map[string]any)
		if issue["name"] == "sample-task" {
			<-ctx.Done() // block until the task deadline fires
			return nil, ctx.Err()
		}
		return map[string]any{"complete": true, "files_changed": []any{}, "summary": "done"}, nil
	})
	secondTask := map[string]any{"name": "second-task", "title": "S", "description": "d", "acceptance_criteria": []any{"x"}}

	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{sampleTask, secondTask}, "repo_path": "/tmp/repo", "task_timeout_seconds": 1,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	results := asMap(t, out)["task_results"].([]any)
	if len(results) != 2 {
		t.Fatalf("results len = %d, want 2", len(results))
	}
	if results[0].(map[string]any)["outcome"] != "timeout" {
		t.Errorf("first outcome = %v, want timeout", results[0].(map[string]any)["outcome"])
	}
	if results[1].(map[string]any)["outcome"] != "completed" {
		t.Errorf("second outcome = %v, want completed", results[1].(map[string]any)["outcome"])
	}
}

// Contract: a generic error marks outcome "failed", records the error, and
// execution continues.
func TestFastExecuteTasks_FailedContinues(t *testing.T) {
	deps, _ := execDeps(func(_ context.Context, _ string, kwargs map[string]any) (map[string]any, error) {
		issue := kwargs["issue"].(map[string]any)
		if issue["name"] == "sample-task" {
			return nil, errors.New("Some unexpected error")
		}
		return map[string]any{"complete": true, "files_changed": []any{}, "summary": "done"}, nil
	})
	secondTask := map[string]any{"name": "second-task", "title": "S", "description": "d", "acceptance_criteria": []any{"x"}}

	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{sampleTask, secondTask}, "repo_path": "/tmp/repo", "task_timeout_seconds": 30,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	results := asMap(t, out)["task_results"].([]any)
	first := results[0].(map[string]any)
	if first["outcome"] != "failed" {
		t.Errorf("first outcome = %v, want failed", first["outcome"])
	}
	if first["error"] != "Some unexpected error" {
		t.Errorf("first error = %v, want 'Some unexpected error'", first["error"])
	}
	if results[1].(map[string]any)["outcome"] != "completed" {
		t.Errorf("second outcome = %v, want completed", results[1].(map[string]any)["outcome"])
	}
}

// Contract: completed_count and failed_count are accurate; complete=false → failed.
func TestFastExecuteTasks_Counts(t *testing.T) {
	n := 0
	deps, _ := execDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		n++
		if n == 1 {
			return map[string]any{"complete": true, "files_changed": []any{}, "summary": "done"}, nil
		}
		return map[string]any{"complete": false, "files_changed": []any{}, "summary": "partial"}, nil
	})
	secondTask := map[string]any{"name": "second-task", "title": "S", "description": "d", "acceptance_criteria": []any{"x"}}

	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{sampleTask, secondTask}, "repo_path": "/tmp/repo", "task_timeout_seconds": 30,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if intOf(m["completed_count"]) != 1 {
		t.Errorf("completed_count = %v, want 1", m["completed_count"])
	}
	if intOf(m["failed_count"]) != 1 {
		t.Errorf("failed_count = %v, want 1", m["failed_count"])
	}
}

// Contract: an empty task list returns completed_count=0, failed_count=0, and
// task_results serialized as [] (not null).
func TestFastExecuteTasks_EmptyTasks(t *testing.T) {
	deps, s := execDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	})
	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{}, "repo_path": "/tmp/repo", "task_timeout_seconds": 30,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if intOf(m["completed_count"]) != 0 || intOf(m["failed_count"]) != 0 {
		t.Errorf("counts = %v/%v, want 0/0", m["completed_count"], m["failed_count"])
	}
	tr, ok := m["task_results"].([]any)
	if !ok || len(tr) != 0 {
		t.Errorf("task_results = %v, want [] (non-null empty)", m["task_results"])
	}
	if n := s.count(); n != 0 {
		t.Errorf("expected no coder calls for empty task list, got %d", n)
	}
}

// Contract: the timeout FastTaskResult serializes files_changed as [] not null.
func TestFastExecuteTasks_TimeoutFilesChangedEmpty(t *testing.T) {
	deps, _ := execDeps(func(ctx context.Context, _ string, _ map[string]any) (map[string]any, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	})
	start := time.Now()
	out, err := FastExecuteTasks(context.Background(), deps, map[string]any{
		"tasks": []any{sampleTask}, "repo_path": "/tmp/repo", "task_timeout_seconds": 1,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	if time.Since(start) > 3*time.Second {
		t.Errorf("timeout took too long: %v", time.Since(start))
	}
	tr := asMap(t, out)["task_results"].([]any)[0].(map[string]any)
	if fc, ok := tr["files_changed"].([]any); !ok || len(fc) != 0 {
		t.Errorf("files_changed = %v, want []", tr["files_changed"])
	}
	if tr["error"] != "Timed out after 1s" {
		t.Errorf("error = %v, want 'Timed out after 1s'", tr["error"])
	}
}

package fast

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"
)

// asMap marshals a handler result to a map so key/value assertions mirror the
// Python model_dump() dict the reasoner returns.
func asMap(t *testing.T, v any) map[string]any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal result: %v", err)
	}
	return m
}

func plannerDeps(h *mockHarness) (*Deps, *noteRecorder) {
	notes := &noteRecorder{}
	return &Deps{Harness: h, Note: notes}, notes
}

// Contract: a clean parse yields a FastPlanResult with the parsed tasks and
// fallback_used==false.
func TestFastPlanTasks_ValidResponse(t *testing.T) {
	body := `{"tasks":[{"name":"step-one","title":"Do","description":"d","acceptance_criteria":["a"]},
	                   {"name":"step-two","title":"Do","description":"d","acceptance_criteria":["a"]}],
	          "rationale":"Two logical steps."}`
	h := &mockHarness{fn: parsedResult(body)}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "Build a REST API", "repo_path": "/tmp/repo", "max_tasks": 10,
	})
	if err != nil {
		t.Fatalf("FastPlanTasks error: %v", err)
	}
	m := asMap(t, out)
	tasks, _ := m["tasks"].([]any)
	if len(tasks) != 2 {
		t.Fatalf("tasks len = %d, want 2", len(tasks))
	}
	if first := tasks[0].(map[string]any); first["name"] != "step-one" {
		t.Errorf("tasks[0].name = %v, want step-one", first["name"])
	}
	if m["fallback_used"] != false {
		t.Errorf("fallback_used = %v, want false", m["fallback_used"])
	}
	if !h.called {
		t.Error("harness was not called")
	}
}

// Contract: a clean parse that (wrongly) carries fallback_used=true is reset to
// false — the flag is planner-side state, not an LLM self-assessment.
func TestFastPlanTasks_ForcesFallbackUsedFalse(t *testing.T) {
	body := `{"tasks":[{"name":"real-task","title":"t","description":"d","acceptance_criteria":["a"]}],
	          "rationale":"codex mistake","fallback_used":true}`
	h := &mockHarness{fn: parsedResult(body)}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "Add /health", "repo_path": "/tmp/repo",
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["fallback_used"] != false {
		t.Errorf("fallback_used = %v, want false", m["fallback_used"])
	}
	tasks := m["tasks"].([]any)
	if tasks[0].(map[string]any)["name"] != "real-task" {
		t.Errorf("task name = %v, want real-task", tasks[0].(map[string]any)["name"])
	}
}

// Contract: a nil parsed response triggers the single-task fallback with
// 'implement-goal' and fallback_used==true.
func TestFastPlanTasks_NilParsedTriggersFallback(t *testing.T) {
	h := &mockHarness{fn: nilParsedResult("no structured output")}
	deps, notes := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "Build something", "repo_path": "/tmp/repo",
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["fallback_used"] != true {
		t.Errorf("fallback_used = %v, want true", m["fallback_used"])
	}
	tasks := m["tasks"].([]any)
	if len(tasks) != 1 || tasks[0].(map[string]any)["name"] != "implement-goal" {
		t.Errorf("expected single implement-goal task, got %v", tasks)
	}
	if !notes.hasTag("fallback") {
		t.Error("expected a fallback-tagged note")
	}
}

// Contract: a harness error (including fatal) triggers the fallback — the fast
// planner is deliberately lenient and never propagates the error.
func TestFastPlanTasks_HarnessErrorTriggersFallback(t *testing.T) {
	h := &mockHarness{fn: func(any) (*harness.Result, error) {
		return nil, errors.New("LLM connection error")
	}}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "Build something", "repo_path": "/tmp/repo",
	})
	if err != nil {
		t.Fatalf("error: %v (fast planner must not propagate)", err)
	}
	m := asMap(t, out)
	if m["fallback_used"] != true {
		t.Errorf("fallback_used = %v, want true", m["fallback_used"])
	}
	tasks := m["tasks"].([]any)
	if tasks[0].(map[string]any)["name"] != "implement-goal" {
		t.Errorf("expected implement-goal, got %v", tasks)
	}
}

// Contract: even a fatal API error yields the fallback (fast planner catches all).
func TestFastPlanTasks_FatalErrorTriggersFallback(t *testing.T) {
	h := &mockHarness{fn: func(any) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "Credit balance is too low"}, nil
	}}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "g", "repo_path": "/tmp/repo",
	})
	if err != nil {
		t.Fatalf("fast planner propagated a fatal error: %v", err)
	}
	if asMap(t, out)["fallback_used"] != true {
		t.Error("expected fallback on fatal error")
	}
}

// Contract: when the LLM returns more tasks than max_tasks, the result is
// truncated to max_tasks.
func TestFastPlanTasks_MaxTasksTruncation(t *testing.T) {
	body := `{"tasks":[
	  {"name":"t0","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t1","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t2","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t3","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t4","title":"t","description":"d","acceptance_criteria":["a"]}
	],"rationale":"many"}`
	h := &mockHarness{fn: parsedResult(body)}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "g", "repo_path": "/tmp/repo", "max_tasks": 1,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	if tasks := asMap(t, out)["tasks"].([]any); len(tasks) != 1 {
		t.Errorf("tasks len = %d, want 1 (truncated)", len(tasks))
	}
}

// Contract: exactly max_tasks tasks are all preserved.
func TestFastPlanTasks_MaxTasksExactPreserved(t *testing.T) {
	body := `{"tasks":[
	  {"name":"t0","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t1","title":"t","description":"d","acceptance_criteria":["a"]},
	  {"name":"t2","title":"t","description":"d","acceptance_criteria":["a"]}
	],"rationale":"exact"}`
	h := &mockHarness{fn: parsedResult(body)}
	deps, _ := plannerDeps(h)

	out, err := FastPlanTasks(context.Background(), deps, map[string]any{
		"goal": "g", "repo_path": "/tmp/repo", "max_tasks": 3,
	})
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	if tasks := asMap(t, out)["tasks"].([]any); len(tasks) != 3 {
		t.Errorf("tasks len = %d, want 3", len(tasks))
	}
}

// Contract: the fallback FastTask carries the Pydantic list/int defaults
// (files_to_create=[], files_to_modify=[], estimated_minutes=5) as [] not null.
func TestFastPlanTasks_FallbackTaskDefaults(t *testing.T) {
	h := &mockHarness{fn: nilParsedResult("x")}
	deps, _ := plannerDeps(h)
	out, _ := FastPlanTasks(context.Background(), deps, map[string]any{"goal": "g", "repo_path": "/r"})
	task := asMap(t, out)["tasks"].([]any)[0].(map[string]any)
	if fc, ok := task["files_to_create"].([]any); !ok || len(fc) != 0 {
		t.Errorf("files_to_create = %v, want []", task["files_to_create"])
	}
	if em := intOf(task["estimated_minutes"]); em != 5 {
		t.Errorf("estimated_minutes = %d, want 5", em)
	}
}

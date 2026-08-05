package pro

import (
	"context"
	"errors"
	"strings"
	"testing"
)

type callRec struct {
	target string
	kwargs map[string]any
	res    map[string]any
	err    error
}

func (c *callRec) call(_ context.Context, target string, kwargs map[string]any) (map[string]any, error) {
	c.target = target
	c.kwargs = kwargs
	return c.res, c.err
}

type noteRec struct{ notes []string }

func (n *noteRec) Note(_ context.Context, msg string, _ ...string) { n.notes = append(n.notes, msg) }

func sampleIssue() map[string]any {
	return map[string]any{
		"name":                "add-abs",
		"title":               "Add Abs helper",
		"description":         "Implement Abs(int) int in mathx.",
		"acceptance_criteria": []any{"Abs(-2) == 2", "Abs(2) == 2"},
		"files_to_create":     []any{"mathx/abs.go"},
		"files_to_modify":     []any{"mathx/doc.go"},
		"testing_strategy":    "table-driven unit tests",
	}
}

func TestComposeGoal(t *testing.T) {
	goal := ComposeGoal(sampleIssue())
	for _, want := range []string{
		"Add Abs helper",
		"Implement Abs(int) int in mathx.",
		"- Abs(-2) == 2",
		"Files to create: mathx/abs.go",
		"Files to modify: mathx/doc.go",
		"Testing strategy: table-driven unit tests",
	} {
		if !strings.Contains(goal, want) {
			t.Errorf("ComposeGoal missing %q in:\n%s", want, goal)
		}
	}
}

func TestComposeGoalMinimal(t *testing.T) {
	goal := ComposeGoal(map[string]any{"name": "fix-typo", "description": "Fix the typo."})
	if !strings.HasPrefix(goal, "fix-typo") || !strings.Contains(goal, "Fix the typo.") {
		t.Errorf("minimal goal = %q", goal)
	}
	if strings.Contains(goal, "Acceptance criteria") {
		t.Errorf("empty sections must be omitted: %q", goal)
	}
}

func TestProExecutePass(t *testing.T) {
	for _, env := range []string{EnvMaxCost, EnvModelsHigh, EnvModelsLow, EnvVariant} {
		t.Setenv(env, "")
	}
	rec := &callRec{res: map[string]any{
		"status": "pass", "run_id": "r-1", "cost_usd": 1.25, "cycle": 2.0,
	}}
	notes := &noteRec{}
	deps := &Deps{Call: rec.call, Note: notes, EngineNode: "swe-pro"}

	out, err := ProExecute(context.Background(), deps,
		map[string]any{"issue": sampleIssue(), "repo_path": "/tmp/repo"})
	if err != nil {
		t.Fatal(err)
	}
	if rec.target != "swe-pro.code_task" {
		t.Errorf("target = %q, want swe-pro.code_task", rec.target)
	}
	if rec.kwargs["dir"] != "/tmp/repo" {
		t.Errorf("dir = %v", rec.kwargs["dir"])
	}
	for _, kw := range []string{"max_cost", "high", "low", "variant"} {
		if _, ok := rec.kwargs[kw]; ok {
			t.Errorf("%s forwarded without its env override set", kw)
		}
	}
	m := out.(map[string]any)
	if m["outcome"] != "completed" {
		t.Errorf("outcome = %v, want completed", m["outcome"])
	}
	if s := m["result_summary"].(string); !strings.Contains(s, "run=r-1") || !strings.Contains(s, "cost=$1.25") {
		t.Errorf("summary = %q", s)
	}
	if _, ok := m["error_message"]; ok {
		t.Error("error_message set on a pass")
	}
	if len(notes.notes) != 2 {
		t.Errorf("want dispatch+outcome notes, got %v", notes.notes)
	}
}

func TestProExecuteFailureMapping(t *testing.T) {
	cases := map[string]string{
		"fail":             "failed_retryable",
		"escalated":        "failed_retryable",
		"crashed":          "failed_retryable",
		"unknown":          "failed_retryable",
		"budget-exhausted": "failed_unrecoverable",
	}
	for status, wantOutcome := range cases {
		rec := &callRec{res: map[string]any{"status": status, "reason": "because"}}
		deps := &Deps{Call: rec.call, EngineNode: "swe-pro"}
		out, err := ProExecute(context.Background(), deps,
			map[string]any{"issue": sampleIssue(), "repo_path": "/tmp/repo"})
		if err != nil {
			t.Fatalf("%s: %v", status, err)
		}
		m := out.(map[string]any)
		if m["outcome"] != wantOutcome {
			t.Errorf("status %q → outcome %v, want %s", status, m["outcome"], wantOutcome)
		}
		if msg := m["error_message"].(string); !strings.Contains(msg, status) || !strings.Contains(msg, "because") {
			t.Errorf("status %q error_message = %q", status, msg)
		}
	}
}

func TestProExecuteMaxCostForwarded(t *testing.T) {
	t.Setenv(EnvMaxCost, "2.50")
	rec := &callRec{res: map[string]any{"status": "pass"}}
	deps := &Deps{Call: rec.call, EngineNode: "swe-pro"}
	if _, err := ProExecute(context.Background(), deps,
		map[string]any{"issue": sampleIssue(), "repo_path": "/tmp/repo"}); err != nil {
		t.Fatal(err)
	}
	if rec.kwargs["max_cost"] != "2.50" {
		t.Errorf("max_cost = %v, want 2.50", rec.kwargs["max_cost"])
	}
}

func TestProExecuteModelOverridesForwarded(t *testing.T) {
	t.Setenv(EnvModelsHigh, "openrouter/openai/gpt-5.6-sol")
	t.Setenv(EnvModelsLow, "openrouter/openai/gpt-5.6-sol")
	t.Setenv(EnvVariant, "low")
	rec := &callRec{res: map[string]any{"status": "pass"}}
	deps := &Deps{Call: rec.call, EngineNode: "swe-pro"}
	if _, err := ProExecute(context.Background(), deps,
		map[string]any{"issue": sampleIssue(), "repo_path": "/tmp/repo"}); err != nil {
		t.Fatal(err)
	}
	if rec.kwargs["high"] != "openrouter/openai/gpt-5.6-sol" ||
		rec.kwargs["low"] != "openrouter/openai/gpt-5.6-sol" ||
		rec.kwargs["variant"] != "low" {
		t.Errorf("model overrides not forwarded: %v", rec.kwargs)
	}
}

func TestProExecuteCallErrorPropagates(t *testing.T) {
	rec := &callRec{err: errors.New("connection refused")}
	deps := &Deps{Call: rec.call, EngineNode: "swe-pro"}
	if _, err := ProExecute(context.Background(), deps,
		map[string]any{"issue": sampleIssue(), "repo_path": "/tmp/repo"}); err == nil {
		t.Fatal("transport error must propagate for the retry loop")
	}
}

func TestProExecuteValidation(t *testing.T) {
	deps := &Deps{Call: (&callRec{}).call, EngineNode: "swe-pro"}
	if _, err := ProExecute(context.Background(), deps, map[string]any{"repo_path": "/tmp/repo"}); err == nil {
		t.Error("missing issue must error")
	}
	if _, err := ProExecute(context.Background(), deps, map[string]any{"issue": sampleIssue()}); err == nil {
		t.Error("missing repo_path must error")
	}
}

package orch

import (
	"context"
	"errors"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/reasonerfail"
)

// --- mock App -------------------------------------------------------------

type mockApp struct {
	handler func(ctx context.Context, target string, input map[string]any) (map[string]any, error)
	notes   []string
}

func (m *mockApp) Call(ctx context.Context, target string, input map[string]any) (map[string]any, error) {
	return m.handler(ctx, target, input)
}

func (m *mockApp) Note(ctx context.Context, message string, tags ...string) {
	m.notes = append(m.notes, message)
}

// withExecCtx overrides the execution-context seam for a test.
func withExecCtx(runID, execID string) func() {
	prev := executionContextFrom
	executionContextFrom = func(context.Context) agent.ExecutionContext {
		return agent.ExecutionContext{RunID: runID, ExecutionID: execID}
	}
	return func() { executionContextFrom = prev }
}

// --- isEmptyBuild (maps to test_empty_build_guard.py) ---------------------

func TestIsEmptyBuildTruthTable(t *testing.T) {
	cases := []struct {
		success       bool
		everCompleted int
		everMerged    int
		want          bool
	}{
		{false, 0, 0, true},
		{false, 1, 0, false},
		{false, 0, 1, false},
		{false, 3, 2, false},
		{true, 0, 0, false},
		{true, 5, 4, false},
	}
	for _, c := range cases {
		if got := isEmptyBuild(c.success, c.everCompleted, c.everMerged); got != c.want {
			t.Errorf("isEmptyBuild(%v,%d,%d)=%v want %v",
				c.success, c.everCompleted, c.everMerged, got, c.want)
		}
	}
}

// --- deriveRepoName (maps to _derive_repo_name) ---------------------------

func TestDeriveRepoName(t *testing.T) {
	cases := map[string]string{
		"https://github.com/org/my-project.git": "my-project",
		"git@github.com:org/repo.git":           "repo",
		"https://github.com/org/repo":           "repo",
		"https://github.com/org/repo/":          "repo",
		"":                                      "",
	}
	for url, want := range cases {
		if got := deriveRepoName(url); got != want {
			t.Errorf("deriveRepoName(%q)=%q want %q", url, got, want)
		}
	}
}

// --- NewCallFn / Call unwrap the envelope ---------------------------------

func TestNewCallFnUnwrapsResult(t *testing.T) {
	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		return map[string]any{
			"status":       "succeeded",
			"execution_id": "e1",
			"result":       map[string]any{"x": float64(1)},
		}, nil
	}}
	d := &Deps{App: app, NodeID: "swe-planner"}
	fn := d.NewCallFn()
	out, err := fn(context.Background(), "swe-planner.run_coder", nil)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if asInt(out["x"]) != 1 {
		t.Fatalf("expected unwrapped result {x:1}, got %v", out)
	}
}

func TestCallPropagatesFailureEnvelope(t *testing.T) {
	app := &mockApp{handler: func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return map[string]any{"status": "failed", "error_message": "boom"}, nil
	}}
	d := &Deps{App: app, NodeID: "swe-planner"}
	if _, err := d.Call(context.Background(), "execute", nil, "execute"); err == nil {
		t.Fatal("expected error from failed envelope")
	}
}

func TestCallRawReturnsEnvelope(t *testing.T) {
	env := map[string]any{"status": "succeeded", "result": map[string]any{"ok": true}}
	app := &mockApp{handler: func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return env, nil
	}}
	d := &Deps{App: app, NodeID: "swe-planner"}
	raw, err := d.CallRaw(context.Background(), "run_git_init", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := raw["status"]; !ok {
		t.Fatalf("CallRaw should return the raw envelope, got %v", raw)
	}
}

// --- dumpToMap / manifestFromMap round-trip -------------------------------

func TestDumpToMapNilStaysNil(t *testing.T) {
	if got := dumpToMap(nil); got != nil {
		t.Fatalf("dumpToMap(nil) should be nil, got %v", got)
	}
	var mp *struct{}
	if got := dumpToMap(mp); got != nil {
		t.Fatalf("dumpToMap(nil ptr) should be nil, got %v", got)
	}
}

func TestPosterConfigSeam(t *testing.T) {
	d := &Deps{AgentFieldServer: "http://cp:8080", Token: "tok"}
	pc := d.PosterConfig()
	if pc.AgentFieldURL != "http://cp:8080" || pc.Token != "tok" {
		t.Fatalf("PosterConfig mismatch: %+v", pc)
	}
}

// sanity: postFailedFn seam is overridable and returns the message error.
func TestPostFailedSeamOverridable(t *testing.T) {
	prev := postFailedFn
	defer func() { postFailedFn = prev }()
	var got any
	postFailedFn = func(_ context.Context, _ reasonerfail.PosterConfig, result any, message string) error {
		got = result
		return errors.New(message)
	}
	err := postFailedFn(context.Background(), reasonerfail.PosterConfig{}, map[string]any{"a": 1}, "msg")
	if err == nil || err.Error() != "msg" {
		t.Fatalf("expected msg error, got %v", err)
	}
	if got == nil {
		t.Fatal("result not captured")
	}
}

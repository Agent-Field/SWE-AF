package advisor

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// newHaxServer returns a HaxClient wired to an httptest server that always
// replies with a request id/url, so the ask-user pause completes without a live
// hax backend.
func newHaxServer(t *testing.T) (*hitl.HaxClient, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"id": "req-1", "url": "https://hax.example/req-1"})
	}))
	t.Cleanup(srv.Close)
	return &hitl.HaxClient{BaseURL: srv.URL, APIKey: "test-key", HTTPClient: srv.Client()}, srv
}

// ---------------------------------------------------------------------------
// run_verifier
// ---------------------------------------------------------------------------

func verifierInputMap() map[string]any {
	return map[string]any{
		"prd":              map[string]any{"validated_description": "build a thing"},
		"repo_path":        "/repo",
		"artifacts_dir":    "/artifacts",
		"completed_issues": []any{},
		"failed_issues":    []any{},
		"skipped_issues":   []any{},
	}
}

func TestRunVerifierSuccess(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.VerificationResult)
		d.Passed = true
		d.Summary = "all good"
		d.CriteriaResults = []schemas.CriterionResult{{Criterion: "c1", Passed: true}}
		return &harness.Result{Parsed: dest}, nil
	}}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app}

	out, err := RunVerifier(context.Background(), deps, verifierInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	for _, k := range []string{"passed", "criteria_results", "summary", "suggested_fixes"} {
		if _, ok := m[k]; !ok {
			t.Errorf("output missing key %q", k)
		}
	}
	if m["passed"] != true {
		t.Errorf("passed = %v, want true", m["passed"])
	}
	if got := app.messageWithTag("complete"); got != "Verifier complete: passed=True, summary=all good" {
		t.Errorf("complete note = %q", got)
	}
}

func TestRunVerifierFallbackListShape(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	out, err := RunVerifier(context.Background(), deps, verifierInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["passed"] != false {
		t.Errorf("passed = %v, want false", m["passed"])
	}
	// criteria_results must be an (empty) list, not null.
	cr, ok := m["criteria_results"].([]any)
	if !ok {
		t.Fatalf("criteria_results is not a list: %v (%T)", m["criteria_results"], m["criteria_results"])
	}
	if len(cr) != 0 {
		t.Errorf("criteria_results = %v, want []", cr)
	}
	if m["summary"] != "Verifier agent failed to produce a valid result." {
		t.Errorf("summary = %q", m["summary"])
	}
	sf, ok := m["suggested_fixes"].([]any)
	if !ok || len(sf) != 1 || sf[0] != "Re-run verification manually." {
		t.Errorf("suggested_fixes = %v", m["suggested_fixes"])
	}
}

func TestRunVerifierFatalPropagates(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return fatalResult() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}
	out, err := RunVerifier(context.Background(), deps, verifierInputMap())
	if err == nil || out != nil {
		t.Fatalf("expected fatal propagation, got out=%v err=%v", out, err)
	}
}

// ---------------------------------------------------------------------------
// generate_fix_issues
// ---------------------------------------------------------------------------

func fixInputMap() map[string]any {
	return map[string]any{
		"failed_criteria": []any{
			map[string]any{"criterion": "must do X", "issue_name": "issue-1"},
			map[string]any{"criterion": "must do Y", "issue_name": "issue-2"},
		},
		"dag_state": map[string]any{"repo_path": "/repo"},
		"prd":       map[string]any{"validated_description": "desc"},
	}
}

func TestGenerateFixIssuesSuccess(t *testing.T) {
	// Unmarshal JSON into dest to mirror the real harness parse, which triggers
	// fixGeneratorOutput.UnmarshalJSON and seeds the empty-list defaults.
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		if err := json.Unmarshal([]byte(`{"fix_issues":[{"name":"fix-1"}],"summary":"one fix"}`), dest); err != nil {
			return nil, err
		}
		return &harness.Result{Parsed: dest}, nil
	}}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app}

	out, err := GenerateFixIssues(context.Background(), deps, fixInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	for _, k := range []string{"fix_issues", "debt_items", "summary"} {
		if _, ok := m[k]; !ok {
			t.Errorf("output missing key %q", k)
		}
	}
	// debt_items must serialize as [] (empty), not null (seeded default).
	if di, ok := m["debt_items"].([]any); !ok || len(di) != 0 {
		t.Errorf("debt_items = %v, want []", m["debt_items"])
	}
	if got := app.messageWithTag("complete"); got != "Fix generator complete: 1 fix issues, 0 debt items" {
		t.Errorf("complete note = %q", got)
	}
}

func TestGenerateFixIssuesFallbackRecordsDebt(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	out, err := GenerateFixIssues(context.Background(), deps, fixInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if fi, ok := m["fix_issues"].([]any); !ok || len(fi) != 0 {
		t.Errorf("fix_issues = %v, want []", m["fix_issues"])
	}
	di, ok := m["debt_items"].([]any)
	if !ok || len(di) != 2 {
		t.Fatalf("debt_items = %v, want 2 entries", m["debt_items"])
	}
	first := di[0].(map[string]any)
	if first["criterion"] != "must do X" || first["reason"] != "Fix generator failed to analyze" || first["severity"] != "high" {
		t.Errorf("debt item 0 = %v", first)
	}
	if m["summary"] != "Fix generator failed — all criteria recorded as debt" {
		t.Errorf("summary = %q", m["summary"])
	}
}

// Contract: multi-repo workspace augments the task prompt with a target_repo
// instruction and lists each repo.
func TestGenerateFixIssuesMultiRepoPromptAugmentation(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		return &harness.Result{Parsed: dest}, nil
	}}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	input := fixInputMap()
	input["workspace_manifest"] = map[string]any{
		"workspace_root":    "/ws",
		"primary_repo_name": "app",
		"repos": []any{
			map[string]any{"repo_name": "app", "role": "primary", "absolute_path": "/ws/app"},
			map[string]any{"repo_name": "lib", "role": "dependency", "absolute_path": "/ws/lib"},
		},
	}
	if _, err := GenerateFixIssues(context.Background(), deps, input); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(mh.lastPrompt, "## Multi-Repo Context") {
		t.Error("prompt missing Multi-Repo Context header")
	}
	if !strings.Contains(mh.lastPrompt, "- **app** (role: primary): `/ws/app`") {
		t.Error("prompt missing primary repo line")
	}
	if !strings.Contains(mh.lastPrompt, "- **lib** (role: dependency): `/ws/lib`") {
		t.Error("prompt missing dependency repo line")
	}
}

// ---------------------------------------------------------------------------
// run_issue_writer
// ---------------------------------------------------------------------------

func issueWriterInputMap() map[string]any {
	return map[string]any{
		"issue":                map[string]any{"name": "issue-3", "title": "Do the thing"},
		"prd_summary":          "prd",
		"architecture_summary": "arch",
		"issues_dir":           "/issues",
		"repo_path":            "/repo",
	}
}

func TestRunIssueWriterSuccess(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*issueWriterOutput)
		d.IssueName = "issue-3"
		d.IssueFilePath = "/issues/issue-03-issue-3.md"
		d.Success = true
		return &harness.Result{Parsed: dest}, nil
	}}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app}

	out, err := RunIssueWriter(context.Background(), deps, issueWriterInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	for _, k := range []string{"issue_name", "issue_file_path", "success"} {
		if _, ok := m[k]; !ok {
			t.Errorf("output missing key %q", k)
		}
	}
	if m["success"] != true || m["issue_file_path"] != "/issues/issue-03-issue-3.md" {
		t.Errorf("output = %v", m)
	}
}

func TestRunIssueWriterFallback(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	out, err := RunIssueWriter(context.Background(), deps, issueWriterInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["issue_name"] != "issue-3" || m["issue_file_path"] != "" || m["success"] != false {
		t.Errorf("fallback output = %v", m)
	}
}

// Contract: issue name defaults to "unknown" (not "?") when absent — the
// issue_writer-specific default.
func TestRunIssueWriterMissingNameDefault(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	input := issueWriterInputMap()
	input["issue"] = map[string]any{}
	out, err := RunIssueWriter(context.Background(), deps, input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["issue_name"] != "unknown" {
		t.Errorf("issue_name = %v, want unknown", m["issue_name"])
	}
}

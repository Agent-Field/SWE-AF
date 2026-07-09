package advisor

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/client"
	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

// mockHarness is the harnessx.HarnessCaller seam — the Go equivalent of the
// Python tests patching router.harness. fn is invoked with the dest pointer so a
// test can populate the parsed struct or return a failing result.
type mockHarness struct {
	fn         func(call int, dest any) (*harness.Result, error)
	calls      int
	lastPrompt string
	lastOpts   harness.Options
}

func (m *mockHarness) Harness(_ context.Context, prompt string, _ map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	m.calls++
	m.lastPrompt = prompt
	m.lastOpts = opts
	return m.fn(m.calls, dest)
}

// noOutput is a non-fatal failing result with no parsed output (schema parse
// failure) — drives every reasoner's deterministic fallback path.
func noOutput() (*harness.Result, error) {
	return &harness.Result{IsError: true, ErrorMessage: "no structured output returned", Parsed: nil}, nil
}

// fatalResult is a non-retryable API failure that must propagate.
func fatalResult() (*harness.Result, error) {
	return &harness.Result{IsError: true, ErrorMessage: "Credit balance is too low"}, nil
}

// noteEntry records one router.note call.
type noteEntry struct {
	message string
	tags    []string
}

// captureApp records notes so tests can assert message/tag parity.
type captureApp struct{ notes []noteEntry }

func (c *captureApp) Note(_ context.Context, message string, tags ...string) {
	c.notes = append(c.notes, noteEntry{message: message, tags: tags})
}

func (c *captureApp) hasTag(tag string) bool {
	for _, n := range c.notes {
		for _, tg := range n.tags {
			if tg == tag {
				return true
			}
		}
	}
	return false
}

func (c *captureApp) messageWithTag(tag string) string {
	for _, n := range c.notes {
		for _, tg := range n.tags {
			if tg == tag {
				return n.message
			}
		}
	}
	return ""
}

// fakeApprovals is an in-memory ApprovalClient.
type fakeApprovals struct {
	reqCalls int
	resp     *client.ApprovalStatusResponse
}

func (f *fakeApprovals) RequestApproval(_ context.Context, _, _ string, req client.RequestApprovalRequest) (*client.RequestApprovalResponse, error) {
	f.reqCalls++
	return &client.RequestApprovalResponse{ApprovalRequestID: req.ApprovalRequestID}, nil
}

func (f *fakeApprovals) WaitForApproval(_ context.Context, _, _ string, _ *client.WaitForApprovalOptions) (*client.ApprovalStatusResponse, error) {
	if f.resp != nil {
		return f.resp, nil
	}
	return &client.ApprovalStatusResponse{Status: "approved", Response: map[string]any{"values": map[string]any{}}}, nil
}

func asMap(t *testing.T, v any) map[string]any {
	t.Helper()
	m, err := structToMap(v)
	if err != nil {
		t.Fatalf("structToMap: %v", err)
	}
	return m
}

// ---------------------------------------------------------------------------
// Handlers registration surface
// ---------------------------------------------------------------------------

func TestHandlersExposesExactNames(t *testing.T) {
	want := []string{
		"run_retry_advisor", "run_issue_advisor", "run_replanner",
		"run_issue_writer", "run_verifier", "generate_fix_issues",
	}
	h := Handlers()
	if len(h) != len(want) {
		t.Fatalf("Handlers() has %d entries, want %d: %v", len(h), len(want), h)
	}
	for _, name := range want {
		if h[name] == nil {
			t.Errorf("missing handler for %q", name)
		}
	}
}

// ---------------------------------------------------------------------------
// run_retry_advisor
// ---------------------------------------------------------------------------

func TestRunRetryAdvisorSuccess(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.RetryAdvice)
		d.ShouldRetry = true
		d.Diagnosis = "root cause"
		d.Confidence = 0.8
		return &harness.Result{Parsed: dest}, nil
	}}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app}

	out, err := RunRetryAdvisor(context.Background(), deps, map[string]any{
		"issue":          map[string]any{"name": "issue-1"},
		"error_message":  "boom",
		"error_context":  "trace",
		"attempt_number": 2,
		"repo_path":      "/repo",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	for _, k := range []string{"should_retry", "diagnosis", "strategy", "modified_context", "confidence"} {
		if _, ok := m[k]; !ok {
			t.Errorf("output missing key %q", k)
		}
	}
	if m["should_retry"] != true {
		t.Errorf("should_retry = %v, want true", m["should_retry"])
	}
	// Verbatim note parity: bool -> True, float -> keeps decimal.
	if got := app.messageWithTag("complete"); got != "Retry advisor: should_retry=True, confidence=0.8" {
		t.Errorf("complete note = %q", got)
	}
}

func TestRunRetryAdvisorFallbackOnParseFailure(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	out, err := RunRetryAdvisor(context.Background(), deps, map[string]any{
		"issue": map[string]any{"name": "x"}, "error_message": "", "error_context": "",
		"attempt_number": 1, "repo_path": "/repo",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["should_retry"] != false {
		t.Errorf("fallback should_retry = %v, want false", m["should_retry"])
	}
	if m["diagnosis"] != "Retry advisor agent failed to produce a valid analysis." {
		t.Errorf("fallback diagnosis = %q", m["diagnosis"])
	}
	if m["strategy"] != "Cannot advise — advisor failure." {
		t.Errorf("fallback strategy = %q", m["strategy"])
	}
	if m["confidence"] != float64(0) {
		t.Errorf("fallback confidence = %v, want 0", m["confidence"])
	}
}

func TestRunRetryAdvisorFatalPropagates(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return fatalResult() }}
	deps := &Deps{Harness: mh, App: &captureApp{}}

	out, err := RunRetryAdvisor(context.Background(), deps, map[string]any{
		"issue": map[string]any{"name": "x"}, "attempt_number": 1, "repo_path": "/repo",
	})
	if err == nil {
		t.Fatal("expected fatal error to propagate, got nil")
	}
	if out != nil {
		t.Errorf("expected nil output on fatal error, got %v", out)
	}
	if !isFatal(err) {
		t.Errorf("expected a fatal harness error, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// run_issue_advisor (HITL)
// ---------------------------------------------------------------------------

func issueAdvisorInputMap() map[string]any {
	return map[string]any{
		"issue":             map[string]any{"name": "issue-7"},
		"original_issue":    map[string]any{"name": "issue-7"},
		"failure_result":    map[string]any{},
		"iteration_history": []any{},
		"dag_state_summary": map[string]any{"repo_path": "/repo"},
	}
}

func TestRunIssueAdvisorSuccess(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.IssueAdvisorDecision)
		d.Action = schemas.AdvisorActionRetryApproach
		d.Summary = "try again differently"
		return &harness.Result{Parsed: dest}, nil
	}}
	app := &captureApp{}
	// BuildHaxClient returns nil so HITL is disabled; a parsed result with no
	// form still returns straight through.
	deps := &Deps{Harness: mh, App: app, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunIssueAdvisor(context.Background(), deps, issueAdvisorInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["action"] != "retry_approach" {
		t.Errorf("action = %v, want retry_approach", m["action"])
	}
	if got := app.messageWithTag("complete"); got != "Issue advisor decision: retry_approach — try again differently" {
		t.Errorf("complete note = %q", got)
	}
}

func TestRunIssueAdvisorFallbackAcceptWithDebt(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunIssueAdvisor(context.Background(), deps, issueAdvisorInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	// Verbatim fallback fields.
	if m["action"] != string(schemas.AdvisorActionAcceptWithDebt) {
		t.Errorf("action = %v, want accept_with_debt", m["action"])
	}
	if m["failure_diagnosis"] != "Issue Advisor agent failed to produce a valid analysis." {
		t.Errorf("failure_diagnosis = %q", m["failure_diagnosis"])
	}
	if m["failure_category"] != "environment" {
		t.Errorf("failure_category = %q", m["failure_category"])
	}
	if m["rationale"] != "Advisor failure — accepting with debt to avoid pipeline stall." {
		t.Errorf("rationale = %q", m["rationale"])
	}
	if m["confidence"] != 0.1 {
		t.Errorf("confidence = %v, want 0.1", m["confidence"])
	}
	if m["debt_severity"] != "high" {
		t.Errorf("debt_severity = %q, want high", m["debt_severity"])
	}
	if m["summary"] != "Issue advisor failed — accepting issue-7 with debt" {
		t.Errorf("summary = %q", m["summary"])
	}
	mf, ok := m["missing_functionality"].([]any)
	if !ok || len(mf) != 1 || mf[0] != "Full implementation of issue-7" {
		t.Errorf("missing_functionality = %v", m["missing_functionality"])
	}
	if !app.hasTag("fallback") {
		t.Error("expected a fallback note")
	}
}

// Contract: an emitted ask_user_form is ignored (stripped) when HITL is
// disabled (nil hax client) — the reasoner is invoked exactly once.
func TestRunIssueAdvisorHaxDisabledStripsForm(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.IssueAdvisorDecision)
		d.Action = schemas.AdvisorActionAcceptWithDebt
		d.AskUserForm = &schemas.AskUserForm{
			Title:  "Need input",
			Fields: []schemas.AskUserFormField{{ID: "q", Type: schemas.FieldTypeInput, Label: "?"}},
		}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunIssueAdvisor(context.Background(), deps, issueAdvisorInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if mh.calls != 1 {
		t.Errorf("harness calls = %d, want 1 (no re-invocation when HITL disabled)", mh.calls)
	}
	m := asMap(t, out)
	if m["ask_user_form"] != nil {
		t.Errorf("ask_user_form = %v, want nil (stripped)", m["ask_user_form"])
	}
}

// Contract: with HITL enabled and the form re-emitted every call, re-invocation
// is bounded — budget 2 => 3 invocations, 2 pauses.
func TestRunIssueAdvisorReInvocationBounded(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.IssueAdvisorDecision)
		d.Action = schemas.AdvisorActionAcceptWithDebt
		d.AskUserForm = &schemas.AskUserForm{
			Title:  "Need input",
			Fields: []schemas.AskUserFormField{{ID: "q", Type: schemas.FieldTypeInput, Label: "?"}},
		}
		return &harness.Result{Parsed: dest}, nil
	}}
	haxClient, _ := newHaxServer(t)
	approvals := &fakeApprovals{}
	deps := &Deps{
		Harness:        mh,
		App:            &captureApp{},
		Approvals:      approvals,
		BuildHaxClient: func() *hitl.HaxClient { return haxClient },
	}

	if _, err := RunIssueAdvisor(context.Background(), deps, issueAdvisorInputMap()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if mh.calls != 3 {
		t.Errorf("harness calls = %d, want 3 (budget 2 => 3 invocations)", mh.calls)
	}
	if approvals.reqCalls != 2 {
		t.Errorf("pause count = %d, want 2 (budget bound)", approvals.reqCalls)
	}
}

func TestRunIssueAdvisorFatalPropagates(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return fatalResult() }}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunIssueAdvisor(context.Background(), deps, issueAdvisorInputMap())
	if err == nil || out != nil {
		t.Fatalf("expected fatal propagation, got out=%v err=%v", out, err)
	}
	if !isFatal(err) {
		t.Errorf("expected fatal error, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// run_replanner (HITL)
// ---------------------------------------------------------------------------

func replannerInputMap() map[string]any {
	return map[string]any{
		"dag_state":     map[string]any{"repo_path": "/repo", "replan_count": 1, "max_replans": 2},
		"failed_issues": []any{map[string]any{"issue_name": "issue-a"}, map[string]any{"issue_name": "issue-b"}},
	}
}

func TestRunReplannerActionMapping(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.ReplanDecision)
		d.Action = schemas.ReplanActionModifyDAG
		d.Summary = "restructured"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunReplanner(context.Background(), deps, replannerInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["action"] != "modify_dag" {
		t.Errorf("action = %v, want modify_dag", m["action"])
	}
	if mh.calls != 1 {
		t.Errorf("harness calls = %d, want 1", mh.calls)
	}
}

func TestRunReplannerFallbackContinue(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return noOutput() }}
	app := &captureApp{}
	deps := &Deps{Harness: mh, App: app, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunReplanner(context.Background(), deps, replannerInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["action"] != string(schemas.ReplanActionContinue) {
		t.Errorf("action = %v, want continue", m["action"])
	}
	if !strings.Contains(m["summary"].(string), "['issue-a', 'issue-b']") {
		t.Errorf("summary missing python list repr of failed names: %q", m["summary"])
	}
	// Two harness attempts (inner parse-retry loop) before the fallback fires.
	if mh.calls != 2 {
		t.Errorf("harness calls = %d, want 2 (inner parse-retry loop)", mh.calls)
	}
	if !app.hasTag("parse_error") {
		t.Error("expected a parse_error note on the unparseable attempt")
	}
	if !app.hasTag("fallback") {
		t.Error("expected a fallback note")
	}
}

// Contract: first attempt unparseable, second attempt parses -> returns the
// decision; the second prompt is prefixed with the re-parse instruction.
func TestRunReplannerParseRetryThenSuccess(t *testing.T) {
	mh := &mockHarness{fn: func(call int, dest any) (*harness.Result, error) {
		if call == 1 {
			return &harness.Result{IsError: true, ErrorMessage: "bad json", Result: "not json", Parsed: nil}, nil
		}
		d := dest.(*schemas.ReplanDecision)
		d.Action = schemas.ReplanActionContinue
		d.Summary = "ok"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunReplanner(context.Background(), deps, replannerInputMap())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if mh.calls != 2 {
		t.Fatalf("harness calls = %d, want 2", mh.calls)
	}
	if !strings.HasPrefix(mh.lastPrompt, "YOUR PREVIOUS RESPONSE COULD NOT BE PARSED.") {
		t.Errorf("retry prompt not prefixed with re-parse instruction: %q", mh.lastPrompt[:60])
	}
	m := asMap(t, out)
	if m["action"] != "continue" {
		t.Errorf("action = %v, want continue", m["action"])
	}
}

// Contract: raw agent output is persisted to <artifacts>/logs on each attempt.
func TestRunReplannerWritesRawLog(t *testing.T) {
	dir := t.TempDir()
	mh := &mockHarness{fn: func(_ int, dest any) (*harness.Result, error) {
		d := dest.(*schemas.ReplanDecision)
		d.Action = schemas.ReplanActionContinue
		return &harness.Result{Parsed: dest, Result: "raw-agent-text"}, nil
	}}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	input := replannerInputMap()
	input["dag_state"].(map[string]any)["artifacts_dir"] = dir
	if _, err := RunReplanner(context.Background(), deps, input); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	logPath := filepath.Join(dir, "logs", "replanner_1_raw_0.txt")
	b, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatalf("expected raw log at %s: %v", logPath, err)
	}
	if string(b) != "raw-agent-text" {
		t.Errorf("raw log = %q, want raw-agent-text", string(b))
	}
}

func TestRunReplannerFatalPropagates(t *testing.T) {
	mh := &mockHarness{fn: func(_ int, _ any) (*harness.Result, error) { return fatalResult() }}
	deps := &Deps{Harness: mh, App: &captureApp{}, BuildHaxClient: func() *hitl.HaxClient { return nil }}

	out, err := RunReplanner(context.Background(), deps, replannerInputMap())
	if err == nil || out != nil {
		t.Fatalf("expected fatal propagation, got out=%v err=%v", out, err)
	}
	if !isFatal(err) {
		t.Errorf("expected fatal error, got %v", err)
	}
}

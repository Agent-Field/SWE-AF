package hitl

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

// silentApp swallows notes (mirrors the Python tests mocking app.note).
type silentApp struct{ notes []string }

func (s *silentApp) Note(_ context.Context, message string, _ ...string) {
	s.notes = append(s.notes, message)
}

// fakePauser is an in-memory Pauser standing in for *agent.Agent.Pause. It
// records the pause count and last options, and returns a scripted result/err,
// mirroring the webhook-resumed pause (no polling).
type fakePauser struct {
	calls    int
	lastOpts agent.PauseOptions
	result   *agent.ApprovalResult
	err      error
}

func (f *fakePauser) Pause(_ context.Context, opts agent.PauseOptions) (*agent.ApprovalResult, error) {
	f.calls++
	f.lastOpts = opts
	if f.err != nil {
		return nil, f.err
	}
	return f.result, nil
}

// newHaxTestServer returns a HaxClient wired to an httptest server that always
// replies with the given id/url. The last request body is captured.
func newHaxTestServer(t *testing.T, id, url string) (*HaxClient, *[]byte) {
	t.Helper()
	var body []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		body = b
		if got := r.Header.Get("Authorization"); got != "Bearer test-key" {
			t.Errorf("Authorization header = %q, want Bearer test-key", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"id": id, "url": url})
	}))
	t.Cleanup(srv.Close)
	return &HaxClient{BaseURL: srv.URL, APIKey: "test-key", HTTPClient: srv.Client()}, &body
}

func strptr(s string) *string { return &s }

// ---------------------------------------------------------------------------
// BuildHaxFormPayload  (maps to test_ask_user.py build_form_builder tests)
// ---------------------------------------------------------------------------

func TestBuildHaxFormPayloadInputField(t *testing.T) {
	spec := schemas.AskUserForm{
		Title:  "Need clarification",
		Fields: []schemas.AskUserFormField{{ID: "reason", Type: schemas.FieldTypeInput, Label: "Why?", Required: true}},
	}
	payload, err := BuildHaxFormPayload(spec)
	if err != nil {
		t.Fatal(err)
	}
	if payload["title"] != "Need clarification" {
		t.Errorf("title = %v", payload["title"])
	}
	fields := payload["fields"].([]map[string]any)
	if len(fields) != 1 {
		t.Fatalf("want 1 field, got %d", len(fields))
	}
	f := fields[0]
	if f["type"] != "input" || f["id"] != "reason" || f["label"] != "Why?" || f["required"] != true {
		t.Errorf("field = %v", f)
	}
}

func TestBuildHaxFormPayloadSelectWithOptions(t *testing.T) {
	opts := []map[string]string{{"value": "a", "label": "Option A"}, {"value": "b", "label": "Option B"}}
	spec := schemas.AskUserForm{
		Title:  "Pick one",
		Fields: []schemas.AskUserFormField{{ID: "choice", Type: schemas.FieldTypeSelect, Label: "Choice", Options: opts}},
	}
	payload, err := BuildHaxFormPayload(spec)
	if err != nil {
		t.Fatal(err)
	}
	f := payload["fields"].([]map[string]any)[0]
	if f["type"] != "select" {
		t.Errorf("type = %v", f["type"])
	}
	got := f["options"].([]map[string]string)
	if len(got) != 2 || got[0]["value"] != "a" || got[1]["label"] != "Option B" {
		t.Errorf("options = %v", got)
	}
}

func TestBuildHaxFormPayloadAllTypesSmoke(t *testing.T) {
	opt := []map[string]string{{"value": "x", "label": "X"}}
	spec := schemas.AskUserForm{
		Title: "All types",
		Fields: []schemas.AskUserFormField{
			{ID: "i", Type: schemas.FieldTypeInput, Label: "i"},
			{ID: "t", Type: schemas.FieldTypeTextarea, Label: "t"},
			{ID: "n", Type: schemas.FieldTypeNumber, Label: "n", Min: fptr(0), Max: fptr(10)},
			{ID: "sl", Type: schemas.FieldTypeSlider, Label: "sl", Min: fptr(0), Max: fptr(100), Step: fptr(5)},
			{ID: "s", Type: schemas.FieldTypeSelect, Label: "s", Options: opt},
			{ID: "r", Type: schemas.FieldTypeRadio, Label: "r", Options: opt},
			{ID: "cg", Type: schemas.FieldTypeCheckboxGroup, Label: "cg", Options: opt},
			{ID: "c", Type: schemas.FieldTypeCheckbox, Label: "c"},
			{ID: "sw", Type: schemas.FieldTypeSwitch, Label: "sw"},
			{ID: "d", Type: schemas.FieldTypeDate, Label: "d"},
		},
	}
	payload, err := BuildHaxFormPayload(spec)
	if err != nil {
		t.Fatal(err)
	}
	var types []string
	for _, f := range payload["fields"].([]map[string]any) {
		types = append(types, f["type"].(string))
	}
	want := []string{"input", "textarea", "number", "slider", "select", "radio-group", "checkbox-group", "checkbox", "switch", "date"}
	if strings.Join(types, ",") != strings.Join(want, ",") {
		t.Errorf("types = %v, want %v", types, want)
	}
	// checkbox/switch carry checkboxLabel/switchLabel and no placeholder.
	cb := payload["fields"].([]map[string]any)[7]
	if cb["checkboxLabel"] != "c" {
		t.Errorf("checkboxLabel = %v", cb["checkboxLabel"])
	}
	sw := payload["fields"].([]map[string]any)[8]
	if sw["switchLabel"] != "sw" {
		t.Errorf("switchLabel = %v", sw["switchLabel"])
	}
}

func TestBuildHaxFormPayloadSelectWithoutOptionsErrors(t *testing.T) {
	spec := schemas.AskUserForm{Title: "Bad", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeSelect, Label: "x"}}}
	_, err := BuildHaxFormPayload(spec)
	if err == nil || err.Error() != "select field 'x' requires options" {
		t.Errorf("err = %v", err)
	}
}

func TestBuildHaxFormPayloadSliderWithoutMinMaxErrors(t *testing.T) {
	spec := schemas.AskUserForm{Title: "Bad", Fields: []schemas.AskUserFormField{{ID: "s", Type: schemas.FieldTypeSlider, Label: "s"}}}
	_, err := BuildHaxFormPayload(spec)
	if err == nil || err.Error() != "slider field 's' requires both min and max" {
		t.Errorf("err = %v", err)
	}
}

func fptr(f float64) *float64 { return &f }

// ---------------------------------------------------------------------------
// parseApprovalResult  (maps to test_ask_user.py _parse_approval_result tests)
// ---------------------------------------------------------------------------

func TestParseApprovedWithValuesInRaw(t *testing.T) {
	raw := map[string]any{"values": map[string]any{"reason": "bug", "priority": "high"}}
	out := parseApprovalResult("approved", nil, raw)
	if out.Status != "submitted" {
		t.Errorf("status = %s", out.Status)
	}
	if out.Values["reason"] != "bug" || out.Values["priority"] != "high" {
		t.Errorf("values = %v", out.Values)
	}
}

func TestParseApprovedWithValuesNested(t *testing.T) {
	raw := map[string]any{"response": map[string]any{"values": map[string]any{"a": 1}}}
	out := parseApprovalResult("approved", nil, raw)
	if out.Status != "submitted" || out.Values["a"] != 1 {
		t.Errorf("out = %+v", out)
	}
}

func TestParseRejectedMapsToCancelled(t *testing.T) {
	out := parseApprovalResult("rejected", strptr("not now"), nil)
	if out.Status != "cancelled" {
		t.Errorf("status = %s", out.Status)
	}
	if out.Feedback == nil || *out.Feedback != "not now" {
		t.Errorf("feedback = %v", out.Feedback)
	}
}

func TestParseExpiredMapsToTimeout(t *testing.T) {
	out := parseApprovalResult("expired", nil, nil)
	if out.Status != "timeout" {
		t.Errorf("status = %s", out.Status)
	}
}

func TestParseRequestChangesMapsToSubmitted(t *testing.T) {
	raw := map[string]any{"values": map[string]any{"x": "y"}}
	out := parseApprovalResult("request_changes", nil, raw)
	if out.Status != "submitted" || out.Values["x"] != "y" {
		t.Errorf("out = %+v", out)
	}
}

func TestParseErrorDecision(t *testing.T) {
	out := parseApprovalResult("error", nil, nil)
	if out.Status != "error" {
		t.Errorf("status = %s", out.Status)
	}
	if out.Error == nil || *out.Error != "agentfield reported decision=error" {
		t.Errorf("error = %v", out.Error)
	}
}

func TestParseUnknownDecision(t *testing.T) {
	out := parseApprovalResult("weird", nil, nil)
	if out.Status != "error" || out.Error == nil || !strings.Contains(*out.Error, "unknown decision") {
		t.Errorf("out = %+v", out)
	}
}

func TestParseFeedbackJSONFallback(t *testing.T) {
	// No values in raw; feedback is a JSON object → parsed into values.
	out := parseApprovalResult("approved", strptr(`{"k":"v"}`), nil)
	if out.Status != "submitted" || out.Values["k"] != "v" {
		t.Errorf("out = %+v", out)
	}
}

// ---------------------------------------------------------------------------
// RequestUserInputAndPause  (maps to test_ask_user.py request_user_input tests)
// ---------------------------------------------------------------------------

func TestRequestUserInputSubmittedPath(t *testing.T) {
	hax, _ := newHaxTestServer(t, "req-1", "https://hax/r/req-1")
	// decision="approved" with the submitted form values under the webhook's
	// parsed response (RawResponse["values"]).
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision:    "approved",
		RawResponse: map[string]any{"values": map[string]any{"x": "yes"}},
	}}
	spec := schemas.AskUserForm{Title: "Question", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{ExpiresInHours: 1})
	if out.Status != "submitted" || out.Values["x"] != "yes" {
		t.Errorf("out = %+v", out)
	}
	// The hax-created request id/url are threaded into the pause options (the
	// SDK derives the callback URL from the agent's public URL, so we assert on
	// the approval-request identity we do control).
	if pauser.lastOpts.ApprovalRequestID != "req-1" || pauser.lastOpts.ApprovalRequestURL != "https://hax/r/req-1" {
		t.Errorf("pause opts got %+v", pauser.lastOpts)
	}
	if pauser.lastOpts.ExpiresInHours != 1 {
		t.Errorf("expires_in_hours = %d, want 1", pauser.lastOpts.ExpiresInHours)
	}
}

func TestRequestUserInputRequestChangesIsSubmitted(t *testing.T) {
	// decision="request_changes" still counts as a filled-out form → submitted,
	// per the decision→status table.
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision:    "request_changes",
		RawResponse: map[string]any{"values": map[string]any{"k": "v"}},
	}}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "k", Type: schemas.FieldTypeInput, Label: "K"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{ExpiresInHours: 1})
	if out.Status != "submitted" || out.Values["k"] != "v" {
		t.Errorf("out = %+v", out)
	}
}

func TestRequestUserInputTimeoutOnExpired(t *testing.T) {
	// A plain expiry is delivered as decision="expired" (NOT an error) and maps
	// to status="timeout".
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{Decision: "expired"}}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{ExpiresInHours: 1})
	if out.Status != "timeout" {
		t.Errorf("status = %s", out.Status)
	}
}

func TestRequestUserInputCancelledOnRejected(t *testing.T) {
	// Feedback now rides on ApprovalResult.Feedback (a top-level field the SDK
	// parses out of the webhook payload), not inside RawResponse.
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision: "rejected",
		Feedback: "user said no",
	}}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{ExpiresInHours: 1})
	if out.Status != "cancelled" {
		t.Errorf("status = %s", out.Status)
	}
	if out.Feedback == nil || *out.Feedback != "user said no" {
		t.Errorf("feedback = %v", out.Feedback)
	}
}

func TestRequestUserInputErrorDecisionIsError(t *testing.T) {
	// decision="error" maps to status="error", surfacing the feedback as the
	// error message.
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision: "error",
		Feedback: "control plane unreachable",
	}}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{ExpiresInHours: 1})
	if out.Status != "error" || out.Error == nil || *out.Error != "control plane unreachable" {
		t.Errorf("out = %+v", out)
	}
}

func TestRequestUserInputPauseDeadlineIsTimeout(t *testing.T) {
	// A context deadline surfaced as a Pause error maps to timeout (mirrors
	// Python's asyncio.TimeoutError branch).
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{err: context.DeadlineExceeded}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{})
	if out.Status != "timeout" {
		t.Errorf("status = %s", out.Status)
	}
}

func TestRequestUserInputPauseErrorIsError(t *testing.T) {
	// A non-deadline Pause error (e.g. control plane could not be notified)
	// surfaces as status="error".
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{err: errors.New("boom")}
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "x", Type: schemas.FieldTypeInput, Label: "X"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{})
	if out.Status != "error" || out.Error == nil || !strings.Contains(*out.Error, "pause failed") {
		t.Errorf("out = %+v", out)
	}
}

func TestRequestUserInputBuildFormErrorIsError(t *testing.T) {
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{}
	// Slider with no min/max → build error → status error, no pause call.
	spec := schemas.AskUserForm{Title: "Q", Fields: []schemas.AskUserFormField{{ID: "s", Type: schemas.FieldTypeSlider, Label: "s"}}}

	out := RequestUserInputAndPause(context.Background(), &silentApp{}, pauser, hax, spec, RequestUserInputParams{})
	if out.Status != "error" || out.Error == nil || !strings.Contains(*out.Error, "Failed to build form") {
		t.Errorf("out = %+v", out)
	}
	if pauser.calls != 0 {
		t.Errorf("pause should not be called on build error")
	}
}

// ---------------------------------------------------------------------------
// hax_client  (maps to test_hax_create_request_timeout.py intent)
// ---------------------------------------------------------------------------

func TestCreateRequestHappyPath(t *testing.T) {
	hax, bodyPtr := newHaxTestServer(t, "id-1", "https://hax/r/id-1")
	created, err := hax.CreateRequest(context.Background(), CreateRequestParams{
		Type:             "form-builder",
		Payload:          map[string]any{"title": "t"},
		Title:            "T",
		ExpiresInSeconds: 3600,
	})
	if err != nil {
		t.Fatal(err)
	}
	if created.ID != "id-1" || created.URL != "https://hax/r/id-1" {
		t.Errorf("created = %+v", created)
	}
	var body map[string]any
	if err := json.Unmarshal(*bodyPtr, &body); err != nil {
		t.Fatal(err)
	}
	if body["type"] != "form-builder" || body["title"] != "T" || body["expiresInSeconds"].(float64) != 3600 {
		t.Errorf("body = %v", body)
	}
	if _, present := body["publicKey"]; present {
		t.Errorf("publicKey must be omitted")
	}
}

func TestCreateRequestTimeoutSurfacesError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_ = json.NewEncoder(w).Encode(map[string]string{"id": "x", "url": "u"})
	}))
	t.Cleanup(srv.Close)
	hax := &HaxClient{BaseURL: srv.URL, APIKey: "k", HTTPClient: srv.Client(), Timeout: 40 * time.Millisecond}

	start := time.Now()
	_, err := hax.CreateRequest(context.Background(), CreateRequestParams{Type: "form-builder", Payload: map[string]any{}})
	elapsed := time.Since(start)
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("want timeout error, got %v", err)
	}
	if elapsed > 1*time.Second {
		t.Errorf("timeout did not fire promptly: %s", elapsed)
	}
}

func TestBuildHaxClientFromEnv(t *testing.T) {
	t.Setenv("HAX_API_KEY", "")
	if BuildHaxClientFromEnv() != nil {
		t.Error("empty HAX_API_KEY must yield nil client (HITL disabled)")
	}
	t.Setenv("HAX_API_KEY", "hax_live_x")
	t.Setenv("HAX_SDK_URL", "")
	c := BuildHaxClientFromEnv()
	if c == nil || c.BaseURL != DefaultHaxBaseURL || c.APIKey != "hax_live_x" {
		t.Errorf("client = %+v", c)
	}
	t.Setenv("HAX_SDK_URL", "https://hax.example.com/")
	c = BuildHaxClientFromEnv()
	if c.BaseURL != "https://hax.example.com" {
		t.Errorf("BaseURL = %q (trailing slash not trimmed)", c.BaseURL)
	}
}

func TestApprovalWebhookURL(t *testing.T) {
	if got := ApprovalWebhookURL("http://cp:8080"); got != "http://cp:8080/api/v1/webhooks/approval-response" {
		t.Errorf("got %q", got)
	}
	if got := ApprovalWebhookURL("http://cp:8080/"); got != "http://cp:8080/api/v1/webhooks/approval-response" {
		t.Errorf("trailing slash: got %q", got)
	}
	if got := ApprovalWebhookURL(""); got != "" {
		t.Errorf("empty server should give empty URL, got %q", got)
	}
}

// ---------------------------------------------------------------------------
// RunWithAskUser  (maps to test_ask_user.py wrapper tests)
// ---------------------------------------------------------------------------

func askForm() map[string]any {
	return map[string]any{
		"title":  "Pick",
		"fields": []any{map[string]any{"id": "x", "type": "input", "label": "X"}},
	}
}

func TestWrapperNoAskPassthrough(t *testing.T) {
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{}
	calls := 0
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) {
		calls++
		return map[string]any{"action": "DONE"}, nil
	}
	out, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{
		App: &silentApp{}, Pauser: pauser, Hax: hax, Budget: &AskUserBudget{Remaining: 3},
	})
	if err != nil {
		t.Fatal(err)
	}
	if out["action"] != "DONE" || calls != 1 || pauser.calls != 0 {
		t.Errorf("out=%v calls=%d reqCalls=%d", out, calls, pauser.calls)
	}
}

func TestWrapperHaxDisabledClearsField(t *testing.T) {
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) {
		return map[string]any{"action": "ASKING", "ask_user_form": askForm()}, nil
	}
	out, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{
		App: &silentApp{}, Hax: nil, Budget: &AskUserBudget{Remaining: 5},
	})
	if err != nil {
		t.Fatal(err)
	}
	if out["ask_user_form"] != nil {
		t.Errorf("ask_user_form should be cleared, got %v", out["ask_user_form"])
	}
}

func TestWrapperBudgetExhaustedClearsField(t *testing.T) {
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{}
	budget := &AskUserBudget{Remaining: 0}
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) {
		return map[string]any{"action": "ASKING", "ask_user_form": askForm()}, nil
	}
	out, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{
		App: &silentApp{}, Pauser: pauser, Hax: hax, Budget: budget,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out["ask_user_form"] != nil || pauser.calls != 0 || budget.Remaining != 0 {
		t.Errorf("out=%v reqCalls=%d budget=%d", out, pauser.calls, budget.Remaining)
	}
}

func TestWrapperOneAskRoundThenNoAsk(t *testing.T) {
	hax, _ := newHaxTestServer(t, "req-99", "https://hax/r/req-99")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision:    "approved",
		RawResponse: map[string]any{"values": map[string]any{"x": "answer"}},
	}}
	seq := []map[string]any{
		{"action": "ASKING", "ask_user_form": askForm()},
		{"action": "FINAL", "ask_user_form": nil},
	}
	var priorSeenSecondCall []any
	call := 0
	invoke := func(_ context.Context, kwargs map[string]any) (map[string]any, error) {
		if call == 1 {
			priorSeenSecondCall, _ = kwargs["prior_user_responses"].([]any)
		}
		r := seq[call]
		call++
		return r, nil
	}
	budget := &AskUserBudget{Remaining: 5}
	out, err := RunWithAskUser(context.Background(), invoke, map[string]any{"prior_user_responses": []any{}}, RunWithAskUserParams{
		App: &silentApp{}, Pauser: pauser, Hax: hax, Budget: budget,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out["action"] != "FINAL" || out["ask_user_form"] != nil {
		t.Errorf("out = %v", out)
	}
	if call != 2 {
		t.Errorf("invoke called %d times, want 2", call)
	}
	if budget.Remaining != 4 {
		t.Errorf("budget = %d, want 4", budget.Remaining)
	}
	if len(priorSeenSecondCall) != 1 {
		t.Fatalf("second call prior len = %d, want 1", len(priorSeenSecondCall))
	}
	entry := priorSeenSecondCall[0].(map[string]any)
	if entry["question"] != "Pick" || entry["status"] != "submitted" {
		t.Errorf("prior entry = %v", entry)
	}
	vals := entry["values"].(map[string]any)
	if vals["x"] != "answer" {
		t.Errorf("prior values = %v", vals)
	}
}

func TestWrapperBudgetBoundsReinvocationToTwo(t *testing.T) {
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision: "approved", RawResponse: map[string]any{"values": map[string]any{"x": "y"}},
	}}
	budget := &AskUserBudget{Remaining: 2}
	call := 0
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) {
		call++
		return map[string]any{"action": "ASKING", "ask_user_form": askForm()}, nil
	}
	out, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{
		App: &silentApp{}, Pauser: pauser, Hax: hax, Budget: budget, MaxIterations: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	if pauser.calls != 2 {
		t.Errorf("pauses = %d, want 2 (budget bound)", pauser.calls)
	}
	if call != 3 {
		t.Errorf("invoke = %d, want 3 (2 asks + 1 that hits budget=0)", call)
	}
	if out["ask_user_form"] != nil {
		t.Errorf("form should be cleared")
	}
}

func TestWrapperMaxIterationsBound(t *testing.T) {
	hax, _ := newHaxTestServer(t, "r", "u")
	pauser := &fakePauser{result: &agent.ApprovalResult{
		Decision: "approved", RawResponse: map[string]any{"values": map[string]any{"x": "y"}},
	}}
	budget := &AskUserBudget{Remaining: 10}
	call := 0
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) {
		call++
		return map[string]any{"action": "ASKING", "ask_user_form": askForm()}, nil
	}
	_, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{
		App: &silentApp{}, Pauser: pauser, Hax: hax, Budget: budget, MaxIterations: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if pauser.calls != 3 {
		t.Errorf("pauses = %d, want 3 (max_iterations bound)", pauser.calls)
	}
	if call != 4 {
		t.Errorf("invoke = %d, want 4", call)
	}
	if budget.Remaining != 7 {
		t.Errorf("budget = %d, want 7", budget.Remaining)
	}
}

func TestWrapperInvokeErrorPropagates(t *testing.T) {
	sentinel := errors.New("boom")
	invoke := func(_ context.Context, _ map[string]any) (map[string]any, error) { return nil, sentinel }
	_, err := RunWithAskUser(context.Background(), invoke, map[string]any{}, RunWithAskUserParams{App: &silentApp{}})
	if !errors.Is(err, sentinel) {
		t.Errorf("err = %v, want sentinel", err)
	}
}

// ---------------------------------------------------------------------------
// services  (maps to test_environment_scout.py detection tests)
// ---------------------------------------------------------------------------

func TestDetectServicesEmptyForMissingPath(t *testing.T) {
	if len(DetectServicesFromRepo("")) != 0 {
		t.Error("empty path should detect nothing")
	}
	if len(DetectServicesFromRepo("/this/path/does/not/exist")) != 0 {
		t.Error("missing path should detect nothing")
	}
}

func TestDetectServicesFindsRailwayAndSentry(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "railway.toml"), []byte("[deploy]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sentry.properties"), []byte("dsn=foo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	names := serviceNames(DetectServicesFromRepo(dir))
	if !names["Railway"] || !names["Sentry"] {
		t.Errorf("names = %v", names)
	}
}

func TestDetectServicesSignalCanBeDirectory(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "supabase", "migrations"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !serviceNames(DetectServicesFromRepo(dir))["Supabase"] {
		t.Error("supabase/migrations directory should be a signal hit")
	}
}

func TestKnownServiceSummaryIsMarkdownBullets(t *testing.T) {
	out := KnownServiceSummaryForPrompt(schemas.KnownServices[:2])
	if !strings.HasPrefix(out, "- **") || !strings.Contains(out, "env `") || !strings.Contains(out, "mint at ") {
		t.Errorf("summary = %q", out)
	}
}

func TestKnownServiceSummaryNoStaticSignal(t *testing.T) {
	// OpenAI has no signal_files → "(no static signal)".
	out := KnownServiceSummaryForPrompt([]schemas.ServiceCredentialSpec{{ServiceName: "OpenAI", EnvVarName: "OPENAI_API_KEY", MintURL: "https://x", PermissionsHint: "h"}})
	if !strings.Contains(out, "(no static signal)") {
		t.Errorf("summary = %q", out)
	}
}

func serviceNames(specs []schemas.ServiceCredentialSpec) map[string]bool {
	out := map[string]bool{}
	for _, s := range specs {
		out[s.ServiceName] = true
	}
	return out
}

// ---------------------------------------------------------------------------
// scout helpers
// ---------------------------------------------------------------------------

func TestBuildScoutFormNilWhenNoServices(t *testing.T) {
	if BuildScoutForm(nil) != nil {
		t.Error("no detected services → nil form")
	}
}

func TestBuildScoutFormOneFieldPerService(t *testing.T) {
	form := BuildScoutForm([]schemas.ServiceCredentialSpec{
		{ServiceName: "Railway", EnvVarName: "RAILWAY_TOKEN", MintURL: "https://x", PermissionsHint: "h"},
		{ServiceName: "Sentry", EnvVarName: "SENTRY_AUTH_TOKEN", MintURL: "https://y", PermissionsHint: "h2"},
	})
	if form == nil || len(form.Fields) != 2 {
		t.Fatalf("form = %+v", form)
	}
	if form.Fields[0].ID != "RAILWAY_TOKEN" || form.Fields[0].Type != schemas.FieldTypeInput || form.Fields[0].Required {
		t.Errorf("field0 = %+v", form.Fields[0])
	}
}

func TestScopedCredentialsFromPriorResponses(t *testing.T) {
	prior := []map[string]any{
		{"values": map[string]any{"OLD": "stale"}},
		{"values": map[string]any{"RAILWAY_TOKEN": "rt_xxx", "BLANK": "", "NUM": 5}},
	}
	got := ScopedCredentialsFromPriorResponses(prior)
	if len(got) != 1 || got["RAILWAY_TOKEN"] != "rt_xxx" {
		t.Errorf("got = %v (only last-response non-blank strings expected)", got)
	}
}

func TestScopedCredentialsEmpty(t *testing.T) {
	if len(ScopedCredentialsFromPriorResponses(nil)) != 0 {
		t.Error("nil prior → empty creds")
	}
}

// Ensure the real SDK agent satisfies Pauser (compile-time guard).
var _ Pauser = (*agent.Agent)(nil)

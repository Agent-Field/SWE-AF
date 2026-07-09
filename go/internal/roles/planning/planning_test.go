package planning

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// --- fakes ------------------------------------------------------------------

// fakeHarness is the HarnessCaller seam (the Python tests get it by patching
// router.harness). fn receives the 1-based call index, the prompt, the *T dest
// to populate, and the resolved options.
type fakeHarness struct {
	calls      int
	lastPrompt string
	lastOpts   harness.Options
	prompts    []string
	fn         func(call int, prompt string, dest any, opts harness.Options) (*harness.Result, error)
}

func (f *fakeHarness) Harness(_ context.Context, prompt string, _ map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	f.calls++
	f.lastPrompt = prompt
	f.lastOpts = opts
	f.prompts = append(f.prompts, prompt)
	return f.fn(f.calls, prompt, dest, opts)
}

// recNote records notes so tests can assert tags/messages.
type recNote struct {
	msgs []string
	tags [][]string
}

func (r *recNote) Note(_ context.Context, message string, tags ...string) {
	r.msgs = append(r.msgs, message)
	r.tags = append(r.tags, tags)
}

// fakePauser returns a scripted ApprovalResult (used for the ask-user loop).
type fakePauser struct {
	result *agent.ApprovalResult
}

func (f *fakePauser) Pause(_ context.Context, _ agent.PauseOptions) (*agent.ApprovalResult, error) {
	return f.result, nil
}

// haxTestServer returns a *hitl.HaxClient whose CreateRequest hits an httptest
// server that always returns {id,url}, so the ask-user pause can proceed.
func haxTestServer(t *testing.T) (*hitl.HaxClient, func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "req-1", "url": "https://hax.test/req-1"})
	}))
	return &hitl.HaxClient{BaseURL: srv.URL, APIKey: "test-key"}, srv.Close
}

// newDeps builds Deps with a recording note channel and no HITL (Hax nil).
func newDeps(h *fakeHarness) (*Deps, *recNote) {
	notes := &recNote{}
	return &Deps{Harness: h, App: notes, NodeID: "swe-planner"}, notes
}

func keys(m map[string]any) map[string]bool {
	out := map[string]bool{}
	for k := range m {
		out[k] = true
	}
	return out
}

func assertKeys(t *testing.T, got map[string]any, want ...string) {
	t.Helper()
	k := keys(got)
	if len(k) != len(want) {
		t.Fatalf("key count mismatch: got %v, want %v", sortedSet(k), want)
	}
	for _, w := range want {
		if !k[w] {
			t.Fatalf("missing key %q; got %v", w, sortedSet(k))
		}
	}
}

func sortedSet(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// --- run_product_manager ----------------------------------------------------

// Contract: on success PM returns a PRD model_dump (the full PRD key set).
func TestProductManagerSuccessKeys(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		p := dest.(*schemas.PRD)
		p.ValidatedDescription = "do the thing"
		p.MustHave = []string{"a"}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, notes := newDeps(h)

	out, err := RunProductManager(context.Background(), deps, map[string]any{
		"goal": "do the thing", "repo_path": t.TempDir(),
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := out.(map[string]any)
	assertKeys(t, m, "validated_description", "acceptance_criteria", "must_have",
		"nice_to_have", "out_of_scope", "assumptions", "risks", "ask_user_form")
	if m["validated_description"] != "do the thing" {
		t.Fatalf("unexpected validated_description: %v", m["validated_description"])
	}
	// note discipline: PM starting + PM complete
	if len(notes.msgs) < 2 || notes.msgs[0] != "PM starting" || notes.msgs[len(notes.msgs)-1] != "PM complete" {
		t.Fatalf("expected PM start/complete notes, got %v", notes.msgs)
	}
}

// Contract: default tool list + adapter provider are passed to the harness.
func TestProductManagerHarnessOptions(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	if _, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": t.TempDir()}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.lastOpts.Provider != "claude-code" {
		t.Fatalf("expected adapter provider claude-code, got %q", h.lastOpts.Provider)
	}
	if strings.Join(h.lastOpts.Tools, ",") != "Read,Write,Glob,Grep,Bash" {
		t.Fatalf("unexpected tools: %v", h.lastOpts.Tools)
	}
	if h.lastOpts.Model != "sonnet" || h.lastOpts.MaxTurns != 150 {
		t.Fatalf("unexpected model/max_turns: %q/%d", h.lastOpts.Model, h.lastOpts.MaxTurns)
	}
}

// Contract: parse failure raises (not a fallback).
func TestProductManagerParseFailureRaises(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "schema validation failed", Parsed: nil}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "Product manager failed to produce a valid PRD") {
		t.Fatalf("expected PRD failure error, got %v", err)
	}
}

// Contract: a fatal harness error propagates as *FatalHarnessError.
func TestProductManagerFatalPropagates(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "Credit balance is too low"}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	var fe *fatal.FatalHarnessError
	if !errors.As(err, &fe) {
		t.Fatalf("expected *fatal.FatalHarnessError, got %T: %v", err, err)
	}
}

// Contract: HITL disabled (Hax nil) — an emitted ask_user_form is stripped and
// the current decision proceeds (single harness call, no re-invocation).
func TestProductManagerAskUserStrippedWhenHaxDisabled(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		p := dest.(*schemas.PRD)
		p.ValidatedDescription = "needs input"
		p.AskUserForm = &schemas.AskUserForm{Title: "clarify", SubmitLabel: "Submit"}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h) // Hax nil
	out, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.calls != 1 {
		t.Fatalf("expected exactly 1 harness call when HITL disabled, got %d", h.calls)
	}
	if m := out.(map[string]any); m["ask_user_form"] != nil {
		t.Fatalf("expected ask_user_form stripped to nil, got %v", m["ask_user_form"])
	}
}

// Contract: HITL-wrapped roles re-invoke on ask_user_form (bounded by budget 2).
// First call emits a form; after the user answers, the second call returns a
// clean PRD → exactly 2 harness calls.
func TestProductManagerReinvokesOnAskUserForm(t *testing.T) {
	h := &fakeHarness{fn: func(call int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		p := dest.(*schemas.PRD)
		if call == 1 {
			p.AskUserForm = &schemas.AskUserForm{Title: "clarify", SubmitLabel: "Submit"}
		} else {
			p.ValidatedDescription = "resolved"
		}
		return &harness.Result{Parsed: dest}, nil
	}}
	hax, closeSrv := haxTestServer(t)
	defer closeSrv()
	deps, _ := newDeps(h)
	deps.Hax = hax
	deps.Pauser = &fakePauser{result: &agent.ApprovalResult{
		Decision:    "approved",
		RawResponse: map[string]any{"values": map[string]any{"answer": "yes"}},
	}}

	out, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 harness calls (initial + re-invoke), got %d", h.calls)
	}
	m := out.(map[string]any)
	if m["validated_description"] != "resolved" || m["ask_user_form"] != nil {
		t.Fatalf("expected resolved PRD with cleared form, got %v", m)
	}
	// The re-invoked prompt must surface the prior response so the LLM does not re-ask.
	if !strings.Contains(h.prompts[1], "Prior Clarification From User") {
		t.Fatalf("expected prior-response block in re-invoked prompt")
	}
}

// --- run_environment_scout --------------------------------------------------

// Contract: scout return EXCLUDES scoped_credentials, and stores them in the
// process-local store keyed by the execution run_id.
func TestScoutExcludesAndStoresCredentials(t *testing.T) {
	const runID = "scout-run-1"
	restore := executionContextFrom
	executionContextFrom = func(context.Context) agent.ExecutionContext {
		return agent.ExecutionContext{RunID: runID}
	}
	defer func() { executionContextFrom = restore }()
	defer hitl.ClearScopedCredentials(runID)

	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		s := dest.(*schemas.ScoutResult)
		s.Summary = "found railway"
		s.ScopedCredentials = map[string]string{"RAILWAY_TOKEN": "tok-123"}
		s.SkippedServices = []string{"stripe"}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, notes := newDeps(h)

	out, err := RunEnvironmentScout(context.Background(), deps, map[string]any{
		"prd": map[string]any{"validated_description": "deploy it"}, "repo_path": t.TempDir(),
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := out.(map[string]any)
	if _, present := m["scoped_credentials"]; present {
		t.Fatalf("scoped_credentials MUST be excluded from the return, got %v", m)
	}
	assertKeys(t, m, "detected_services", "skipped_services", "summary", "ask_user_form")

	stored := hitl.GetScopedCredentials(runID)
	if stored["RAILWAY_TOKEN"] != "tok-123" {
		t.Fatalf("expected credential stashed under run_id, got %v", stored)
	}
	// complete note reports counts
	last := notes.msgs[len(notes.msgs)-1]
	if !strings.Contains(last, "1 credential(s) negotiated, 1 skipped") {
		t.Fatalf("unexpected scout complete note: %q", last)
	}
}

// Contract: parse failure returns the deterministic fallback (NOT an error), and
// the fallback still excludes scoped_credentials.
func TestScoutFallbackOnParseFailure(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "unparseable", Parsed: nil}, nil
	}}
	deps, notes := newDeps(h)
	out, err := RunEnvironmentScout(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err != nil {
		t.Fatalf("expected fallback, not error: %v", err)
	}
	m := out.(map[string]any)
	if _, present := m["scoped_credentials"]; present {
		t.Fatalf("fallback must exclude scoped_credentials, got %v", m)
	}
	if !strings.Contains(m["summary"].(string), "proceeding without credentials") {
		t.Fatalf("unexpected fallback summary: %v", m["summary"])
	}
	joined := strings.Join(notes.tags[len(notes.tags)-1], ",")
	if joined != "scout,fallback" {
		t.Fatalf("expected scout,fallback tags, got %q", joined)
	}
}

// Contract: a fatal harness error propagates (not swallowed by the fallback).
func TestScoutFatalPropagates(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "Credit balance is too low"}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunEnvironmentScout(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	var fe *fatal.FatalHarnessError
	if !errors.As(err, &fe) {
		t.Fatalf("expected fatal error to propagate, got %T: %v", err, err)
	}
}

// --- run_architect ----------------------------------------------------------

// Contract: architect returns an Architecture model_dump on success.
func TestArchitectSuccessKeys(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		a := dest.(*schemas.Architecture)
		a.Summary = "layered"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	out, err := RunArchitect(context.Background(), deps, map[string]any{
		"prd": map[string]any{"validated_description": "x"}, "repo_path": t.TempDir(),
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	assertKeys(t, out.(map[string]any), "summary", "components", "interfaces",
		"decisions", "file_changes_overview")
}

// Contract: when feedback is given it is included in the (task) prompt.
func TestArchitectIncludesFeedback(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunArchitect(context.Background(), deps, map[string]any{
		"prd":       map[string]any{"validated_description": "x"},
		"repo_path": t.TempDir(),
		"feedback":  "TIGHTEN_THE_BOUNDS_PLEASE",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(h.lastOpts.SystemPrompt, "TIGHTEN_THE_BOUNDS_PLEASE") &&
		!strings.Contains(h.lastPrompt, "TIGHTEN_THE_BOUNDS_PLEASE") {
		t.Fatalf("expected feedback threaded into the architect prompt")
	}
}

// Contract: parse failure raises.
func TestArchitectParseFailureRaises(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunArchitect(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "Architect failed to produce a valid architecture") {
		t.Fatalf("expected architect failure error, got %v", err)
	}
}

// --- run_tech_lead ----------------------------------------------------------

// Contract: tech_lead returns a ReviewResult AND writes plan/review.json.
func TestTechLeadWritesReviewJSON(t *testing.T) {
	repo := t.TempDir()
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		r := dest.(*schemas.ReviewResult)
		r.Approved = true
		r.Summary = "looks good"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	out, err := RunTechLead(context.Background(), deps, map[string]any{"repo_path": repo})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	assertKeys(t, out.(map[string]any), "approved", "feedback", "scope_issues",
		"complexity_assessment", "summary")

	reviewPath := filepath.Join(repo, ".artifacts", "plan", "review.json")
	blob, err := os.ReadFile(reviewPath)
	if err != nil {
		t.Fatalf("expected review.json written at %s: %v", reviewPath, err)
	}
	var onDisk map[string]any
	if err := json.Unmarshal(blob, &onDisk); err != nil {
		t.Fatalf("review.json not valid JSON: %v", err)
	}
	if onDisk["approved"] != true || onDisk["summary"] != "looks good" {
		t.Fatalf("review.json content mismatch: %v", onDisk)
	}
}

// Contract: parse failure raises.
func TestTechLeadParseFailureRaises(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunTechLead(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "Tech lead failed to produce a valid review") {
		t.Fatalf("expected tech lead failure error, got %v", err)
	}
}

// --- run_sprint_planner -----------------------------------------------------

// Contract: sprint planner returns exactly {issues, rationale}; issues is a list
// of PlannedIssue model_dumps.
func TestSprintPlannerSuccess(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		s := dest.(*sprintPlanOutput)
		s.Rationale = "split by layer"
		s.Issues = []schemas.PlannedIssue{{Name: "issue-a", Title: "A"}, {Name: "issue-b", Title: "B"}}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	out, err := RunSprintPlanner(context.Background(), deps, map[string]any{
		"prd":          map[string]any{"validated_description": "x"},
		"architecture": map[string]any{"summary": "y"},
		"repo_path":    t.TempDir(),
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := out.(map[string]any)
	assertKeys(t, m, "issues", "rationale")
	if m["rationale"] != "split by layer" {
		t.Fatalf("unexpected rationale: %v", m["rationale"])
	}
	issues := m["issues"].([]any)
	if len(issues) != 2 {
		t.Fatalf("expected 2 issues, got %d", len(issues))
	}
	first := issues[0].(map[string]any)
	if first["name"] != "issue-a" || first["title"] != "A" {
		t.Fatalf("issue model_dump mismatch: %v", first)
	}
	// full PlannedIssue key set surfaces (no omitempty on the schema struct).
	assertKeys(t, first, "name", "title", "description", "acceptance_criteria",
		"depends_on", "provides", "estimated_complexity", "files_to_create",
		"files_to_modify", "testing_strategy", "sequence_number", "guidance", "target_repo")
}

// Contract: parse failure raises.
func TestSprintPlannerParseFailureRaises(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "Sprint planner failed to produce valid issues") {
		t.Fatalf("expected sprint planner failure error, got %v", err)
	}
}

// --- registration surface ---------------------------------------------------

// Contract: Handlers() exposes the five roles under their exact Python names.
func TestHandlersRegistrationSurface(t *testing.T) {
	got := Handlers()
	for _, name := range []string{
		"run_product_manager", "run_environment_scout", "run_architect",
		"run_tech_lead", "run_sprint_planner",
	} {
		if got[name] == nil {
			t.Fatalf("missing handler registration for %q", name)
		}
	}
	if len(got) != 5 {
		t.Fatalf("expected exactly 5 handlers, got %d", len(got))
	}
}

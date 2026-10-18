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

// badSchemaResult builds a harness result that produced raw text which failed
// schema validation.
func badSchemaResult(raw string) *harness.Result {
	return &harness.Result{
		IsError:      true,
		Parsed:       nil,
		Result:       raw,
		FailureType:  harness.FailureSchema,
		ErrorMessage: "Schema validation failed after retries.",
	}
}

// assertRetryLog checks that an append-only stage retry log carries a run
// header and contains the wanted snippets and its terminal outcome line.
func assertRetryLog(t *testing.T, path string, want []string, outcome string) {
	t.Helper()
	blob, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("expected retry log at %s: %v", path, err)
	}
	log := string(blob)
	if !strings.Contains(log, "===== run ") || !strings.Contains(log, " | started ") {
		t.Fatalf("retry log missing run header:\n%s", log)
	}
	for _, snippet := range want {
		if !strings.Contains(log, snippet) {
			t.Fatalf("retry log missing %q:\n%s", snippet, log)
		}
	}
	if !strings.Contains(log, outcome) {
		t.Fatalf("retry log missing outcome %q:\n%s", outcome, log)
	}
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

func TestProductManagerDirectCallRuntimeDefaults(t *testing.T) {
	clearRuntimeEnv := func(t *testing.T) {
		t.Helper()
		for _, key := range []string{"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "SWE_DEFAULT_RUNTIME", "SWE_MODEL_HIGH", "SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"} {
			t.Setenv(key, "")
		}
	}
	run := func(t *testing.T, input map[string]any) harness.Options {
		t.Helper()
		h := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
			return &harness.Result{Parsed: dest}, nil
		}}
		deps, _ := newDeps(h)
		input["repo_path"] = t.TempDir()
		if _, err := RunProductManager(context.Background(), deps, input); err != nil {
			t.Fatalf("RunProductManager: %v", err)
		}
		return h.lastOpts
	}

	t.Run("OpenRouter only", func(t *testing.T) {
		clearRuntimeEnv(t)
		t.Setenv("OPENROUTER_API_KEY", "test-key")
		opts := run(t, map[string]any{})
		if opts.Provider != "opencode" || opts.Model != "openrouter/deepseek/deepseek-v4-flash-0731" {
			t.Fatalf("defaults = provider %q, model %q", opts.Provider, opts.Model)
		}
	})
	t.Run("configured codex", func(t *testing.T) {
		clearRuntimeEnv(t)
		t.Setenv("SWE_DEFAULT_RUNTIME", "codex")
		if got := run(t, map[string]any{}).Provider; got != "codex" {
			t.Fatalf("provider = %q, want codex", got)
		}
	})
	t.Run("explicit values win", func(t *testing.T) {
		clearRuntimeEnv(t)
		t.Setenv("OPENROUTER_API_KEY", "test-key")
		opts := run(t, map[string]any{"ai_provider": "claude", "model": "sonnet"})
		if opts.Provider != "claude-code" || opts.Model != "sonnet" {
			t.Fatalf("explicit = provider %q, model %q", opts.Provider, opts.Model)
		}
	})
}

// Contract: parse failure raises after the bounded retries, naming the stage
// and the failing field, with the raw response and terminal outcome retained.
func TestProductManagerParseFailureRaises(t *testing.T) {
	repo := t.TempDir()
	raw := `{"validated_description": 7}`
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(raw), nil
	}}
	deps, _ := newDeps(h)
	_, err := RunProductManager(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Product manager failed to produce a valid PRD after 2 attempt(s)") {
		t.Fatalf("expected PRD failure error, got %v", err)
	}
	if !strings.Contains(err.Error(), "validated_description") {
		t.Fatalf("expected the failing field named in the error, got %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 attempts (1 retry), got %d", h.calls)
	}
	assertRetryLog(t,
		filepath.Join(repo, ".artifacts", "plan", "product_manager_raw_response.txt"),
		[]string{raw, "attempt 1/2 failed"},
		"outcome: FAILED after 2 attempt(s)")
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

// Contract: parse failure raises after the bounded retries, naming the stage
// and the failing field, with the raw response and terminal outcome retained.
func TestArchitectParseFailureRaises(t *testing.T) {
	repo := t.TempDir()
	raw := `{"summary": 7}`
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(raw), nil
	}}
	deps, _ := newDeps(h)
	_, err := RunArchitect(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Architect failed to produce a valid architecture after 2 attempt(s)") {
		t.Fatalf("expected architect failure error, got %v", err)
	}
	if !strings.Contains(err.Error(), "summary") {
		t.Fatalf("expected the failing field named in the error, got %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 attempts (1 retry), got %d", h.calls)
	}
	assertRetryLog(t,
		filepath.Join(repo, ".artifacts", "plan", "architect_raw_response.txt"),
		[]string{raw, "attempt 1/2 failed"},
		"outcome: FAILED after 2 attempt(s)")
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

// Contract: parse failure raises after the bounded retries, naming the stage
// and the failing field, with the raw response and terminal outcome retained.
func TestTechLeadParseFailureRaises(t *testing.T) {
	repo := t.TempDir()
	raw := `{"approved": 3, "feedback": "ok", "summary": "x"}`
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(raw), nil
	}}
	deps, _ := newDeps(h)
	_, err := RunTechLead(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Tech lead failed to produce a valid review after 2 attempt(s)") {
		t.Fatalf("expected tech lead failure error, got %v", err)
	}
	if !strings.Contains(err.Error(), "approved") {
		t.Fatalf("expected the failing field named in the error, got %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 attempts (1 retry), got %d", h.calls)
	}
	assertRetryLog(t,
		filepath.Join(repo, ".artifacts", "plan", "tech_lead_raw_response.txt"),
		[]string{raw, "attempt 1/2 failed"},
		"outcome: FAILED after 2 attempt(s)")
}

// planningStageCase describes one of the newly covered planning stages
// (PM / architect / tech lead) so its retry behavior can be tested uniformly.
type planningStageCase struct {
	name         string
	artifact     string
	role         string
	badRaw       string
	failingField string
	run          func(deps *Deps, repo string) (any, error)
	populate     func(dest any)
}

func newPlanningStageCases() []planningStageCase {
	return []planningStageCase{
		{
			name:         "product_manager",
			artifact:     "product_manager_raw_response.txt",
			role:         "PM",
			badRaw:       `{"validated_description": 7}`,
			failingField: "validated_description",
			run: func(deps *Deps, repo string) (any, error) {
				return RunProductManager(context.Background(), deps, map[string]any{
					"goal": "build a fixture", "repo_path": repo,
				})
			},
			populate: func(dest any) {
				dest.(*schemas.PRD).ValidatedDescription = "build a fixture"
			},
		},
		{
			name:         "architect",
			artifact:     "architect_raw_response.txt",
			role:         "Architect",
			badRaw:       `{"summary": 7}`,
			failingField: "summary",
			run: func(deps *Deps, repo string) (any, error) {
				return RunArchitect(context.Background(), deps, map[string]any{
					"prd":       map[string]any{"validated_description": "x"},
					"repo_path": repo,
				})
			},
			populate: func(dest any) {
				dest.(*schemas.Architecture).Summary = "one component"
			},
		},
		{
			name:         "tech_lead",
			artifact:     "tech_lead_raw_response.txt",
			role:         "Tech lead",
			badRaw:       `{"approved": 3, "feedback": "ok", "summary": "x"}`,
			failingField: "approved",
			run: func(deps *Deps, repo string) (any, error) {
				return RunTechLead(context.Background(), deps, map[string]any{
					"prd":       map[string]any{"validated_description": "x"},
					"repo_path": repo,
				})
			},
			populate: func(dest any) {
				dest.(*schemas.ReviewResult).Approved = true
			},
		},
	}
}

// Contract: each newly covered planning stage retries a schema-invalid
// response with the validation error fed back, then records its recovery.
func TestPlanningStagesRetryWithValidationError(t *testing.T) {
	for _, tc := range newPlanningStageCases() {
		t.Run(tc.name, func(t *testing.T) {
			repo := t.TempDir()
			h := &fakeHarness{fn: func(call int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
				if call == 1 {
					return badSchemaResult(tc.badRaw), nil
				}
				tc.populate(dest)
				return &harness.Result{Parsed: dest}, nil
			}}
			deps, _ := newDeps(h)
			if _, err := tc.run(deps, repo); err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if h.calls != 2 {
				t.Fatalf("expected 2 harness calls, got %d", h.calls)
			}
			if !strings.Contains(h.prompts[1], "Retry Context") || !strings.Contains(h.prompts[1], tc.failingField) {
				t.Fatalf("expected validation error fed back into retry prompt, got %q", h.prompts[1])
			}
			assertRetryLog(t,
				filepath.Join(repo, ".artifacts", "plan", tc.artifact),
				[]string{tc.badRaw, "attempt 1/2 failed"},
				"outcome: succeeded on attempt 2/2")
		})
	}
}

// Contract: each newly covered planning stage fails fast on an empty
// completion instead of burning its retry bound, and the log still ends with a
// terminal outcome line.
func TestPlanningStagesEmptyCompletionFailsFast(t *testing.T) {
	for _, tc := range newPlanningStageCases() {
		t.Run(tc.name, func(t *testing.T) {
			repo := t.TempDir()
			h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
				return &harness.Result{IsError: true, Parsed: nil}, nil
			}}
			deps, _ := newDeps(h)
			_, err := tc.run(deps, repo)
			if err == nil || !strings.Contains(err.Error(), tc.role+" harness returned an empty completion") {
				t.Fatalf("expected empty-completion error naming %s, got %v", tc.role, err)
			}
			if h.calls != 1 {
				t.Fatalf("empty completion must not be retried, got %d calls", h.calls)
			}
			log := readRetryLog(t, repo, tc.artifact)
			if !strings.Contains(log, "outcome: FAILED after attempt 1/2:") {
				t.Fatalf("empty completion must record a terminal outcome:\n%s", log)
			}
		})
	}
}

// Contract: a fatal API error arriving on a retry records a terminal outcome
// before it propagates, so the log never ends on "attempt 1/N failed".
func TestPlanningStagesFatalOnRetryRecordsOutcome(t *testing.T) {
	for _, tc := range newPlanningStageCases() {
		t.Run(tc.name, func(t *testing.T) {
			repo := t.TempDir()
			h := &fakeHarness{fn: func(call int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
				if call == 1 {
					return badSchemaResult(tc.badRaw), nil
				}
				return &harness.Result{
					IsError:      true,
					Parsed:       nil,
					ErrorMessage: "Credit balance is too low. Add funds.",
				}, nil
			}}
			deps, _ := newDeps(h)
			_, err := tc.run(deps, repo)
			if err == nil || !strings.Contains(err.Error(), "Fatal API error") {
				t.Fatalf("expected fatal harness error, got %v", err)
			}
			if h.calls != 2 {
				t.Fatalf("expected 2 harness calls, got %d", h.calls)
			}
			log := readRetryLog(t, repo, tc.artifact)
			if !strings.Contains(log, "attempt 1/2 failed") {
				t.Fatalf("failed first attempt not retained:\n%s", log)
			}
			lastLine := lastLogLine(log)
			if !strings.HasPrefix(lastLine, "===== outcome: FAILED after attempt 2/2:") {
				t.Fatalf("expected terminal outcome as the last line, got %q", lastLine)
			}
			if !strings.Contains(strings.ToLower(lastLine), "credit balance is too low") {
				t.Fatalf("outcome must carry the fatal reason, got %q", lastLine)
			}
		})
	}
}

// Contract: an empty completion arriving on a retry gets the same terminal
// outcome treatment as any other early exit.
func TestPlanningStagesEmptyOnRetryRecordsOutcome(t *testing.T) {
	for _, tc := range newPlanningStageCases() {
		t.Run(tc.name, func(t *testing.T) {
			repo := t.TempDir()
			h := &fakeHarness{fn: func(call int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
				if call == 1 {
					return badSchemaResult(tc.badRaw), nil
				}
				return &harness.Result{IsError: true, Parsed: nil}, nil
			}}
			deps, _ := newDeps(h)
			_, err := tc.run(deps, repo)
			if err == nil || !strings.Contains(err.Error(), "empty completion") {
				t.Fatalf("expected empty-completion error, got %v", err)
			}
			if h.calls != 2 {
				t.Fatalf("expected 2 harness calls, got %d", h.calls)
			}
			lastLine := lastLogLine(readRetryLog(t, repo, tc.artifact))
			if !strings.HasPrefix(lastLine, "===== outcome: FAILED after attempt 2/2:") || !strings.Contains(lastLine, "empty completion") {
				t.Fatalf("expected empty-completion outcome as the last line, got %q", lastLine)
			}
		})
	}
}

// Contract: a second build against the same repo path appends a new run
// section whose outcome is the last line, not the first build's FAILED outcome.
func TestRetryLogSeparatesBuilds(t *testing.T) {
	repo := t.TempDir()
	rawPath := filepath.Join(repo, ".artifacts", "plan", "sprint_planner_raw_response.txt")

	first := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(`{"issues": "not-a-list", "rationale": 7}`), nil
	}}
	deps1, _ := newDeps(first)
	if _, err := RunSprintPlanner(context.Background(), deps1, map[string]any{
		"prd":          map[string]any{"validated_description": "x"},
		"architecture": map[string]any{"summary": "y"},
		"repo_path":    repo,
	}); err == nil {
		t.Fatalf("expected build 1 to exhaust its bound")
	}

	second := &fakeHarness{fn: func(_ int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		s := dest.(*sprintPlanOutput)
		s.Rationale = "recovered"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps2, _ := newDeps(second)
	if _, err := RunSprintPlanner(context.Background(), deps2, map[string]any{
		"prd":          map[string]any{"validated_description": "x"},
		"architecture": map[string]any{"summary": "y"},
		"repo_path":    repo,
	}); err != nil {
		t.Fatalf("build 2 should succeed: %v", err)
	}

	blob, err := os.ReadFile(rawPath)
	if err != nil {
		t.Fatalf("expected retry log at %s: %v", rawPath, err)
	}
	log := string(blob)
	if !strings.Contains(log, "outcome: FAILED after 3 attempt(s)") {
		t.Fatalf("build 1's outcome must be retained:\n%s", log)
	}
	if strings.Count(log, "===== run ") != 2 {
		t.Fatalf("expected one run header per build:\n%s", log)
	}
	if lastLine := lastLogLine(log); lastLine != "===== outcome: succeeded on attempt 1/3 =====" {
		t.Fatalf("expected build 2's outcome as the last line, got %q", lastLine)
	}
}

// Contract: run-scoped credentials echoed into a response cannot land in the
// archived retry log or the raised error.
func TestScopedCredentialsRedactedFromRetryLog(t *testing.T) {
	repo := t.TempDir()
	oldContext := executionContextFrom
	executionContextFrom = func(context.Context) agent.ExecutionContext {
		return agent.ExecutionContext{RunID: "run-redact"}
	}
	defer func() { executionContextFrom = oldContext }()

	secret := "deploy-token-9f3a2b7c"
	hitl.StoreScopedCredentials("run-redact", map[string]string{"DEPLOY_TOKEN": secret})
	defer hitl.ClearScopedCredentials("run-redact")

	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(`{"issues": "` + secret + `", "rationale": 7}`), nil
	}}
	deps, _ := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil {
		t.Fatalf("expected the schema failure to surface")
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatalf("secret leaked into the raised error: %v", err)
	}
	log := readRetryLog(t, repo, "sprint_planner_raw_response.txt")
	if strings.Contains(log, secret) {
		t.Fatalf("secret leaked into the retry log:\n%s", log)
	}
	if !strings.Contains(log, "[REDACTED:DEPLOY_TOKEN]") {
		t.Fatalf("expected the redaction marker in the log:\n%s", log)
	}
}

// Contract: when RunID is empty the scout stores under RootWorkflowID, and the
// retry-log redactor looks the credential up under that same root scope rather
// than an empty key.
func TestScopedCredentialsRedactedWithRootWorkflowScope(t *testing.T) {
	repo := t.TempDir()
	const rootID = "root-workflow-redact"
	restore := executionContextFrom
	executionContextFrom = func(context.Context) agent.ExecutionContext {
		return agent.ExecutionContext{RunID: "", RootWorkflowID: rootID}
	}
	defer func() { executionContextFrom = restore }()

	secret := "deploy-token-9f3a2b7c"
	hitl.StoreScopedCredentials(rootID, map[string]string{"DEPLOY_TOKEN": secret})
	defer hitl.ClearScopedCredentials(rootID)

	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return badSchemaResult(`{"issues": "` + secret + `", "rationale": 7}`), nil
	}}
	deps, _ := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil {
		t.Fatalf("expected the schema failure to surface")
	}

	log := readRetryLog(t, repo, "sprint_planner_raw_response.txt")
	if strings.Contains(log, secret) {
		t.Fatalf("secret leaked into the retry log when scoped by root workflow id:\n%s", log)
	}
	if !strings.Contains(log, "[REDACTED:DEPLOY_TOKEN]") {
		t.Fatalf("expected the redaction marker in the log:\n%s", log)
	}
}

// Contract: a credential containing JSON-special or URL-special characters is
// redacted in its exact, JSON-escaped, and percent-/form-encoded spellings, and
// non-secret text is preserved.
func TestRedactScopedCredentialsCoversEncodedSpellings(t *testing.T) {
	const scopeID = "run-encoded-forms"
	secret := `ab"cd\ef /?:+=`
	hitl.StoreScopedCredentials(scopeID, map[string]string{"DEPLOY_TOKEN": secret})
	defer hitl.ClearScopedCredentials(scopeID)

	jsonBody, err := json.Marshal(secret)
	if err != nil {
		t.Fatalf("marshal secret: %v", err)
	}
	spellings := []struct{ name, form string }{
		{"exact", secret},
		// JSON-escaped body without the surrounding quotes, built by an
		// independent encoder rather than the production helper.
		{"json_escaped", string(jsonBody[1 : len(jsonBody)-1])},
		{"url_percent", percentEncode(secret, false)},
		{"url_plus", percentEncode(secret, true)},
	}
	for _, spelling := range spellings {
		t.Run(spelling.name, func(t *testing.T) {
			got := redactScopedCredentials(scopeID, "echo "+spelling.form+" done")
			if strings.Contains(got, spelling.form) {
				t.Fatalf("spelling %q survived redaction: %q", spelling.form, got)
			}
			if !strings.Contains(got, "[REDACTED:DEPLOY_TOKEN]") {
				t.Fatalf("expected redaction marker for spelling %q, got %q", spelling.form, got)
			}
			if !strings.Contains(got, "echo ") || !strings.Contains(got, " done") {
				t.Fatalf("non-secret text was not preserved: %q", got)
			}
		})
	}
}

func TestEncodedCredentialsRedactedFromRetryLog(t *testing.T) {
	const scopeID = "run-encoded-log"
	oldContext := executionContextFrom
	executionContextFrom = func(context.Context) agent.ExecutionContext {
		return agent.ExecutionContext{RunID: scopeID}
	}
	defer func() { executionContextFrom = oldContext }()

	for _, tc := range []struct{ name, secret, rendered string }{
		{"url_lower", "ab/cd+ef", "ab%2fcd%2bef"},
		{"url_mixed", "Ab/cD+ef", "Ab%2fcD%2Bef"},
		{"form_mixed", "Ab/cD+ ef", "Ab%2FcD%2b+ef"},
		{"json_ascii", `ab"café`, `ab\"caf\u00e9`},
		{"json_ascii_surrogates", "ab\"\\café\x7f😀<&", `ab\"\\caf\u00e9\u007f\ud83d\ude00<&`},
		{"json_go_html", `ab"cd&ef`, ""},
		{"json_go_html_separators", "ab\"\\<>&\u2028\u2029ef", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			repo := t.TempDir()
			rendered := tc.rendered
			if rendered == "" {
				body, err := json.Marshal(tc.secret)
				if err != nil {
					t.Fatal(err)
				}
				rendered = string(body[1 : len(body)-1])
			}
			hitl.StoreScopedCredentials(scopeID, map[string]string{"DEPLOY_TOKEN": tc.secret})
			defer hitl.ClearScopedCredentials(scopeID)
			h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
				return badSchemaResult(`{"issues": "` + rendered + `", "rationale": 7}`), nil
			}}
			deps, _ := newDeps(h)
			if _, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo}); err == nil {
				t.Fatal("expected schema failure")
			}
			log := readRetryLog(t, repo, "sprint_planner_raw_response.txt")
			if strings.Contains(log, tc.secret) || strings.Contains(log, rendered) {
				t.Fatalf("credential survived in persisted log: %s", log)
			}
			if !strings.Contains(log, `"issues": "[REDACTED:DEPLOY_TOKEN]", "rationale": 7`) {
				t.Fatalf("redaction lost surrounding output: %s", log)
			}
		})
	}
}

// Contract: a multi-line fatal reason must not split the terminal outcome over
// several physical lines; the last line stays the flattened outcome.
func TestRetryLogFlattensMultilineFatalReason(t *testing.T) {
	repo := t.TempDir()
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{
			IsError:      true,
			Parsed:       nil,
			ErrorMessage: "Credit balance is too low.\nAdd funds.\nSee the billing page.",
		}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunProductManager(context.Background(), deps, map[string]any{"goal": "x", "repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Fatal API error") {
		t.Fatalf("expected fatal harness error, got %v", err)
	}

	log := readRetryLog(t, repo, "product_manager_raw_response.txt")
	lastLine := lastLogLine(log)
	if !strings.HasPrefix(lastLine, "===== outcome: FAILED after attempt 1/2:") {
		t.Fatalf("expected terminal outcome as the last line, got %q", lastLine)
	}
	if !strings.Contains(lastLine, "Add funds.") || !strings.HasSuffix(lastLine, "=====") {
		t.Fatalf("outcome reason was not flattened onto one line: %q", lastLine)
	}
	for _, line := range strings.Split(log, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "Add funds." || trimmed == "See the billing page." {
			t.Fatalf("reason continuation leaked onto its own physical line:\n%s", log)
		}
	}
}

// Contract: a schema failure whose harness message spans lines keeps each
// attempt header and the terminal outcome on one physical line each.
func TestRetryLogFlattensMultilineSchemaFailure(t *testing.T) {
	repo := t.TempDir()
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		bad := badSchemaResult(`{"issues": "not-a-list", "rationale": 7}`)
		bad.ErrorMessage = "Schema validation failed.\nretry budget gone."
		return bad, nil
	}}
	deps, _ := newDeps(h)
	if _, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo}); err == nil {
		t.Fatalf("expected the schema failure to surface")
	}

	log := readRetryLog(t, repo, "sprint_planner_raw_response.txt")
	attempts := 0
	for _, line := range strings.Split(log, "\n") {
		if strings.HasPrefix(line, "===== attempt ") {
			attempts++
			if !strings.Contains(line, "retry budget gone.") {
				t.Fatalf("attempt header lost the reason: %q", line)
			}
		}
	}
	if attempts != 3 {
		t.Fatalf("expected 3 attempt headers, got %d:\n%s", attempts, log)
	}
	lastLine := lastLogLine(log)
	if !strings.HasPrefix(lastLine, "===== outcome: FAILED after 3 attempt(s):") ||
		!strings.Contains(lastLine, "retry budget gone.") {
		t.Fatalf("expected one flattened terminal outcome line, got %q", lastLine)
	}
	for _, line := range strings.Split(log, "\n") {
		if strings.TrimSpace(line) == "retry budget gone." {
			t.Fatalf("reason continuation leaked onto its own physical line:\n%s", log)
		}
	}
}

// Contract: two invocations with the same scope id in the same second get
// distinguishable run headers — the second-granular timestamp alone is not
// enough.
func TestRetryLogRunHeadersDistinctWithinSameSecond(t *testing.T) {
	repo := t.TempDir()
	path := filepath.Join(repo, "headers.txt")
	deps, _ := newDeps(&fakeHarness{})
	ctx := context.Background()

	recordRunHeaderBestEffort(ctx, deps, "Sprint planner", path, "run-same")
	recordRunHeaderBestEffort(ctx, deps, "Sprint planner", path, "run-same")

	blob, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("expected header log: %v", err)
	}
	var headers []string
	for _, line := range strings.Split(string(blob), "\n") {
		if strings.HasPrefix(line, "===== run ") {
			headers = append(headers, line)
		}
	}
	if len(headers) != 2 {
		t.Fatalf("expected 2 run headers, got %d:\n%s", len(headers), blob)
	}
	if headers[0] == headers[1] {
		t.Fatalf("same-second invocations produced identical headers: %q", headers[0])
	}
	if !strings.Contains(headers[0], "| section ") || !strings.Contains(headers[1], "| section ") {
		t.Fatalf("headers must carry section numbers: %q / %q", headers[0], headers[1])
	}
}

// readRetryLog reads a stage retry log and fails the test when it is missing.
func readRetryLog(t *testing.T, repo, artifact string) string {
	t.Helper()
	blob, err := os.ReadFile(filepath.Join(repo, ".artifacts", "plan", artifact))
	if err != nil {
		t.Fatalf("expected retry log for %s: %v", artifact, err)
	}
	return string(blob)
}

// lastLogLine returns the final non-empty line of a retry log.
func lastLogLine(log string) string {
	lines := strings.Split(strings.TrimRight(log, "\n"), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.TrimSpace(lines[i]) != "" {
			return lines[i]
		}
	}
	return ""
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

// Contract: parse failure raises after the bounded retries (default 2 retries,
// 3 attempts), naming the stage and the failing field, with every failed
// attempt and the terminal outcome retained in the retry log.
func TestSprintPlannerParseFailureRaises(t *testing.T) {
	repo := t.TempDir()
	raw := []string{
		`{"issues": "not-a-list", "rationale": 7}`,
		`{"issues": 42, "rationale": "ok"}`,
		`{"issues": {"nested": true}, "rationale": "ok"}`,
	}
	h := &fakeHarness{fn: func(call int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{
			IsError:      true,
			Parsed:       nil,
			Result:       raw[call-1],
			FailureType:  harness.FailureSchema,
			ErrorMessage: "Schema validation failed after retries.",
		}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Sprint planner failed to produce valid issues after 3 attempt(s)") {
		t.Fatalf("expected sprint planner failure error, got %v", err)
	}
	if !strings.Contains(err.Error(), "issues") {
		t.Fatalf("expected the failing field named in the error, got %v", err)
	}
	if h.calls != 3 {
		t.Fatalf("expected 3 attempts (default 2 retries), got %d", h.calls)
	}
	rawPath := filepath.Join(repo, ".artifacts", "plan", "sprint_planner_raw_response.txt")
	blob, readErr := os.ReadFile(rawPath)
	if readErr != nil {
		t.Fatalf("expected raw responses retained at %s: %v", rawPath, readErr)
	}
	log := string(blob)
	for i, want := range raw {
		if !strings.Contains(log, want) {
			t.Fatalf("attempt %d raw response not retained; log:\n%s", i+1, log)
		}
	}
	if !strings.Contains(log, "outcome: FAILED after 3 attempt(s)") {
		t.Fatalf("expected terminal FAILED outcome in log:\n%s", log)
	}
}

// Contract: a failed attempt is retried with the validation error fed back into
// the prompt, and the final-outcome line records the recovery.
func TestSprintPlannerRetriesWithValidationError(t *testing.T) {
	repo := t.TempDir()
	h := &fakeHarness{fn: func(call int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		if call == 1 {
			return &harness.Result{
				IsError:      true,
				Parsed:       nil,
				Result:       `{"issues": "not-a-list", "rationale": 7}`,
				FailureType:  harness.FailureSchema,
				ErrorMessage: "bad output",
			}, nil
		}
		s := dest.(*sprintPlanOutput)
		s.Rationale = "split by layer"
		s.Issues = []schemas.PlannedIssue{{Name: "issue-a", Title: "A"}}
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, _ := newDeps(h)
	out, err := RunSprintPlanner(context.Background(), deps, map[string]any{
		"prd":          map[string]any{"validated_description": "x"},
		"architecture": map[string]any{"summary": "y"},
		"repo_path":    repo,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 harness calls, got %d", h.calls)
	}
	if !strings.Contains(h.prompts[1], "Retry Context") || !strings.Contains(h.prompts[1], "issues") {
		t.Fatalf("expected validation error fed back into retry prompt, got %q", h.prompts[1])
	}
	m := out.(map[string]any)
	issues := m["issues"].([]any)
	if len(issues) != 1 || issues[0].(map[string]any)["name"] != "issue-a" {
		t.Fatalf("unexpected issues: %v", issues)
	}
	rawPath := filepath.Join(repo, ".artifacts", "plan", "sprint_planner_raw_response.txt")
	blob, readErr := os.ReadFile(rawPath)
	if readErr != nil {
		t.Fatalf("expected retry log at %s: %v", rawPath, readErr)
	}
	log := string(blob)
	if !strings.Contains(log, "attempt 1/3 failed") || !strings.Contains(log, "not-a-list") {
		t.Fatalf("failed attempt not retained in log:\n%s", log)
	}
	if !strings.Contains(log, "outcome: succeeded on attempt 2/3") {
		t.Fatalf("expected recovery outcome in log:\n%s", log)
	}
}

// Contract: a raw-response write failure is noted and never replaces the real
// failure: the stage keeps retrying and still raises the schema error.
func TestSprintPlannerRawResponseWriteFailureStillRaises(t *testing.T) {
	repo := t.TempDir()
	// A directory at the log path makes every append fail.
	rawPath := filepath.Join(repo, ".artifacts", "plan", "sprint_planner_raw_response.txt")
	if err := os.MkdirAll(rawPath, 0o755); err != nil {
		t.Fatalf("mkdir raw path: %v", err)
	}
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{
			IsError:      true,
			Parsed:       nil,
			Result:       `{"issues": "not-a-list", "rationale": 7}`,
			FailureType:  harness.FailureSchema,
			ErrorMessage: "bad output",
		}, nil
	}}
	deps, notes := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": repo})
	if err == nil || !strings.Contains(err.Error(), "Sprint planner failed to produce valid issues after 3 attempt(s)") {
		t.Fatalf("expected schema failure to survive the write error, got %v", err)
	}
	if !strings.Contains(err.Error(), "raw response could not be written") {
		t.Fatalf("expected the write failure named in the error, got %v", err)
	}
	if h.calls != 3 {
		t.Fatalf("expected retries to continue past the write error, got %d calls", h.calls)
	}
	if joined := strings.Join(notes.msgs, "\n"); !strings.Contains(joined, "could not write") {
		t.Fatalf("expected a run note for the write failure, got %v", notes.msgs)
	}
}

// Contract: a raw-response write failure does not abort a run that recovers.
func TestSprintPlannerRawResponseWriteFailureIsNonFatal(t *testing.T) {
	repo := t.TempDir()
	rawPath := filepath.Join(repo, ".artifacts", "plan", "sprint_planner_raw_response.txt")
	if err := os.MkdirAll(rawPath, 0o755); err != nil {
		t.Fatalf("mkdir raw path: %v", err)
	}
	h := &fakeHarness{fn: func(call int, _ string, dest any, _ harness.Options) (*harness.Result, error) {
		if call == 1 {
			return &harness.Result{
				IsError:      true,
				Parsed:       nil,
				Result:       `{"issues": "not-a-list", "rationale": 7}`,
				FailureType:  harness.FailureSchema,
				ErrorMessage: "bad output",
			}, nil
		}
		s := dest.(*sprintPlanOutput)
		s.Rationale = "recovered"
		return &harness.Result{Parsed: dest}, nil
	}}
	deps, notes := newDeps(h)
	out, err := RunSprintPlanner(context.Background(), deps, map[string]any{
		"prd":          map[string]any{"validated_description": "x"},
		"architecture": map[string]any{"summary": "y"},
		"repo_path":    repo,
	})
	if err != nil {
		t.Fatalf("write failure must not abort a recovering run: %v", err)
	}
	if h.calls != 2 {
		t.Fatalf("expected 2 harness calls, got %d", h.calls)
	}
	if out.(map[string]any)["rationale"] != "recovered" {
		t.Fatalf("unexpected result: %v", out)
	}
	if joined := strings.Join(notes.msgs, "\n"); !strings.Contains(joined, "could not write") {
		t.Fatalf("expected a run note for the write failure, got %v", notes.msgs)
	}
}

// Contract: an empty completion (no parsed object, no raw text) is the
// provider/model mismatch shape and fails fast without burning retries.
func TestSprintPlannerEmptyCompletionFailsFast(t *testing.T) {
	h := &fakeHarness{fn: func(_ int, _ string, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	deps, _ := newDeps(h)
	_, err := RunSprintPlanner(context.Background(), deps, map[string]any{"repo_path": t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "empty completion") {
		t.Fatalf("expected empty-completion error, got %v", err)
	}
	if h.calls != 1 {
		t.Fatalf("empty completion must not be retried, got %d calls", h.calls)
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

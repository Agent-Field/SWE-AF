package coding

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/ai"
	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

// recordedNote captures a single note() call for assertions.
type recordedNote struct {
	message string
	tags    []string
}

// noteRecorder is the Noter seam. It records every note so tests can assert the
// verbatim message/tag strings the Python roles emit.
type noteRecorder struct {
	notes []recordedNote
}

func (n *noteRecorder) Note(_ context.Context, message string, tags ...string) {
	n.notes = append(n.notes, recordedNote{message: message, tags: tags})
}

func (n *noteRecorder) hasTag(tag string) bool {
	for _, r := range n.notes {
		for _, t := range r.tags {
			if t == tag {
				return true
			}
		}
	}
	return false
}

// mockHarness is the HarnessCaller seam — the Go equivalent of patching
// router.harness. It scripts the (*harness.Result, error) reply and records the
// options it was called with (so guardrail/cwd/tools can be asserted).
type mockHarness struct {
	fn      func(dest any) (*harness.Result, error)
	gotOpts harness.Options
	called  bool
}

func (m *mockHarness) Harness(_ context.Context, _ string, _ map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	m.called = true
	m.gotOpts = opts
	return m.fn(dest)
}

// mockAI is the AICaller seam for run_qa_synthesizer (Python's router.ai).
type mockAI struct {
	resp *ai.Response
	err  error
	// called records that the direct-LLM path was exercised, so tests can prove
	// run_qa_synthesizer used the AI seam rather than the harness.
	called bool
}

func (m *mockAI) AI(_ context.Context, _ string, _ ...ai.Option) (*ai.Response, error) {
	m.called = true
	return m.resp, m.err
}

// aiJSONResponse builds an *ai.Response whose text content is the given JSON.
func aiJSONResponse(jsonBody string) *ai.Response {
	return &ai.Response{
		Choices: []ai.Choice{{
			Message: ai.Message{
				Content: []ai.ContentPart{{Type: "text", Text: jsonBody}},
			},
		}},
	}
}

// asMap marshals a handler result to a map so key-set assertions are exact.
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

func keySet(m map[string]any) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

func assertKeys(t *testing.T, got map[string]any, want []string) {
	t.Helper()
	sort.Strings(want)
	gk := keySet(got)
	if strings.Join(gk, ",") != strings.Join(want, ",") {
		t.Fatalf("key set mismatch:\n got: %v\nwant: %v", gk, want)
	}
}

// coderResultKeys / etc. are the model_dump() key sets Python returns.
var (
	coderResultKeys      = []string{"files_changed", "summary", "complete", "iteration_id", "tests_passed", "test_summary", "codebase_learnings", "agent_retro", "repo_name"}
	qaResultKeys         = []string{"passed", "summary", "test_failures", "coverage_gaps", "iteration_id"}
	codeReviewResultKeys = []string{"approved", "summary", "blocking", "debt_items", "iteration_id"}
	qaSynthesisKeys      = []string{"action", "summary", "stuck", "iteration_id"}
)

func newDeps(h harnessx.HarnessCaller, a AICaller, n Noter) *Deps {
	return &Deps{Harness: h, AI: a, Note: n}
}

// ---------------------------------------------------------------------------
// run_coder
// ---------------------------------------------------------------------------

// Contract: on success run_coder returns the exact CoderResult key set and
// threads iteration_id through, overwriting the parsed value.
func TestRunCoderSuccessKeySetAndIterationID(t *testing.T) {
	nr := &noteRecorder{}
	// The harness dest is *schemas.CoderResult; script a parsed success.
	mh := &mockHarness{fn: func(dest any) (*harness.Result, error) {
		cr := dest.(*schemas.CoderResult)
		cr.FilesChanged = []string{"a.go"}
		cr.Summary = "did it"
		cr.Complete = true
		cr.IterationID = "should-be-overwritten"
		return &harness.Result{Parsed: dest}, nil
	}}

	out, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue":         map[string]any{"name": "issue-1"},
		"worktree_path": "/wt",
		"iteration_id":  "iter-42",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	assertKeys(t, m, coderResultKeys)
	if m["iteration_id"] != "iter-42" {
		t.Fatalf("iteration_id not threaded/overwritten: %v", m["iteration_id"])
	}
	if !nr.hasTag("complete") {
		t.Fatalf("expected a completion note, got %+v", nr.notes)
	}
}

// Contract: coder applies the web-search guardrail to its system prompt (via
// tools.MaybeApplyCoderGuardrail) and runs with cwd = worktree.
func TestRunCoderAppliesGuardrailAndCwd(t *testing.T) {
	t.Setenv("OPENCODE_ENABLE_EXA", "1")
	t.Setenv("EXA_API_KEY", "k")

	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(dest any) (*harness.Result, error) {
		return &harness.Result{Parsed: dest}, nil
	}}

	if _, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue":         map[string]any{"name": "i"},
		"worktree_path": "/my/worktree",
	}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(mh.gotOpts.SystemPrompt, "When to use the web_search") {
		t.Fatalf("guardrail not appended to coder system prompt")
	}
	if mh.gotOpts.Cwd != "/my/worktree" {
		t.Fatalf("expected cwd=worktree, got %q", mh.gotOpts.Cwd)
	}
}

// Contract: coder does NOT append the guardrail when web search is disabled.
func TestRunCoderNoGuardrailWhenDisabled(t *testing.T) {
	t.Setenv("OPENCODE_ENABLE_EXA", "")
	t.Setenv("EXA_API_KEY", "")

	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(dest any) (*harness.Result, error) {
		return &harness.Result{Parsed: dest}, nil
	}}
	if _, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue":         map[string]any{"name": "i"},
		"worktree_path": "/wt",
	}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.Contains(mh.gotOpts.SystemPrompt, "When to use the web_search") {
		t.Fatalf("guardrail wrongly appended when web search disabled")
	}
}

// Contract: on Parsed==nil (schema parse failure) run_coder returns its
// deterministic fallback (complete=false, key set intact) — NOT an error.
func TestRunCoderParsedNilFallback(t *testing.T) {
	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "boom-parse", Parsed: nil}, nil
	}}
	out, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue":        map[string]any{"name": "issue-x"},
		"iteration_id": "it-1",
	})
	if err != nil {
		t.Fatalf("fallback must not be an error, got %v", err)
	}
	m := asMap(t, out)
	assertKeys(t, m, coderResultKeys)
	if m["complete"] != false {
		t.Fatalf("fallback must have complete=false, got %v", m["complete"])
	}
	if !strings.Contains(m["summary"].(string), "boom-parse") {
		t.Fatalf("fallback summary should carry the harness error, got %q", m["summary"])
	}
	// Empty collections must serialize as [] / {}, not null (model_dump parity).
	if _, ok := m["files_changed"].([]any); !ok {
		t.Fatalf("files_changed should be an empty array, got %#v", m["files_changed"])
	}
	if !nr.hasTag("error") {
		t.Fatalf("expected an error note on the no-result path")
	}
}

// Contract: a fatal harness error propagates (as *FatalHarnessError) and is not
// swallowed into a fallback.
func TestRunCoderFatalPropagates(t *testing.T) {
	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "credit balance is too low"}, nil
	}}
	out, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue": map[string]any{"name": "i"},
	})
	if err == nil {
		t.Fatal("expected fatal error to propagate")
	}
	var fe *fatal.FatalHarnessError
	if !errors.As(err, &fe) {
		t.Fatalf("expected *fatal.FatalHarnessError, got %T", err)
	}
	if out != nil {
		t.Fatalf("expected nil result on fatal, got %v", out)
	}
}

// Contract: a non-fatal transport error falls back deterministically (Python's
// `except Exception` branch) rather than propagating.
func TestRunCoderTransportErrorFallsBack(t *testing.T) {
	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		return nil, errors.New("network blip")
	}}
	out, err := RunCoder(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"issue": map[string]any{"name": "i"},
	})
	if err != nil {
		t.Fatalf("non-fatal error should fall back, got %v", err)
	}
	m := asMap(t, out)
	if m["complete"] != false || !strings.Contains(m["summary"].(string), "network blip") {
		t.Fatalf("expected fallback carrying the transport error, got %v", m)
	}
}

// ---------------------------------------------------------------------------
// run_qa
// ---------------------------------------------------------------------------

func TestRunQASuccessAndFallback(t *testing.T) {
	nr := &noteRecorder{}
	// success
	mh := &mockHarness{fn: func(dest any) (*harness.Result, error) {
		qr := dest.(*schemas.QAResult)
		qr.Passed = true
		qr.Summary = "ok"
		return &harness.Result{Parsed: dest}, nil
	}}
	out, err := RunQA(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"worktree_path": "/wt",
		"coder_result":  map[string]any{},
		"issue":         map[string]any{"name": "i"},
		"iteration_id":  "q1",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	assertKeys(t, m, qaResultKeys)
	if m["iteration_id"] != "q1" || m["passed"] != true {
		t.Fatalf("unexpected qa success result: %v", m)
	}

	// fallback on Parsed==nil
	mhf := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	out2, err := RunQA(context.Background(), newDeps(mhf, nil, nr), map[string]any{
		"worktree_path": "/wt",
		"coder_result":  map[string]any{},
		"issue":         map[string]any{"name": "i"},
	})
	if err != nil {
		t.Fatalf("fallback must not error: %v", err)
	}
	m2 := asMap(t, out2)
	assertKeys(t, m2, qaResultKeys)
	if m2["passed"] != false {
		t.Fatalf("qa fallback must be passed=false, got %v", m2["passed"])
	}
}

// ---------------------------------------------------------------------------
// run_code_reviewer
// ---------------------------------------------------------------------------

// Contract: reviewer passes qa_ran through to its prompt AND uses the reviewer
// tool set; on failure it falls back to approved=true (non-blocking).
func TestRunCodeReviewerQARanAndFallback(t *testing.T) {
	nr := &noteRecorder{}
	mh := &mockHarness{fn: func(dest any) (*harness.Result, error) {
		rr := dest.(*schemas.CodeReviewResult)
		rr.Approved = true
		rr.Blocking = false
		return &harness.Result{Parsed: dest}, nil
	}}
	out, err := RunCodeReviewer(context.Background(), newDeps(mh, nil, nr), map[string]any{
		"worktree_path": "/wt",
		"coder_result":  map[string]any{},
		"issue":         map[string]any{"name": "i"},
		"qa_ran":        true,
		"iteration_id":  "r1",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	assertKeys(t, m, codeReviewResultKeys)
	// reviewer tools ordering (Bash last) is the reviewer-specific set.
	if strings.Join(mh.gotOpts.Tools, ",") != "Read,Write,Glob,Grep,Bash" {
		t.Fatalf("reviewer tool set mismatch: %v", mh.gotOpts.Tools)
	}

	// fallback: not-blocking approve
	mhf := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		return &harness.Result{IsError: true, Parsed: nil}, nil
	}}
	out2, err := RunCodeReviewer(context.Background(), newDeps(mhf, nil, nr), map[string]any{
		"worktree_path": "/wt",
		"coder_result":  map[string]any{},
		"issue":         map[string]any{"name": "i"},
	})
	if err != nil {
		t.Fatalf("fallback must not error: %v", err)
	}
	m2 := asMap(t, out2)
	if m2["approved"] != true || m2["blocking"] != false {
		t.Fatalf("reviewer fallback must be approved=true, blocking=false, got %v", m2)
	}
}

// Contract: qa_ran is forwarded to the reviewer prompt builder. We capture the
// rendered prompt for qa_ran true vs false and assert they differ, proving the
// flag is a live input (not dropped on the floor).
func TestRunCodeReviewerForwardsQARanToPrompt(t *testing.T) {
	nr := &noteRecorder{}

	renderReviewer := func(qaRan bool) string {
		var captured string
		hc := harnessFuncCaller(func(_ context.Context, prompt string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
			captured = prompt
			return &harness.Result{Parsed: dest}, nil
		})
		if _, err := RunCodeReviewer(context.Background(), newDeps(hc, nil, nr), map[string]any{
			"worktree_path": "/wt",
			"coder_result":  map[string]any{},
			"issue":         map[string]any{"name": "i"},
			"qa_ran":        qaRan,
		}); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		return captured
	}
	promptWithQA := renderReviewer(true)
	promptWithoutQA := renderReviewer(false)
	if promptWithQA == promptWithoutQA {
		t.Fatalf("qa_ran did not affect the rendered reviewer prompt — not forwarded")
	}
}

// harnessFuncCaller adapts a bare function to the HarnessCaller interface.
type harnessFuncCaller func(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error)

func (f harnessFuncCaller) Harness(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	return f(ctx, prompt, schema, dest, opts)
}

// ---------------------------------------------------------------------------
// run_qa_synthesizer (AI path)
// ---------------------------------------------------------------------------

// Contract: qa_synthesizer uses the AI path (not the harness) and, on a valid
// AI response, returns the parsed decision with the exact key set.
func TestRunQASynthesizerUsesAIPath(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{resp: aiJSONResponse(`{"action":"approve","summary":"lgtm","stuck":false}`)}
	// A harness that would fail the test if called — the synthesizer must not
	// touch the harness.
	mh := &mockHarness{fn: func(_ any) (*harness.Result, error) {
		t.Fatal("run_qa_synthesizer must not call the harness")
		return nil, nil
	}}

	out, err := RunQASynthesizer(context.Background(), newDeps(mh, mai, nr), map[string]any{
		"qa_result":         map[string]any{"passed": true},
		"review_result":     map[string]any{"approved": true},
		"iteration_history": []any{},
		"iteration_id":      "s1",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !mai.called {
		t.Fatal("expected the AI path to be used")
	}
	m := asMap(t, out)
	assertKeys(t, m, qaSynthesisKeys)
	if m["action"] != "approve" || m["iteration_id"] != "s1" {
		t.Fatalf("unexpected synthesis result: %v", m)
	}
}

// Contract: qa_synthesizer falls back deterministically when the AI response is
// unparseable — approve when QA passed + review approved + not blocking.
func TestRunQASynthesizerFallbackApprove(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{resp: aiJSONResponse("not json")}
	out, err := RunQASynthesizer(context.Background(), newDeps(nil, mai, nr), map[string]any{
		"qa_result":     map[string]any{"passed": true},
		"review_result": map[string]any{"approved": true, "blocking": false},
		"iteration_id":  "s2",
	})
	if err != nil {
		t.Fatalf("fallback must not error: %v", err)
	}
	m := asMap(t, out)
	assertKeys(t, m, qaSynthesisKeys)
	if m["action"] != "approve" {
		t.Fatalf("expected fallback approve, got %v", m["action"])
	}
}

// Contract: fallback blocks when review is blocking.
func TestRunQASynthesizerFallbackBlock(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{resp: aiJSONResponse("")}
	out, err := RunQASynthesizer(context.Background(), newDeps(nil, mai, nr), map[string]any{
		"qa_result":     map[string]any{"passed": false},
		"review_result": map[string]any{"approved": false, "blocking": true},
		"iteration_id":  "s3",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["action"] != "block" {
		t.Fatalf("expected fallback block, got %v", m["action"])
	}
}

// Contract: fallback defaults to FIX otherwise, and the summary embeds Python
// True/False booleans.
func TestRunQASynthesizerFallbackFix(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{err: errors.New("transient provider error")}
	out, err := RunQASynthesizer(context.Background(), newDeps(nil, mai, nr), map[string]any{
		"qa_result":     map[string]any{"passed": false},
		"review_result": map[string]any{"approved": true, "blocking": false},
		"iteration_id":  "s4",
	})
	if err != nil {
		t.Fatalf("non-fatal AI error should fall back, got %v", err)
	}
	m := asMap(t, out)
	if m["action"] != "fix" {
		t.Fatalf("expected fallback fix, got %v", m["action"])
	}
	if !strings.Contains(m["summary"].(string), "QA passed=False, review approved=True") {
		t.Fatalf("fix summary must embed Python-style booleans, got %q", m["summary"])
	}
	if !nr.hasTag("error") {
		t.Fatalf("expected an error note when the AI call fails")
	}
}

// Contract: a fatal AI error (billing/auth) propagates as *FatalHarnessError.
func TestRunQASynthesizerFatalPropagates(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{err: errors.New("Authentication failed for provider")}
	out, err := RunQASynthesizer(context.Background(), newDeps(nil, mai, nr), map[string]any{
		"qa_result":     map[string]any{"passed": true},
		"review_result": map[string]any{"approved": true},
		"iteration_id":  "s5",
	})
	if err == nil {
		t.Fatal("expected fatal AI error to propagate")
	}
	var fe *fatal.FatalHarnessError
	if !errors.As(err, &fe) {
		t.Fatalf("expected *fatal.FatalHarnessError, got %T: %v", err, err)
	}
	if out != nil {
		t.Fatalf("expected nil result on fatal, got %v", out)
	}
}

// Contract: an AI response missing the required action enum is treated as a
// parse failure (parsed=None) → deterministic fallback, not a bogus decision.
func TestRunQASynthesizerInvalidActionFallsBack(t *testing.T) {
	nr := &noteRecorder{}
	mai := &mockAI{resp: aiJSONResponse(`{"summary":"no action field","stuck":false}`)}
	out, err := RunQASynthesizer(context.Background(), newDeps(nil, mai, nr), map[string]any{
		"qa_result":     map[string]any{"passed": true},
		"review_result": map[string]any{"approved": true, "blocking": false},
		"iteration_id":  "s6",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	m := asMap(t, out)
	if m["action"] != "approve" {
		t.Fatalf("invalid-action response should fall back deterministically, got %v", m["action"])
	}
}

// ---------------------------------------------------------------------------
// Registration surface
// ---------------------------------------------------------------------------

// Contract: Handlers() exposes exactly the four coding roles under their exact
// Python reasoner names.
func TestHandlersRegistrationNames(t *testing.T) {
	h := Handlers()
	want := []string{"run_code_reviewer", "run_coder", "run_qa", "run_qa_synthesizer"}
	got := make([]string, 0, len(h))
	for k, fn := range h {
		if fn == nil {
			t.Fatalf("handler %q is nil", k)
		}
		got = append(got, k)
	}
	sort.Strings(got)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("registration names mismatch:\n got: %v\nwant: %v", got, want)
	}
}

// Contract: input binding applies the Python default model per role.
func TestInputDefaults(t *testing.T) {
	ci, err := bindInput[coderInput](map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	if ci.Model != "sonnet" || ci.AIProvider != "claude" || ci.Iteration != 1 {
		t.Fatalf("coder defaults wrong: %+v", ci)
	}
	si, err := bindInput[qaSynthInput](map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	if si.Model != "haiku" {
		t.Fatalf("qa_synthesizer default model must be haiku, got %q", si.Model)
	}
}

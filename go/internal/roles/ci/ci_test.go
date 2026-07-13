package ci

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/prompts/advisor"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// --- test fixtures ----------------------------------------------------------

type noteRec struct {
	msg  string
	tags []string
}

// mockApp is the App seam the Python tests get by patching router.harness /
// router.note. It records notes and whether the harness was invoked, and drives
// a scripted harness result.
type mockApp struct {
	harnessFn func(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error)

	harnessCalled bool
	gotPrompt     string
	gotOpts       harness.Options
	notes         []noteRec
}

func (m *mockApp) Harness(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	m.harnessCalled = true
	m.gotPrompt = prompt
	m.gotOpts = opts
	return m.harnessFn(ctx, prompt, schema, dest, opts)
}

func (m *mockApp) Note(_ context.Context, message string, tags ...string) {
	m.notes = append(m.notes, noteRec{msg: message, tags: tags})
}

func (m *mockApp) noteAt(i int) noteRec {
	if i < 0 {
		i += len(m.notes)
	}
	return m.notes[i]
}

func tagsEq(got, want []string) bool { return reflect.DeepEqual(got, want) }

// --- Handlers registration surface -----------------------------------------

// Contract: the package registers exactly run_ci_watcher / run_ci_fixer /
// run_pr_resolver under those exact Python names.
func TestHandlersRegistrationSurface(t *testing.T) {
	h := Handlers()
	want := []string{"run_ci_watcher", "run_ci_fixer", "run_pr_resolver"}
	if len(h) != len(want) {
		t.Fatalf("expected %d handlers, got %d: %v", len(want), len(h), keys(h))
	}
	for _, name := range want {
		if h[name] == nil {
			t.Errorf("missing handler %q", name)
		}
	}
}

func keys(m map[string]Handler) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// --- run_ci_watcher ---------------------------------------------------------

// Contract: ci_watcher forwards its params to cigate.WatchPRChecks, returns the
// result unchanged (exact key set), emits the start+complete notes, and NEVER
// invokes the harness.
func TestRunCIWatcher_DelegatesAndNeverCallsHarness(t *testing.T) {
	restore := watchFn
	defer func() { watchFn = restore }()

	var got struct {
		repo       string
		pr         int
		wait, poll int
		sha        string
	}
	want := schemas.CIWatchResult{
		Status:         "failed",
		PRNumber:       42,
		ElapsedSeconds: 17,
		FailedChecks:   []schemas.CIFailedCheck{{Name: "unit", Conclusion: "FAILURE"}},
		Summary:        "1 check failing",
	}
	watchFn = func(_ context.Context, repoPath string, prNumber, waitSeconds, pollSeconds int, headSHA string) schemas.CIWatchResult {
		got.repo, got.pr, got.wait, got.poll, got.sha = repoPath, prNumber, waitSeconds, pollSeconds, headSHA
		return want
	}

	app := &mockApp{}
	out, err := RunCIWatcher(context.Background(), &Deps{App: app}, map[string]any{
		"repo_path": "/repo",
		"pr_number": 42,
		"head_sha":  "abcdef0123456789",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if app.harnessCalled {
		t.Fatal("ci_watcher must not invoke the harness")
	}

	// Defaults forwarded verbatim.
	if got.repo != "/repo" || got.pr != 42 || got.wait != 1500 || got.poll != 30 || got.sha != "abcdef0123456789" {
		t.Fatalf("params not forwarded correctly: %+v", got)
	}

	// Result returned unchanged, with the exact CIWatchResult key set.
	res, ok := out.(schemas.CIWatchResult)
	if !ok {
		t.Fatalf("expected schemas.CIWatchResult, got %T", out)
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("result changed: got %+v want %+v", res, want)
	}
	assertKeySet(t, out, []string{"status", "pr_number", "elapsed_seconds", "failed_checks", "summary"})

	// Notes: start (anchored, sha truncated to 10 runes) + complete (status tag).
	if len(app.notes) != 2 {
		t.Fatalf("expected 2 notes, got %d: %+v", len(app.notes), app.notes)
	}
	start := app.noteAt(0)
	if start.msg != "CI watcher: PR #42, wait_cap=1500s, poll=30s, anchored to abcdef0123" {
		t.Fatalf("start note text: %q", start.msg)
	}
	if !tagsEq(start.tags, []string{"ci_watcher", "start"}) {
		t.Fatalf("start note tags: %v", start.tags)
	}
	done := app.noteAt(1)
	if done.msg != "CI watcher: status=failed (1 check failing)" {
		t.Fatalf("complete note text: %q", done.msg)
	}
	if !tagsEq(done.tags, []string{"ci_watcher", "complete", "failed"}) {
		t.Fatalf("complete note tags: %v", done.tags)
	}
}

// Contract: with no head_sha the start note omits the anchor clause.
func TestRunCIWatcher_NoAnchorClause(t *testing.T) {
	restore := watchFn
	defer func() { watchFn = restore }()
	watchFn = func(_ context.Context, _ string, _, _, _ int, _ string) schemas.CIWatchResult {
		return schemas.CIWatchResult{Status: "passed", PRNumber: 7, Summary: "green"}
	}

	app := &mockApp{}
	if _, err := RunCIWatcher(context.Background(), &Deps{App: app},
		map[string]any{"repo_path": "/r", "pr_number": 7, "wait_seconds": 60, "poll_seconds": 5}); err != nil {
		t.Fatal(err)
	}
	if app.noteAt(0).msg != "CI watcher: PR #7, wait_cap=60s, poll=5s" {
		t.Fatalf("start note: %q", app.noteAt(0).msg)
	}
}

// --- run_ci_fixer -----------------------------------------------------------

// Contract: on a parsed harness result ci_fixer returns it and emits the
// complete note with True/False-rendered booleans; the model/provider/tools/cwd
// are wired from the input (model default "sonnet", provider "claude-code").
func TestRunCIFixer_SuccessReturnsParsed(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
			d := dest.(*schemas.CIFixResult)
			d.Fixed = true
			d.Pushed = true
			d.FilesChanged = []string{"a.go", "b.go"}
			return &harness.Result{Parsed: dest}, nil
		},
	}

	out, err := RunCIFixer(context.Background(), &Deps{App: app}, map[string]any{
		"repo_path":          "/repo",
		"pr_number":          12,
		"integration_branch": "integration",
		"base_branch":        "main",
		"failed_checks":      []any{map[string]any{"name": "unit"}, map[string]any{"name": "lint"}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	res, ok := out.(*schemas.CIFixResult)
	if !ok || !res.Fixed || !res.Pushed {
		t.Fatalf("expected parsed CIFixResult, got %T %+v", out, out)
	}

	// Model/provider/tools/cwd/system prompt wired from defaults + input.
	if app.gotOpts.Model != "sonnet" {
		t.Errorf("model: got %q want sonnet (default)", app.gotOpts.Model)
	}
	if app.gotOpts.Provider != "claude-code" {
		t.Errorf("provider: got %q want claude-code", app.gotOpts.Provider)
	}
	if !reflect.DeepEqual(app.gotOpts.Tools, resolveTools) {
		t.Errorf("tools: got %v", app.gotOpts.Tools)
	}
	if app.gotOpts.Cwd != "/repo" {
		t.Errorf("cwd: got %q", app.gotOpts.Cwd)
	}
	if app.gotOpts.SystemPrompt != advisor.CIFixerSystemPrompt {
		t.Errorf("system prompt not the ci_fixer system prompt")
	}

	// Start note counts the input failing checks; complete note renders bools.
	if app.noteAt(0).msg != "CI fixer: PR #12, attempt 1/2, 2 failing check(s)" {
		t.Fatalf("start note: %q", app.noteAt(0).msg)
	}
	if !tagsEq(app.noteAt(0).tags, []string{"ci_fixer", "start"}) {
		t.Fatalf("start tags: %v", app.noteAt(0).tags)
	}
	if app.noteAt(-1).msg != "CI fixer complete: fixed=True, pushed=True, 2 file(s) changed" {
		t.Fatalf("complete note: %q", app.noteAt(-1).msg)
	}
	if !tagsEq(app.noteAt(-1).tags, []string{"ci_fixer", "complete"}) {
		t.Fatalf("complete tags: %v", app.noteAt(-1).tags)
	}
}

// Contract: a fatal (non-retryable) harness error propagates as an error — it is
// NOT swallowed into the deterministic fallback.
func TestRunCIFixer_FatalPropagates(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
			return &harness.Result{IsError: true, ErrorMessage: "Credit balance is too low"}, nil
		},
	}
	out, err := RunCIFixer(context.Background(), &Deps{App: app}, map[string]any{"pr_number": 1})
	if err == nil {
		t.Fatal("expected the fatal error to propagate")
	}
	if out != nil {
		t.Fatalf("expected nil result on fatal, got %+v", out)
	}
}

// Contract: on a schema parse failure (Parsed==nil, non-fatal) ci_fixer returns
// its deterministic fallback — NOT an error — with empty-list fields (matching
// Python model_dump, not Go nil→null) and the failure message on both summary
// and error_message.
func TestRunCIFixer_ParseFailureFallback(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
			return &harness.Result{IsError: true, ErrorMessage: "schema validation failed after retries", Parsed: nil}, nil
		},
	}
	out, err := RunCIFixer(context.Background(), &Deps{App: app}, map[string]any{"pr_number": 1})
	if err != nil {
		t.Fatalf("fallback must not be an error, got %v", err)
	}
	res := out.(*schemas.CIFixResult)
	if res.Fixed {
		t.Error("fallback fixed must be false")
	}
	if res.Summary != "CI fixer agent failed to produce a valid result." ||
		res.ErrorMessage != "CI fixer agent failed to produce a valid result." {
		t.Errorf("fallback messages: %+v", res)
	}
	// Empty-list fields must marshal to [] not null.
	assertJSONField(t, res, "files_changed", "[]")
	assertJSONField(t, res, "rejected_workarounds", "[]")

	// No error note on the pure parse-failure path (Python only notes on except).
	for _, n := range app.notes {
		for _, tag := range n.tags {
			if tag == "error" {
				t.Fatalf("unexpected error note on parse-failure path: %q", n.msg)
			}
		}
	}
}

// --- run_pr_resolver --------------------------------------------------------

// Contract: pr_resolver returns addressed_comments entries carrying
// comment_id/thread_id/addressed/note keys; the complete note renders the
// addressed/total tally; merge_state defaults to "skipped".
func TestRunPRResolver_SuccessAddressedComments(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
			d := dest.(*schemas.PRResolveResult)
			d.Fixed = true
			d.Pushed = true
			d.FilesChanged = []string{"x.go"}
			d.AddressedComments = []schemas.AddressedComment{
				{CommentID: 1, ThreadID: "t1", Addressed: true, Note: "fixed"},
				{CommentID: 2, ThreadID: "t2", Addressed: false, Note: "not actionable"},
			}
			return &harness.Result{Parsed: dest}, nil
		},
	}

	out, err := RunPRResolver(context.Background(), &Deps{App: app}, map[string]any{
		"repo_path":   "/repo",
		"pr_number":   9,
		"head_branch": "feat",
		"base_branch": "main",
		"review_comments": []any{
			map[string]any{"comment_id": 1}, map[string]any{"comment_id": 2},
		},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	res := out.(*schemas.PRResolveResult)
	if len(res.AddressedComments) != 2 {
		t.Fatalf("expected 2 addressed comments, got %d", len(res.AddressedComments))
	}

	// Each addressed_comments entry serializes with the four required keys.
	b, _ := json.Marshal(res.AddressedComments[0])
	var entry map[string]any
	_ = json.Unmarshal(b, &entry)
	for _, k := range []string{"comment_id", "thread_id", "addressed", "note"} {
		if _, ok := entry[k]; !ok {
			t.Errorf("addressed_comment missing key %q: %v", k, entry)
		}
	}

	// Start note (merge_state default skipped) + complete note tally.
	if app.noteAt(0).msg != "PR resolver: PR #9, merge_state=skipped, 0 failing check(s), 2 review comment(s)" {
		t.Fatalf("start note: %q", app.noteAt(0).msg)
	}
	if app.noteAt(-1).msg != "PR resolver complete: fixed=True, pushed=True, merge_resolved=False, 1 file(s) changed, 1/2 comment(s) addressed" {
		t.Fatalf("complete note: %q", app.noteAt(-1).msg)
	}
	if !tagsEq(app.noteAt(-1).tags, []string{"pr_resolver", "complete"}) {
		t.Fatalf("complete tags: %v", app.noteAt(-1).tags)
	}
}

// Contract: pr_resolver fatal error propagates rather than falling back.
func TestRunPRResolver_FatalPropagates(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
			return &harness.Result{IsError: true, ErrorMessage: "credit balance is too low"}, nil
		},
	}
	if _, err := RunPRResolver(context.Background(), &Deps{App: app}, map[string]any{"pr_number": 1}); err == nil {
		t.Fatal("expected fatal error to propagate")
	}
}

// Contract: on parse failure pr_resolver returns its deterministic fallback with
// empty-list fields (addressed_comments=[] etc.), not an error.
func TestRunPRResolver_ParseFailureFallback(t *testing.T) {
	app := &mockApp{
		harnessFn: func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
			return &harness.Result{IsError: true, ErrorMessage: "no structured output", Parsed: nil}, nil
		},
	}
	out, err := RunPRResolver(context.Background(), &Deps{App: app}, map[string]any{"pr_number": 1})
	if err != nil {
		t.Fatalf("fallback must not error, got %v", err)
	}
	res := out.(*schemas.PRResolveResult)
	if res.Fixed || res.Summary != "PR resolver agent failed to produce a valid result." ||
		res.ErrorMessage != "PR resolver agent failed to produce a valid result." {
		t.Errorf("fallback: %+v", res)
	}
	assertJSONField(t, res, "files_changed", "[]")
	assertJSONField(t, res, "commit_shas", "[]")
	assertJSONField(t, res, "addressed_comments", "[]")
	assertJSONField(t, res, "rejected_workarounds", "[]")
}

// --- helpers ----------------------------------------------------------------

// assertKeySet marshals v and asserts its top-level JSON object keys equal want
// (order-independent).
func assertKeySet(t *testing.T, v any, want []string) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got := make(map[string]bool, len(m))
	for k := range m {
		got[k] = true
	}
	if len(got) != len(want) {
		t.Fatalf("key set size: got %v want %v", keysOf(m), want)
	}
	for _, k := range want {
		if !got[k] {
			t.Errorf("missing key %q (got %v)", k, keysOf(m))
		}
	}
}

func keysOf(m map[string]json.RawMessage) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// assertJSONField marshals v and asserts the raw JSON of field is exactly want
// (used to prove empty slices serialize as [] not null).
func assertJSONField(t *testing.T, v any, field, want string) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	raw, ok := m[field]
	if !ok {
		t.Fatalf("field %q absent", field)
	}
	if string(raw) != want {
		t.Errorf("field %q: got %s want %s", field, string(raw), want)
	}
}

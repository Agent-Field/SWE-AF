package coding

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Test scaffolding — mirrors tests/test_coding_loop.py::_CallFnBuilder
// ---------------------------------------------------------------------------

// callBuilder builds a scripted CallFn keyed by iteration (determined by coder
// call count) and role (matched on the target substring, exactly like the Python
// helper — run_qa_synthesizer must be checked before run_qa).
type callBuilder struct {
	mu         sync.Mutex
	coder      map[int]map[string]any
	reviewer   map[int]map[string]any
	qa         map[int]map[string]any
	synth      map[int]map[string]any
	coderCalls int

	// observed call bookkeeping
	targets       []string
	permissionMap map[string]string
}

func newBuilder() *callBuilder {
	return &callBuilder{
		coder:         map[int]map[string]any{},
		reviewer:      map[int]map[string]any{},
		qa:            map[int]map[string]any{},
		synth:         map[int]map[string]any{},
		permissionMap: map[string]string{},
	}
}

func (b *callBuilder) onCoder(it int, files []string, summary string) *callBuilder {
	b.coder[it] = map[string]any{"files_changed": files, "summary": summary, "complete": true}
	return b
}

func (b *callBuilder) onReviewer(it int, approved, blocking bool, summary string) *callBuilder {
	b.reviewer[it] = map[string]any{"approved": approved, "blocking": blocking, "summary": summary, "debt_items": []any{}}
	return b
}

func (b *callBuilder) onQA(it int, passed bool, summary string) *callBuilder {
	b.qa[it] = map[string]any{"passed": passed, "summary": summary, "test_failures": []any{}}
	return b
}

func (b *callBuilder) onSynth(it int, action, summary string, stuck bool) *callBuilder {
	b.synth[it] = map[string]any{"action": action, "summary": summary, "stuck": stuck}
	return b
}

func (b *callBuilder) build() CallFn {
	return func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		b.mu.Lock()
		defer b.mu.Unlock()
		b.targets = append(b.targets, target)
		if pm, ok := kwargs["permission_mode"].(string); ok {
			b.permissionMap[lastSeg(target)] = pm
		}

		if strings.Contains(target, "run_coder") {
			b.coderCalls++
			it := b.coderCalls
			if r, ok := b.coder[it]; ok {
				return r, nil
			}
			return map[string]any{"files_changed": []any{}, "summary": "No changes", "complete": true}, nil
		}

		it := b.coderCalls

		if strings.Contains(target, "run_code_reviewer") {
			if r, ok := b.reviewer[it]; ok {
				return r, nil
			}
			return map[string]any{"approved": false, "blocking": false, "summary": "Needs work"}, nil
		}
		if strings.Contains(target, "run_qa_synthesizer") {
			if r, ok := b.synth[it]; ok {
				return r, nil
			}
			return map[string]any{"action": "fix", "summary": "Continue fixing", "stuck": false}, nil
		}
		if strings.Contains(target, "run_qa") {
			if r, ok := b.qa[it]; ok {
				return r, nil
			}
			return map[string]any{"passed": true, "summary": "Tests pass", "test_failures": []any{}}, nil
		}
		return map[string]any{}, nil
	}
}

func (b *callBuilder) roleCount(role string) int {
	n := 0
	for _, t := range b.targets {
		if strings.HasSuffix(t, "."+role) {
			n++
		}
	}
	return n
}

func lastSeg(target string) string {
	if i := strings.LastIndex(target, "."); i >= 0 {
		return target[i+1:]
	}
	return target
}

func makeConfig(t *testing.T, overrides map[string]any) *config.ExecutionConfig {
	t.Helper()
	raw := map[string]any{"max_coding_iterations": 5, "agent_timeout_seconds": 30}
	for k, v := range overrides {
		raw[k] = v
	}
	cfg, err := config.LoadExecutionConfig(raw)
	if err != nil {
		t.Fatalf("LoadExecutionConfig: %v", err)
	}
	return cfg
}

func makeDAGState(artifactsDir string) *schemas.DAGState {
	return &schemas.DAGState{RepoPath: "/tmp/fake-repo", ArtifactsDir: artifactsDir}
}

func makeIssue(name string, needsDeeperQA bool) map[string]any {
	issue := map[string]any{
		"name":          name,
		"title":         "Test issue",
		"description":   "A test issue for the coding loop",
		"worktree_path": "/tmp/fake-repo",
		"branch_name":   "test/issue-1",
		"depends_on":    []any{},
		"provides":      []any{},
	}
	if needsDeeperQA {
		issue["guidance"] = map[string]any{"needs_deeper_qa": true}
	}
	return issue
}

type noteCapture struct {
	msgs []string
	tags [][]string
}

func (n *noteCapture) fn() NoteFn {
	return func(msg string, tags []string) {
		n.msgs = append(n.msgs, msg)
		n.tags = append(n.tags, tags)
	}
}

func run(t *testing.T, issue map[string]any, ds *schemas.DAGState, callFn CallFn, cfg *config.ExecutionConfig, note NoteFn) schemas.IssueResult {
	t.Helper()
	res, err := RunCodingLoop(context.Background(), issue, ds, callFn, "test-node", cfg, note, nil)
	if err != nil {
		t.Fatalf("RunCodingLoop returned unexpected error: %v", err)
	}
	return res
}

// ---------------------------------------------------------------------------
// DetectStuckLoop unit tests (port of TestDetectStuckLoop)
// ---------------------------------------------------------------------------

func TestDetectStuckLoop(t *testing.T) {
	fix := func(blocking bool) map[string]any {
		return map[string]any{"action": "fix", "review_blocking": blocking}
	}
	approve := map[string]any{"action": "approve", "review_blocking": false}

	cases := []struct {
		name    string
		history []map[string]any
		window  int
		want    bool
	}{
		{"too few", []map[string]any{fix(false), fix(false)}, 3, false},
		{"all non-blocking fix", []map[string]any{fix(false), fix(false), fix(false)}, 3, true},
		{"blocking prevents", []map[string]any{fix(false), fix(true), fix(false)}, 3, false},
		{"approve prevents", []map[string]any{approve, fix(false), fix(false)}, 3, false},
		{"only recent window", []map[string]any{approve, fix(false), fix(false), fix(false)}, 3, true},
		{"custom window", []map[string]any{fix(false), fix(false)}, 2, true},
		{"empty", []map[string]any{}, 3, false},
	}
	for _, c := range cases {
		if got := DetectStuckLoop(c.history, c.window); got != c.want {
			t.Errorf("%s: DetectStuckLoop = %v, want %v", c.name, got, c.want)
		}
	}
}

// ---------------------------------------------------------------------------
// Integration tests (port of TestCodingLoopIntegration)
// ---------------------------------------------------------------------------

func TestApprovedFirstIteration(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/app.py"}, "impl").onReviewer(1, true, false, "LGTM")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeCompleted {
		t.Fatalf("outcome = %s, want completed", res.Outcome)
	}
	if res.Attempts != 1 {
		t.Errorf("attempts = %d, want 1", res.Attempts)
	}
	if !contains(res.FilesChanged, "src/app.py") {
		t.Errorf("files_changed missing src/app.py: %v", res.FilesChanged)
	}
	if len(res.IterationHistory) != 1 {
		t.Fatalf("iteration_history len = %d, want 1", len(res.IterationHistory))
	}
	if res.IterationHistory[0]["action"] != "approve" {
		t.Errorf("history[0].action = %v, want approve", res.IterationHistory[0]["action"])
	}
}

func TestApprovedAfterFixCycle(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/app.py"}, "impl").onReviewer(1, false, false, "Fix imports")
	b.onCoder(2, []string{"src/app.py"}, "impl").onReviewer(2, true, false, "LGTM now")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeCompleted || res.Attempts != 2 {
		t.Fatalf("outcome=%s attempts=%d, want completed/2", res.Outcome, res.Attempts)
	}
	if len(res.IterationHistory) != 2 {
		t.Fatalf("history len = %d, want 2", len(res.IterationHistory))
	}
	if res.IterationHistory[0]["action"] != "fix" || res.IterationHistory[1]["action"] != "approve" {
		t.Errorf("history actions = %v/%v, want fix/approve", res.IterationHistory[0]["action"], res.IterationHistory[1]["action"])
	}
}

func TestStuckLoopNonBlockingAcceptsDebt(t *testing.T) {
	b := newBuilder()
	for i := 1; i <= 5; i++ {
		b.onCoder(i, []string{"src/app.py"}, "impl").onReviewer(i, false, false, "Minor style issue")
	}
	nc := &noteCapture{}
	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, map[string]any{"max_coding_iterations": 5}), nc.fn())

	if res.Outcome != schemas.IssueOutcomeCompletedWithDebt {
		t.Fatalf("outcome = %s, want completed_with_debt", res.Outcome)
	}
	if res.Attempts != 3 {
		t.Errorf("attempts = %d, want 3 (stuck at window)", res.Attempts)
	}
	stuck := false
	for _, m := range nc.msgs {
		if strings.Contains(m, "STUCK") {
			stuck = true
		}
	}
	if !stuck {
		t.Errorf("expected a STUCK note, got %v", nc.msgs)
	}
}

func TestBlockingReviewFailsImmediately(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/app.py"}, "impl").onReviewer(1, false, true, "SQL injection")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want failed_unrecoverable/1", res.Outcome, res.Attempts)
	}
	if res.IterationHistory[0]["action"] != "block" {
		t.Errorf("history[0].action = %v, want block", res.IterationHistory[0]["action"])
	}
}

func TestStagnationNoFilesChanged(t *testing.T) {
	b := newBuilder()
	for i := 1; i <= 5; i++ {
		b.onCoder(i, []string{}, "impl").onReviewer(i, false, false, "Nothing to review")
	}
	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, map[string]any{"max_coding_iterations": 5}), nil)

	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable {
		t.Fatalf("outcome = %s, want failed_unrecoverable", res.Outcome)
	}
	if len(res.FilesChanged) != 0 {
		t.Errorf("files_changed = %v, want empty", res.FilesChanged)
	}
}

func TestExhaustionNonBlockingAcceptsDebt(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/main.py"}, "impl").onReviewer(1, false, false, "Needs work")
	b.onCoder(2, []string{"src/main.py", "src/util.py"}, "impl").onReviewer(2, false, false, "Still needs work")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, map[string]any{"max_coding_iterations": 2}), nil)

	if res.Outcome != schemas.IssueOutcomeCompletedWithDebt || res.Attempts != 2 {
		t.Fatalf("outcome=%s attempts=%d, want completed_with_debt/2", res.Outcome, res.Attempts)
	}
	if !contains(res.FilesChanged, "src/main.py") || !contains(res.FilesChanged, "src/util.py") {
		t.Errorf("files_changed = %v, want both main+util", res.FilesChanged)
	}
}

func TestCoderExceptionFailsUnrecoverable(t *testing.T) {
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_coder") {
			return nil, errors.New("Model API timeout")
		}
		return map[string]any{}, nil
	}
	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), callFn, makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want failed_unrecoverable/1", res.Outcome, res.Attempts)
	}
	if !strings.Contains(res.ErrorMessage, "Coder agent failed") {
		t.Errorf("error_message = %q, want to contain 'Coder agent failed'", res.ErrorMessage)
	}
}

func TestFatalErrorPropagates(t *testing.T) {
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_coder") {
			return nil, &fatal.FatalHarnessError{OriginalMessage: "credit balance is too low"}
		}
		return map[string]any{}, nil
	}
	_, err := RunCodingLoop(context.Background(), makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), callFn, "test-node", makeConfig(t, nil), nil, nil)
	var fhe *fatal.FatalHarnessError
	if !errors.As(err, &fhe) {
		t.Fatalf("expected *fatal.FatalHarnessError to propagate, got %v", err)
	}
}

func TestFlaggedPathSynthesizerStuck(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/module.py"}, "impl").onQA(1, true, "pass").onReviewer(1, false, false, "Minor")
	b.onSynth(1, "fix", "Continue", true)

	res := run(t, makeIssue("ISSUE-1", true), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeCompletedWithDebt || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want completed_with_debt/1", res.Outcome, res.Attempts)
	}
	if !contains(res.FilesChanged, "src/module.py") {
		t.Errorf("files_changed missing module.py: %v", res.FilesChanged)
	}
}

func TestFlaggedPathStuckBlockingFails(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/module.py"}, "impl").onQA(1, false, "sec fail").onReviewer(1, false, true, "vuln")
	b.onSynth(1, "fix", "Critical", true)

	res := run(t, makeIssue("ISSUE-1", true), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want failed_unrecoverable/1", res.Outcome, res.Attempts)
	}
}

func TestFlaggedPathApproved(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"src/feature.py", "tests/test_feature.py"}, "impl").onQA(1, true, "pass").onReviewer(1, true, false, "Clean")
	b.onSynth(1, "approve", "All checks pass", false)

	res := run(t, makeIssue("ISSUE-1", true), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if res.Outcome != schemas.IssueOutcomeCompleted || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want completed/1", res.Outcome, res.Attempts)
	}
	if len(res.FilesChanged) != 2 {
		t.Errorf("files_changed len = %d, want 2", len(res.FilesChanged))
	}
}

func TestIterationHistoryAccumulated(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"a.py"}, "impl").onReviewer(1, false, false, "Fix A")
	b.onCoder(2, []string{"b.py"}, "impl").onReviewer(2, true, false, "LGTM")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if len(res.IterationHistory) != 2 {
		t.Fatalf("history len = %d, want 2", len(res.IterationHistory))
	}
	h1 := res.IterationHistory[0]
	if toInt(h1["iteration"]) != 1 || h1["action"] != "fix" || h1["path"] != "default" {
		t.Errorf("h1 = %v", h1)
	}
	if v, _ := h1["review_blocking"].(bool); v {
		t.Errorf("h1.review_blocking = true, want false")
	}
	h2 := res.IterationHistory[1]
	if toInt(h2["iteration"]) != 2 || h2["action"] != "approve" {
		t.Errorf("h2 = %v", h2)
	}
	if v, _ := h2["review_approved"].(bool); !v {
		t.Errorf("h2.review_approved = false, want true")
	}
}

func TestArtifactsSaved(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"x.py"}, "impl").onReviewer(1, true, false, "Good")
	artifactsDir := t.TempDir()
	run(t, makeIssue("ISSUE-1", false), makeDAGState(artifactsDir), b.build(), makeConfig(t, nil), nil)

	codingLoopDir := filepath.Join(artifactsDir, "coding-loop")
	subdirs, err := os.ReadDir(codingLoopDir)
	if err != nil || len(subdirs) < 1 {
		t.Fatalf("expected coding-loop subdirs at %s: err=%v", codingLoopDir, err)
	}
	firstIter := filepath.Join(codingLoopDir, subdirs[0].Name())
	for _, name := range []string{"coder.json", "review.json"} {
		if _, err := os.Stat(filepath.Join(firstIter, name)); err != nil {
			t.Errorf("missing artifact %s: %v", name, err)
		}
	}
}

func TestIterationCheckpointSaved(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"x.py"}, "impl").onReviewer(1, true, false, "Good")
	artifactsDir := t.TempDir()
	run(t, makeIssue("CHECKPOINT-1", false), makeDAGState(artifactsDir), b.build(), makeConfig(t, nil), nil)

	cp := filepath.Join(artifactsDir, "execution", "iterations", "CHECKPOINT-1.json")
	if _, err := os.Stat(cp); err != nil {
		t.Errorf("missing checkpoint %s: %v", cp, err)
	}
}

func TestFilesAccumulateAcrossIterations(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"a.py", "b.py"}, "impl").onReviewer(1, false, false, "Fix")
	b.onCoder(2, []string{"b.py", "c.py"}, "impl").onReviewer(2, true, false, "LGTM")

	res := run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	got := append([]string{}, res.FilesChanged...)
	sort.Strings(got)
	want := []string{"a.py", "b.py", "c.py"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("files_changed = %v, want %v", got, want)
	}
}

func TestNoteFnReceivesTags(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"x.py"}, "impl").onReviewer(1, true, false, "Good")
	nc := &noteCapture{}
	run(t, makeIssue("TAG-TEST", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nc.fn())

	all := map[string]bool{}
	for _, tags := range nc.tags {
		for _, tag := range tags {
			all[tag] = true
		}
	}
	for _, want := range []string{"TAG-TEST", "coding_loop", "start", "complete"} {
		if !all[want] {
			t.Errorf("missing note tag %q; got %v", want, all)
		}
	}
}

// ---------------------------------------------------------------------------
// Call-count contract: default = 2 role calls/iter, flagged = 4
// ---------------------------------------------------------------------------

func TestDefaultPathTwoCalls(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"x.py"}, "impl").onReviewer(1, true, false, "Good")
	run(t, makeIssue("ISSUE-1", false), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if got := len(b.targets); got != 2 {
		t.Fatalf("default path made %d calls, want 2 (%v)", got, b.targets)
	}
	if b.roleCount("run_coder") != 1 || b.roleCount("run_code_reviewer") != 1 {
		t.Errorf("expected 1 coder + 1 reviewer, got %v", b.targets)
	}
	if b.roleCount("run_qa") != 0 || b.roleCount("run_qa_synthesizer") != 0 {
		t.Errorf("default path must not call qa/synthesizer, got %v", b.targets)
	}
}

func TestFlaggedPathFourCalls(t *testing.T) {
	b := newBuilder()
	b.onCoder(1, []string{"x.py"}, "impl").onQA(1, true, "pass").onReviewer(1, true, false, "Good")
	b.onSynth(1, "approve", "ok", false)
	run(t, makeIssue("ISSUE-1", true), makeDAGState(t.TempDir()), b.build(), makeConfig(t, nil), nil)

	if got := len(b.targets); got != 4 {
		t.Fatalf("flagged path made %d calls, want 4 (%v)", got, b.targets)
	}
	for _, role := range []string{"run_coder", "run_qa", "run_code_reviewer", "run_qa_synthesizer"} {
		if b.roleCount(role) != 1 {
			t.Errorf("expected exactly 1 %s, got %v", role, b.targets)
		}
	}
}

// ---------------------------------------------------------------------------
// repo_name propagation (port of test_coding_loop_repo_name.py)
// ---------------------------------------------------------------------------

func TestRepoNamePropagation(t *testing.T) {
	cases := []struct {
		name     string
		coder    map[string]any
		wantRepo string
	}{
		{"present", map[string]any{"files_changed": []string{"src/main.py"}, "summary": "done", "complete": true, "repo_name": "api"}, "api"},
		{"empty", map[string]any{"files_changed": []string{}, "summary": "done", "complete": true, "repo_name": ""}, ""},
		{"absent", map[string]any{"files_changed": []string{}, "summary": "done", "complete": true}, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			coder := c.coder
			callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
				if strings.Contains(target, "run_coder") {
					return coder, nil
				}
				return map[string]any{"approved": true, "blocking": false, "summary": "ok"}, nil
			}
			issue := map[string]any{"name": "test-issue", "branch_name": "feat/test"}
			res := run(t, issue, makeDAGState(t.TempDir()), callFn, makeConfig(t, map[string]any{"max_coding_iterations": 1}), nil)
			if res.Outcome != schemas.IssueOutcomeCompleted {
				t.Fatalf("outcome = %s, want completed", res.Outcome)
			}
			if res.RepoName != c.wantRepo {
				t.Errorf("repo_name = %q, want %q", res.RepoName, c.wantRepo)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Regression tests (port of test_coding_loop_regressions.py)
// ---------------------------------------------------------------------------

func TestIgnoresLegacyIterationStateWhenBuildIDPresent(t *testing.T) {
	artifactsDir := t.TempDir()
	// Legacy state written WITHOUT a build_id path segment.
	legacyDir := filepath.Join(artifactsDir, "execution", "iterations")
	if err := os.MkdirAll(legacyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	legacy := `{"iteration":1,"feedback":"approved","files_changed":[],"iteration_history":[{"iteration":1,"action":"approve","summary":"legacy","path":"default"}]}`
	if err := os.WriteFile(filepath.Join(legacyDir, "create-hello-script.json"), []byte(legacy), 0o644); err != nil {
		t.Fatal(err)
	}

	ds := &schemas.DAGState{RepoPath: artifactsDir, ArtifactsDir: artifactsDir, BuildID: "newbuild1"}
	cfg := makeConfig(t, map[string]any{"max_coding_iterations": 1, "permission_mode": "bypassPermissions"})
	issue := map[string]any{"name": "create-hello-script", "guidance": map[string]any{"needs_deeper_qa": false}}

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		if strings.HasSuffix(target, ".run_coder") {
			return map[string]any{"files_changed": []any{}}, nil
		}
		if strings.HasSuffix(target, ".run_code_reviewer") {
			return map[string]any{"approved": true, "blocking": false, "summary": "ok", "debt_items": []any{}}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, issue, ds, callFn, cfg, nil)
	if res.Outcome != schemas.IssueOutcomeCompleted || res.Attempts != 1 {
		t.Fatalf("outcome=%s attempts=%d, want completed/1", res.Outcome, res.Attempts)
	}
	if len(res.IterationHistory) == 0 || res.IterationHistory[0]["summary"] != "ok" {
		t.Errorf("expected fresh run (summary 'ok'), got %v", res.IterationHistory)
	}
}

func TestPropagatesPermissionModeToAllAgents(t *testing.T) {
	b := newBuilder()
	cfg := makeConfig(t, map[string]any{"max_coding_iterations": 1, "permission_mode": "bypassPermissions"})
	issue := map[string]any{"name": "flagged-issue", "guidance": map[string]any{"needs_deeper_qa": true}}

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		b.mu.Lock()
		defer b.mu.Unlock()
		if pm, ok := kwargs["permission_mode"].(string); ok {
			b.permissionMap[lastSeg(target)] = pm
		}
		switch {
		case strings.HasSuffix(target, ".run_coder"):
			return map[string]any{"files_changed": []any{}}, nil
		case strings.HasSuffix(target, ".run_qa"):
			return map[string]any{"passed": true, "summary": "qa ok", "test_failures": []any{}}, nil
		case strings.HasSuffix(target, ".run_code_reviewer"):
			return map[string]any{"approved": true, "blocking": false, "summary": "review ok", "debt_items": []any{}}, nil
		case strings.HasSuffix(target, ".run_qa_synthesizer"):
			return map[string]any{"action": "approve", "summary": "approved", "stuck": false}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, issue, makeDAGState(t.TempDir()), callFn, cfg, nil)
	if res.Outcome != schemas.IssueOutcomeCompleted {
		t.Fatalf("outcome = %s, want completed", res.Outcome)
	}
	for _, role := range []string{"run_coder", "run_qa", "run_code_reviewer", "run_qa_synthesizer"} {
		if b.permissionMap[role] != "bypassPermissions" {
			t.Errorf("%s permission_mode = %q, want bypassPermissions", role, b.permissionMap[role])
		}
	}
}

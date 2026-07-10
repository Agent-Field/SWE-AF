package orch

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// Test harness: a name-dispatched, thread-safe mock App for the plan pipeline.
//
// Issue writers run concurrently (errgroup), so the mock records calls under a
// mutex and dispatches responses by reasoner name (the segment after the last
// ".") rather than by call order — parity with the Python test's happy-path
// mock, where the parallel gather makes writer ordering non-deterministic.
// ---------------------------------------------------------------------------

type planCall struct {
	name  string
	input map[string]any
}

type planMock struct {
	mu        sync.Mutex
	calls     []planCall
	responses map[string]func(input map[string]any) (map[string]any, error)
}

func (p *planMock) Call(_ context.Context, target string, input map[string]any) (map[string]any, error) {
	name := target
	if i := strings.LastIndex(target, "."); i >= 0 {
		name = target[i+1:]
	}
	p.mu.Lock()
	p.calls = append(p.calls, planCall{name: name, input: input})
	fn := p.responses[name]
	p.mu.Unlock()
	if fn == nil {
		return map[string]any{}, nil
	}
	return fn(input)
}

func (p *planMock) Note(context.Context, string, ...string) {}

func (p *planMock) callsFor(name string) []planCall {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []planCall
	for _, c := range p.calls {
		if c.name == name {
			out = append(out, c)
		}
	}
	return out
}

func constResp(m map[string]any) func(map[string]any) (map[string]any, error) {
	return func(map[string]any) (map[string]any, error) { return m, nil }
}

func validPRD() map[string]any {
	return map[string]any{
		"validated_description": "A test goal.",
		"acceptance_criteria":   []any{"AC-1: does something"},
		"must_have":             []any{"feature-x"},
		"nice_to_have":          []any{},
		"out_of_scope":          []any{},
		"assumptions":           []any{},
		"risks":                 []any{},
	}
}

func validArch() map[string]any {
	return map[string]any{
		"summary": "Simple architecture.",
		"components": []any{map[string]any{
			"name":           "component-a",
			"responsibility": "Does A",
			"touches_files":  []any{"a.py"},
			"depends_on":     []any{},
		}},
		"interfaces":            []any{"interface-1"},
		"decisions":             []any{map[string]any{"decision": "Use Python", "rationale": "It is available."}},
		"file_changes_overview": "Only a.py is changed.",
	}
}

func approvedReview() map[string]any {
	return map[string]any{
		"approved":              true,
		"feedback":              "Looks good.",
		"scope_issues":          []any{},
		"complexity_assessment": "appropriate",
		"summary":               "Architecture approved.",
	}
}

func rejectedReview() map[string]any {
	return map[string]any{
		"approved":              false,
		"feedback":              "Needs work.",
		"scope_issues":          []any{"scope-problem"},
		"complexity_assessment": "too_complex",
		"summary":               "Architecture rejected.",
	}
}

func issue(name string, deps []any, create []any) map[string]any {
	return map[string]any{
		"name":                 name,
		"title":                "Title " + name,
		"description":          "Do " + name,
		"acceptance_criteria":  []any{"AC"},
		"depends_on":           deps,
		"provides":             []any{},
		"estimated_complexity": "small",
		"files_to_create":      create,
		"files_to_modify":      []any{},
		"testing_strategy":     "",
		"sequence_number":      nil,
		"guidance":             nil,
	}
}

func sprintResult(issues ...map[string]any) map[string]any {
	list := make([]any, len(issues))
	for i, is := range issues {
		list[i] = is
	}
	return map[string]any{"issues": list, "rationale": "This is the rationale."}
}

// planApp builds Deps + mock with the standard happy-path responses; callers
// override individual reasoner responses before invoking Plan.
func planApp(sprint map[string]any) (*Deps, *planMock) {
	m := &planMock{responses: map[string]func(map[string]any) (map[string]any, error){
		"run_product_manager": constResp(validPRD()),
		"run_architect":       constResp(validArch()),
		"run_tech_lead":       constResp(approvedReview()),
		"run_sprint_planner":  constResp(sprint),
		"run_issue_writer":    constResp(map[string]any{"success": true, "path": "/tmp/x.md"}),
	}}
	return &Deps{App: m, NodeID: "swe-planner-go"}, m
}

func runPlan(t *testing.T, deps *Deps, repoPath string, extra map[string]any) (map[string]any, error) {
	t.Helper()
	in := map[string]any{
		"goal":                 "Build a test app",
		"repo_path":            repoPath,
		"artifacts_dir":        ".artifacts",
		"pm_model":             "sonnet",
		"architect_model":      "sonnet",
		"tech_lead_model":      "sonnet",
		"sprint_planner_model": "sonnet",
		"issue_writer_model":   "sonnet",
		"ai_provider":          "claude",
	}
	for k, v := range extra {
		in[k] = v
	}
	out, err := Plan(context.Background(), deps, in)
	if err != nil {
		return nil, err
	}
	m, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("Plan returned %T, want map[string]any", out)
	}
	return m, nil
}

// --- Contract: happy path returns the exact PlanResult key set ------------

func TestPlanHappyPathKeySet(t *testing.T) {
	deps, _ := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	res, err := runPlan(t, deps, t.TempDir(), nil)
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	want := []string{"prd", "architecture", "review", "issues", "levels",
		"file_conflicts", "artifacts_dir", "rationale"}
	for _, k := range want {
		if _, ok := res[k]; !ok {
			t.Errorf("PlanResult missing key %q", k)
		}
	}
	if len(res) != len(want) {
		t.Errorf("PlanResult has %d keys, want exactly %d: %v", len(res), len(want), keysOf(res))
	}
	if res["rationale"] != "This is the rationale." {
		t.Errorf("rationale = %v, want %q", res["rationale"], "This is the rationale.")
	}
}

// --- Contract: review loop iterates on rejection with feedback, bounded ----

func TestPlanReviewLoopBoundedAutoApprove(t *testing.T) {
	deps, m := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	m.responses["run_tech_lead"] = constResp(rejectedReview()) // always rejects

	res, err := runPlan(t, deps, t.TempDir(), map[string]any{"max_review_iterations": 1})
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}

	// Bounded: max_review_iterations+1 = 2 tech-lead passes.
	if got := len(m.callsFor("run_tech_lead")); got != 2 {
		t.Errorf("run_tech_lead called %d times, want 2 (bounded)", got)
	}
	// Auto-approval forces approved=true and annotates the summary.
	review, _ := res["review"].(map[string]any)
	if !asBool(review["approved"]) {
		t.Errorf("auto-approved review must have approved=true, got %v", review["approved"])
	}
	if !strings.Contains(strings.ToLower(mapStr(review, "summary", "")), "auto-approved") {
		t.Errorf("auto-approved summary must mention 'auto-approved', got %q", review["summary"])
	}
}

func TestPlanReviewRevisionFeedsBackFeedback(t *testing.T) {
	deps, m := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	m.responses["run_tech_lead"] = constResp(rejectedReview())

	if _, err := runPlan(t, deps, t.TempDir(), map[string]any{"max_review_iterations": 2}); err != nil {
		t.Fatalf("Plan: %v", err)
	}

	archCalls := m.callsFor("run_architect")
	// 1 initial + max_review_iterations(2) revisions = 3.
	if len(archCalls) != 3 {
		t.Fatalf("run_architect called %d times, want 3 (1 initial + 2 revisions)", len(archCalls))
	}
	// The initial call carries no feedback; every revision carries the tech
	// lead's feedback string.
	if _, ok := archCalls[0].input["feedback"]; ok {
		t.Errorf("initial architect call must not carry feedback")
	}
	for i := 1; i < len(archCalls); i++ {
		if fb, _ := archCalls[i].input["feedback"].(string); fb != "Needs work." {
			t.Errorf("revision %d feedback = %q, want %q", i, fb, "Needs work.")
		}
	}
}

func TestPlanReviewApprovesEarlyStopsLoop(t *testing.T) {
	deps, m := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	// approved on first pass (default response) → no revisions.
	if _, err := runPlan(t, deps, t.TempDir(), map[string]any{"max_review_iterations": 2}); err != nil {
		t.Fatalf("Plan: %v", err)
	}
	if got := len(m.callsFor("run_tech_lead")); got != 1 {
		t.Errorf("run_tech_lead called %d times, want 1 (approved early)", got)
	}
	if got := len(m.callsFor("run_architect")); got != 1 {
		t.Errorf("run_architect called %d times, want 1 (no revisions)", got)
	}
}

// --- Contract: levels computed; independent issues share level 0 -----------

func TestPlanLevelsParallelIndependentIssues(t *testing.T) {
	deps, _ := planApp(sprintResult(
		issue("issue-alpha", nil, []any{"alpha.py"}),
		issue("issue-beta", nil, []any{"beta.py"}),
	))
	res, err := runPlan(t, deps, t.TempDir(), nil)
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	// dumpToMap JSON-round-trips, so levels arrives as []any of []any.
	levels := levelsFromResult(res["levels"])
	if len(levels) != 1 {
		t.Fatalf("expected 1 level for two independent issues, got %d: %v", len(levels), levels)
	}
	if !strContains(levels[0], "issue-alpha") || !strContains(levels[0], "issue-beta") {
		t.Errorf("both issues must be in level 0, got %v", levels[0])
	}
}

func levelsFromResult(v any) [][]string {
	outer, _ := v.([]any)
	out := make([][]string, 0, len(outer))
	for _, lvl := range outer {
		out = append(out, asStrList(lvl))
	}
	return out
}

func strContains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

// --- Contract: dependency cycle propagates as an error --------------------

func TestPlanCycleErrorPropagates(t *testing.T) {
	deps, _ := planApp(sprintResult(
		issue("a", []any{"b"}, []any{"a.py"}),
		issue("b", []any{"a"}, []any{"b.py"}),
	))
	_, err := runPlan(t, deps, t.TempDir(), nil)
	if err == nil {
		t.Fatal("expected a cycle error, got nil")
	}
	if !strings.Contains(err.Error(), "Dependency cycle detected") {
		t.Errorf("error = %q, want a 'Dependency cycle detected' message", err.Error())
	}
}

// --- Contract: malformed PM result surfaces an error at assembly ----------

func TestPlanMalformedPMErrors(t *testing.T) {
	deps, m := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	// PM returns a dict lacking required PRD fields → PlanResult validation fails.
	m.responses["run_product_manager"] = constResp(map[string]any{"not_a_valid_field": "oops"})

	_, err := runPlan(t, deps, t.TempDir(), nil)
	if err == nil {
		t.Fatal("expected an error for a malformed PM result, got nil")
	}
	// The whole pipeline still ran before failing (parity: fails at assembly).
	if got := len(m.callsFor("run_sprint_planner")); got != 1 {
		t.Errorf("sprint planner should still have run once before the assembly error, got %d", got)
	}
}

// --- Contract: issue writers run for every issue; failures tolerated -------

func TestPlanIssueWritersRunPerIssueAndTolerateFailure(t *testing.T) {
	deps, m := planApp(sprintResult(
		issue("issue-alpha", nil, []any{"alpha.py"}),
		issue("issue-beta", nil, []any{"beta.py"}),
		issue("issue-gamma", nil, []any{"gamma.py"}),
	))
	// alpha succeeds; beta returns success=false; gamma errors. Plan must still
	// succeed (return_exceptions=True parity).
	m.responses["run_issue_writer"] = func(in map[string]any) (map[string]any, error) {
		is, _ := in["issue"].(map[string]any)
		switch mapStr(is, "name", "") {
		case "issue-beta":
			return map[string]any{"success": false}, nil
		case "issue-gamma":
			return nil, errors.New("writer blew up")
		default:
			return map[string]any{"success": true, "path": "/tmp/a.md"}, nil
		}
	}

	res, err := runPlan(t, deps, t.TempDir(), nil)
	if err != nil {
		t.Fatalf("Plan must tolerate writer failures, got: %v", err)
	}
	// One writer call per issue.
	if got := len(m.callsFor("run_issue_writer")); got != 3 {
		t.Errorf("run_issue_writer called %d times, want 3 (one per issue)", got)
	}
	// All three issues present in the result regardless of writer outcome.
	issues, _ := res["issues"].([]any)
	if len(issues) != 3 {
		t.Errorf("result issues = %d, want 3", len(issues))
	}
}

func TestPlanIssueWriterSiblingsExcludeSelf(t *testing.T) {
	deps, m := planApp(sprintResult(
		issue("issue-alpha", nil, []any{"alpha.py"}),
		issue("issue-beta", nil, []any{"beta.py"}),
	))
	if _, err := runPlan(t, deps, t.TempDir(), nil); err != nil {
		t.Fatalf("Plan: %v", err)
	}
	for _, c := range m.callsFor("run_issue_writer") {
		is, _ := c.input["issue"].(map[string]any)
		self := mapStr(is, "name", "")
		sibs := asMapList(c.input["sibling_issues"])
		if len(sibs) != 1 {
			t.Fatalf("writer for %s: expected 1 sibling, got %d", self, len(sibs))
		}
		if mapStr(sibs[0], "name", "") == self {
			t.Errorf("writer for %s: sibling list must exclude self", self)
		}
	}
}

// --- Contract: artifacts written at the exact paths -----------------------

func TestPlanWritesArtifactsAtExactPaths(t *testing.T) {
	repo := t.TempDir()
	deps, _ := planApp(sprintResult(issue("my-issue", nil, []any{"thing.py"})))
	if _, err := runPlan(t, deps, repo, nil); err != nil {
		t.Fatalf("Plan: %v", err)
	}
	base := filepath.Join(repo, ".artifacts")

	// rationale.md at <abs repo>/.artifacts/rationale.md with the exact content.
	rationale, err := os.ReadFile(filepath.Join(base, "rationale.md"))
	if err != nil {
		t.Fatalf("rationale.md not written: %v", err)
	}
	if string(rationale) != "This is the rationale." {
		t.Errorf("rationale.md = %q, want %q", string(rationale), "This is the rationale.")
	}

	// The plan/issues directory is created.
	if fi, err := os.Stat(filepath.Join(base, "plan", "issues")); err != nil || !fi.IsDir() {
		t.Errorf("plan/issues dir not created: err=%v", err)
	}
}

// --- Contract: env-resolved provider/model defaults (OpenRouter-only) ------

func TestPlanOpenRouterOnlyDefaults(t *testing.T) {
	for _, k := range []string{"ANTHROPIC_API_KEY", "SWE_DEFAULT_RUNTIME",
		"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"} {
		t.Setenv(k, "")
	}
	t.Setenv("OPENROUTER_API_KEY", "sk-or-test")

	m := &planMock{responses: map[string]func(map[string]any) (map[string]any, error){
		"run_product_manager": constResp(validPRD()),
		"run_architect":       constResp(validArch()),
		"run_tech_lead":       constResp(approvedReview()),
		"run_sprint_planner":  constResp(sprintResult(issue("my-issue", nil, []any{"thing.py"}))),
		"run_issue_writer":    constResp(map[string]any{"success": true}),
	}}
	deps := &Deps{App: m, NodeID: "swe-planner-go"}

	// Omit ai_provider/*_model so env resolution runs.
	if _, err := Plan(context.Background(), deps, map[string]any{
		"goal": "Build a test app", "repo_path": t.TempDir(),
	}); err != nil {
		t.Fatalf("Plan: %v", err)
	}

	pm := m.callsFor("run_product_manager")
	if len(pm) != 1 {
		t.Fatalf("expected 1 PM call, got %d", len(pm))
	}
	if got := mapStr(pm[0].input, "ai_provider", ""); got != "open_code" {
		t.Errorf("ai_provider = %q, want open_code", got)
	}
	if got := mapStr(pm[0].input, "model", ""); got != "openrouter/deepseek/deepseek-v4-flash" {
		t.Errorf("model = %q, want the OpenRouter auto default", got)
	}
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

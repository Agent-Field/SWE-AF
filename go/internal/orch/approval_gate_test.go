package orch

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
)

// --- test doubles ---------------------------------------------------------

// fakePauser returns a scripted decision per Pause call, one per revision
// iteration. It counts calls; feedback rides on ApprovalResult.Feedback (a
// top-level field, as the webhook delivers it) rather than inside a response
// payload.
type fakePauser struct {
	decisions []string
	feedbacks []string
	idx       int
	calls     int
}

func (f *fakePauser) Pause(_ context.Context, _ agent.PauseOptions) (*agent.ApprovalResult, error) {
	i := f.idx
	if i >= len(f.decisions) {
		i = len(f.decisions) - 1
	}
	f.idx++
	f.calls++
	var feedback string
	if i < len(f.feedbacks) {
		feedback = f.feedbacks[i]
	}
	return &agent.ApprovalResult{Decision: f.decisions[i], Feedback: feedback}, nil
}

// haxStub returns an httptest server answering the create-request POST with a
// fresh {id,url}, counting hits.
func haxStub(t *testing.T, hits *int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		n := atomic.AddInt32(hits, 1)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":  "req-" + itoa(int(n)),
			"url": "https://hax.example/r/" + itoa(int(n)),
		})
	}))
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

// wireHax points haxClientProvider at a stub server (short timeout) and returns
// a restore func. pauserProvider is wired to the given fake.
func wireHax(t *testing.T, server *httptest.Server, pauser hitl.Pauser) func() {
	t.Helper()
	prevHax := haxClientProvider
	prevPauser := pauserProvider
	if server != nil {
		haxClientProvider = func() *hitl.HaxClient {
			return &hitl.HaxClient{BaseURL: server.URL, APIKey: "k", Timeout: 2 * time.Second}
		}
	} else {
		haxClientProvider = func() *hitl.HaxClient { return nil }
	}
	pauserProvider = func(ApprovalRequest) hitl.Pauser { return pauser }
	return func() {
		haxClientProvider = prevHax
		pauserProvider = prevPauser
	}
}

// testCfg builds a BuildConfig via the real loader (so AIProvider/defaults are
// valid), overriding max_plan_revision_iterations.
func testCfg(t *testing.T, maxRev int) *config.BuildConfig {
	t.Helper()
	cfg, err := config.LoadBuildConfig(map[string]any{
		"repo_url":                     "https://github.com/o/r.git",
		"max_plan_revision_iterations": maxRev,
	})
	if err != nil {
		t.Fatalf("LoadBuildConfig: %v", err)
	}
	return cfg
}

func samplePlan() map[string]any {
	return map[string]any{
		"prd":          map[string]any{"validated_description": "d"},
		"architecture": map[string]any{"summary": "s"},
		"rationale":    "original rationale",
		"issues":       []any{map[string]any{"name": "i1", "title": "t1"}},
	}
}

// replanApp records call inputs and returns approved tech-lead + a revised plan.
func replanApp() (*mockApp, *[]map[string]any) {
	var calls []map[string]any
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		calls = append(calls, map[string]any{"target": target, "input": input})
		switch {
		case strings.HasSuffix(target, ".run_architect"):
			return map[string]any{"summary": "revised arch"}, nil
		case strings.HasSuffix(target, ".run_tech_lead"):
			return map[string]any{"approved": true, "feedback": "", "summary": "tl ok"}, nil
		case strings.HasSuffix(target, ".run_sprint_planner"):
			return map[string]any{
				"issues":    []any{map[string]any{"name": "i2"}},
				"rationale": "revised rationale",
			}, nil
		default:
			return map[string]any{}, nil
		}
	}}
	return app, &calls
}

func req(deps *Deps, cfg *config.BuildConfig, plan map[string]any, artifactsDir string) ApprovalRequest {
	return ApprovalRequest{
		Deps: deps, Cfg: cfg, Resolved: map[string]string{
			"architect_model": "sonnet", "tech_lead_model": "sonnet", "sprint_planner_model": "sonnet",
		},
		PlanResult: plan, RepoPath: "/repo", Goal: "g",
		ArtifactsDir: ".artifacts", AbsArtifactsDir: artifactsDir, ExecutionID: "exec-1",
	}
}

// --- contract tests -------------------------------------------------------

// approved -> non-terminal outcome, PlanResult unchanged, no replan.
func TestApprovalApprovedProceeds(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	fake := &fakePauser{decisions: []string{"approved"}}
	defer wireHax(t, server, fake)()

	app, calls := replanApp()
	deps := &Deps{App: app, NodeID: "swe-planner"}
	plan := samplePlan()

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), plan, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if out.Terminal {
		t.Fatal("approved must be non-terminal")
	}
	if !reflect.DeepEqual(out.PlanResult, plan) {
		t.Fatalf("PlanResult changed on approve: %v", out.PlanResult)
	}
	if len(*calls) != 0 {
		t.Fatalf("approve must not replan, got %d calls", len(*calls))
	}
	if fake.calls != 1 {
		t.Fatalf("expected one pause, got %d", fake.calls)
	}
	if hits != 1 {
		t.Fatalf("expected 1 hax request, got %d", hits)
	}
}

// request_changes then approved -> replan invoked with feedback, bounded,
// PlanResult revised.
func TestApprovalChangesThenApproved(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	fake := &fakePauser{
		decisions: []string{"request_changes", "approved"},
		feedbacks: []string{"please fix X", ""},
	}
	defer wireHax(t, server, fake)()

	app, calls := replanApp()
	deps := &Deps{App: app, NodeID: "swe-planner"}

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), samplePlan(), t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if out.Terminal {
		t.Fatal("changes->approved must be non-terminal")
	}
	// Revised plan carries sprint planner output.
	if got := mapStr(out.PlanResult, "rationale", ""); got != "revised rationale" {
		t.Fatalf("rationale not revised: %q", got)
	}
	// The first architect call must carry the reviewer feedback verbatim.
	var archFeedback string
	for _, c := range *calls {
		if strings.HasSuffix(c["target"].(string), ".run_architect") {
			archFeedback, _ = c["input"].(map[string]any)["feedback"].(string)
			break
		}
	}
	if archFeedback != "please fix X" {
		t.Fatalf("architect feedback = %q, want reviewer feedback", archFeedback)
	}
	if hits != 2 {
		t.Fatalf("expected 2 hax requests (initial + revision), got %d", hits)
	}
}

// request_changes beyond the limit -> Terminal with the revision-limit
// BuildResult (verbatim summary + keys).
func TestApprovalRevisionLimit(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	fake := &fakePauser{
		decisions: []string{"request_changes", "request_changes"},
		feedbacks: []string{"f0", "f1"},
	}
	defer wireHax(t, server, fake)()

	app, _ := replanApp()
	deps := &Deps{App: app, NodeID: "swe-planner"}

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 1), samplePlan(), t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if !out.Terminal {
		t.Fatal("revision limit must be terminal")
	}
	if asBool(out.Result["success"]) {
		t.Fatal("terminal result success must be false")
	}
	if got := mapStr(out.Result, "summary", ""); got != "Plan revision limit reached after 2 iterations" {
		t.Fatalf("summary = %q", got)
	}
	for _, k := range []string{"plan_result", "dag_state", "success", "summary"} {
		if _, ok := out.Result[k]; !ok {
			t.Fatalf("terminal BuildResult missing key %q", k)
		}
	}
	if ds, ok := out.Result["dag_state"].(map[string]any); !ok || len(ds) != 0 {
		t.Fatalf("dag_state must be empty map, got %v", out.Result["dag_state"])
	}
}

// rejected -> Terminal failure BuildResult, summary "Plan rejected: <reason>".
func TestApprovalRejected(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	fake := &fakePauser{
		decisions: []string{"rejected"},
		feedbacks: []string{"not good"},
	}
	defer wireHax(t, server, fake)()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		t.Fatal("rejected must not replan")
		return nil, nil
	}}, NodeID: "swe-planner"}

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), samplePlan(), t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if !out.Terminal || asBool(out.Result["success"]) {
		t.Fatal("rejected must be terminal failure")
	}
	if got := mapStr(out.Result, "summary", ""); got != "Plan rejected: not good" {
		t.Fatalf("summary = %q", got)
	}
}

// expired -> Terminal timeout shape, summary "Plan expired: expired" (no
// feedback -> reason is the decision).
func TestApprovalExpired(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	fake := &fakePauser{decisions: []string{"expired"}}
	defer wireHax(t, server, fake)()

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner"}

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), samplePlan(), t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if !out.Terminal || asBool(out.Result["success"]) {
		t.Fatal("expired must be terminal failure")
	}
	if got := mapStr(out.Result, "summary", ""); got != "Plan expired: expired" {
		t.Fatalf("summary = %q", got)
	}
}

// no hax client (HAX_API_KEY unset) -> gate skipped, PlanResult unchanged, no
// hax hit, no approval calls.
func TestApprovalNoHaxClientSkips(t *testing.T) {
	fake := &fakePauser{decisions: []string{"approved"}}
	defer wireHax(t, nil, fake)() // nil server => haxClientProvider returns nil

	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner"}
	plan := samplePlan()

	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), plan, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if out.Terminal {
		t.Fatal("no hax client must be a non-terminal skip")
	}
	if !reflect.DeepEqual(out.PlanResult, plan) {
		t.Fatal("PlanResult must be unchanged when gate skipped")
	}
	if fake.calls != 0 {
		t.Fatal("no pause when hax disabled")
	}
}

// no pauser wired -> gate skipped even with a hax client.
func TestApprovalNoClientWiredSkips(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()

	prevHax := haxClientProvider
	prevPauser := pauserProvider
	defer func() { haxClientProvider = prevHax; pauserProvider = prevPauser }()
	haxClientProvider = func() *hitl.HaxClient {
		return &hitl.HaxClient{BaseURL: server.URL, APIKey: "k", Timeout: time.Second}
	}
	pauserProvider = nil

	deps := &Deps{App: &mockApp{}, NodeID: "swe-planner"}
	plan := samplePlan()
	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), plan, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if out.Terminal || !reflect.DeepEqual(out.PlanResult, plan) {
		t.Fatal("unwired pauser must skip, unchanged plan")
	}
	if hits != 0 {
		t.Fatal("no hax request when pauser unwired")
	}
}

// _format_plan_for_approval: verbatim markdown + camelCase issue keys.
func TestFormatPlanForApproval(t *testing.T) {
	plan := map[string]any{
		"rationale": "the plan",
		"prd": map[string]any{
			"validated_description": "Build it",
			"must_have":             []any{"a", "b"},
			"acceptance_criteria":   []any{"ac1"},
		},
		"architecture": map[string]any{
			"summary": "arch summary",
			"components": []any{
				map[string]any{"name": "C1", "responsibility": "does x", "touches_files": []any{"f.go"}},
			},
			"decisions": []any{map[string]any{"decision": "use Go", "rationale": "speed"}},
		},
		"issues": []any{
			map[string]any{"name": "n1", "title": "T", "description": "D", "depends_on": []any{"n0"}},
		},
	}
	summary, prd, arch, issues := formatPlanForApproval(plan)
	if summary != "the plan" {
		t.Fatalf("summary = %q", summary)
	}
	if prd != "## Description\nBuild it\n\n## Must Have\n- a\n- b\n\n## Acceptance Criteria\n- ac1" {
		t.Fatalf("prd markdown mismatch:\n%q", prd)
	}
	if !strings.Contains(arch, "## Summary\narch summary") ||
		!strings.Contains(arch, "### C1\ndoes x") ||
		!strings.Contains(arch, "Files: `f.go`") ||
		!strings.Contains(arch, "- **use Go**: speed") {
		t.Fatalf("arch markdown mismatch:\n%q", arch)
	}
	if len(issues) != 1 {
		t.Fatalf("expected 1 issue, got %d", len(issues))
	}
	i0 := issues[0]
	if i0["name"] != "n1" || i0["title"] != "T" || i0["description"] != "D" {
		t.Fatalf("issue base fields wrong: %v", i0)
	}
	if dep, ok := i0["dependsOn"].([]any); !ok || len(dep) != 1 || dep[0] != "n0" {
		t.Fatalf("dependsOn mapping wrong: %v", i0["dependsOn"])
	}
	// Absent list fields default to empty list (Python .get(k, [])).
	if fm, ok := i0["filesToModify"].([]any); !ok || len(fm) != 0 {
		t.Fatalf("filesToModify default wrong: %v", i0["filesToModify"])
	}
}

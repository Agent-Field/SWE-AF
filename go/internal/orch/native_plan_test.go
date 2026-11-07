package orch

import (
	"context"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
)

// recordingPauser answers each Pause from a script and keeps every option set
// it was given, so a test can read what a person would have been shown.
type recordingPauser struct {
	answers []agent.ApprovalResult
	seen    []agent.PauseOptions
}

func (r *recordingPauser) Pause(_ context.Context, opts agent.PauseOptions) (*agent.ApprovalResult, error) {
	r.seen = append(r.seen, opts)
	i := len(r.seen) - 1
	if i >= len(r.answers) {
		i = len(r.answers) - 1
	}
	res := r.answers[i]
	return &res, nil
}

func richPlan() map[string]any {
	return map[string]any{
		"rationale": "One small change; the ledger already tracks entries.",
		"prd": map[string]any{
			"validated_description": "Ledger gains Len() returning the number of entries.",
			"must_have":             []any{"Len() int on *Ledger"},
			"acceptance_criteria":   []any{"go test ./... passes", "Len() of an empty ledger is 0"},
			"out_of_scope":          []any{"Changing Add"},
		},
		"architecture": map[string]any{
			"summary":    "Add one method beside Add.",
			"components": []any{map[string]any{"name": "ledger", "responsibility": "entries", "touches_files": []any{"ledger.go"}}},
			"decisions":  []any{map[string]any{"decision": "No new type", "rationale": "Keep it small"}},
		},
		"issues": []any{
			map[string]any{"name": "add-len-tests", "title": "Test Len", "description": "Table tests.",
				"files_to_create": []any{"ledger_len_test.go"}, "depends_on": []any{"add-len"},
				"acceptance_criteria": []any{"tests cover 0, 1 and 3 entries"}},
			map[string]any{"name": "add-len", "title": "Add Len()", "description": "Return len(l.entries).",
				"files_to_modify": []any{"ledger.go"}, "testing_strategy": "unit tests",
				"acceptance_criteria": []any{"Len() returns the entry count"}},
		},
		"levels": []any{[]any{"add-len"}, []any{"add-len-tests"}},
	}
}

// Without hax the gate pauses on the control plane with the whole plan, feeds
// the person's words back into planning on request_changes, asks again with a
// fresh id, and proceeds on approve.
func TestApprovalWithoutHaxAsksOnTheControlPlane(t *testing.T) {
	p := &recordingPauser{answers: []agent.ApprovalResult{
		{Decision: "request_changes", Feedback: "also document Len in the README"},
		{Decision: "approved"},
	}}
	defer wireHax(t, nil, p)()
	app, calls := replanApp()
	deps := &Deps{App: app, NodeID: "swe-planner"}
	r := req(deps, testCfg(t, 2), richPlan(), t.TempDir())
	r.ReviewTitle = "#7: Add a Len() method to Ledger"

	out, err := PlanApprovalGate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if out.Terminal {
		t.Fatalf("approved after changes must proceed, got terminal %v", out.Result)
	}
	if len(p.seen) != 2 {
		t.Fatalf("paused %d times, want 2", len(p.seen))
	}
	first, second := p.seen[0], p.seen[1]
	if first.Kind != hitl.KindPlanReview || first.Question != "Approve the plan for #7: Add a Len() method to Ledger?" {
		t.Fatalf("first question = %q / %q", first.Kind, first.Question)
	}
	if first.ApprovalRequestID != "swe-plan-exec-1-r1" || second.ApprovalRequestID != "swe-plan-exec-1-r2" {
		t.Fatalf("ids = %q, %q; every round needs its own", first.ApprovalRequestID, second.ApprovalRequestID)
	}
	if first.ApprovalRequestURL != "" {
		t.Fatalf("no hax means no URL, got %q", first.ApprovalRequestURL)
	}
	for _, want := range []string{
		"# Plan: #7: Add a Len() method to Ledger", "## Summary", "## Details", "### Goal", "Ledger gains Len()",
		"### How you will know it is done", "Len() of an empty ledger is 0",
		"### Planned changes (2)", "#### 1. Add Len()", "#### 2. Test Len", "`ledger.go`",
		"`ledger_len_test.go`", "Left out on purpose", "No new type",
	} {
		if !strings.Contains(first.Body, want) {
			t.Errorf("plan body is missing %q:\n%s", want, first.Body)
		}
	}
	if strings.Join(first.Options, ",") != "approve,request_changes,reject" {
		t.Fatalf("options = %v", first.Options)
	}
	if !strings.Contains(second.Question, "revised after your first note") ||
		!strings.Contains(second.Body, "> also document Len in the README") {
		t.Fatalf("second round must say it is revised and quote the words:\n%s\n%s", second.Question, second.Body)
	}
	var architectFeedback any
	for _, c := range *calls {
		if strings.HasSuffix(c["target"].(string), ".run_architect") {
			architectFeedback = c["input"].(map[string]any)["feedback"]
		}
	}
	if architectFeedback != "also document Len in the README" {
		t.Fatalf("the architect got feedback %v, want the person's words", architectFeedback)
	}
	// The revised plan is a whole new plan: issue files rewritten, levels
	// recomputed, and the words an acceptance check the verifier reads.
	wroteIssues := false
	for _, c := range *calls {
		if strings.HasSuffix(c["target"].(string), ".run_issue_writer") {
			wroteIssues = true
		}
	}
	pmHeard := false
	for _, c := range *calls {
		if strings.HasSuffix(c["target"].(string), ".run_product_manager") &&
			strings.Contains(c["input"].(map[string]any)["additional_context"].(string), "also document Len in the README") {
			pmHeard = true
		}
	}
	if !pmHeard {
		t.Fatal("a revised plan must rebuild its PRD from the person's words")
	}
	for _, gone := range []string{"## Your answer", "## Why this plan"} {
		if strings.Contains(first.Body, gone) {
			t.Errorf("plan body still has %q; the door draws its own answers and planner reasoning is not for the person", gone)
		}
	}
	if !wroteIssues {
		t.Fatal("a revised plan must rewrite its issue files")
	}
	if got := out.PlanResult["levels"]; len(got.([][]string)) != 1 || got.([][]string)[0][0] != "i2" {
		t.Fatalf("levels = %v, want the revised plan's", got)
	}
	ac := coerceAnyList(out.PlanResult["prd"].(map[string]any)["acceptance_criteria"])
	if len(ac) == 0 || !strings.Contains(ac[len(ac)-1].(string), "also document Len in the README") {
		t.Fatalf("PRD acceptance = %v", ac)
	}
}

// Reject stops before any code with the person's words in the summary.
func TestApprovalWithoutHaxRejectStops(t *testing.T) {
	p := &recordingPauser{answers: []agent.ApprovalResult{{Decision: "rejected", Feedback: "not now"}}}
	defer wireHax(t, nil, p)()
	deps := &Deps{App: &mockApp{}, NodeID: "swe-planner"}
	out, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), richPlan(), t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	if !out.Terminal || out.Result["summary"] != "Plan rejected: not now" || out.Result["success"] != false {
		t.Fatalf("reject must stop cleanly, got %+v", out)
	}
}

// With hax the same round carries hax's id and URL as well as the plan.
func TestApprovalWithHaxAlsoCarriesThePlan(t *testing.T) {
	var hits int32
	server := haxStub(t, &hits)
	defer server.Close()
	p := &recordingPauser{answers: []agent.ApprovalResult{{Decision: "approved"}}}
	defer wireHax(t, server, p)()
	deps := &Deps{App: &mockApp{}, NodeID: "swe-planner"}
	if _, err := PlanApprovalGate(context.Background(), req(deps, testCfg(t, 2), richPlan(), t.TempDir())); err != nil {
		t.Fatal(err)
	}
	got := p.seen[0]
	if got.ApprovalRequestID != "req-1" || got.ApprovalRequestURL == "" || !strings.Contains(got.Body, "## Planned changes") {
		t.Fatalf("hax round = %+v", got)
	}
}

func TestBuildIssueInput(t *testing.T) {
	in, err := decodeBuildIssue(map[string]any{
		"title": " Add a Len() method to Ledger ", "body": "Callers count entries by hand.",
		"repo_url": "https://github.com/santoshkumarradha/factory-demo", "base_branch": "main",
		"issue_number": 7, "issue_url": "https://github.com/santoshkumarradha/factory-demo/issues/7",
	})
	if err != nil {
		t.Fatal(err)
	}
	b := in.buildInput()
	goal := b["goal"].(string)
	if !strings.HasPrefix(goal, "Resolve GitHub issue #7: Add a Len() method to Ledger") ||
		!strings.Contains(goal, "Callers count entries by hand.") || !strings.Contains(goal, `"Closes #7"`) {
		t.Fatalf("goal = %q", goal)
	}
	if b["plan_review"] != true || b["review_title"] != "#7: Add a Len() method to Ledger" {
		t.Fatalf("review = %v %v", b["plan_review"], b["review_title"])
	}
	if b["config"].(map[string]any)["github_pr_base"] != "main" || b["repo_url"] == nil {
		t.Fatalf("build input = %v", b)
	}
	for _, n := range []any{"7", "#7", 7.0} {
		in, err := decodeBuildIssue(map[string]any{"title": "t", "repo_url": "u", "issue_number": n})
		if err != nil || in.IssueNumber != 7 {
			t.Fatalf("issue_number %v → %v, %v", n, in.IssueNumber, err)
		}
	}
	if _, err := decodeBuildIssue(map[string]any{"title": "t", "repo_url": "u", "issue_number": "seven"}); err == nil {
		t.Fatal("a non-number issue_number must be refused")
	}
	if _, err := decodeBuildIssue(map[string]any{"title": "x"}); err == nil {
		t.Fatal("no repo must be refused")
	}
	if _, err := decodeBuildIssue(map[string]any{"repo_url": "u"}); err == nil {
		t.Fatal("no title must be refused")
	}
}

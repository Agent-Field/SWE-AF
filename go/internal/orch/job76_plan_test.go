package orch

import (
	"context"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// The cases below are job 76 of the second 2026-11-07 live run (issue #205,
// "Add a Refunds() method to Ledger"):
// ~/factory-e2e/workaf-ab-r2/jobs/6-76-plan-r{1,2}.md. The plan the person
// sent back added three tests; their note named "the test", and the revision
// came back with a fourth beside the three.
const job76Note = "Also list Refunds() in the README API sentence beside New, Add, Entries and Total, and name the test TestRefundsSumsAbsoluteValues."

var (
	job76Before = []string{"TestRefunds", "TestRefundsEmpty", "TestRefundsNone"}
	job76Files  = []any{
		map[string]any{"path": "ledger.go", "purpose": "the new Refunds method"},
		map[string]any{"path": "README.md", "purpose": "the API sentence naming Refunds"},
		map[string]any{"path": "ledger_test.go", "purpose": "the refunds tests"},
	}
)

func job76Plan(tests ...string) map[string]any {
	var named []any
	for _, n := range tests {
		named = append(named, n)
	}
	return job70Plan(named, job76Files)
}

// S1: of three tests, the name is the one it extends — TestRefunds, the
// -300 + -200 = 500 case that "sums absolute values" — and the gate says so.
func TestJob76NamingTheTestRenamesTheOneItExtends(t *testing.T) {
	got := readRenames(job76Note, job76Before)
	if len(got) != 1 || got[0].From != "TestRefunds" || got[0].To != "TestRefundsSumsAbsoluteValues" {
		t.Fatalf("renames = %+v", got)
	}
	if r := renameReading(job76Note, job76Before); !strings.Contains(r, "rename TestRefunds to TestRefundsSumsAbsoluteValues") {
		t.Fatalf("the planners are not told it is a rename: %q", r)
	}
	reqs := readReviewRequests(job76Note)
	check := func(tests ...string) string {
		return strings.Join(revisionChecks(job76Plan(tests...), reqs, job76Note, job76Before), "\n")
	}

	// Round 2 as it happened: the fourth test beside the three is sent back.
	if p := check("TestRefunds", "TestRefundsEmpty", "TestRefundsNone", "TestRefundsSumsAbsoluteValues"); !strings.Contains(p, "still adds TestRefunds beside it") {
		t.Fatalf("the duplicate is not sent back: %q", p)
	}
	// Round 1 as it happened: everything collapsed into the new name. The
	// renamed test is excused, the two the person never touched are not.
	p := check("TestRefundsSumsAbsoluteValues")
	for _, kept := range []string{"no longer adds TestRefundsEmpty", "no longer adds TestRefundsNone"} {
		if !strings.Contains(p, kept) {
			t.Errorf("dropping a test the person did not rename passes (%s): %q", kept, p)
		}
	}
	if strings.Contains(p, "no longer adds TestRefunds,") {
		t.Errorf("the renamed test is reported as dropped: %q", p)
	}
	// The rename done: nothing is sent back.
	if p := check("TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone"); p != "" {
		t.Fatalf("the renamed plan is sent back: %q", p)
	}
}

// S1: when the name extends none of the tests, which one is meant cannot be
// told. Any one of them may take it; the name added beside all is refused.
func TestJob76ANameNoTestExtendsRenamesOneAndAddsNone(t *testing.T) {
	note := "Also add Refunds() to the README, and name the test TestRefundsAreAbsolute."
	before := []string{"TestRefundsTwo", "TestRefundsEmpty", "TestRefundsNone"}
	got := readRenames(note, before)
	if len(got) != 1 || got[0].From != "" || len(got[0].Among) != 3 {
		t.Fatalf("renames = %+v", got)
	}
	if r := renameReading(note, before); !strings.Contains(r, "ONE of TestRefundsTwo, TestRefundsEmpty, TestRefundsNone") || !strings.Contains(r, "keep the same number of tests") {
		t.Fatalf("the planners are not told to rename one: %q", r)
	}
	reqs := readReviewRequests(note)
	check := func(tests ...string) string {
		return strings.Join(revisionChecks(job76Plan(tests...), reqs, note, before), "\n")
	}
	if p := check("TestRefundsTwo", "TestRefundsEmpty", "TestRefundsNone", "TestRefundsAreAbsolute"); !strings.Contains(p, "not to add one") {
		t.Fatalf("the name added beside all three passes: %q", p)
	}
	if p := check("TestRefundsAreAbsolute", "TestRefundsEmpty", "TestRefundsNone"); p != "" {
		t.Fatalf("one test renamed is sent back: %q", p)
	}
	if p := check("TestRefundsAreAbsolute", "TestRefundsNone"); !strings.Contains(p, "no longer adds TestRefundsEmpty") {
		t.Fatalf("a second test dropped passes: %q", p)
	}
}

// S1 through the gate: the architect is told which test is renamed, and a
// revision with the fourth test is re-planned once more.
func TestJob76TheGateRenamesInsteadOfAddingAFourthTest(t *testing.T) {
	p := &recordingPauser{answers: []agent.ApprovalResult{
		{Decision: "request_changes", Feedback: job76Note},
		{Decision: "approved"},
	}}
	defer wireHax(t, nil, p)()
	var architectFeedback []string
	sprint := 0
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".run_product_manager"):
			return job76Plan("TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone")["prd"].(map[string]any), nil
		case strings.HasSuffix(target, ".run_architect"):
			architectFeedback = append(architectFeedback, input["feedback"].(string))
			return map[string]any{"summary": "One method."}, nil
		case strings.HasSuffix(target, ".run_tech_lead"):
			return map[string]any{"approved": true}, nil
		case strings.HasSuffix(target, ".run_sprint_planner"):
			// First the live run's fourth test, then the rename.
			sprint++
			plan := job76Plan("TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone")
			if sprint == 1 {
				plan = job76Plan("TestRefunds", "TestRefundsEmpty", "TestRefundsNone", "TestRefundsSumsAbsoluteValues")
			}
			return map[string]any{"issues": plan["issues"], "approver_summary": plan["approver_summary"]}, nil
		}
		return map[string]any{"success": true}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	r := req(deps, testCfg(t, 2), job76Plan(job76Before...), t.TempDir())
	r.ReviewTitle = "#205: Add a Refunds() method to Ledger"
	if _, err := PlanApprovalGate(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	if len(architectFeedback) < 2 || !strings.Contains(architectFeedback[0], "rename TestRefunds to TestRefundsSumsAbsoluteValues") {
		t.Fatalf("the architect is not told it is a rename: %q", architectFeedback)
	}
	if !strings.Contains(architectFeedback[1], "still adds TestRefunds beside it") {
		t.Fatalf("the fourth test is not sent back: %q", architectFeedback[1])
	}
	body := p.seen[1].Body
	if strings.Contains(body, "TestRefunds`,") || strings.Contains(body, "Not reflected yet") {
		t.Errorf("the revised plan still adds TestRefunds or is flagged:\n%s", body)
	}
}

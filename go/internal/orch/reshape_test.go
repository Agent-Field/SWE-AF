package orch

import (
	"context"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// keptBothRevision is run 4's revised plan of 2026-11-07 (07-revised-plan.md):
// asked to make the zero-allocation check its own test, the planner added
// TestLenDoesNotAllocate, KEPT TestLenNoAllocations, ruled removing either
// out of scope and wrote a risk defending the duplicate.
func keptBothRevision() map[string]any {
	plan := consistentRevision()
	prd := plan["prd"].(map[string]any)
	prd["out_of_scope"] = []any{
		"Editing README.md at all.",
		"Renaming, removing or merging either allocation test.",
	}
	prd["risks"] = []any{"Two allocation tests make near-identical assertions on purpose, so a later tidy-up must not delete one of them."}
	plan["approver_summary"] = map[string]any{
		"what_changes":    "Adds Ledger.Len() and four tests.",
		"tests_added":     []any{"TestLen", "TestLenMatchesEntriesCount", "TestLenNoAllocations", "TestLenDoesNotAllocate"},
		"will_not_change": []any{"README.md"},
		"risks":           []any{"Two allocation tests make near-identical assertions on purpose, so a later tidy-up must not delete one of them."},
	}
	return plan
}

// movedRevision is what the person asked for: the check moved, not copied.
func movedRevision() map[string]any {
	plan := consistentRevision()
	plan["approver_summary"] = map[string]any{
		"what_changes": "Adds Ledger.Len() and three tests.",
		"tests_added":  []any{"TestLen", "TestLenMatchesEntriesCount", "TestLenDoesNotAllocate"},
		"risks":        []any{},
	}
	return plan
}

var run4First = []string{"TestLen", "TestLenMatchesEntriesCount", "TestLenNoAllocations"}

// "Also make the check its own test" is a MOVE: the additive gate must not
// hold the old allocation test in place.
func TestAMoveIsNotHeldAsAnAddition(t *testing.T) {
	if p := additiveProblems(liveFeedback, run4First, movedRevision()); len(p) != 0 {
		t.Fatalf("a move was held as an addition: %q", p)
	}
	// A real addition is still held: the table-test request of run round 2.
	add := "Also add a table test for Len() covering an empty ledger and one with three entries"
	if p := additiveProblems(add, run4First, movedRevision()); len(p) != 1 || !strings.Contains(p[0], "TestLenNoAllocations") {
		t.Fatalf("an addition that dropped a test must be held: %q", p)
	}
	for _, s := range []string{"Also rename TestLen to TestCount", "Also split the TestLen checks into their own tests", "Replace the tests with one table test"} {
		if additiveSentence(s) {
			t.Errorf("%q reshapes; it is not additive", s)
		}
	}
}

// Run 4's revision kept both allocation tests and defended the duplicate in
// Out of scope and Risks; every one of those is named as falling short.
func TestKeepingBothAllocationTestsFallsShort(t *testing.T) {
	probs := duplicateProblems(keptBothRevision())
	joined := strings.Join(probs, "\n")
	for _, want := range []string{
		"adds both TestLenNoAllocations and TestLenDoesNotAllocate, which check the same thing",
		"protects a duplicate instead of removing it: Renaming, removing or merging either allocation test",
		"protects a duplicate instead of removing it: Two allocation tests make near-identical assertions",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %q in:\n%s", want, joined)
		}
	}
	if p := duplicateProblems(movedRevision()); len(p) != 0 {
		t.Fatalf("the moved plan has no duplicate: %q", p)
	}
	if sameCheck("TestLen", "TestLenEmpty") || sameCheck("TestLenMatchesEntriesCount", "TestLenDoesNotAllocate") {
		t.Fatal("different checks read as the same")
	}
}

// The gate sends such a revision back once more, with the duplicate spelled
// out, and the person sees the moved plan, named by their note.
func TestTheGateSendsBackARevisionThatKeptTheDuplicate(t *testing.T) {
	p := &recordingPauser{answers: []agent.ApprovalResult{
		{Decision: "request_changes", Feedback: liveFeedback},
		{Decision: "approved"},
	}}
	defer wireHax(t, nil, p)()
	sprints := 0
	var architectFeedback []string
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".run_product_manager"):
			if sprints == 0 {
				return keptBothRevision()["prd"].(map[string]any), nil
			}
			return movedRevision()["prd"].(map[string]any), nil
		case strings.HasSuffix(target, ".run_architect"):
			architectFeedback = append(architectFeedback, input["feedback"].(string))
			return map[string]any{"summary": "Len and its tests."}, nil
		case strings.HasSuffix(target, ".run_tech_lead"):
			return map[string]any{"approved": true}, nil
		case strings.HasSuffix(target, ".run_sprint_planner"):
			sprints++
			plan := movedRevision()
			if sprints == 1 {
				plan = keptBothRevision()
			}
			return map[string]any{"issues": plan["issues"], "approver_summary": plan["approver_summary"]}, nil
		}
		return map[string]any{"success": true}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	first := richPlan()
	first["approver_summary"] = map[string]any{"tests_added": []any{"TestLen", "TestLenMatchesEntriesCount", "TestLenNoAllocations"}}
	r := req(deps, testCfg(t, 2), first, t.TempDir())
	r.ReviewTitle = "#83: Add a Len() method"
	if _, err := PlanApprovalGate(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	if sprints != 2 || !strings.Contains(architectFeedback[1], "check the same thing") {
		t.Fatalf("re-planned %d time(s); second feedback:\n%s", sprints, strings.Join(architectFeedback, "\n---\n"))
	}
	if q := p.seen[1].Question; !strings.Contains(q, "revised after your first note") {
		t.Fatalf("question %q", q)
	}
	body := p.seen[1].Body
	if strings.Contains(body, "Not reflected yet") || strings.Contains(body, "TestLenNoAllocations`,") {
		t.Fatalf("the person must see the moved plan:\n%s", body)
	}
	if !strings.Contains(body, "Tests no longer added: `TestLenNoAllocations`") {
		t.Fatalf("the move must be listed:\n%s", body)
	}
}

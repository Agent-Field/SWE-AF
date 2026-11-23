package orch

import (
	"strings"
	"testing"
)

// The person's note on live run r3 (2026-11-07, job 73), against a first plan
// that added TestRefunds, TestRefundsEmpty and TestRefundsNone.
const r3Note = "Name the test TestRefundsSumsAbsoluteValues."

var r3First = []string{"TestRefunds", "TestRefundsEmpty", "TestRefundsNone"}

// r3Revision is r3's revised plan (jobs/2-73-plan-r2.md), its lists copied as
// the planner wrote them. It does exactly what was asked: TestRefunds is
// renamed, three tests remain, and four lines say the old name is removed.
func r3Revision() map[string]any {
	return map[string]any{
		"prd": map[string]any{
			"validated_description": "Adds Ledger.Refunds() and three tests for it.",
			"must_have": []any{
				"Add `func (l *Ledger) Refunds() int64` to ledger.go: the sum of the absolute values of every entry whose Amount is strictly less than zero, in cents, with a doc comment starting with the identifier name Refunds.",
				"Add TestRefundsSumsAbsoluteValues to ledger_test.go: the renamed two-refund test, built with New()/Add() holding -300 and -200 plus at least one positive entry, asserting Refunds() is exactly 500.",
				"Rename, meaning remove: the superseded working name TestRefunds must not exist anywhere. ledger_test.go contains no `func TestRefunds(` at all — not a duplicate function beside the renamed test, not an alias, not a wrapper, not a commented-out copy.",
				"Add TestRefundsEmpty to ledger_test.go: a bare New() with no entries, asserting Refunds() is exactly 0.",
				"Add TestRefundsNone to ledger_test.go: amounts all >= 0, asserting Refunds() is exactly 0.",
				"Exactly three refund tests exist in package ledger, named literally TestRefundsSumsAbsoluteValues, TestRefundsEmpty and TestRefundsNone; the count is fixed so the rename cannot add a fourth.",
			},
			"out_of_scope": []any{
				"Any change to Total(), Add, Entries, New, Entry, or their existing tests TestTotal, TestTotalEmpty, TestEntriesIsACopy.",
				"Keeping, restoring or aliasing the superseded name TestRefunds in any form (duplicate function, thin wrapper, or t.Run subtest). The old name is deleted, not protected.",
				"A Refunds mention in README.md: README.md is a tracked file, so editing it would violate the no-unrelated-files criterion.",
			},
			"assumptions": []any{
				"The reviewer's instruction supersedes the original wording of issue #213: the two-refund test is named TestRefundsSumsAbsoluteValues instead of TestRefunds, and this is a rename that removes the old name rather than an addition beside it.",
				"Because no TestRefunds and no refund test of any name exist in ledger_test.go today, the implementer writes the three refund tests from scratch under the exact required names; there is nothing to preserve and nothing to duplicate.",
			},
			"risks": []any{
				"The superseded name TestRefunds being kept instead of removed (an implementer following the original issue text could write it, alone or as a second function). The plan forecloses this by requirement, not merely by detection: the single two-refund test is named TestRefundsSumsAbsoluteValues and no TestRefunds may exist; the removal gate then fails the run unless grep -c 'func TestRefunds(' ledger_test.go prints exactly 0 and the renamed-test gate requires exactly 1.",
				"Total() accidentally edited while adding the method. Mitigation: the additive-only diff gate prints nothing, and the existing Total tests are re-run.",
			},
			"acceptance_criteria": []any{
				"go test ./... -run '^TestRefundsSumsAbsoluteValues$' -count=1 -v exits 0 and its output contains '--- PASS: TestRefundsSumsAbsoluteValues'.",
				"grep -c 'func TestRefunds(' ledger_test.go prints exactly 0 (the superseded name is removed outright: no TestRefunds duplicate, alias, wrapper or subtest survives beside the renamed test).",
				"grep -c 'func TestRefundsSumsAbsoluteValues(' ledger_test.go prints exactly 1 (the renamed test is the single two-refund test).",
				reviewerRequestPrefix + r3Note,
			},
		},
		"approver_summary": map[string]any{
			"what_changes": "Adds Ledger.Refunds() plus three tests for it.",
			"tests_added":  []any{"TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone"},
			"will_not_change": []any{
				"Total() and every other existing symbol are left byte-for-byte unchanged",
				"The three existing tests keep their names and behaviour",
			},
			"risks": []any{
				"The two-refund test must carry exactly the reviewer's name, with no stray function left under the old working name",
			},
		},
		"issues": []any{map[string]any{
			"name": "add-refunds", "title": "Add Ledger.Refunds() with its three refund tests",
			"description":     "Add Refunds, TestRefundsSumsAbsoluteValues, TestRefundsEmpty and TestRefundsNone.",
			"files_to_modify": []any{"ledger.go", "ledger_test.go"},
		}},
	}
}

// S2 (live r3): a plan that renamed a test and says the old name must not
// survive was held to four "protects a duplicate" lines about the test it
// removed. Saying a removed test is removed protects nothing.
func TestARenamedTestsRemovalIsNotADuplicateProtected(t *testing.T) {
	if p := duplicateProblems(r3Revision()); len(p) != 0 {
		t.Fatalf("the removal of a renamed test was reported as unreflected:\n%s", strings.Join(p, "\n"))
	}
	if p := revisionChecks(r3Revision(), readReviewRequests(r3Note), r3Note, r3First); len(p) != 0 {
		t.Fatalf("r3's revision did what was asked, yet:\n%s", strings.Join(p, "\n"))
	}
}

// The plan's own tests are still guarded: a line defending a duplicate the
// plan ADDS is held, even with no look-alike pair among the names.
func TestALineDefendingAPlannedDuplicateIsStillHeld(t *testing.T) {
	plan := r3Revision()
	plan["prd"].(map[string]any)["risks"] = []any{
		"TestRefundsNone duplicates TestRefundsEmpty on purpose, so a later tidy-up must not delete either of them.",
	}
	p := duplicateProblems(plan)
	if len(p) != 1 || !strings.Contains(p[0], "TestRefundsNone duplicates TestRefundsEmpty") {
		t.Fatalf("a defended duplicate the plan adds must be held: %q", p)
	}
	// And a plan that KEEPS the old name beside the new one is still told.
	kept := r3Revision()
	kept["approver_summary"].(map[string]any)["tests_added"] = []any{"TestRefunds", "TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone"}
	if p := renameProblems(r3Note, r3First, kept); len(p) != 1 || !strings.Contains(p[0], "still adds TestRefunds") {
		t.Fatalf("keeping the old name must be held: %q", p)
	}
}

// The revised plan names its note once, in the question the desk titles the
// card with — not again in the body's heading right under it.
func TestARevisedPlanSaysWhichNoteOnce(t *testing.T) {
	q := NativePlanQuestion(ApprovalRequest{ExecutionID: "exec-r3", ReviewTitle: "#213: Add a Refunds() method to Ledger"}, r3Revision(), 1,
		[]map[string]any{{"feedback": r3Note}})
	if !strings.Contains(q.Question, "revised after your first note") {
		t.Fatalf("question must say it is revised: %q", q.Question)
	}
	if n := strings.Count(q.Question+"\n"+q.Body, "revised after your first note"); n != 1 {
		t.Fatalf("\"revised after your first note\" appears %d times:\n%s\n%s", n, q.Question, q.Body)
	}
}

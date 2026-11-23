package orch

import (
	"strings"
	"testing"
)

// r7Revision is live r7's revised plan (job 267, 2026-11-08): r3's plan, which
// renamed TestRefunds to TestRefundsSumsAbsoluteValues as asked, with the
// assumption that confirmed it — the line that was shown as "Not reflected
// yet" after one note.
func r7Revision() map[string]any {
	plan := r3Revision()
	prd := plan["prd"].(map[string]any)
	prd["assumptions"] = append(prd["assumptions"].([]any),
		"The two-refund test is named TestRefundsSumsAbsoluteValues, and no function named TestRefunds exists in ledger_test.go, so there is no duplicate to protect.")
	return plan
}

func r7Body(plan map[string]any) string {
	return PlanReviewMarkdown("#267: Add a Refunds() method to Ledger", "g", plan, 1,
		[]map[string]any{{"feedback": r3Note, "before_tests": r3First, "before_files": []string{"ledger.go", "ledger_test.go"}}})
}

// #267: one revision, the rename made — the plan says what changed and
// claims nothing is missing, nor that it was revised twice.
func TestOneRevisionThatMadeTheRenameIsReflected(t *testing.T) {
	if p := revisionChecks(r7Revision(), readReviewRequests(r3Note), r3Note, r3First); len(p) != 0 {
		t.Fatalf("r7's revision did what was asked, yet:\n%s", strings.Join(p, "\n"))
	}
	body := r7Body(r7Revision())
	for _, banned := range []string{"Not reflected yet", "not fully reflected", "revised twice"} {
		if strings.Contains(body, banned) {
			t.Fatalf("body says %q after one revision that made the rename:\n%s", banned, body)
		}
	}
	for _, want := range []string{"Tests now added: `TestRefundsSumsAbsoluteValues`", "Tests no longer added: `TestRefunds`"} {
		if !strings.Contains(body, want) {
			t.Fatalf("body misses %q:\n%s", want, body)
		}
	}
}

// The true negative: one revision that kept TestRefunds beside the new name is
// still not reflected, and says it was revised once.
func TestOneRevisionThatKeptTheOldNameIsNotReflected(t *testing.T) {
	kept := r7Revision()
	kept["approver_summary"].(map[string]any)["tests_added"] = []any{"TestRefunds", "TestRefundsSumsAbsoluteValues", "TestRefundsEmpty", "TestRefundsNone"}
	body := r7Body(kept)
	for _, want := range []string{"## Not reflected yet", "The plan was revised once and still says this", "still adds TestRefunds"} {
		if !strings.Contains(body, want) {
			t.Fatalf("body misses %q:\n%s", want, body)
		}
	}
	if strings.Contains(body, "revised twice") {
		t.Fatalf("one note is one revision:\n%s", body)
	}
}

func TestRevisedTimesCountsTheNotes(t *testing.T) {
	for n, want := range map[int]string{1: "revised once", 2: "revised twice", 3: "revised 3 times"} {
		if got := revisedTimes(n); !strings.Contains(got, want) {
			t.Errorf("revisedTimes(%d) = %q, want %q", n, got, want)
		}
	}
}

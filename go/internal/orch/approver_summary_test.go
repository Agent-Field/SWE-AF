package orch

import (
	"strings"
	"testing"
)

func summaryOf(t *testing.T, body string) string {
	t.Helper()
	start, end := strings.Index(body, "## Summary"), strings.Index(body, "## Details")
	if start < 0 || end < start {
		t.Fatalf("no Summary before Details:\n%s", body)
	}
	return body[start:end]
}

// The planner's own approver summary is what the approver reads: its
// sentence, each file with its own purpose, and only tests that are new.
func TestTheSummaryIsThePlannersOwnForTheApprover(t *testing.T) {
	plan := richPlan()
	plan["approver_summary"] = map[string]any{
		"what_changes": "Adds `Ledger.Len()`, an O(1) count that doesn't copy entries, plus three tests.",
		"files": []any{
			map[string]any{"path": "ledger.go", "purpose": "the new Len method"},
			map[string]any{"path": "ledger_test.go", "purpose": "TestLen, TestLenAgreesWithEntries, TestLenDoesNotAllocate"},
		},
		"tests_added":     []any{"TestLen", "TestLenAgreesWithEntries", "TestLenDoesNotAllocate", "TestRun"},
		"will_not_change": []any{"the command-line tool", "how entries are stored"},
		"risks":           []any{},
	}
	body := planReviewMarkdown("#83: Add a Len() method", "g", plan, 0, nil, map[string]bool{"TestRun": true})
	s := summaryOf(t, body)
	for _, want := range []string{
		"**What changes:** Adds `Ledger.Len()`, an O(1) count that doesn't copy entries, plus three tests.",
		"- `ledger.go` — the new Len method",
		"- `ledger_test.go` — TestLen, TestLenAgreesWithEntries, TestLenDoesNotAllocate",
		"**Tests it adds:** `TestLen`, `TestLenAgreesWithEntries`, `TestLenDoesNotAllocate`\n",
		"**Will not change:** the command-line tool; how entries are stored",
		"**Risks:** none",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("summary misses %q:\n%s", want, s)
		}
	}
	if strings.Contains(s, "TestRun") {
		t.Errorf("an existing test is listed as added:\n%s", s)
	}
}

// Without the planner's field, files still get purposes of their own and no
// entry is cut mid-word: a long item shortens to a clause or is left out.
func TestTheFallbackSummaryKeepsPurposesApartAndPhrasesWhole(t *testing.T) {
	plan := richPlan()
	iss := plan["issues"].([]any)[0].(map[string]any)
	iss["files_to_modify"] = []any{"ledger.go", "ledger_test.go"}
	iss["description"] = "Add Len and the tests TestLen and TestLenDoesNotAllocate."
	prd := plan["prd"].(map[string]any)
	prd["out_of_scope"] = []any{
		"Do not modify the Entries method, the Append method, the Total method, or any persistence or serialization behaviour of the ledger package whatsoever",
		"The CLI",
	}
	prd["risks"] = []any{"The allocation check could be flaky on a loaded machine, because testing.AllocsPerRun averages over runs and a GC in the middle of a run can count against it"}
	s := summaryOf(t, planReviewMarkdown("x", "g", plan, 0, nil, nil))
	if !strings.Contains(s, "- `ledger_test.go` — TestLen, TestLenDoesNotAllocate") {
		t.Errorf("the test file has no purpose of its own:\n%s", s)
	}
	if strings.Contains(s, "…") {
		t.Errorf("a summary entry was cut:\n%s", s)
	}
	if !strings.Contains(s, "**Will not change:** The CLI") {
		t.Errorf("the short out-of-scope item should stand alone:\n%s", s)
	}
	if !strings.Contains(s, "**Risks:** The allocation check could be flaky on a loaded machine\n") {
		t.Errorf("the risk should shorten to its first clause:\n%s", s)
	}
}

func TestPhraseNeverCutsAWord(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{"short", "short"},
		{"First sentence here. Second one is much longer than the limit allows", "First sentence here."},
		{"A clause that fits, but then a very long tail that does not fit anywhere", "A clause that fits"},
		{"Do not modify A, B, C, D, E, F, G or any other part of it", ""},
		{"Keep `a; b (c)` as is; then a much longer tail goes here", "Keep `a; b (c)` as is"},
		{"Run `go doc -u (x); y` and a long tail that will not fit", ""},
		{"onegiantwordwithnoseparatorsatallthatcannotbeshortened", ""},
	} {
		if got := phrase(c.in, 30); got != c.want {
			t.Errorf("phrase(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Live on 2026-11-07 the architect named components "C1 — Ledger.Len (root
// package ledger)" and cited them by id; stripping the ids left "→ →" and a
// bold name of "— Ledger.Len". Each id is now said as its component's name.
func TestComponentIDsAreSaidAsTheirNames(t *testing.T) {
	plan := richPlan()
	arch := plan["architecture"].(map[string]any)
	arch["summary"] = "Three components, implemented in order C1 → C2 → C3."
	arch["components"] = []any{
		map[string]any{"name": "C1 — Ledger.Len (root package ledger)", "responsibility": "Expose the count."},
		map[string]any{"name": "C2 — Len test suite (package ledger, internal)", "responsibility": "Pin it; see C1."},
		map[string]any{"name": "C3 — CLI count migration (cmd/ledger)", "responsibility": "Call Len."},
	}
	body := planReviewMarkdown("x", "g", plan, 0, nil, nil)
	for _, want := range []string{"Ledger.Len → Len test suite → CLI count migration", "**Ledger.Len (root package ledger)**", "see Ledger.Len"} {
		if !strings.Contains(body, want) {
			t.Errorf("body misses %q", want)
		}
	}
	for _, bad := range []string{"→ →", "**—", "C1", "C2"} {
		if strings.Contains(body, bad) {
			t.Errorf("body still has %q", bad)
		}
	}
}

// A comma after a code span belongs to the sentence (live: "`Cap()“IsEmpty()`indexed").
func TestPunctuationAfterCodeSurvives(t *testing.T) {
	got := personText("`Cap()`, `IsEmpty()`, indexed `Get(i)`, and `cmd/ledger/main.go`, which copies", 0)
	if got != "`Cap()`, `IsEmpty()`, indexed `Get(i)`, and `cmd/ledger/main.go`, which copies" {
		t.Fatalf("got %q", got)
	}
}

// Live round 2, 2026-11-07: a command's output file was redacted to "the
// repo", and the build's own approval file was cited to the person.
func TestCommandsKeepPathsAndRecordsAreNotCited(t *testing.T) {
	got := personText("Run `go test ./... -v > /tmp/len.out && grep -q PASS /tmp/len.out`. Per the override in `.artifacts/approval_state.json`, Total() stays; drafts in .artifacts/plan/prd.md lack it. Built at /Users/santosh/af/x/ledger.go.", 0)
	for _, want := range []string{"> /tmp/len.out && grep -q PASS /tmp/len.out`", "override in your review", "drafts in the build's notes lack"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %q", want, got)
		}
	}
	for _, bad := range []string{".artifacts", "/Users/santosh", "> the repo"} {
		if strings.Contains(got, bad) {
			t.Errorf("still has %q in %q", bad, got)
		}
	}
	if strings.Count(got, "`")%2 != 0 {
		t.Errorf("unbalanced code in %q", got)
	}
}

// "Also add a table test" adds; a revision that drops the tests the person
// already saw is not what they asked, and the change is listed, not implied.
func TestAnAdditiveRequestKeepsTheTestsAndListsTheChange(t *testing.T) {
	before := []string{"TestLenEmpty", "TestLenAfterAdd", "TestLenDoesNotAllocate"}
	revised := richPlan()
	revised["approver_summary"] = map[string]any{"tests_added": []any{"TestLen", "TestLenDoesNotAllocate"}}
	fb := "Also add a table test for Len() covering an empty ledger and one with three entries, and do not change Total()"
	probs := additiveProblems(fb, before, revised)
	if len(probs) != 2 || !strings.Contains(probs[0], "TestLenEmpty") {
		t.Fatalf("problems = %q", probs)
	}
	if p := additiveProblems("Replace the tests with one table test", before, revised); len(p) != 0 {
		t.Fatalf("a replace request is not additive: %q", p)
	}
	hist := []map[string]any{{"iteration": 0, "feedback": fb, "before_tests": before, "before_files": []any{"ledger.go"}}}
	body := planReviewMarkdown("x", "g", revised, 1, hist, nil)
	hc := body[strings.Index(body, "## How the plan changed"):strings.Index(body, "## Summary")]
	for _, want := range []string{"Tests now added: `TestLen`", "Tests no longer added: `TestLenEmpty`, `TestLenAfterAdd`", "not fully reflected yet"} {
		if !strings.Contains(hc, want) {
			t.Errorf("How the plan changed misses %q:\n%s", want, hc)
		}
	}
	if !strings.Contains(body, "## Not reflected yet") {
		t.Error("dropping the person's tests must be named as not reflected")
	}
}

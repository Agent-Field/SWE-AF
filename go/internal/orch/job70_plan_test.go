package orch

import (
	"context"
	"regexp"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// The cases below are job 70 of the 2026-11-07 live run (issue #197, "Add a
// Negatives() method to Ledger"): ~/factory-e2e/workaf-ab/jobs/B-70-plan-round{1,2}.md.
const job70Note = "Also list Negatives() in the README API sentence beside New, Add, Entries and Total, and name the test TestNegativesCountsRefunds."

// job70Plan is a plan whose summary is the planner's own, as in both rounds.
func job70Plan(tests []any, files []any) map[string]any {
	var names []string
	var paths []any
	for _, n := range tests {
		names = append(names, n.(string))
	}
	for _, f := range files {
		paths = append(paths, f.(map[string]any)["path"])
	}
	named := strings.Join(names, " and ")
	plan := map[string]any{
		"prd": map[string]any{
			"validated_description": "Ledger gains Negatives() counting entries below zero.",
			"must_have":             []any{"Negatives() int on *Ledger", "the test " + named},
			"acceptance_criteria":   []any{"go test -run '" + named + "' passes"},
		},
		"architecture": map[string]any{"summary": "One method between Entries and Total."},
		"issues": []any{map[string]any{"name": "add-negatives", "title": "Add Negatives()",
			"description": "Add Negatives and the test " + named + ".", "files_to_modify": paths,
			"acceptance_criteria": []any{named + " passes"}}},
	}
	plan["approver_summary"] = map[string]any{
		"what_changes":    "Adds Ledger.Negatives(), a read-only count of entries whose amount is below zero.",
		"files":           files,
		"tests_added":     tests,
		"will_not_change": []any{"Total(), Entries(), Add() and New() keep their current behaviour", "The command-line tool still prints the same summary line"},
		"risks":           []any{"Both files must stay additions-only, so the test has to build its entries from literal ids"},
	}
	return plan
}

var job70Files = []any{
	map[string]any{"path": "ledger.go", "purpose": "the new Negatives method"},
	map[string]any{"path": "README.md", "purpose": "the API sentence naming Negatives"},
	map[string]any{"path": "ledger_test.go", "purpose": "TestNegativesCountsRefunds"},
}

// S3.1: written line after line, "**Tests it adds:**" sat directly under the
// Files list and markdown read it, "Will not change" and "Risks" as a lazy
// continuation of the last file. Every field is now its own paragraph, and
// the list stays tight.
func TestJob70SummaryFieldsAreNotSwallowedByTheFilesList(t *testing.T) {
	s := summaryOf(t, planReviewMarkdown("#197: Add a Negatives() method to Ledger", "g",
		job70Plan([]any{"TestNegatives"}, job70Files), 0, nil, nil))
	for _, want := range []string{
		"**Files:**\n- `ledger.go` — the new Negatives method\n- `README.md` — the API sentence naming Negatives\n- `ledger_test.go` — TestNegativesCountsRefunds\n\n**Tests it adds:**",
		"**What changes:** Adds Ledger.Negatives(), a read-only count of entries whose amount is below zero.\n\n**Files:**",
		"\n\n**Will not change:**",
		"\n\n**Risks:**",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("summary misses %q:\n%s", want, s)
		}
	}
	// No field line follows a list item or another field without a blank line.
	if m := regexp.MustCompile(`(?m)^(?:- .*|\*\*[^*]+:\*\*.*)\n\*\*[^*]+:\*\*`).FindString(s); m != "" {
		t.Errorf("a field runs on from the line above it: %q\n%s", m, s)
	}
}

// S3.2: "name the test X" of a plan that added one test renames that test.
func TestJob70NamingTheTestIsARename(t *testing.T) {
	before := []string{"TestNegatives"}
	got := readRenames(job70Note, before)
	if len(got) != 1 || got[0].From != "TestNegatives" || got[0].To != "TestNegativesCountsRefunds" || got[0].Among != nil {
		t.Fatalf("renames = %+v", got)
	}
	reading := renameReading(job70Note, before)
	if !strings.Contains(reading, "rename TestNegatives to TestNegativesCountsRefunds") || !strings.Contains(reading, "do not add a second test") {
		t.Fatalf("the planners are not told it is a rename: %q", reading)
	}
	if w := personWords(job70Note + reading + recheckMarker + "x"); w != job70Note {
		t.Fatalf("the requirements must carry only the person's words, got %q", w)
	}

	// Round 2 as it happened: both tests. The revision is sent back, and the
	// person's sentence is marked as not reflected.
	both := job70Plan([]any{"TestNegatives", "TestNegativesCountsRefunds"}, job70Files)
	probs := renameProblems(job70Note, before, both)
	if len(probs) != 1 || !strings.Contains(probs[0], "still adds TestNegatives beside it") {
		t.Fatalf("problems = %q", probs)
	}
	if p := revisionChecks(both, readReviewRequests(job70Note), job70Note, before); !containsAny(strings.Join(p, "\n"), []string{"not to add a second one"}) {
		t.Fatalf("the gate's checks miss the second test: %q", p)
	}
	// The rename done: nothing to send back, and the old name is not counted
	// as a test the person asked to keep.
	renamed := job70Plan([]any{"TestNegativesCountsRefunds"}, job70Files)
	if p := renameProblems(job70Note, before, renamed); len(p) != 0 {
		t.Fatalf("a renamed plan is sent back: %q", p)
	}
	addAndName := "Also add a test for an all-negative ledger and name the test TestNegativesCountsRefunds"
	if p := additiveProblems(addAndName, before, renamed); len(p) != 0 {
		t.Fatalf("a renamed test is reported as dropped: %q", p)
	}

	// Not renames: a NEW test named X.
	for _, note := range []string{
		"Add a test named TestNegativesCountsRefunds",
		"Also add another test, TestNegativesCountsRefunds",
	} {
		if r := readRenames(note, before); len(r) != 0 {
			t.Errorf("%q over %v read as a rename: %+v", note, before, r)
		}
	}
	if r := readRenames("Rename TestNegatives to TestNegativesCountsRefunds", []string{"TestNegatives", "TestOther"}); len(r) != 1 {
		t.Errorf("an explicit rename is not read: %+v", r)
	}
}

// S3.2: the person reading the plan is the reviewer.
func TestJob70PlanTextDoesNotCallThePersonTheReviewer(t *testing.T) {
	got := personText("`ledger_test.go` gains two tests: the issue's `TestNegatives` and the reviewer-requested `TestNegativesCountsRefunds`. The reviewer asked to add it.", 1)
	want := "`ledger_test.go` gains two tests: the issue's `TestNegatives` and the requested `TestNegativesCountsRefunds`. You asked to add it."
	if got != want {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
}

// S3.3: the note is quoted right above "How the plan changed"; its first line
// is the outcome, not the note again.
func TestJob70HowThePlanChangedDoesNotRepeatTheNote(t *testing.T) {
	hist := []map[string]any{{"iteration": 0, "feedback": job70Note,
		"before_tests": []any{"TestNegatives"}, "before_files": []any{"ledger.go", "ledger_test.go"}}}
	body := planReviewMarkdown("#197: Add a Negatives() method to Ledger", "g",
		job70Plan([]any{"TestNegativesCountsRefunds"}, job70Files), 1, hist, nil)
	hc := body[strings.Index(body, "## How the plan changed"):strings.Index(body, "## Summary")]
	if strings.Count(body, "README API sentence beside New") != 1 {
		t.Errorf("the note is said more than once:\n%s", body)
	}
	for _, want := range []string{
		"\n- The plan now includes `TestNegativesCountsRefunds`.\n",
		"- Tests now added: `TestNegativesCountsRefunds`.",
		"- Files now changed: `README.md`.",
	} {
		if !strings.Contains(hc, want) {
			t.Errorf("How the plan changed misses %q:\n%s", want, hc)
		}
	}
	if strings.Contains(body, "Not reflected yet") {
		t.Errorf("a renamed plan is flagged:\n%s", body)
	}
}

// The gate tells the planners a name is a rename before the first revision,
// and holds the revision to it.
func TestJob70TheGateTellsThePlannersItIsARename(t *testing.T) {
	p := &recordingPauser{answers: []agent.ApprovalResult{
		{Decision: "request_changes", Feedback: job70Note},
		{Decision: "approved"},
	}}
	defer wireHax(t, nil, p)()
	var architectFeedback []string
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".run_product_manager"):
			return job70Plan([]any{"TestNegativesCountsRefunds"}, job70Files)["prd"].(map[string]any), nil
		case strings.HasSuffix(target, ".run_architect"):
			architectFeedback = append(architectFeedback, input["feedback"].(string))
			return map[string]any{"summary": "One method."}, nil
		case strings.HasSuffix(target, ".run_tech_lead"):
			return map[string]any{"approved": true}, nil
		case strings.HasSuffix(target, ".run_sprint_planner"):
			renamed := job70Plan([]any{"TestNegativesCountsRefunds"}, job70Files)
			return map[string]any{"issues": renamed["issues"], "approver_summary": renamed["approver_summary"]}, nil
		}
		return map[string]any{"success": true}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	r := req(deps, testCfg(t, 2), job70Plan([]any{"TestNegatives"}, job70Files), t.TempDir())
	r.ReviewTitle = "#197: Add a Negatives() method to Ledger"
	if _, err := PlanApprovalGate(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	if len(architectFeedback) == 0 || !strings.Contains(architectFeedback[0], "rename TestNegatives to TestNegativesCountsRefunds") {
		t.Fatalf("the architect is not told it is a rename: %q", architectFeedback)
	}
	if body := p.seen[1].Body; !strings.Contains(body, "> "+job70Note) || strings.Contains(body, "How to read this request") {
		t.Errorf("the person sees the gate's reading, or not their own note:\n%s", body)
	}
}

package orch

import (
	"context"
	"regexp"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// The exact words the person wrote on 2026-11-07's live run.
const liveFeedback = "Leave README.md alone. Also make the TestLen zero-allocation check its own test named TestLenDoesNotAllocate."

func TestReadReviewRequestsFromTheLiveFeedback(t *testing.T) {
	reqs := readReviewRequests(liveFeedback)
	if len(reqs) != 2 || reqs[0].Avoid != "README.md" || len(reqs[0].Require) != 0 {
		t.Fatalf("first request = %+v", reqs)
	}
	if strings.Join(reqs[1].Require, ",") != "TestLenDoesNotAllocate" || reqs[1].Avoid != "" {
		t.Fatalf("second request = %+v", reqs[1])
	}
}

// contradictoryRevision is run 1's revised plan in miniature: the design did
// what was asked, the requirements did not.
func contradictoryRevision() map[string]any {
	return map[string]any{
		"prd": map[string]any{
			"validated_description": "Ledger gains Len().",
			"must_have": []any{
				"Add a `TestLen` unit test covering empty, small, and large-N counts plus a zero-allocation assertion.",
			},
			"nice_to_have":        []any{"Add `Len` to the method list in `README.md` without altering the documented CLI output example."},
			"acceptance_criteria": []any{"go test ./... passes"},
		},
		"architecture": map[string]any{"summary": "README.md is left alone; TestLenDoesNotAllocate is its own test."},
		"issues": []any{map[string]any{
			"name": "add-len", "title": "Add Len()", "description": "Add Len and tests TestLen and TestLenDoesNotAllocate.",
			"files_to_modify": []any{"ledger.go", "ledger_test.go"},
		}},
	}
}

func consistentRevision() map[string]any {
	return map[string]any{
		"prd": map[string]any{
			"validated_description": "Ledger gains Len().",
			"must_have": []any{
				"Add `TestLen` for counts and `TestLenDoesNotAllocate` for the zero-allocation check.",
				"README.md is left untouched.",
			},
			"acceptance_criteria": []any{"go test -run TestLenDoesNotAllocate passes"},
		},
		"issues": []any{map[string]any{
			"name": "add-len", "title": "Add Len()", "description": "Add Len, TestLen and TestLenDoesNotAllocate.",
			"files_to_modify": []any{"ledger.go", "ledger_test.go"},
		}},
	}
}

func TestRevisionCheckCatchesTheLiveContradiction(t *testing.T) {
	reqs := readReviewRequests(liveFeedback)
	problems := revisionProblems(contradictoryRevision(), reqs)
	joined := strings.Join(problems, "\n")
	for _, want := range []string{"Nice to have still says", "README.md", "Must have and the checks do not mention TestLenDoesNotAllocate"} {
		if !strings.Contains(joined, want) {
			t.Errorf("problems miss %q:\n%s", want, joined)
		}
	}
	if p := revisionProblems(consistentRevision(), reqs); len(p) != 0 {
		t.Fatalf("a consistent revision must pass, got %v", p)
	}
}

// A revision that still contradicts the words is re-planned once more before
// the person is asked; the plan they see says how each request was applied.
func TestTheGateReplansAContradictoryRevisionBeforeAsking(t *testing.T) {
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
			sprints := sprints // the PM follows the sprint planner's view in this fixture
			if sprints == 0 {
				return contradictoryRevision()["prd"].(map[string]any), nil
			}
			return consistentRevision()["prd"].(map[string]any), nil
		case strings.HasSuffix(target, ".run_architect"):
			architectFeedback = append(architectFeedback, input["feedback"].(string))
			return map[string]any{"summary": "Revision 2 of the architecture: README.md is left alone (F1, AC14)."}, nil
		case strings.HasSuffix(target, ".run_tech_lead"):
			return map[string]any{"approved": true}, nil
		case strings.HasSuffix(target, ".run_sprint_planner"):
			sprints++
			if sprints == 1 {
				return map[string]any{"issues": contradictoryRevision()["issues"]}, nil
			}
			return map[string]any{"issues": consistentRevision()["issues"]}, nil
		}
		return map[string]any{"success": true}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}
	r := req(deps, testCfg(t, 2), richPlan(), t.TempDir())
	r.ReviewTitle = "#83: Add a Len() method"
	if _, err := PlanApprovalGate(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	if sprints != 2 || len(architectFeedback) != 2 {
		t.Fatalf("re-planned %d time(s) (architect %d), want twice", sprints, len(architectFeedback))
	}
	if !strings.Contains(architectFeedback[1], "Must have and the checks do not mention TestLenDoesNotAllocate") {
		t.Fatalf("the second re-plan must be told what still contradicts:\n%s", architectFeedback[1])
	}
	body := p.seen[1].Body
	for _, want := range []string{
		"## What you asked to change", "> " + liveFeedback, "## How the plan changed",
		"“Leave README.md alone” — no planned change touches `README.md`.",
		"the plan now includes `TestLenDoesNotAllocate`.", "## Summary", "## Details",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("revised body misses %q:\n%s", want, body)
		}
	}
	if strings.Contains(body, "Not reflected yet") {
		t.Fatalf("a consistent revision must not be flagged:\n%s", body)
	}
	if strings.Index(body, "## What you asked to change") > strings.Index(body, "## Summary") {
		t.Fatal("the person's words come before the summary")
	}
	// ONE COUNTER, in the person's terms: the planner's "Revision 2" is said
	// as "this revision", and the plan is named by the note that produced it
	// — once, in the question the card is titled with, not again in the body.
	q := p.seen[1].Question
	if regexp.MustCompile(`(?i)revision \d`).MatchString(body) || !strings.Contains(q, "revised after your first note") ||
		strings.Contains(body, "revised after your") {
		t.Fatalf("revision numbering must come from the gate, once:\n%s\n%s", q, body)
	}
}

// The summary exists, fits its cap, and carries no machine paths, HTML
// comments or references to documents the person never sees.
func TestThePlanBodyIsSummaryFirstAndSelfContained(t *testing.T) {
	plan := richPlan()
	prd := plan["prd"].(map[string]any)
	prd["validated_description"] = "Ledger gains Len() (AC1, §4). The repo is at /Users/x/af/swe-planner/workspaces/factory-demo-1/ledger.go today."
	prd["risks"] = []any{"Allocation check may be flaky per tech-lead finding (b); see C2 and D9."}
	plan["issues"].([]any)[1].(map[string]any)["description"] = "Edit /Users/x/af/workspaces/factory-demo-f1/.worktrees/issue-01/ledger.go per architecture 2.1 (F2)."
	goal := "Resolve GitHub issue #83: Add a Len() method\n\nCallers copy.\n\n<!-- demo-seed:s019 -->"
	body := PlanReviewMarkdown("#83: Add a Len() method", goal, plan, 0, nil)

	start := strings.Index(body, "## Summary\n")
	end := strings.Index(body, "## Details")
	if start < 0 || end < start {
		t.Fatalf("no Summary before Details:\n%s", body)
	}
	var lines int
	for _, l := range strings.Split(strings.TrimSpace(body[start+len("## Summary\n"):end]), "\n") {
		if strings.TrimSpace(l) != "" {
			lines++
		}
	}
	if lines == 0 || lines > summaryCap {
		t.Fatalf("summary has %d lines, want 1..%d", lines, summaryCap)
	}
	summary := body[start:end]
	for _, want := range []string{"**What changes:**", "**Files:**", "`ledger.go`", "**Will not change:**", "**Risks:**"} {
		if !strings.Contains(summary, want) {
			t.Errorf("summary misses %q:\n%s", want, summary)
		}
	}
	for _, banned := range []*regexp.Regexp{
		regexp.MustCompile(`/Users/`), regexp.MustCompile(`<!--`), regexp.MustCompile(`\bAC\d`),
		regexp.MustCompile(`§`), regexp.MustCompile(`\b[CDF]\d\b`), regexp.MustCompile(`(?i)tech-lead finding`),
		regexp.MustCompile(`architecture 2\.1`), regexp.MustCompile(`## Your answer`),
	} {
		if banned.MatchString(body) {
			t.Errorf("body still has %s:\n%s", banned, body)
		}
	}
	if !strings.Contains(body, "Len()") {
		t.Fatal("code like Len() must survive the cleanup")
	}
}

// "Tests it adds" lists only tests the repository does not already have.
func TestSummaryListsOnlyNewTests(t *testing.T) {
	plan := richPlan()
	plan["issues"].([]any)[0].(map[string]any)["description"] = "Add TestLen beside TestTotal and TestEntriesIsACopy."
	body := planReviewMarkdown("x", "g", plan, 0, nil, map[string]bool{"TestTotal": true, "TestEntriesIsACopy": true})
	start, end := strings.Index(body, "## Summary"), strings.Index(body, "## Details")
	summary := body[start:end]
	if !strings.Contains(summary, "`TestLen`") || strings.Contains(summary, "TestTotal") || strings.Contains(summary, "TestEntriesIsACopy") {
		t.Fatalf("summary tests wrong:\n%s", summary)
	}
}

// Run 3's revised plan said README.md was left alone in a file list and in a
// command; neither may read as a contradiction.
func TestLeftAloneInAListOrACommandIsNotAContradiction(t *testing.T) {
	for _, ok := range []string{
		"The diff touches exactly two files: ledger.go and ledger_test.go — README.md, cmd/ledger/main.go and every other file untouched.",
		"— README.md is untouched: `test -z \"$(git diff --name-only main...HEAD -- README.md)\"` exits 0.",
		"Documentation edits (README.md or any non-Go file) — left untouched.",
		"Leave README.md alone; do not amend its API sentence to mention Len.",
	} {
		if mentionsWithoutLeavingAlone(ok, "README.md") {
			t.Errorf("read as changing README.md: %s", ok)
		}
	}
	for _, bad := range []string{
		"Add `Len` to the method list in `README.md` without altering the documented CLI output example.",
		"Update README.md to list Len.",
	} {
		if !mentionsWithoutLeavingAlone(bad, "README.md") {
			t.Errorf("missed a change to README.md: %s", bad)
		}
	}
}

// Code spans survive the cleanup exactly; reference labels leave no debris.
func TestPersonTextKeepsCommandsAndDropsLabels(t *testing.T) {
	got := personText("AC6: No regressions. Command (exit 0): `go test ./... -count=1`.", 0)
	if got != "No regressions. Command (exit 0): `go test ./... -count=1`." {
		t.Fatalf("got %q", got)
	}
}

// The recheck notes reach the planners, never the requirements.
func TestRecheckNotesStayOutOfTheRequirements(t *testing.T) {
	fb := liveFeedback + recheckMarker + "Must have still says: x"
	prd := prdWithReviewerRequest(map[string]any{}, personWords(fb))
	ac := coerceAnyList(prd["acceptance_criteria"])
	if len(ac) != 1 || strings.Contains(ac[0].(string), "still says") {
		t.Fatalf("acceptance = %v", ac)
	}
}

// Issue #185: "one additional named table subtest zero_width_space" must
// capture zero_width_space as the required identifier and report it in
// howThePlanChanged / PlanReviewMarkdown, without mistaking the descriptive
// prose word "table" for a requested symbol, while preserving explicitly
// backticked code names.
func TestNamedTableSubtestCapturesRequestedIdentifierNotProseWord(t *testing.T) {
	const g7Feedback = "Please revise the plan to require one additional named table subtest zero_width_space with input \"a\\u200bb\" and expected \"a\\u200bb\". State that U+200B is preserved because it is not whitespace. This tests the existing strings.Fields contract; do not special-case the implementation. Keep the rest of the scope unchanged."

	reqs := readReviewRequests(g7Feedback)
	if len(reqs) == 0 {
		t.Fatal("readReviewRequests returned empty")
	}
	if got := strings.Join(reqs[0].Require, ","); got != "zero_width_space" {
		t.Fatalf("reqs[0].Require = %q, want %q", got, "zero_width_space")
	}

	plan := map[string]any{
		"prd": map[string]any{
			"validated_description": "Add textutil.NormalizeSpace.",
			"must_have": []any{
				"Table-driven TestNormalizeSpace includes subtest zero_width_space with input \"a\\u200bb\" and want \"a\\u200bb\".",
			},
			"acceptance_criteria": []any{
				"`go test ./textutil -run TestNormalizeSpace/zero_width_space -v` passes",
			},
		},
		"issues": []any{map[string]any{
			"name":                "normalize-space",
			"title":               "Add textutil.NormalizeSpace",
			"description":         "Add NormalizeSpace and TestNormalizeSpace including subtest zero_width_space.",
			"files_to_modify":     []any{"textutil/textutil.go", "textutil/textutil_test.go"},
			"acceptance_criteria": []any{"TestNormalizeSpace/zero_width_space passes"},
		}},
	}
	problems := revisionProblems(plan, reqs)
	if len(problems) != 0 {
		t.Fatalf("revisionProblems = %v, want empty", problems)
	}
	lines := howThePlanChanged(reqs, problems, true)
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "the plan now includes `zero_width_space`.") {
		t.Fatalf("howThePlanChanged = %q, want `zero_width_space`", joined)
	}
	if strings.Contains(joined, "includes `table`") {
		t.Fatalf("howThePlanChanged falsely reports `table` as a requested symbol: %q", joined)
	}

	// Explicitly backticked `table` is still preserved as a code name.
	backtickedReqs := readReviewRequests("Add a helper struct named `table` in textutil.go.")
	if len(backtickedReqs) != 1 || strings.Join(backtickedReqs[0].Require, ",") != "table" {
		t.Fatalf("backticked `table` Require = %+v, want [table]", backtickedReqs)
	}

	// Adjective "named" with no identifier after prose nouns does not capture a stopword.
	proseOnlyReqs := readReviewRequests("Run each named table subtest in the suite.")
	if len(proseOnlyReqs) != 1 || len(proseOnlyReqs[0].Require) != 0 {
		t.Fatalf("prose-only `named table subtest in` Require = %+v, want empty", proseOnlyReqs)
	}
}

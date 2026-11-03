package coding

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// These tests mirror tests/test_escalation_fixes.py:
//   - a default-path blocking review is a fix iteration ("must be fixed before
//     merge"), with the blocking findings leading the feedback;
//   - three fix iterations on the same blocking finding are a stuck loop;
//   - a window that ran zero iterations never reports a completion;
//   - every completed-with-debt records at least one debt item built from the
//     last review's blocking and should-fix findings.

func blockingReview(title, summary string) map[string]any {
	return map[string]any{
		"approved": false,
		"blocking": true,
		"summary":  summary,
		"debt_items": []any{map[string]any{
			"severity":    "blocking",
			"title":       title,
			"description": "finding for " + title,
			"file_path":   "src/app.py",
		}},
	}
}

func approvedReview() map[string]any {
	return map[string]any{"approved": true, "blocking": false, "summary": "LGTM", "debt_items": []any{}}
}

func coderResult(files ...string) map[string]any {
	out := make([]any, len(files))
	for i, f := range files {
		out[i] = f
	}
	return map[string]any{"files_changed": out}
}

func TestDefaultPathBlockingReviewGetsFixIterationWithFindingsFirst(t *testing.T) {
	var feedbacks []string
	coderCalls := 0

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch {
		case strings.Contains(target, "run_coder"):
			coderCalls++
			feedbacks = append(feedbacks, mapGetStr(kwargs, "feedback", ""))
			return coderResult("src/app.py"), nil
		case strings.Contains(target, "run_code_reviewer"):
			if coderCalls == 1 {
				return blockingReview("Unjustified changed files", "Summary: fix the blocking finding."), nil
			}
			return approvedReview(), nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, makeIssue("escalation-issue", false), makeDAGState(t.TempDir()), callFn,
		makeConfig(t, map[string]any{"max_coding_iterations": 3}), nil)

	if res.Outcome != schemas.IssueOutcomeCompleted || res.Attempts != 2 {
		t.Fatalf("outcome=%s attempts=%d, want completed/2", res.Outcome, res.Attempts)
	}
	if res.IterationHistory[0]["action"] != "fix" || res.IterationHistory[1]["action"] != "approve" {
		t.Fatalf("history actions = %v/%v, want fix/approve",
			res.IterationHistory[0]["action"], res.IterationHistory[1]["action"])
	}
	if v, _ := res.IterationHistory[0]["review_blocking"].(bool); !v {
		t.Errorf("history[0].review_blocking = false, want true")
	}

	second := feedbacks[1]
	if !strings.Contains(second, "Blocking Review Issues (fix before merge)") {
		t.Fatalf("fix feedback does not lead with the blocking section: %q", second)
	}
	findingAt := strings.Index(second, "Unjustified changed files")
	summaryAt := strings.Index(second, "Summary: fix the blocking finding.")
	if findingAt < 0 || summaryAt < 0 || findingAt > summaryAt {
		t.Errorf("blocking findings must precede the summary: finding=%d summary=%d feedback=%q",
			findingAt, summaryAt, second)
	}
}

func TestSameBlockingFindingsThreeTimesCountsAsStuck(t *testing.T) {
	coderCalls := 0

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch {
		case strings.Contains(target, "run_coder"):
			coderCalls++
			return coderResult("src/app.py"), nil
		case strings.Contains(target, "run_code_reviewer"):
			return blockingReview("Unjustified changed files", "still blocked"), nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, makeIssue("escalation-issue", false), makeDAGState(t.TempDir()), callFn,
		makeConfig(t, map[string]any{"max_coding_iterations": 5}), nil)

	if coderCalls != 3 || res.Attempts != 3 {
		t.Fatalf("coder calls=%d attempts=%d, want 3/3 (stuck window)", coderCalls, res.Attempts)
	}
	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable {
		t.Fatalf("outcome = %s, want failed_unrecoverable (never debt)", res.Outcome)
	}
	if len(res.DebtItems) != 0 {
		t.Errorf("debt_items = %v, want none on a blocking stuck loop", res.DebtItems)
	}
}

func TestDifferentBlockingFindingsAreNotStuck(t *testing.T) {
	coderCalls := 0
	reviewCalls := 0

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch {
		case strings.Contains(target, "run_coder"):
			coderCalls++
			return coderResult("src/app.py"), nil
		case strings.Contains(target, "run_code_reviewer"):
			reviewCalls++
			if reviewCalls < 4 {
				return blockingReview("Finding "+string(rune('0'+reviewCalls)), "fix it"), nil
			}
			return approvedReview(), nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, makeIssue("escalation-issue", false), makeDAGState(t.TempDir()), callFn,
		makeConfig(t, map[string]any{"max_coding_iterations": 5}), nil)

	if res.Outcome != schemas.IssueOutcomeCompleted || res.Attempts != 4 {
		t.Fatalf("outcome=%s attempts=%d, want completed/4", res.Outcome, res.Attempts)
	}
}

func TestExhaustedWindowNeverReportsCompletion(t *testing.T) {
	artifactsDir := t.TempDir()
	statePath := filepath.Join(artifactsDir, "execution", "iterations", "stale-issue.json")
	if err := os.MkdirAll(filepath.Dir(statePath), 0o755); err != nil {
		t.Fatal(err)
	}
	stale := map[string]any{
		"iteration":     2,
		"feedback":      "still blocked",
		"files_changed": []any{"src/app.py"},
		"iteration_history": []any{map[string]any{
			"iteration":       2,
			"action":          "fix",
			"summary":         "still blocked",
			"review_approved": false,
			"review_blocking": true,
			"path":            "default",
		}},
	}
	data, err := json.Marshal(stale)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(statePath, data, 0o644); err != nil {
		t.Fatal(err)
	}

	coderCalls := 0
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_coder") {
			coderCalls++
			return coderResult("src/app.py"), nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	ds := makeDAGState(artifactsDir)
	res := run(t, makeIssue("stale-issue", false), ds, callFn,
		makeConfig(t, map[string]any{"max_coding_iterations": 2}), nil)

	if coderCalls != 0 {
		t.Fatalf("coder calls = %d, want 0 (window already exhausted)", coderCalls)
	}
	if res.Outcome != schemas.IssueOutcomeFailedUnrecoverable || res.Attempts != 0 {
		t.Fatalf("outcome=%s attempts=%d, want failed_unrecoverable/0", res.Outcome, res.Attempts)
	}
	if len(res.DebtItems) != 0 {
		t.Errorf("debt_items = %v, want none", res.DebtItems)
	}
	if !strings.Contains(res.ErrorMessage, "zero iterations") {
		t.Errorf("error_message = %q, want to mention zero iterations", res.ErrorMessage)
	}
}

func TestCompletedWithDebtRecordsReviewFindings(t *testing.T) {
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch {
		case strings.Contains(target, "run_coder"):
			return coderResult("src/app.py"), nil
		case strings.Contains(target, "run_code_reviewer"):
			return map[string]any{
				"approved": false,
				"blocking": false,
				"summary":  "Functionally complete, polish left",
				"debt_items": []any{
					map[string]any{"severity": "should_fix", "title": "Weak error handling", "description": "parse() swallows decode errors"},
					map[string]any{"severity": "suggestion", "title": "Style nit", "description": "prefer a named constant"},
				},
			}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	res := run(t, makeIssue("debt-issue", false), makeDAGState(t.TempDir()), callFn,
		makeConfig(t, map[string]any{"max_coding_iterations": 2}), nil)

	if res.Outcome != schemas.IssueOutcomeCompletedWithDebt {
		t.Fatalf("outcome = %s, want completed_with_debt", res.Outcome)
	}
	if len(res.DebtItems) == 0 {
		t.Fatal("completed_with_debt must record debt items")
	}
	titles := map[string]bool{}
	for _, item := range res.DebtItems {
		titles[mapGetStr(item, "title", "")] = true
		if mapGetStr(item, "type", "") != "review_finding" {
			t.Errorf("debt item type = %q, want review_finding", mapGetStr(item, "type", ""))
		}
		if mapGetStr(item, "severity", "") != "should_fix" {
			t.Errorf("debt item severity = %q, want should_fix", mapGetStr(item, "severity", ""))
		}
	}
	if !titles["Weak error handling"] {
		t.Errorf("should_fix finding missing from debt: %v", res.DebtItems)
	}
	if titles["Style nit"] {
		t.Errorf("suggestion must not become debt: %v", res.DebtItems)
	}
}

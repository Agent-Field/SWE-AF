package dag

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// These tests mirror the advisor-loop cases in tests/test_escalation_fixes.py
// and the split-guidance cases in tests/test_prompt_field_rendering.py.

func advisorIssue(name string) map[string]any {
	return map[string]any{
		"name":                name,
		"title":               "Test issue",
		"description":         "A test issue for escalation fixes",
		"acceptance_criteria": []any{"AC-1: it works"},
		"worktree_path":       "/tmp/fake-repo",
		"branch_name":         "test/" + name,
	}
}

func advisorState(t *testing.T) *schemas.DAGState {
	t.Helper()
	return &schemas.DAGState{
		RepoPath:     t.TempDir(),
		ArtifactsDir: t.TempDir(),
		BuildID:      "build-l12",
	}
}

// TestAdvisorRetryRunsCoderIterations: a RETRY_APPROACH round starts a fresh
// window instead of resuming an exhausted one, and writes its own checkpoint.
func TestAdvisorRetryRunsCoderIterations(t *testing.T) {
	coderCalls := 0
	advisorCalls := 0

	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch lastSeg(target) {
		case "run_coder":
			coderCalls++
			return map[string]any{"files_changed": []any{"src/app.py"}}, nil
		case "run_code_reviewer":
			// Round 0 exhausts its two iterations still blocking; the retry
			// (coder call 3) passes.
			if coderCalls <= 2 {
				return map[string]any{
					"approved": false, "blocking": true, "summary": "still blocked",
					"debt_items": []any{map[string]any{"severity": "blocking", "title": "Unjustified changed files"}},
				}, nil
			}
			return map[string]any{"approved": true, "blocking": false, "summary": "LGTM", "debt_items": []any{}}, nil
		case "run_issue_advisor":
			advisorCalls++
			return map[string]any{
				"action":                "retry_approach",
				"failure_diagnosis":     "Coder kept the extra file",
				"rationale":             "Remove the unrequested file",
				"new_approach":          "Use a simpler data model",
				"approach_changes":      []any{"delete src/extra_helper.py"},
				"summary":               "Retry with a simpler approach",
				"missing_functionality": []any{},
			}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	state := advisorState(t)
	cfg := testCfg(t, map[string]any{
		"max_coding_iterations":   2,
		"enable_issue_advisor":    true,
		"max_advisor_invocations": 1,
	})

	result, err := executeSingleIssue(context.Background(), advisorIssue("retry-issue"), state, nil, cfg, callFn, "test-node", nil, nil)
	if err != nil {
		t.Fatalf("executeSingleIssue: %v", err)
	}

	if advisorCalls != 1 {
		t.Errorf("advisor calls = %d, want 1", advisorCalls)
	}
	if coderCalls != 3 {
		t.Errorf("coder calls = %d, want 3 (two in round 0, one real iteration in round 1)", coderCalls)
	}
	if result.Outcome != schemas.IssueOutcomeCompleted {
		t.Fatalf("outcome = %s, want completed", result.Outcome)
	}
	if result.Attempts != 1 {
		t.Errorf("attempts = %d, want 1 (the retry window's own counter)", result.Attempts)
	}

	checkpoint := filepath.Join(state.ArtifactsDir, "execution", "iterations", "build-l12", "retry-issue.r1.json")
	data, err := os.ReadFile(checkpoint)
	if err != nil {
		t.Fatalf("retry checkpoint missing at %s: %v", checkpoint, err)
	}
	var saved map[string]any
	if err := json.Unmarshal(data, &saved); err != nil {
		t.Fatalf("decode checkpoint: %v", err)
	}
	if got := int(saved["iteration"].(float64)); got != 1 {
		t.Errorf("retry checkpoint iteration = %d, want 1", got)
	}
}

// TestAdvisorAcceptWithDebtAlwaysRecordsAnItem: COMPLETED_WITH_DEBT records an
// item even when the advisor itemises nothing.
func TestAdvisorAcceptWithDebtAlwaysRecordsAnItem(t *testing.T) {
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch lastSeg(target) {
		case "run_coder":
			return map[string]any{"files_changed": []any{"src/app.py"}}, nil
		case "run_code_reviewer":
			return map[string]any{
				"approved": false, "blocking": true, "summary": "cannot meet AC2",
				"debt_items": []any{map[string]any{"severity": "blocking", "title": "Missing core functionality"}},
			}, nil
		case "run_issue_advisor":
			return map[string]any{
				"action":                "accept_with_debt",
				"failure_diagnosis":     "Acceptance criterion cannot be met",
				"rationale":             "Ship the rest",
				"missing_functionality": []any{},
				"summary":               "Accepted with debt",
			}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	cfg := testCfg(t, map[string]any{
		"max_coding_iterations":   1,
		"enable_issue_advisor":    true,
		"max_advisor_invocations": 1,
	})
	result, err := executeSingleIssue(context.Background(), advisorIssue("accept-debt-issue"), advisorState(t), nil, cfg, callFn, "test-node", nil, nil)
	if err != nil {
		t.Fatalf("executeSingleIssue: %v", err)
	}

	if result.Outcome != schemas.IssueOutcomeCompletedWithDebt {
		t.Fatalf("outcome = %s, want completed_with_debt", result.Outcome)
	}
	if len(result.DebtItems) == 0 {
		t.Fatal("completed_with_debt must record at least one debt item")
	}
	if got := mapGetStr(result.DebtItems[0], "type", ""); got != "advisor_accepted_failure" {
		t.Errorf("first debt item type = %q, want advisor_accepted_failure", got)
	}
}

// TestCompletedWithDebtKeepsLoopAndAdvisorDebt: the coding loop's review-derived
// debt is merged with the advisor's instead of being overwritten.
func TestCompletedWithDebtKeepsLoopAndAdvisorDebt(t *testing.T) {
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch lastSeg(target) {
		case "run_coder":
			return map[string]any{"files_changed": []any{"src/app.py"}}, nil
		case "run_code_reviewer":
			return map[string]any{
				"approved": false, "blocking": false, "summary": "polish left",
				"debt_items": []any{map[string]any{"severity": "should_fix", "title": "Weak error handling"}},
			}, nil
		}
		t.Fatalf("unexpected target: %s", target)
		return nil, nil
	}

	cfg := testCfg(t, map[string]any{
		"max_coding_iterations": 2,
		"enable_issue_advisor":  false,
	})
	result, err := executeSingleIssue(context.Background(), advisorIssue("debt-issue"), advisorState(t), nil, cfg, callFn, "test-node", nil, nil)
	if err != nil {
		t.Fatalf("executeSingleIssue: %v", err)
	}

	if result.Outcome != schemas.IssueOutcomeCompletedWithDebt {
		t.Fatalf("outcome = %s, want completed_with_debt", result.Outcome)
	}
	titles := map[string]bool{}
	for _, item := range result.DebtItems {
		titles[mapGetStr(item, "title", "")] = true
	}
	if !titles["Weak error handling"] {
		t.Errorf("coding-loop debt lost: %v", result.DebtItems)
	}
}

// TestSplitChildInheritsParentGuidance: a child that omits guidance inherits
// the parent's, a child that states its own keeps it, and a copy is made so
// mutating the child cannot touch the parent.
func TestSplitChildInheritsParentGuidance(t *testing.T) {
	parentGuidance := map[string]any{
		"needs_deeper_qa":  true,
		"needs_new_tests":  false,
		"estimated_scope":  "small",
		"testing_guidance": "extend the existing suite",
	}
	parent := map[string]any{"name": "parent", "guidance": parentGuidance}
	split := []schemas.SplitIssueSpec{
		{Name: "child", Title: "Child", Description: "d", AcceptanceCriteria: []string{"a"}},
	}

	children := splitChildIssueDicts(split, parent, "parent")
	if len(children) != 1 {
		t.Fatalf("children len = %d, want 1", len(children))
	}
	child := children[0]
	if got := mapGetStr(child, "parent_issue_name", ""); got != "parent" {
		t.Errorf("parent_issue_name = %q, want parent", got)
	}
	childGuidance, _ := child["guidance"].(map[string]any)
	if len(childGuidance) != len(parentGuidance) {
		t.Fatalf("child guidance = %v, want %v", childGuidance, parentGuidance)
	}
	for k, v := range parentGuidance {
		if childGuidance[k] != v {
			t.Errorf("child guidance[%q] = %v, want %v", k, childGuidance[k], v)
		}
	}
	// Copy, not a shared reference.
	childGuidance["needs_deeper_qa"] = false
	if parentGuidance["needs_deeper_qa"] != true {
		t.Error("mutating child guidance changed the parent's")
	}

	// A child with its own guidance wins.
	own := []schemas.SplitIssueSpec{{
		Name: "child", Title: "Child", Description: "d", AcceptanceCriteria: []string{"a"},
		Guidance: map[string]any{"needs_deeper_qa": false},
	}}
	children = splitChildIssueDicts(own, parent, "parent")
	ownGuidance, _ := children[0]["guidance"].(map[string]any)
	if len(ownGuidance) != 1 || ownGuidance["needs_deeper_qa"] != false {
		t.Errorf("child guidance = %v, want its own value", ownGuidance)
	}

	// A parent without guidance leaves the child empty.
	children = splitChildIssueDicts(split, map[string]any{}, "parent")
	if g, _ := children[0]["guidance"].(map[string]any); len(g) != 0 {
		t.Errorf("child guidance = %v, want empty", g)
	}
}

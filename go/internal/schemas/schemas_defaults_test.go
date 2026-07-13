package schemas

import (
	"encoding/json"
	"testing"
)

// These tests are derived from the T1.1 validation contract: unmarshaling
// zero-input JSON ("{}") into a struct with non-zero Pydantic defaults must
// seed those defaults, while a present key (even a zero value) must override.

func mustUnmarshal[T any](t *testing.T, data string) T {
	t.Helper()
	var v T
	if err := json.Unmarshal([]byte(data), &v); err != nil {
		t.Fatalf("unmarshal %q into %T: %v", data, v, err)
	}
	return v
}

func TestCoderResultDefaults(t *testing.T) {
	c := mustUnmarshal[CoderResult](t, "{}")
	if !c.Complete {
		t.Errorf("CoderResult{}.Complete = false, want true")
	}
	if c.TestsPassed != nil {
		t.Errorf("CoderResult{}.TestsPassed = %v, want nil", c.TestsPassed)
	}
	// Present false must override the seeded true.
	c2 := mustUnmarshal[CoderResult](t, `{"complete":false}`)
	if c2.Complete {
		t.Errorf(`CoderResult{"complete":false}.Complete = true, want false`)
	}
	// Present tests_passed=false must produce a non-nil pointer to false.
	c3 := mustUnmarshal[CoderResult](t, `{"tests_passed":false}`)
	if c3.TestsPassed == nil || *c3.TestsPassed {
		t.Errorf(`CoderResult{"tests_passed":false}.TestsPassed = %v, want *false`, c3.TestsPassed)
	}
}

func TestIssueGuidanceDefaults(t *testing.T) {
	g := mustUnmarshal[IssueGuidance](t, "{}")
	if !g.NeedsNewTests {
		t.Errorf("IssueGuidance{}.NeedsNewTests = false, want true")
	}
	if g.EstimatedScope != "medium" {
		t.Errorf("IssueGuidance{}.EstimatedScope = %q, want %q", g.EstimatedScope, "medium")
	}
	g2 := mustUnmarshal[IssueGuidance](t, `{"needs_new_tests":false,"estimated_scope":"large"}`)
	if g2.NeedsNewTests {
		t.Errorf("override NeedsNewTests = true, want false")
	}
	if g2.EstimatedScope != "large" {
		t.Errorf("override EstimatedScope = %q, want %q", g2.EstimatedScope, "large")
	}
}

func TestIssueResultDefaults(t *testing.T) {
	r := mustUnmarshal[IssueResult](t, "{}")
	if r.Attempts != 1 {
		t.Errorf("IssueResult{}.Attempts = %d, want 1", r.Attempts)
	}
	if r.SplitRequest != nil {
		t.Errorf("IssueResult{}.SplitRequest = %v, want nil", r.SplitRequest)
	}
	r0 := mustUnmarshal[IssueResult](t, `{"attempts":0}`)
	if r0.Attempts != 0 {
		t.Errorf(`IssueResult{"attempts":0}.Attempts = %d, want 0`, r0.Attempts)
	}
}

func TestAskUserFormDefaults(t *testing.T) {
	f := mustUnmarshal[AskUserForm](t, "{}")
	if f.SubmitLabel != "Submit" {
		t.Errorf("AskUserForm{}.SubmitLabel = %q, want %q", f.SubmitLabel, "Submit")
	}
	f2 := mustUnmarshal[AskUserForm](t, `{"submit_label":"Go"}`)
	if f2.SubmitLabel != "Go" {
		t.Errorf("override SubmitLabel = %q, want %q", f2.SubmitLabel, "Go")
	}
}

func TestReviewResultDefault(t *testing.T) {
	r := mustUnmarshal[ReviewResult](t, "{}")
	if r.ComplexityAssessment != "appropriate" {
		t.Errorf("ReviewResult{}.ComplexityAssessment = %q, want %q", r.ComplexityAssessment, "appropriate")
	}
}

func TestPlannedIssueDefault(t *testing.T) {
	p := mustUnmarshal[PlannedIssue](t, "{}")
	if p.EstimatedComplexity != "medium" {
		t.Errorf("PlannedIssue{}.EstimatedComplexity = %q, want %q", p.EstimatedComplexity, "medium")
	}
	if p.SequenceNumber != nil {
		t.Errorf("PlannedIssue{}.SequenceNumber = %v, want nil", p.SequenceNumber)
	}
	if p.Guidance != nil {
		t.Errorf("PlannedIssue{}.Guidance = %v, want nil", p.Guidance)
	}
}

func TestRepoSpecDefault(t *testing.T) {
	r := mustUnmarshal[RepoSpec](t, "{}")
	if !r.CreatePR {
		t.Errorf("RepoSpec{}.CreatePR = false, want true")
	}
	r2 := mustUnmarshal[RepoSpec](t, `{"create_pr":false}`)
	if r2.CreatePR {
		t.Errorf("override CreatePR = true, want false")
	}
}

func TestWorkspaceRepoDefault(t *testing.T) {
	w := mustUnmarshal[WorkspaceRepo](t, "{}")
	if !w.CreatePR {
		t.Errorf("WorkspaceRepo{}.CreatePR = false, want true")
	}
}

func TestIssueAdaptationDefault(t *testing.T) {
	a := mustUnmarshal[IssueAdaptation](t, "{}")
	if a.Severity != "medium" {
		t.Errorf("IssueAdaptation{}.Severity = %q, want %q", a.Severity, "medium")
	}
}

func TestIssueAdvisorDecisionDefaults(t *testing.T) {
	d := mustUnmarshal[IssueAdvisorDecision](t, "{}")
	if d.Confidence != 0.5 {
		t.Errorf("IssueAdvisorDecision{}.Confidence = %v, want 0.5", d.Confidence)
	}
	if d.DebtSeverity != "medium" {
		t.Errorf("IssueAdvisorDecision{}.DebtSeverity = %q, want %q", d.DebtSeverity, "medium")
	}
}

func TestDAGStateDefault(t *testing.T) {
	s := mustUnmarshal[DAGState](t, "{}")
	if s.MaxReplans != 2 {
		t.Errorf("DAGState{}.MaxReplans = %d, want 2", s.MaxReplans)
	}
	s0 := mustUnmarshal[DAGState](t, `{"max_replans":0}`)
	if s0.MaxReplans != 0 {
		t.Errorf(`DAGState{"max_replans":0}.MaxReplans = %d, want 0`, s0.MaxReplans)
	}
}

func TestRetryAdviceDefault(t *testing.T) {
	a := mustUnmarshal[RetryAdvice](t, "{}")
	if a.Confidence != 0.5 {
		t.Errorf("RetryAdvice{}.Confidence = %v, want 0.5", a.Confidence)
	}
}

func TestFastTaskDefault(t *testing.T) {
	ft := mustUnmarshal[FastTask](t, "{}")
	if ft.EstimatedMinutes != 5 {
		t.Errorf("FastTask{}.EstimatedMinutes = %d, want 5", ft.EstimatedMinutes)
	}
}

// TestNestedDefaultSeeding verifies that a struct containing a typed slice of a
// defaulted type seeds each element's defaults when unmarshaling (e.g. an
// IssueResult nested inside a DAGState).
func TestNestedDefaultSeeding(t *testing.T) {
	s := mustUnmarshal[DAGState](t, `{"completed_issues":[{"issue_name":"x"}]}`)
	if len(s.CompletedIssues) != 1 {
		t.Fatalf("CompletedIssues len = %d, want 1", len(s.CompletedIssues))
	}
	if s.CompletedIssues[0].Attempts != 1 {
		t.Errorf("nested IssueResult.Attempts = %d, want 1 (default seeded)", s.CompletedIssues[0].Attempts)
	}
}

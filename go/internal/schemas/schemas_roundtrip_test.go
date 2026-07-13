package schemas

import (
	"bytes"
	"encoding/json"
	"sort"
	"testing"
)

// TestEnumStringValues asserts each enum const equals the exact Python string.
func TestEnumStringValues(t *testing.T) {
	cases := map[string]string{
		string(AdvisorActionRetryModified):    "retry_modified",
		string(AdvisorActionRetryApproach):    "retry_approach",
		string(AdvisorActionSplit):            "split",
		string(AdvisorActionAcceptWithDebt):   "accept_with_debt",
		string(AdvisorActionEscalateToReplan): "escalate_to_replan",

		string(IssueOutcomeCompleted):           "completed",
		string(IssueOutcomeCompletedWithDebt):   "completed_with_debt",
		string(IssueOutcomeFailedRetryable):     "failed_retryable",
		string(IssueOutcomeFailedUnrecoverable): "failed_unrecoverable",
		string(IssueOutcomeFailedNeedsSplit):    "failed_needs_split",
		string(IssueOutcomeFailedEscalated):     "failed_escalated",
		string(IssueOutcomeSkipped):             "skipped",

		string(ReplanActionContinue):    "continue",
		string(ReplanActionModifyDAG):   "modify_dag",
		string(ReplanActionReduceScope): "reduce_scope",
		string(ReplanActionAbort):       "abort",

		string(QASynthesisActionFix):     "fix",
		string(QASynthesisActionApprove): "approve",
		string(QASynthesisActionBlock):   "block",
	}
	for got, want := range cases {
		if got != want {
			t.Errorf("enum value = %q, want %q", got, want)
		}
	}
}

// TestEnumRoundTrip asserts an enum marshals to and unmarshals from its exact
// string value.
func TestEnumRoundTrip(t *testing.T) {
	r := IssueResult{IssueName: "x", Outcome: IssueOutcomeCompletedWithDebt}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	// Round-trip back.
	var r2 IssueResult
	if err := json.Unmarshal(b, &r2); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if r2.Outcome != IssueOutcomeCompletedWithDebt {
		t.Errorf("Outcome round-trip = %q, want %q", r2.Outcome, IssueOutcomeCompletedWithDebt)
	}
	// Direct string decode.
	var d IssueAdvisorDecision
	if err := json.Unmarshal([]byte(`{"action":"escalate_to_replan","failure_diagnosis":"","rationale":""}`), &d); err != nil {
		t.Fatalf("unmarshal decision: %v", err)
	}
	if d.Action != AdvisorActionEscalateToReplan {
		t.Errorf("Action = %q, want %q", d.Action, AdvisorActionEscalateToReplan)
	}
}

// fullyPopulatedDAGState builds a DAGState with every field set to a non-empty
// value so the marshal→unmarshal→marshal identity is exercised across all
// fields (nested typed lists included).
func fullyPopulatedDAGState() DAGState {
	split := []SplitIssueSpec{{Name: "sub-a", Title: "Sub A", Description: "d", AcceptanceCriteria: []string{"ac1"}}}
	ir := IssueResult{
		IssueName:               "issue-1",
		Outcome:                 IssueOutcomeCompleted,
		ResultSummary:           "done",
		ErrorMessage:            "",
		ErrorContext:            "",
		Attempts:                2,
		FilesChanged:            []string{"a.go", "b.go"},
		BranchName:              "issue/1",
		RepoName:                "repo",
		AdvisorInvocations:      1,
		Adaptations:             []IssueAdaptation{{AdaptationType: AdvisorActionRetryModified, Severity: "high", Rationale: "r"}},
		DebtItems:               []map[string]any{{"severity": "low", "title": "t"}},
		SplitRequest:            &split,
		EscalationContext:       "ctx",
		FinalAcceptanceCriteria: []string{"ac1"},
		IterationHistory:        []map[string]any{{"iter": float64(1)}},
	}
	return DAGState{
		RepoPath:               "/tmp/repo",
		ArtifactsDir:           "/tmp/artifacts",
		PRDPath:                "/tmp/prd.json",
		ArchitecturePath:       "/tmp/arch.json",
		IssuesDir:              "/tmp/issues",
		OriginalPlanSummary:    "plan",
		PRDSummary:             "prd",
		ArchitectureSummary:    "arch",
		AllIssues:              []map[string]any{{"name": "issue-1", "estimated_complexity": "medium"}},
		Levels:                 [][]string{{"issue-1"}, {"issue-2"}},
		CompletedIssues:        []IssueResult{ir},
		FailedIssues:           []IssueResult{},
		SkippedIssues:          []string{"issue-3"},
		InFlightIssues:         []string{"issue-2"},
		CurrentLevel:           1,
		ReplanCount:            1,
		ReplanHistory:          []ReplanDecision{{Action: ReplanActionModifyDAG, Rationale: "r", Summary: "s"}},
		MaxReplans:             2,
		GitIntegrationBranch:   "integration",
		GitOriginalBranch:      "main",
		GitInitialCommit:       "abc123",
		GitMode:                "existing",
		PendingMergeBranches:   []string{"issue/1"},
		MergedBranches:         []string{"issue/0"},
		UnmergedBranches:       []string{},
		WorktreesDir:           "/tmp/repo/.worktrees",
		BuildID:                "build-xyz",
		MergeResults:           []map[string]any{{"success": true}},
		IntegrationTestResults: []map[string]any{{"passed": true}},
		AccumulatedDebt:        []map[string]any{{"severity": "low"}},
		AdaptationHistory:      []map[string]any{{"type": "retry_modified"}},
		WorkspaceManifest:      map[string]any{"workspace_root": "/tmp", "primary_repo_name": "repo"},
	}
}

// TestDAGStateRoundTripIdentical asserts a fully-populated DAGState is
// byte-identical after marshal→unmarshal→marshal (stable key set + values).
func TestDAGStateRoundTripIdentical(t *testing.T) {
	s := fullyPopulatedDAGState()
	b1, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("marshal 1: %v", err)
	}
	var s2 DAGState
	if err := json.Unmarshal(b1, &s2); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	b2, err := json.Marshal(s2)
	if err != nil {
		t.Fatalf("marshal 2: %v", err)
	}
	if !bytes.Equal(b1, b2) {
		t.Errorf("marshal→unmarshal→marshal not byte-identical:\n first: %s\nsecond: %s", b1, b2)
	}
}

// pythonCheckpointFixture is a checkpoint.json shaped exactly like
// DAGState.model_dump() written by the Python code: every DAGState field key is
// present, with realistic values (some empty). It exercises the requirement
// that a Python-written checkpoint unmarshals without error and re-marshals
// with the same key set.
const pythonCheckpointFixture = `{
  "repo_path": "/w/repo",
  "artifacts_dir": "/w/artifacts",
  "prd_path": "/w/artifacts/prd.json",
  "architecture_path": "/w/artifacts/architecture.json",
  "issues_dir": "/w/artifacts/issues",
  "original_plan_summary": "Build the thing",
  "prd_summary": "PRD summary",
  "architecture_summary": "Arch summary",
  "all_issues": [{"name": "setup", "title": "Setup", "estimated_complexity": "small"}],
  "levels": [["setup"], ["feature"]],
  "completed_issues": [{"issue_name": "setup", "outcome": "completed", "attempts": 1, "files_changed": ["x.py"]}],
  "failed_issues": [],
  "skipped_issues": [],
  "in_flight_issues": [],
  "current_level": 1,
  "replan_count": 0,
  "replan_history": [],
  "max_replans": 2,
  "git_integration_branch": "swe-af/build-1",
  "git_original_branch": "main",
  "git_initial_commit": "deadbeef",
  "git_mode": "existing",
  "pending_merge_branches": [],
  "merged_branches": ["issue/build-1-1-setup"],
  "unmerged_branches": [],
  "worktrees_dir": "/w/repo/.worktrees",
  "build_id": "build-1",
  "merge_results": [{"success": true, "summary": "merged"}],
  "integration_test_results": [],
  "accumulated_debt": [],
  "adaptation_history": [],
  "workspace_manifest": null
}`

func TestPythonCheckpointRoundTripKeySet(t *testing.T) {
	// Baseline: the exact key set Python wrote.
	var fixtureMap map[string]json.RawMessage
	if err := json.Unmarshal([]byte(pythonCheckpointFixture), &fixtureMap); err != nil {
		t.Fatalf("unmarshal fixture map: %v", err)
	}

	// Unmarshal the Python-written checkpoint into the Go struct — must succeed.
	var s DAGState
	if err := json.Unmarshal([]byte(pythonCheckpointFixture), &s); err != nil {
		t.Fatalf("unmarshal checkpoint into DAGState: %v", err)
	}
	// Present max_replans (2) must be preserved, not reset.
	if s.MaxReplans != 2 {
		t.Errorf("MaxReplans = %d, want 2", s.MaxReplans)
	}
	// Nested defaulted type seeded: completed issue attempts present as 1.
	if len(s.CompletedIssues) != 1 || s.CompletedIssues[0].Attempts != 1 {
		t.Errorf("completed issue not decoded correctly: %+v", s.CompletedIssues)
	}

	// Re-marshal and compare the key set.
	reB, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("re-marshal: %v", err)
	}
	var reMap map[string]json.RawMessage
	if err := json.Unmarshal(reB, &reMap); err != nil {
		t.Fatalf("unmarshal re-marshaled map: %v", err)
	}

	if !sameKeySet(fixtureMap, reMap) {
		t.Errorf("key set mismatch:\n python keys: %v\n     go keys: %v", keys(fixtureMap), keys(reMap))
	}
}

func keys(m map[string]json.RawMessage) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func sameKeySet(a, b map[string]json.RawMessage) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return false
		}
	}
	return true
}

// TestBuildResultPRURLInjection verifies BuildResult.MarshalJSON injects the
// computed pr_url key (mirroring the Python model_dump override).
func TestBuildResultPRURLInjection(t *testing.T) {
	br := BuildResult{
		PlanResult: map[string]any{},
		DAGState:   map[string]any{},
		Success:    true,
		Summary:    "ok",
		PRResults: []RepoPRResult{
			{RepoName: "a", Success: false, PRURL: "https://x/1"},
			{RepoName: "b", Success: true, PRURL: "https://x/2"},
		},
	}
	b, err := json.Marshal(br)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if m["pr_url"] != "https://x/2" {
		t.Errorf("pr_url = %v, want %q (first successful)", m["pr_url"], "https://x/2")
	}
}

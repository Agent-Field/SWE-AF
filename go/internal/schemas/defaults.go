package schemas

import "encoding/json"

// This file implements the non-zero-default seeding for every struct that has
// at least one field whose Pydantic default is not the Go zero value.
//
// Go's json.Unmarshal leaves an absent key at the Go zero value, whereas
// Pydantic fills the declared default. Each struct below therefore gets a
// defaultXxx() constructor (also usable as the deterministic fallback struct a
// role returns on a harness parse-failure) plus an UnmarshalJSON that seeds the
// default before decoding — so an absent key keeps the default while a present
// key (even false/0/"") overrides it, matching Pydantic exactly.
//
// The `type alias X` trick strips X's methods (including UnmarshalJSON) so the
// inner json.Unmarshal does not recurse; nested field types keep their own
// UnmarshalJSON.

// --- planning.go ---

func defaultReviewResult() ReviewResult {
	return ReviewResult{ComplexityAssessment: "appropriate"}
}

// UnmarshalJSON seeds ReviewResult.ComplexityAssessment = "appropriate".
func (r *ReviewResult) UnmarshalJSON(b []byte) error {
	*r = defaultReviewResult()
	type alias ReviewResult
	return json.Unmarshal(b, (*alias)(r))
}

func defaultIssueGuidance() IssueGuidance {
	return IssueGuidance{NeedsNewTests: true, EstimatedScope: "medium"}
}

// UnmarshalJSON seeds IssueGuidance.NeedsNewTests = true, EstimatedScope = "medium".
func (g *IssueGuidance) UnmarshalJSON(b []byte) error {
	*g = defaultIssueGuidance()
	type alias IssueGuidance
	return json.Unmarshal(b, (*alias)(g))
}

func defaultPlannedIssue() PlannedIssue {
	return PlannedIssue{EstimatedComplexity: "medium"}
}

// UnmarshalJSON seeds PlannedIssue.EstimatedComplexity = "medium".
func (p *PlannedIssue) UnmarshalJSON(b []byte) error {
	*p = defaultPlannedIssue()
	type alias PlannedIssue
	return json.Unmarshal(b, (*alias)(p))
}

// --- execution.go ---

func defaultRepoSpec() RepoSpec {
	return RepoSpec{CreatePR: true}
}

// UnmarshalJSON seeds RepoSpec.CreatePR = true.
func (r *RepoSpec) UnmarshalJSON(b []byte) error {
	*r = defaultRepoSpec()
	type alias RepoSpec
	return json.Unmarshal(b, (*alias)(r))
}

func defaultWorkspaceRepo() WorkspaceRepo {
	return WorkspaceRepo{CreatePR: true}
}

// UnmarshalJSON seeds WorkspaceRepo.CreatePR = true.
func (w *WorkspaceRepo) UnmarshalJSON(b []byte) error {
	*w = defaultWorkspaceRepo()
	type alias WorkspaceRepo
	return json.Unmarshal(b, (*alias)(w))
}

func defaultIssueAdaptation() IssueAdaptation {
	return IssueAdaptation{Severity: "medium"}
}

// UnmarshalJSON seeds IssueAdaptation.Severity = "medium".
func (a *IssueAdaptation) UnmarshalJSON(b []byte) error {
	*a = defaultIssueAdaptation()
	type alias IssueAdaptation
	return json.Unmarshal(b, (*alias)(a))
}

func defaultIssueAdvisorDecision() IssueAdvisorDecision {
	return IssueAdvisorDecision{Confidence: 0.5, DebtSeverity: "medium"}
}

// UnmarshalJSON seeds IssueAdvisorDecision.Confidence = 0.5, DebtSeverity = "medium".
func (d *IssueAdvisorDecision) UnmarshalJSON(b []byte) error {
	*d = defaultIssueAdvisorDecision()
	type alias IssueAdvisorDecision
	return json.Unmarshal(b, (*alias)(d))
}

func defaultIssueResult() IssueResult {
	return IssueResult{Attempts: 1}
}

// UnmarshalJSON seeds IssueResult.Attempts = 1.
func (r *IssueResult) UnmarshalJSON(b []byte) error {
	*r = defaultIssueResult()
	type alias IssueResult
	return json.Unmarshal(b, (*alias)(r))
}

func defaultDAGState() DAGState {
	return DAGState{MaxReplans: 2}
}

// UnmarshalJSON seeds DAGState.MaxReplans = 2.
func (s *DAGState) UnmarshalJSON(b []byte) error {
	*s = defaultDAGState()
	type alias DAGState
	return json.Unmarshal(b, (*alias)(s))
}

func defaultRetryAdvice() RetryAdvice {
	return RetryAdvice{Confidence: 0.5}
}

// UnmarshalJSON seeds RetryAdvice.Confidence = 0.5.
func (a *RetryAdvice) UnmarshalJSON(b []byte) error {
	*a = defaultRetryAdvice()
	type alias RetryAdvice
	return json.Unmarshal(b, (*alias)(a))
}

func defaultCoderResult() CoderResult {
	return CoderResult{Complete: true}
}

// UnmarshalJSON seeds CoderResult.Complete = true.
func (c *CoderResult) UnmarshalJSON(b []byte) error {
	*c = defaultCoderResult()
	type alias CoderResult
	return json.Unmarshal(b, (*alias)(c))
}

// --- fast.go ---

func defaultFastTask() FastTask {
	return FastTask{EstimatedMinutes: 5}
}

// UnmarshalJSON seeds FastTask.EstimatedMinutes = 5.
func (t *FastTask) UnmarshalJSON(b []byte) error {
	*t = defaultFastTask()
	type alias FastTask
	return json.Unmarshal(b, (*alias)(t))
}

// --- askuser.go ---

func defaultAskUserForm() AskUserForm {
	return AskUserForm{SubmitLabel: "Submit"}
}

// UnmarshalJSON seeds AskUserForm.SubmitLabel = "Submit".
func (f *AskUserForm) UnmarshalJSON(b []byte) error {
	*f = defaultAskUserForm()
	type alias AskUserForm
	return json.Unmarshal(b, (*alias)(f))
}

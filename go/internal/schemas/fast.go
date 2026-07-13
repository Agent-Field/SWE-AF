package schemas

// This file ports the data models from fast/schemas.py (not FastBuildConfig —
// that config model lives in internal/config).

// FastTask is a single task in the flat fast-build decomposition.
//
// EstimatedMinutes has a non-zero Pydantic default (5) — seeded in defaults.go.
type FastTask struct {
	Name               string   `json:"name"`                // kebab-case slug
	Title              string   `json:"title"`               // human-readable title
	Description        string   `json:"description"`         // self-contained description for the coder
	AcceptanceCriteria []string `json:"acceptance_criteria"` // task-specific acceptance criteria
	FilesToCreate      []string `json:"files_to_create"`
	FilesToModify      []string `json:"files_to_modify"`
	EstimatedMinutes   int      `json:"estimated_minutes"`
}

// FastPlanResult is the output of the fast planner reasoner.
type FastPlanResult struct {
	Tasks        []FastTask `json:"tasks"`
	Rationale    string     `json:"rationale"`
	FallbackUsed bool       `json:"fallback_used"`
}

// FastTaskResult is the result of executing a single FastTask.
type FastTaskResult struct {
	TaskName     string   `json:"task_name"`
	Outcome      string   `json:"outcome"` // "completed" | "failed" | "timeout"
	FilesChanged []string `json:"files_changed"`
	Summary      string   `json:"summary"`
	Error        string   `json:"error"`
}

// FastExecutionResult is the aggregate result of executing all tasks.
type FastExecutionResult struct {
	TaskResults    []FastTaskResult `json:"task_results"`
	CompletedCount int              `json:"completed_count"`
	FailedCount    int              `json:"failed_count"`
	TimedOut       bool             `json:"timed_out"`
}

// FastVerificationResult is the result of the single verification pass.
type FastVerificationResult struct {
	Passed          bool             `json:"passed"`
	Summary         string           `json:"summary"`
	CriteriaResults []map[string]any `json:"criteria_results"`
	SuggestedFixes  []string         `json:"suggested_fixes"`
}

// FastBuildResult is the top-level result returned by the fast build reasoner.
//
// Verification is dict|None; PRURL is a plain field (default "").
type FastBuildResult struct {
	PlanResult      map[string]any `json:"plan_result"`
	ExecutionResult map[string]any `json:"execution_result"`
	Verification    map[string]any `json:"verification"`
	Success         bool           `json:"success"`
	Summary         string         `json:"summary"`
	PRURL           string         `json:"pr_url"`
}

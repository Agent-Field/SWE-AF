package schemas

// This file ports reasoners/schemas.py — the planning-pipeline artifacts.

// PRD is the Product Requirements Document produced by the product manager.
type PRD struct {
	ValidatedDescription string       `json:"validated_description"`
	AcceptanceCriteria   []string     `json:"acceptance_criteria"`
	MustHave             []string     `json:"must_have"`
	NiceToHave           []string     `json:"nice_to_have"`
	OutOfScope           []string     `json:"out_of_scope"`
	Assumptions          []string     `json:"assumptions"`
	Risks                []string     `json:"risks"`
	AskUserForm          *AskUserForm `json:"ask_user_form"`
}

// ArchitectureComponent is a single component in the architecture.
type ArchitectureComponent struct {
	Name           string   `json:"name"`
	Responsibility string   `json:"responsibility"`
	TouchesFiles   []string `json:"touches_files"`
	DependsOn      []string `json:"depends_on"`
}

// ArchitectureDecision is a key architectural decision with rationale.
type ArchitectureDecision struct {
	Decision  string `json:"decision"`
	Rationale string `json:"rationale"`
}

// Architecture is the architecture document produced by the architect.
type Architecture struct {
	Summary             string                  `json:"summary"`
	Components          []ArchitectureComponent `json:"components"`
	Interfaces          []string                `json:"interfaces"`
	Decisions           []ArchitectureDecision  `json:"decisions"`
	FileChangesOverview string                  `json:"file_changes_overview"`
}

// ReviewResult is the tech lead review of the architecture.
//
// ComplexityAssessment has a non-zero Pydantic default ("appropriate") —
// seeded in defaults.go.
type ReviewResult struct {
	Approved             bool     `json:"approved"`
	Feedback             string   `json:"feedback"`
	ScopeIssues          []string `json:"scope_issues"`
	ComplexityAssessment string   `json:"complexity_assessment"`
	Summary              string   `json:"summary"`
}

// IssueGuidance is per-issue guidance from the sprint planner that shapes
// downstream agent behavior.
//
// NeedsNewTests (true) and EstimatedScope ("medium") have non-zero Pydantic
// defaults — seeded in defaults.go.
type IssueGuidance struct {
	// Structured — drives loop routing
	NeedsNewTests     bool   `json:"needs_new_tests"`
	EstimatedScope    string `json:"estimated_scope"` // "trivial" | "small" | "medium" | "large"
	TouchesInterfaces bool   `json:"touches_interfaces"`
	NeedsDeeperQA     bool   `json:"needs_deeper_qa"` // True => flagged path (QA + reviewer + synthesizer)

	// Freeform — shapes agent behavior
	TestingGuidance string `json:"testing_guidance"` // Proportional test instructions
	ReviewFocus     string `json:"review_focus"`     // What reviewer should focus on
	RiskRationale   string `json:"risk_rationale"`   // Why this needs (or doesn't need) deep QA
}

// PlannedIssue is a single issue in the sprint plan.
//
// EstimatedComplexity has a non-zero Pydantic default ("medium") — seeded in
// defaults.go. SequenceNumber and Guidance are Optional (default None) so map
// to pointers.
type PlannedIssue struct {
	Name                string         `json:"name"`
	Title               string         `json:"title"`
	Description         string         `json:"description"`
	AcceptanceCriteria  []string       `json:"acceptance_criteria"`
	DependsOn           []string       `json:"depends_on"`
	Provides            []string       `json:"provides"`
	EstimatedComplexity string         `json:"estimated_complexity"`
	FilesToCreate       []string       `json:"files_to_create"`
	FilesToModify       []string       `json:"files_to_modify"`
	TestingStrategy     string         `json:"testing_strategy"`
	SequenceNumber      *int           `json:"sequence_number"`
	Guidance            *IssueGuidance `json:"guidance"`
	TargetRepo          string         `json:"target_repo"`
}

// PlanResult is the final output of the planning pipeline.
type PlanResult struct {
	PRD           PRD              `json:"prd"`
	Architecture  Architecture     `json:"architecture"`
	Review        ReviewResult     `json:"review"`
	Issues        []PlannedIssue   `json:"issues"`
	Levels        [][]string       `json:"levels"`
	FileConflicts []map[string]any `json:"file_conflicts"` // Informational only
	ArtifactsDir  string           `json:"artifacts_dir"`
	Rationale     string           `json:"rationale"`
}

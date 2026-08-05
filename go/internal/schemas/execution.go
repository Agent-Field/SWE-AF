package schemas

import (
	"encoding/json"
	"strings"
)

// This file ports the data/result models (not the config models — those live
// in internal/config) from execution/schemas.py.

// StrList is a []string that also accepts a bare JSON string, coercing it to a
// one-element list (blank → empty). Ports the ensure_str_list before-validator
// in execution/schemas.py: weaker models sometimes emit a single criterion as
// a string where the schema wants a list, and pre-fix checkpoints may have
// persisted that shape — decoding must tolerate both.
type StrList []string

// UnmarshalJSON accepts either a JSON array of strings or a bare string.
func (s *StrList) UnmarshalJSON(b []byte) error {
	var one string
	if err := json.Unmarshal(b, &one); err == nil {
		if strings.TrimSpace(one) == "" {
			*s = StrList{}
		} else {
			*s = StrList{one}
		}
		return nil
	}
	var many []string
	if err := json.Unmarshal(b, &many); err != nil {
		return err
	}
	*s = StrList(many)
	return nil
}

// ---------------------------------------------------------------------------
// Multi-repo models
// ---------------------------------------------------------------------------

// RepoSpec is the specification for a single repository in a multi-repo build.
//
// CreatePR has a non-zero Pydantic default (true) — seeded in defaults.go.
type RepoSpec struct {
	RepoURL     string   `json:"repo_url"`     // GitHub/git URL (required if repo_path empty)
	RepoPath    string   `json:"repo_path"`    // Absolute path to an existing local repo
	Role        string   `json:"role"`         // 'primary' or 'dependency'
	Branch      string   `json:"branch"`       // Branch to checkout (empty = default branch)
	SparsePaths []string `json:"sparse_paths"` // For sparse checkout; empty = full checkout
	MountPoint  string   `json:"mount_point"`  // Workspace subdirectory override
	CreatePR    bool     `json:"create_pr"`    // Whether to create a PR for this repo
}

// WorkspaceRepo is a repository that has been cloned into the workspace.
//
// CreatePR has a non-zero Pydantic default (true) — seeded in defaults.go.
// GitInitResult is dict|None (populated by _init_all_repos after cloning).
type WorkspaceRepo struct {
	RepoName      string         `json:"repo_name"`       // Derived name
	RepoURL       string         `json:"repo_url"`        // Original git URL
	Role          string         `json:"role"`            // 'primary' or 'dependency'
	AbsolutePath  string         `json:"absolute_path"`   // Path where the repo was cloned
	Branch        string         `json:"branch"`          // Actual checked-out branch
	SparsePaths   []string       `json:"sparse_paths"`    //
	CreatePR      bool           `json:"create_pr"`       //
	GitInitResult map[string]any `json:"git_init_result"` // Populated by _init_all_repos
}

// WorkspaceManifest is a snapshot of all repositories cloned for a multi-repo
// build.
type WorkspaceManifest struct {
	WorkspaceRoot   string          `json:"workspace_root"`    // Parent directory containing all repos
	Repos           []WorkspaceRepo `json:"repos"`             // All cloned repos
	PrimaryRepoName string          `json:"primary_repo_name"` // Name of the primary repo
}

// PrimaryRepo returns the primary WorkspaceRepo, or nil if not found. Ports the
// WorkspaceManifest.primary_repo property.
func (m *WorkspaceManifest) PrimaryRepo() *WorkspaceRepo {
	for i := range m.Repos {
		if m.Repos[i].RepoName == m.PrimaryRepoName {
			return &m.Repos[i]
		}
	}
	return nil
}

// RepoPRResult is the result of creating a PR for a single repository.
type RepoPRResult struct {
	RepoName     string `json:"repo_name"`
	RepoURL      string `json:"repo_url"`
	Success      bool   `json:"success"`
	PRURL        string `json:"pr_url"`
	PRNumber     int    `json:"pr_number"`
	ErrorMessage string `json:"error_message"`
}

// ---------------------------------------------------------------------------
// Adaptation / advisor / replan models
// ---------------------------------------------------------------------------

// IssueAdaptation records one AC/scope modification, accumulated as technical
// debt.
//
// Severity has a non-zero Pydantic default ("medium") — seeded in defaults.go.
type IssueAdaptation struct {
	AdaptationType             AdvisorAction `json:"adaptation_type"`
	OriginalAcceptanceCriteria []string      `json:"original_acceptance_criteria"`
	ModifiedAcceptanceCriteria []string      `json:"modified_acceptance_criteria"`
	DroppedCriteria            []string      `json:"dropped_criteria"`
	FailureDiagnosis           string        `json:"failure_diagnosis"`
	Rationale                  string        `json:"rationale"`
	NewApproach                string        `json:"new_approach"`
	MissingFunctionality       []string      `json:"missing_functionality"`
	DownstreamImpact           string        `json:"downstream_impact"`
	Severity                   string        `json:"severity"`
}

// SplitIssueSpec is a sub-issue spec when the advisor decides to SPLIT.
type SplitIssueSpec struct {
	Name               string  `json:"name"`
	Title              string  `json:"title"`
	Description        string  `json:"description"`
	AcceptanceCriteria StrList `json:"acceptance_criteria"`
	DependsOn          StrList `json:"depends_on"`
	Provides           StrList `json:"provides"`
	FilesToCreate      StrList `json:"files_to_create"`
	FilesToModify      StrList `json:"files_to_modify"`
	ParentIssueName    string  `json:"parent_issue_name"`
}

// IssueAdvisorDecision is the structured output from the Issue Advisor agent.
//
// Confidence (0.5) and DebtSeverity ("medium") have non-zero Pydantic defaults
// — seeded in defaults.go. AskUserForm is Optional (default None).
type IssueAdvisorDecision struct {
	Action           AdvisorAction `json:"action"`
	FailureDiagnosis string        `json:"failure_diagnosis"`
	FailureCategory  string        `json:"failure_category"` // environment|logic|dependency|approach|scope
	Rationale        string        `json:"rationale"`
	Confidence       float64       `json:"confidence"`
	// RETRY_MODIFIED
	ModifiedAcceptanceCriteria []string `json:"modified_acceptance_criteria"`
	DroppedCriteria            []string `json:"dropped_criteria"`
	ModificationJustification  string   `json:"modification_justification"`
	// RETRY_APPROACH
	NewApproach     string   `json:"new_approach"`
	ApproachChanges []string `json:"approach_changes"`
	// SPLIT
	SubIssues      []SplitIssueSpec `json:"sub_issues"`
	SplitRationale string           `json:"split_rationale"`
	// ACCEPT_WITH_DEBT
	MissingFunctionality []string `json:"missing_functionality"`
	DebtSeverity         string   `json:"debt_severity"`
	// ESCALATE_TO_REPLAN
	EscalationReason       string `json:"escalation_reason"`
	DAGImpact              string `json:"dag_impact"`
	SuggestedRestructuring string `json:"suggested_restructuring"`
	// Always
	DownstreamImpact string `json:"downstream_impact"`
	Summary          string `json:"summary"`
	// HITL
	AskUserForm *AskUserForm `json:"ask_user_form"`
}

// IssueResult is the result of executing a single issue.
//
// Attempts has a non-zero Pydantic default (1) — seeded in defaults.go.
// SplitRequest is list[SplitIssueSpec]|None so maps to a pointer-to-slice.
type IssueResult struct {
	IssueName     string       `json:"issue_name"`
	Outcome       IssueOutcome `json:"outcome"`
	ResultSummary string       `json:"result_summary"`
	ErrorMessage  string       `json:"error_message"`
	ErrorContext  string       `json:"error_context"` // traceback/logs for replanner
	Attempts      int          `json:"attempts"`
	FilesChanged  []string     `json:"files_changed"`
	BranchName    string       `json:"branch_name"`
	RepoName      string       `json:"repo_name"` // Repo where this issue was coded
	// Advisor fields
	AdvisorInvocations      int               `json:"advisor_invocations"`
	Adaptations             []IssueAdaptation `json:"adaptations"`
	DebtItems               []map[string]any  `json:"debt_items"`
	SplitRequest            *[]SplitIssueSpec `json:"split_request"`
	EscalationContext       string            `json:"escalation_context"`
	FinalAcceptanceCriteria StrList           `json:"final_acceptance_criteria"`
	IterationHistory        []map[string]any  `json:"iteration_history"`
}

// LevelResult is the aggregated result of executing all issues in a single
// level.
type LevelResult struct {
	LevelIndex int           `json:"level_index"`
	Completed  []IssueResult `json:"completed"`
	Failed     []IssueResult `json:"failed"`
	Skipped    []IssueResult `json:"skipped"`
}

// ReplanDecision is the structured output from the replanner agent.
//
// AskUserForm is Optional (default None).
type ReplanDecision struct {
	Action            ReplanAction     `json:"action"`
	Rationale         string           `json:"rationale"`
	UpdatedIssues     []map[string]any `json:"updated_issues"` // modified remaining issues
	RemovedIssueNames []string         `json:"removed_issue_names"`
	SkippedIssueNames []string         `json:"skipped_issue_names"`
	NewIssues         []map[string]any `json:"new_issues"`
	Summary           string           `json:"summary"`
	// HITL
	AskUserForm *AskUserForm `json:"ask_user_form"`
}

// DAGState is the full execution state of the DAG — passed to the replanner for
// context and persisted as the resume checkpoint.
//
// MaxReplans has a non-zero Pydantic default (2) — seeded in defaults.go.
// WorkspaceManifest is a serialised dict|None (kept as map[string]any for JSON
// checkpoint compatibility).
type DAGState struct {
	// --- Artifact paths ---
	RepoPath         string `json:"repo_path"`
	ArtifactsDir     string `json:"artifacts_dir"`
	PRDPath          string `json:"prd_path"`
	ArchitecturePath string `json:"architecture_path"`
	IssuesDir        string `json:"issues_dir"`

	// --- Plan context ---
	OriginalPlanSummary string `json:"original_plan_summary"`
	PRDSummary          string `json:"prd_summary"`
	ArchitectureSummary string `json:"architecture_summary"`

	// --- Issue tracking ---
	AllIssues []map[string]any `json:"all_issues"` // full PlannedIssue dicts
	Levels    [][]string       `json:"levels"`     // parallel execution levels

	// --- Execution progress ---
	CompletedIssues []IssueResult `json:"completed_issues"`
	FailedIssues    []IssueResult `json:"failed_issues"`
	SkippedIssues   []string      `json:"skipped_issues"`
	InFlightIssues  []string      `json:"in_flight_issues"` // names currently executing
	CurrentLevel    int           `json:"current_level"`

	// --- Replan tracking ---
	ReplanCount   int              `json:"replan_count"`
	ReplanHistory []ReplanDecision `json:"replan_history"`
	MaxReplans    int              `json:"max_replans"`

	// --- Git branch tracking ---
	GitIntegrationBranch string   `json:"git_integration_branch"`
	GitOriginalBranch    string   `json:"git_original_branch"`
	GitInitialCommit     string   `json:"git_initial_commit"`
	GitMode              string   `json:"git_mode"` // "fresh" or "existing"
	PendingMergeBranches []string `json:"pending_merge_branches"`
	MergedBranches       []string `json:"merged_branches"`
	UnmergedBranches     []string `json:"unmerged_branches"` // branches that failed to merge
	WorktreesDir         string   `json:"worktrees_dir"`
	BuildID              string   `json:"build_id"` // unique per build() call

	// --- Merge/test history ---
	MergeResults           []map[string]any `json:"merge_results"`
	IntegrationTestResults []map[string]any `json:"integration_test_results"`

	// --- Debt tracking ---
	AccumulatedDebt   []map[string]any `json:"accumulated_debt"`
	AdaptationHistory []map[string]any `json:"adaptation_history"`

	// --- Multi-repo workspace ---
	WorkspaceManifest map[string]any `json:"workspace_manifest"` // Serialised WorkspaceManifest
}

// MarshalJSON normalises nil slices to empty slices before serialising, so the
// ~12 list fields never emit as null. Python's pydantic DAGState rejects a
// present-but-null value on every list field (all_issues, levels,
// completed_issues, failed_issues, skipped_issues, in_flight_issues,
// replan_history, pending/merged/unmerged_branches, merge_results,
// integration_test_results, accumulated_debt, adaptation_history), while the
// dict|None workspace_manifest legitimately stays null (EmptyForNilSlices leaves
// maps untouched).
//
// This single override covers every DAGState serialisation point: the
// checkpoint.json write (dag/checkpoint.go saveCheckpoint), the BuildResult
// dag_state embedding, and the replanner context — a value receiver so it fires
// whether a DAGState or *DAGState is marshalled. The alias strips the method set
// (both MarshalJSON and the seeding UnmarshalJSON) to avoid recursion.
func (s DAGState) MarshalJSON() ([]byte, error) {
	// s is already a copy (value receiver); normalise its nil slices in place.
	// nil↔empty is behaviourally identical in Go, so the shallow copy's shared
	// backing arrays are unaffected in any observable way.
	EmptyForNilSlices(&s)
	type alias DAGState
	return json.Marshal(alias(s))
}

// GitInitResult is the result of git initialization.
type GitInitResult struct {
	Mode                string `json:"mode"`                  // "fresh" or "existing"
	OriginalBranch      string `json:"original_branch"`       // "" for fresh, e.g. "main"
	IntegrationBranch   string `json:"integration_branch"`    // branch where merged work accumulates
	InitialCommitSHA    string `json:"initial_commit_sha"`    // commit SHA before any work
	Success             bool   `json:"success"`               //
	ErrorMessage        string `json:"error_message"`         //
	RemoteURL           string `json:"remote_url"`            // origin URL (set if repo was cloned)
	RemoteDefaultBranch string `json:"remote_default_branch"` // e.g. "main" — for PR base
	RepoName            string `json:"repo_name"`             // Repo this result belongs to
}

// WorkspaceInfo is info about a worktree created for an issue.
type WorkspaceInfo struct {
	IssueName    string `json:"issue_name"`
	BranchName   string `json:"branch_name"`
	WorktreePath string `json:"worktree_path"`
}

// MergeResult is the structured output from the merger agent.
type MergeResult struct {
	Success                  bool             `json:"success"`
	MergedBranches           []string         `json:"merged_branches"`
	FailedBranches           []string         `json:"failed_branches"`
	ConflictResolutions      []map[string]any `json:"conflict_resolutions"` // [{file, branches, resolution_strategy}]
	MergeCommitSHA           string           `json:"merge_commit_sha"`
	PreMergeSHA              string           `json:"pre_merge_sha"` // for potential rollback
	NeedsIntegrationTest     bool             `json:"needs_integration_test"`
	IntegrationTestRationale string           `json:"integration_test_rationale"`
	Summary                  string           `json:"summary"`
	RepoName                 string           `json:"repo_name"` // Repo where this merge ran
}

// IntegrationTestResult is the result of integration testing after a merge.
type IntegrationTestResult struct {
	Passed         bool             `json:"passed"`
	TestsWritten   []string         `json:"tests_written"` // test file paths
	TestsRun       int              `json:"tests_run"`
	TestsPassed    int              `json:"tests_passed"`
	TestsFailed    int              `json:"tests_failed"`
	FailureDetails []map[string]any `json:"failure_details"` // [{test_name, error, file}]
	Summary        string           `json:"summary"`
}

// RetryAdvice is the structured output from the retry advisor agent.
//
// Confidence has a non-zero Pydantic default (0.5) — seeded in defaults.go.
type RetryAdvice struct {
	ShouldRetry     bool    `json:"should_retry"`
	Diagnosis       string  `json:"diagnosis"`        // Root cause analysis
	Strategy        string  `json:"strategy"`         // What to do differently
	ModifiedContext string  `json:"modified_context"` // Additional guidance to inject into retry
	Confidence      float64 `json:"confidence"`       // 0.0-1.0
}

// CriterionResult is the verification result for a single acceptance criterion.
type CriterionResult struct {
	Criterion string `json:"criterion"`
	Passed    bool   `json:"passed"`
	Evidence  string `json:"evidence"`   // What the verifier found
	IssueName string `json:"issue_name"` // Which issue was responsible
}

// VerificationResult is the structured output from the verifier agent.
type VerificationResult struct {
	Passed          bool              `json:"passed"`
	CriteriaResults []CriterionResult `json:"criteria_results"`
	Summary         string            `json:"summary"`
	SuggestedFixes  []string          `json:"suggested_fixes"`
}

// ---------------------------------------------------------------------------
// Coding loop schemas
// ---------------------------------------------------------------------------

// CoderResult is the output from the coder agent.
//
// Complete has a non-zero Pydantic default (true) — seeded in defaults.go.
// TestsPassed is bool|None (default None) so maps to a pointer.
type CoderResult struct {
	FilesChanged      []string       `json:"files_changed"`
	Summary           string         `json:"summary"`
	Complete          bool           `json:"complete"`
	IterationID       string         `json:"iteration_id"`
	TestsPassed       *bool          `json:"tests_passed"`       // Self-reported: did tests pass?
	TestSummary       string         `json:"test_summary"`       // Brief test run output
	CodebaseLearnings []string       `json:"codebase_learnings"` // Conventions discovered
	AgentRetro        map[string]any `json:"agent_retro"`        // What worked, what didn't
	RepoName          string         `json:"repo_name"`          // Repo where coder ran
}

// QAResult is the output from the QA/tester agent.
type QAResult struct {
	Passed       bool             `json:"passed"`
	Summary      string           `json:"summary"`
	TestFailures []map[string]any `json:"test_failures"` // [{test_name, file, error, expected, actual}]
	CoverageGaps []string         `json:"coverage_gaps"` // ACs without test coverage
	IterationID  string           `json:"iteration_id"`
}

// CodeReviewResult is the output from the code reviewer agent.
type CodeReviewResult struct {
	Approved    bool             `json:"approved"`
	Summary     string           `json:"summary"`
	Blocking    bool             `json:"blocking"`   // True ONLY for security/crash/data-loss
	DebtItems   []map[string]any `json:"debt_items"` // [{severity, title, file_path, description}]
	IterationID string           `json:"iteration_id"`
}

// QASynthesisResult is the output from the feedback synthesizer agent.
type QASynthesisResult struct {
	Action      QASynthesisAction `json:"action"`
	Summary     string            `json:"summary"`
	Stuck       bool              `json:"stuck"`
	IterationID string            `json:"iteration_id"`
}

// ---------------------------------------------------------------------------
// Finalize / PR / CI result models
// ---------------------------------------------------------------------------

// RepoFinalizeResult is the result of the repo finalization (cleanup) step.
type RepoFinalizeResult struct {
	Success          bool     `json:"success"`
	FilesRemoved     []string `json:"files_removed"`
	GitignoreUpdated bool     `json:"gitignore_updated"`
	Summary          string   `json:"summary"`
}

// GitHubPRResult is the result of pushing and creating a PR on GitHub.
type GitHubPRResult struct {
	Success      bool   `json:"success"`
	PRURL        string `json:"pr_url"`
	PRNumber     int    `json:"pr_number"`
	ErrorMessage string `json:"error_message"`
}

// CIFailedCheck is one failing GitHub check on a PR.
type CIFailedCheck struct {
	Name        string `json:"name"`
	Workflow    string `json:"workflow"`
	Conclusion  string `json:"conclusion"`   // FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED, etc.
	DetailsURL  string `json:"details_url"`  //
	LogsExcerpt string `json:"logs_excerpt"` // tail of the failed job's log, truncated
}

// CIWatchResult is the outcome of waiting for CI checks on a PR.
//
// Status is a Literal in Pydantic
// ("passed"|"failed"|"timed_out"|"no_checks"|"error") but pseudo-literal
// strings stay a plain string.
type CIWatchResult struct {
	Status         string          `json:"status"`
	PRNumber       int             `json:"pr_number"`
	ElapsedSeconds int             `json:"elapsed_seconds"`
	FailedChecks   []CIFailedCheck `json:"failed_checks"`
	Summary        string          `json:"summary"`
}

// CIFixResult is the output from one iteration of the CI fixer agent.
type CIFixResult struct {
	Fixed               bool     `json:"fixed"`         // True if the agent believes it resolved all failures
	FilesChanged        []string `json:"files_changed"` //
	CommitSHA           string   `json:"commit_sha"`    // SHA of the fix commit, if pushed
	Pushed              bool     `json:"pushed"`        // True if the agent pushed the fix to origin
	Summary             string   `json:"summary"`       //
	RejectedWorkarounds []string `json:"rejected_workarounds"`
	ErrorMessage        string   `json:"error_message"`
}

// ReviewCommentRef is one review comment on an existing PR that the resolver
// should consider.
type ReviewCommentRef struct {
	CommentID int    `json:"comment_id"` // 0 when not a review comment
	ThreadID  string `json:"thread_id"`  // GraphQL node id of the review thread
	Path      string `json:"path"`       // File path the comment is anchored to
	Line      int    `json:"line"`       // Line number the comment is anchored to
	Author    string `json:"author"`     // GitHub login of the commenter
	Body      string `json:"body"`       // The comment body (markdown)
	URL       string `json:"url"`        // html_url for the comment
}

// AddressedComment is the resolver agent's record of one comment it claims to
// have addressed.
type AddressedComment struct {
	CommentID int    `json:"comment_id"`
	ThreadID  string `json:"thread_id"`
	Addressed bool   `json:"addressed"`
	Note      string `json:"note"` // one-line: "fixed by ...", "skipped because ..."
}

// PRResolveResult is the output from one run of the PR-resolver agent.
type PRResolveResult struct {
	Fixed               bool               `json:"fixed"`          // True if the agent believes it produced a correct, complete fix
	MergeResolved       bool               `json:"merge_resolved"` // True iff a merge from base was completed
	FilesChanged        []string           `json:"files_changed"`
	CommitSHAs          []string           `json:"commit_shas"` // All new commits the agent created
	Pushed              bool               `json:"pushed"`      // True if `git push` succeeded
	AddressedComments   []AddressedComment `json:"addressed_comments"`
	Summary             string             `json:"summary"`
	RejectedWorkarounds []string           `json:"rejected_workarounds"`
	ErrorMessage        string             `json:"error_message"`
}

// BuildResult is the final output of the end-to-end build pipeline.
//
// Verification is dict|None. PRURL() ports the computed pr_url property, and
// MarshalJSON injects it into serialisation output exactly as Python's
// model_dump() override does.
type BuildResult struct {
	PlanResult    map[string]any   `json:"plan_result"`
	DAGState      map[string]any   `json:"dag_state"`
	Verification  map[string]any   `json:"verification"`
	Success       bool             `json:"success"`
	Summary       string           `json:"summary"`
	PRResults     []RepoPRResult   `json:"pr_results"`      // Per-repo PR creation results
	CIGateResults []map[string]any `json:"ci_gate_results"` // Per-repo post-PR CI gate result
}

// PRURL returns the first successful PR URL, or empty string (backward-compat).
// Ports BuildResult.pr_url.
func (b BuildResult) PRURL() string {
	for _, r := range b.PRResults {
		if r.Success && r.PRURL != "" {
			return r.PRURL
		}
	}
	return ""
}

// MarshalJSON injects the computed pr_url key into the serialised object,
// mirroring the Python BuildResult.model_dump() override.
func (b BuildResult) MarshalJSON() ([]byte, error) {
	type alias BuildResult
	base, err := json.Marshal(alias(b))
	if err != nil {
		return nil, err
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(base, &m); err != nil {
		return nil, err
	}
	prURL, err := json.Marshal(b.PRURL())
	if err != nil {
		return nil, err
	}
	m["pr_url"] = prURL
	return json.Marshal(m)
}

package config

// ExecutionConfig ports execution/schemas.py::ExecutionConfig — configuration
// for the DAG executor. The 17 role model strings are resolved once at load
// (model_post_init) into resolvedModels and read via the *Model accessors.
type ExecutionConfig struct {
	Runtime string            `json:"runtime"`
	Models  map[string]string `json:"models"`

	MaxRetriesPerIssue         int     `json:"max_retries_per_issue"`
	MaxReplans                 int     `json:"max_replans"`
	EnableReplanning           bool    `json:"enable_replanning"`
	MaxIntegrationTestRetries  int     `json:"max_integration_test_retries"`
	EnableIntegrationTesting   bool    `json:"enable_integration_testing"`
	MaxCodingIterations        int     `json:"max_coding_iterations"`
	AgentMaxTurns              int     `json:"agent_max_turns"`
	PermissionMode             string  `json:"permission_mode"`
	AgentTimeoutSeconds        int     `json:"agent_timeout_seconds"`
	MaxAdvisorInvocations      int     `json:"max_advisor_invocations"`
	EnableIssueAdvisor         bool    `json:"enable_issue_advisor"`
	EnableLearning             bool    `json:"enable_learning"`
	MaxConcurrentIssues        int     `json:"max_concurrent_issues"`
	LevelFailureAbortThreshold float64 `json:"level_failure_abort_threshold"`
	CheckCI                    bool    `json:"check_ci"`
	MaxCIFixCycles             int     `json:"max_ci_fix_cycles"`
	CIWaitSeconds              int     `json:"ci_wait_seconds"`
	CIPollSeconds              int     `json:"ci_poll_seconds"`

	// resolvedModels is the flat role→model map computed at load. Not a JSON
	// field (ports the PrivateAttr _resolved_models).
	resolvedModels map[string]string
}

// defaultExecutionConfig seeds every non-zero Pydantic default. Note
// MaxRetriesPerIssue defaults to 1 here (vs BuildConfig's 2) — the divergence is
// intentional and preserved.
func defaultExecutionConfig() ExecutionConfig {
	return ExecutionConfig{
		Runtime:                    DefaultRuntime(),
		Models:                     nil,
		MaxRetriesPerIssue:         1,
		MaxReplans:                 2,
		EnableReplanning:           true,
		MaxIntegrationTestRetries:  1,
		EnableIntegrationTesting:   true,
		MaxCodingIterations:        5,
		AgentMaxTurns:              DefaultAgentMaxTurns,
		PermissionMode:             "",
		AgentTimeoutSeconds:        2700,
		MaxAdvisorInvocations:      2,
		EnableIssueAdvisor:         true,
		EnableLearning:             false,
		MaxConcurrentIssues:        3,
		LevelFailureAbortThreshold: 0.8,
		CheckCI:                    true,
		MaxCIFixCycles:             2,
		CIWaitSeconds:              1500,
		CIPollSeconds:              30,
	}
}

// LoadExecutionConfig constructs an ExecutionConfig from a raw input map,
// reproducing the Pydantic pipeline: legacy-key rejection → strict decode
// (extra="forbid") → provider normalization (_normalize_provider_field) →
// model resolution (model_post_init). Model resolution errors (unsupported
// runtime, unknown model key) surface here as they do at Python construction.
func LoadExecutionConfig(raw map[string]any) (*ExecutionConfig, error) {
	if raw == nil {
		raw = map[string]any{}
	}
	if err := rejectLegacyConfigKeys(raw); err != nil {
		return nil, err
	}

	cfg := defaultExecutionConfig()
	if err := strictDecode(raw, &cfg); err != nil {
		return nil, err
	}

	// _normalize_provider_field: map the legacy "claude" alias to "claude_code".
	if cfg.Runtime == "claude" {
		cfg.Runtime = "claude_code"
	}

	// model_post_init: resolve runtime model selection once at construction.
	resolved, err := ResolveRuntimeModels(cfg.Runtime, cfg.Models, nil)
	if err != nil {
		return nil, err
	}
	cfg.resolvedModels = resolved

	return &cfg, nil
}

// modelFor ports ExecutionConfig._model_for.
func (c *ExecutionConfig) modelFor(field string) string {
	return c.resolvedModels[field]
}

// AIProvider ports ExecutionConfig.ai_provider.
func (c *ExecutionConfig) AIProvider() string {
	return runtimeToProvider(c.Runtime)
}

// The 17 *Model accessors port the corresponding Pydantic properties.

func (c *ExecutionConfig) PMModel() string            { return c.modelFor("pm_model") }
func (c *ExecutionConfig) ArchitectModel() string     { return c.modelFor("architect_model") }
func (c *ExecutionConfig) TechLeadModel() string      { return c.modelFor("tech_lead_model") }
func (c *ExecutionConfig) SprintPlannerModel() string { return c.modelFor("sprint_planner_model") }
func (c *ExecutionConfig) CoderModel() string         { return c.modelFor("coder_model") }
func (c *ExecutionConfig) QAModel() string            { return c.modelFor("qa_model") }
func (c *ExecutionConfig) CodeReviewerModel() string  { return c.modelFor("code_reviewer_model") }
func (c *ExecutionConfig) QASynthesizerModel() string { return c.modelFor("qa_synthesizer_model") }
func (c *ExecutionConfig) ReplanModel() string        { return c.modelFor("replan_model") }
func (c *ExecutionConfig) RetryAdvisorModel() string  { return c.modelFor("retry_advisor_model") }
func (c *ExecutionConfig) IssueWriterModel() string   { return c.modelFor("issue_writer_model") }
func (c *ExecutionConfig) IssueAdvisorModel() string  { return c.modelFor("issue_advisor_model") }
func (c *ExecutionConfig) VerifierModel() string      { return c.modelFor("verifier_model") }
func (c *ExecutionConfig) GitModel() string           { return c.modelFor("git_model") }
func (c *ExecutionConfig) MergerModel() string        { return c.modelFor("merger_model") }
func (c *ExecutionConfig) IntegrationTesterModel() string {
	return c.modelFor("integration_tester_model")
}
func (c *ExecutionConfig) CIFixerModel() string { return c.modelFor("ci_fixer_model") }

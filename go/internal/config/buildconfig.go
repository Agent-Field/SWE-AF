package config

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// DefaultAgentMaxTurns ports DEFAULT_AGENT_MAX_TURNS.
const DefaultAgentMaxTurns = 150

// BuildConfig ports execution/schemas.py::BuildConfig — configuration for the
// end-to-end build pipeline. JSON tags match the Pydantic field names exactly.
// Non-zero defaults are seeded by defaultBuildConfig() before the strict decode.
type BuildConfig struct {
	Runtime string            `json:"runtime"`
	Models  map[string]string `json:"models"`

	MaxReviewIterations       int     `json:"max_review_iterations"`
	MaxPlanRevisionIterations int     `json:"max_plan_revision_iterations"`
	MaxRetriesPerIssue        int     `json:"max_retries_per_issue"`
	MaxReplans                int     `json:"max_replans"`
	EnableReplanning          bool    `json:"enable_replanning"`
	MaxVerifyFixCycles        int     `json:"max_verify_fix_cycles"`
	GitInitMaxRetries         int     `json:"git_init_max_retries"`
	GitInitRetryDelay         float64 `json:"git_init_retry_delay"`
	MaxIntegrationTestRetries int     `json:"max_integration_test_retries"`
	EnableIntegrationTesting  bool    `json:"enable_integration_testing"`
	MaxCodingIterations       int     `json:"max_coding_iterations"`
	AgentMaxTurns             int     `json:"agent_max_turns"`
	ExecuteFnTarget           string  `json:"execute_fn_target"`
	PermissionMode            string  `json:"permission_mode"`
	RepoURL                   string  `json:"repo_url"`

	Repos []schemas.RepoSpec `json:"repos"`

	EnableGithubPR             bool    `json:"enable_github_pr"`
	GithubPRBase               string  `json:"github_pr_base"`
	CheckCI                    bool    `json:"check_ci"`
	MaxCIFixCycles             int     `json:"max_ci_fix_cycles"`
	CIWaitSeconds              int     `json:"ci_wait_seconds"`
	CIPollSeconds              int     `json:"ci_poll_seconds"`
	CIStartupGraceSeconds      int     `json:"ci_startup_grace_seconds"`
	AgentTimeoutSeconds        int     `json:"agent_timeout_seconds"`
	MaxAdvisorInvocations      int     `json:"max_advisor_invocations"`
	EnableIssueAdvisor         bool    `json:"enable_issue_advisor"`
	EnableLearning             bool    `json:"enable_learning"`
	MaxConcurrentIssues        int     `json:"max_concurrent_issues"`
	LevelFailureAbortThreshold float64 `json:"level_failure_abort_threshold"`
	ApprovalExpiresInHours     int     `json:"approval_expires_in_hours"`
}

// defaultBuildConfig seeds every non-zero Pydantic default (including the
// env-derived runtime default_factory) prior to the strict decode.
func defaultBuildConfig() BuildConfig {
	return BuildConfig{
		Runtime:                    DefaultRuntime(),
		Models:                     nil,
		MaxReviewIterations:        2,
		MaxPlanRevisionIterations:  2,
		MaxRetriesPerIssue:         2,
		MaxReplans:                 2,
		EnableReplanning:           true,
		MaxVerifyFixCycles:         1,
		GitInitMaxRetries:          3,
		GitInitRetryDelay:          1.0,
		MaxIntegrationTestRetries:  1,
		EnableIntegrationTesting:   true,
		MaxCodingIterations:        5,
		AgentMaxTurns:              DefaultAgentMaxTurns,
		ExecuteFnTarget:            "",
		PermissionMode:             "",
		RepoURL:                    "",
		Repos:                      []schemas.RepoSpec{},
		EnableGithubPR:             true,
		GithubPRBase:               "",
		CheckCI:                    true,
		MaxCIFixCycles:             2,
		CIWaitSeconds:              1500,
		CIPollSeconds:              30,
		CIStartupGraceSeconds:      30,
		AgentTimeoutSeconds:        2700,
		MaxAdvisorInvocations:      2,
		EnableIssueAdvisor:         true,
		EnableLearning:             false,
		MaxConcurrentIssues:        3,
		LevelFailureAbortThreshold: 0.8,
		ApprovalExpiresInHours:     72,
	}
}

// strictDecode marshals raw and decodes it into dst with DisallowUnknownFields,
// reproducing Pydantic's ConfigDict(extra="forbid") — an unknown top-level key
// is a hard error. dst is pre-seeded with defaults, so absent keys keep the
// seeded default while present keys override (matching Pydantic).
func strictDecode(raw map[string]any, dst any) error {
	b, err := json.Marshal(raw)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

// validateRepoSpec ports the RepoSpec field validators (_validate_role,
// _validate_repo_url) with verbatim error strings.
func validateRepoSpec(r schemas.RepoSpec) error {
	if r.Role != "primary" && r.Role != "dependency" {
		return fmt.Errorf("role must be 'primary' or 'dependency', got %s", pyRepr(r.Role))
	}
	if r.RepoURL != "" &&
		!(hasPrefix(r.RepoURL, "http://") || hasPrefix(r.RepoURL, "https://") || hasPrefix(r.RepoURL, "git@")) {
		return fmt.Errorf("repo_url must be an HTTP(S) or SSH git URL, got %s", pyRepr(r.RepoURL))
	}
	return nil
}

func hasPrefix(s, prefix string) bool {
	return len(s) >= len(prefix) && s[:len(prefix)] == prefix
}

// normalizeRepos ports BuildConfig._normalize_repos with verbatim error strings.
func (c *BuildConfig) normalizeRepos() error {
	// Step 1: Mutual exclusion.
	if c.RepoURL != "" && len(c.Repos) > 0 {
		return fmt.Errorf(
			"Specify either 'repo_url' (single-repo shorthand) or 'repos' " +
				"(multi-repo list), not both.",
		)
	}

	// Step 2: Synthesise from repo_url (validating the synthesised spec, as
	// Pydantic re-runs RepoSpec validators on construction).
	if c.RepoURL != "" && len(c.Repos) == 0 {
		spec := schemas.RepoSpec{RepoURL: c.RepoURL, Role: "primary", CreatePR: true}
		if err := validateRepoSpec(spec); err != nil {
			return err
		}
		c.Repos = []schemas.RepoSpec{spec}
		return nil
	}

	// Step 3: Empty passthrough.
	if len(c.Repos) == 0 {
		return nil
	}

	// Step 4: Exactly one primary.
	primaries := 0
	for _, r := range c.Repos {
		if r.Role == "primary" {
			primaries++
		}
	}
	if primaries != 1 {
		return fmt.Errorf(
			"Exactly one RepoSpec with role='primary' is required; found %d.",
			primaries,
		)
	}

	// Step 5: No duplicate repo_url values.
	seen := make(map[string]struct{})
	count := 0
	for _, r := range c.Repos {
		if r.RepoURL != "" {
			count++
			seen[r.RepoURL] = struct{}{}
		}
	}
	if count != len(seen) {
		return fmt.Errorf("Duplicate repo_url values are not allowed in 'repos'.")
	}

	// Step 6: Backfill repo_url from primary.
	if c.RepoURL == "" {
		for _, r := range c.Repos {
			if r.Role == "primary" {
				c.RepoURL = r.RepoURL
				break
			}
		}
	}

	return nil
}

// LoadBuildConfig constructs a BuildConfig from a raw input map, reproducing the
// Pydantic pipeline in order: legacy-key rejection (_validate_v2_keys) →
// strict decode (extra="forbid") → per-RepoSpec validation → repo normalization
// (_normalize_repos) → flat-model validation (model_post_init).
func LoadBuildConfig(raw map[string]any) (*BuildConfig, error) {
	if raw == nil {
		raw = map[string]any{}
	}
	if err := rejectLegacyConfigKeys(raw); err != nil {
		return nil, err
	}

	cfg := defaultBuildConfig()
	if err := strictDecode(raw, &cfg); err != nil {
		return nil, err
	}

	// RepoSpec field validators fire during Pydantic parsing (before the
	// after-validator _normalize_repos).
	for _, r := range cfg.Repos {
		if err := validateRepoSpec(r); err != nil {
			return nil, err
		}
	}

	if err := cfg.normalizeRepos(); err != nil {
		return nil, err
	}

	// model_post_init → _validate_flat_models.
	if _, err := validateFlatModels(cfg.Models); err != nil {
		return nil, err
	}

	return &cfg, nil
}

// AIProvider ports BuildConfig.ai_provider = _runtime_to_provider(runtime).
func (c *BuildConfig) AIProvider() string {
	return runtimeToProvider(c.Runtime)
}

// PrimaryRepo ports BuildConfig.primary_repo — the primary RepoSpec, or nil.
func (c *BuildConfig) PrimaryRepo() *schemas.RepoSpec {
	for i := range c.Repos {
		if c.Repos[i].Role == "primary" {
			return &c.Repos[i]
		}
	}
	return nil
}

// ResolvedModels ports BuildConfig.resolved_models() — resolves all internal
// *_model fields from V2 runtime config. Reads env at call time.
func (c *BuildConfig) ResolvedModels() (map[string]string, error) {
	return ResolveRuntimeModels(c.Runtime, c.Models, nil)
}

// ToExecutionConfigDict ports BuildConfig.to_execution_config_dict() — the exact
// key subset carried forward into ExecutionConfig via execute(). "models" holds
// the raw override map (may be nil), mirroring Python passing self.models.
func (c *BuildConfig) ToExecutionConfigDict() map[string]any {
	var models any
	if c.Models != nil {
		models = c.Models
	}
	return map[string]any{
		"runtime":                       c.Runtime,
		"models":                        models,
		"permission_mode":               c.PermissionMode,
		"max_retries_per_issue":         c.MaxRetriesPerIssue,
		"max_replans":                   c.MaxReplans,
		"enable_replanning":             c.EnableReplanning,
		"max_integration_test_retries":  c.MaxIntegrationTestRetries,
		"enable_integration_testing":    c.EnableIntegrationTesting,
		"max_coding_iterations":         c.MaxCodingIterations,
		"agent_max_turns":               c.AgentMaxTurns,
		"agent_timeout_seconds":         c.AgentTimeoutSeconds,
		"max_advisor_invocations":       c.MaxAdvisorInvocations,
		"enable_issue_advisor":          c.EnableIssueAdvisor,
		"enable_learning":               c.EnableLearning,
		"max_concurrent_issues":         c.MaxConcurrentIssues,
		"level_failure_abort_threshold": c.LevelFailureAbortThreshold,
		"check_ci":                      c.CheckCI,
		"max_ci_fix_cycles":             c.MaxCIFixCycles,
		"ci_wait_seconds":               c.CIWaitSeconds,
		"ci_poll_seconds":               c.CIPollSeconds,
	}
}

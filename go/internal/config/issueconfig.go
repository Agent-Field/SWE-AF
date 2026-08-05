package config

// This file ports swe_af/issue/schemas.py::IssueBuildConfig — configuration for
// a single issue-level build run (the implement_issue reasoner). Like
// FastBuildConfig it has no legacy-key scan in Python (only
// ConfigDict(extra="forbid")), plus a model-key validation restricted to the
// roles the issue-level path can invoke.

import (
	"fmt"
	"sort"
	"strings"
)

// issueValidModelKeys is {"default"} | set(ISSUE_MODEL_ROLE_KEYS): the roles the
// issue-level path can invoke ("git" covers the optional PR step).
var issueValidModelKeys = map[string]struct{}{
	"default":        {},
	"coder":          {},
	"code_reviewer":  {},
	"qa":             {},
	"qa_synthesizer": {},
	"verifier":       {},
	"git":            {},
}

// IssueBuildConfig ports issue/schemas.py::IssueBuildConfig.
type IssueBuildConfig struct {
	Runtime string            `json:"runtime"`
	Models  map[string]string `json:"models"`

	MaxCodingIterations int    `json:"max_coding_iterations"`
	AgentMaxTurns       int    `json:"agent_max_turns"`
	AgentTimeoutSeconds int    `json:"agent_timeout_seconds"`
	PermissionMode      string `json:"permission_mode"`
	Verify              bool   `json:"verify"`
	EnableGithubPR      bool   `json:"enable_github_pr"`
	GithubPRBase        string `json:"github_pr_base"`
	BranchPrefix        string `json:"branch_prefix"`
	KeepWorktree        bool   `json:"keep_worktree"`
}

// defaultIssueBuildConfig seeds every non-zero Pydantic default.
func defaultIssueBuildConfig() IssueBuildConfig {
	return IssueBuildConfig{
		Runtime:             DefaultRuntime(),
		Models:              nil,
		MaxCodingIterations: 3,
		AgentMaxTurns:       50,
		AgentTimeoutSeconds: 1800,
		PermissionMode:      "",
		Verify:              true,
		EnableGithubPR:      false,
		GithubPRBase:        "",
		BranchPrefix:        "issue/",
		KeepWorktree:        false,
	}
}

// LoadIssueBuildConfig constructs an IssueBuildConfig from a raw input map:
// strict decode (extra="forbid") then issue-role model-key validation.
func LoadIssueBuildConfig(raw map[string]any) (*IssueBuildConfig, error) {
	if raw == nil {
		raw = map[string]any{}
	}
	cfg := defaultIssueBuildConfig()
	if err := strictDecode(raw, &cfg); err != nil {
		return nil, err
	}

	var unknown []string
	for k := range cfg.Models {
		if _, ok := issueValidModelKeys[k]; !ok {
			unknown = append(unknown, fmt.Sprintf("%q", k))
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		valid := make([]string, 0, len(issueValidModelKeys))
		for k := range issueValidModelKeys {
			valid = append(valid, k)
		}
		sort.Strings(valid)
		return nil, fmt.Errorf(
			"Unknown model keys for implement_issue: %s. Valid keys: %s",
			strings.Join(unknown, ", "), strings.Join(valid, ", "),
		)
	}

	return &cfg, nil
}

// ToExecutionRaw builds the raw map handed to LoadExecutionConfig — the Go
// equivalent of constructing ExecutionConfig(...) inside
// _implement_issue_impl: the coding loop runs with advisor/replanning/
// integration-testing/CI/learning all disabled.
func (c *IssueBuildConfig) ToExecutionRaw() map[string]any {
	raw := map[string]any{
		"runtime":                    c.Runtime,
		"max_coding_iterations":      c.MaxCodingIterations,
		"agent_max_turns":            c.AgentMaxTurns,
		"agent_timeout_seconds":      c.AgentTimeoutSeconds,
		"permission_mode":            c.PermissionMode,
		"enable_learning":            false,
		"enable_replanning":          false,
		"enable_issue_advisor":       false,
		"enable_integration_testing": false,
		"check_ci":                   false,
	}
	if c.Models != nil {
		raw["models"] = c.Models
	}
	return raw
}

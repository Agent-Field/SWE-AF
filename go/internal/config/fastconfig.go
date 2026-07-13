package config

import (
	"fmt"
	"os"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
)

// This file ports fast/schemas.py::FastBuildConfig and its model resolution.
// Unlike BuildConfig/ExecutionConfig, FastBuildConfig has NO legacy-key
// rejection validator in Python (only ConfigDict(extra="forbid")), so
// LoadFastBuildConfig performs strict decode without a legacy scan.

const (
	fastClaudeCodeDefault = "haiku"
	fastOpenCodeDefault   = "qwen/qwen-2.5-coder-32b-instruct"
)

// fastRoles ports _FAST_ROLES — the four resolved model keys, in order.
var fastRoles = []string{"pm_model", "coder_model", "verifier_model", "git_model"}

// fastRoleKeyPair ports _ROLE_KEY_MAP preserving insertion order (pm, coder,
// verifier, git) so per-role overrides apply in the same sequence as Python.
type fastRoleKeyPair struct {
	RoleKey     string
	ResolvedKey string
}

var fastRoleKeyMap = []fastRoleKeyPair{
	{"pm", "pm_model"},
	{"coder", "coder_model"},
	{"verifier", "verifier_model"},
	{"git", "git_model"},
}

// fastValidKeys is {"default"} | set(_ROLE_KEY_MAP.keys()).
var fastValidKeys = map[string]struct{}{
	"default":  {},
	"pm":       {},
	"coder":    {},
	"verifier": {},
	"git":      {},
}

// DefaultFastRuntime ports _default_fast_runtime. Note: no OpenRouter
// auto-detect and no strip — os.getenv default is "claude_code", and any value
// not in RUNTIME_VALUES falls back to "claude_code".
func DefaultFastRuntime() string {
	value, ok := os.LookupEnv("SWE_DEFAULT_RUNTIME")
	if !ok {
		value = "claude_code"
	}
	for _, rv := range runtimex.RuntimeValues {
		if value == rv {
			return value
		}
	}
	return "claude_code"
}

// fastRuntimeDefault ports _runtime_default. codex is auth-mode dependent.
func fastRuntimeDefault(runtime string) string {
	switch runtime {
	case "codex":
		return codexDefaultModel()
	case "claude_code":
		return fastClaudeCodeDefault
	case "open_code":
		return fastOpenCodeDefault
	default:
		return ""
	}
}

// FastBuildConfig ports fast/schemas.py::FastBuildConfig.
type FastBuildConfig struct {
	Runtime string            `json:"runtime"`
	Models  map[string]string `json:"models"`

	MaxTasks            int    `json:"max_tasks"`
	TaskTimeoutSeconds  int    `json:"task_timeout_seconds"`
	BuildTimeoutSeconds int    `json:"build_timeout_seconds"`
	EnableGithubPR      bool   `json:"enable_github_pr"`
	GithubPRBase        string `json:"github_pr_base"`
	PermissionMode      string `json:"permission_mode"`
	RepoURL             string `json:"repo_url"`
	AgentMaxTurns       int    `json:"agent_max_turns"`
}

// defaultFastBuildConfig seeds every non-zero Pydantic default.
func defaultFastBuildConfig() FastBuildConfig {
	return FastBuildConfig{
		Runtime:             DefaultFastRuntime(),
		Models:              nil,
		MaxTasks:            10,
		TaskTimeoutSeconds:  300,
		BuildTimeoutSeconds: 600,
		EnableGithubPR:      true,
		GithubPRBase:        "",
		PermissionMode:      "",
		RepoURL:             "",
		AgentMaxTurns:       50,
	}
}

// LoadFastBuildConfig constructs a FastBuildConfig from a raw input map: strict
// decode (extra="forbid") over the seeded defaults. FastBuildConfig has no
// legacy-key rejection in Python, so none is applied here.
func LoadFastBuildConfig(raw map[string]any) (*FastBuildConfig, error) {
	if raw == nil {
		raw = map[string]any{}
	}
	cfg := defaultFastBuildConfig()
	if err := strictDecode(raw, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// FastResolveModels ports fast_resolve_models — resolves the four role model
// strings. Resolution order (last wins): runtime default → models["default"] →
// models["<role>"]. An unknown key yields the verbatim "Unknown role key" error.
func FastResolveModels(config *FastBuildConfig) (map[string]string, error) {
	runtimeDefault := fastRuntimeDefault(config.Runtime)

	resolved := make(map[string]string, len(fastRoles))
	for _, role := range fastRoles {
		resolved[role] = runtimeDefault
	}

	if config.Models != nil {
		// Validate all keys first.
		for key := range config.Models {
			if _, ok := fastValidKeys[key]; !ok {
				return nil, fmt.Errorf(
					"Unknown role key %s in models dict. Valid keys are: %s",
					pyRepr(key), fastSortedValidKeysRepr(),
				)
			}
		}

		// Apply "default" override first.
		if def, ok := config.Models["default"]; ok {
			for _, role := range fastRoles {
				resolved[role] = def
			}
		}

		// Apply per-role overrides.
		for _, p := range fastRoleKeyMap {
			if v, ok := config.Models[p.RoleKey]; ok {
				resolved[p.ResolvedKey] = v
			}
		}
	}

	return resolved, nil
}

// fastSortedValidKeysRepr renders sorted(valid_keys) as a Python list repr,
// e.g. ['coder', 'default', 'git', 'pm', 'verifier'].
func fastSortedValidKeysRepr() string {
	keys := make([]string, 0, len(fastValidKeys))
	for k := range fastValidKeys {
		keys = append(keys, k)
	}
	sortStrings(keys)
	for i, k := range keys {
		keys[i] = pyRepr(k)
	}
	return "[" + strings.Join(keys, ", ") + "]"
}

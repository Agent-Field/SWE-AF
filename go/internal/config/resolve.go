// Package config ports the SWE-AF configuration models (BuildConfig,
// ExecutionConfig, FastBuildConfig) together with their V2 validators and the
// runtime/model resolution logic, from swe_af/execution/schemas.py and
// swe_af/fast/schemas.py (design §4.7, §6).
//
// Environment variables are read via os.Getenv at call time (not cached at
// package init) so that tests using t.Setenv and deployers changing env after
// import see the current value — matching the Python functions which read
// os.getenv on every call.
package config

import (
	"fmt"
	"os"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
)

// ---------------------------------------------------------------------------
// Role → model field mapping (ROLE_TO_MODEL_FIELD, ordered)
// ---------------------------------------------------------------------------

// roleModelPair preserves the exact insertion order of Python's
// ROLE_TO_MODEL_FIELD dict so derived orderings (ALL_MODEL_FIELDS, legacy hint
// lists, resolution iteration) are byte-identical.
type roleModelPair struct {
	Role  string
	Field string
}

// roleModelPairs ports ROLE_TO_MODEL_FIELD (17 roles) preserving order.
var roleModelPairs = []roleModelPair{
	{"pm", "pm_model"},
	{"architect", "architect_model"},
	{"tech_lead", "tech_lead_model"},
	{"sprint_planner", "sprint_planner_model"},
	{"coder", "coder_model"},
	{"qa", "qa_model"},
	{"code_reviewer", "code_reviewer_model"},
	{"qa_synthesizer", "qa_synthesizer_model"},
	{"replan", "replan_model"},
	{"retry_advisor", "retry_advisor_model"},
	{"issue_writer", "issue_writer_model"},
	{"issue_advisor", "issue_advisor_model"},
	{"verifier", "verifier_model"},
	{"git", "git_model"},
	{"merger", "merger_model"},
	{"integration_tester", "integration_tester_model"},
	{"ci_fixer", "ci_fixer_model"},
}

// RoleToModelField maps a role key to its internal *_model field name.
var RoleToModelField = func() map[string]string {
	m := make(map[string]string, len(roleModelPairs))
	for _, p := range roleModelPairs {
		m[p.Role] = p.Field
	}
	return m
}()

// modelFieldToRole is the inverse of RoleToModelField.
var modelFieldToRole = func() map[string]string {
	m := make(map[string]string, len(roleModelPairs))
	for _, p := range roleModelPairs {
		m[p.Field] = p.Role
	}
	return m
}()

// AllModelFields is the ordered list of internal *_model field names
// (ports ALL_MODEL_FIELDS).
var AllModelFields = func() []string {
	fields := make([]string, len(roleModelPairs))
	for i, p := range roleModelPairs {
		fields[i] = p.Field
	}
	return fields
}()

// allowedModelKeys ports _ALLOWED_MODEL_KEYS = MODEL_ROLE_KEYS | {"default"}.
var allowedModelKeys = func() map[string]struct{} {
	m := make(map[string]struct{}, len(roleModelPairs)+1)
	for _, p := range roleModelPairs {
		m[p.Role] = struct{}{}
	}
	m["default"] = struct{}{}
	return m
}()

// ---------------------------------------------------------------------------
// Model default strings
// ---------------------------------------------------------------------------

const (
	// Codex model defaults are auth-mode dependent (see codexDefaultModel).
	codexAPIKeyModel  = "gpt-5.3-codex" // OpenAI API-key auth (api_key mode)
	codexChatGPTModel = "gpt-5.5"       // ChatGPT-account auth (-codex blocked)

	// Default model for the auto-selected OpenRouter path (see openRouterOnlyEnv).
	openRouterAutoDefaultModel = "openrouter/deepseek/deepseek-v4-flash"
)

// runtimeBaseModels ports _RUNTIME_BASE_MODELS[runtime] as a fresh copy for the
// given runtime, or nil if the runtime is unknown. claude_code is all "sonnet"
// except qa_synthesizer_model="haiku"; open_code is all minimax; codex is all
// the API-key model (adjusted for auth mode by ResolveRuntimeModels).
func runtimeBaseModels(runtime string) map[string]string {
	base := make(map[string]string, len(AllModelFields))
	switch runtime {
	case "claude_code":
		for _, field := range AllModelFields {
			base[field] = "sonnet"
		}
		base["qa_synthesizer_model"] = "haiku"
	case "open_code":
		for _, field := range AllModelFields {
			base[field] = "openrouter/minimax/minimax-m2.5"
		}
	case "codex":
		for _, field := range AllModelFields {
			base[field] = codexAPIKeyModel
		}
	default:
		return nil
	}
	return base
}

// ---------------------------------------------------------------------------
// Environment-driven resolution (read os.Getenv at call time)
// ---------------------------------------------------------------------------

func envStripped(key string) string {
	return strings.TrimSpace(os.Getenv(key))
}

// openRouterOnlyEnv ports _openrouter_only_env: whether the deployer implicitly
// chose the OpenRouter runtime (no explicit SWE_DEFAULT_RUNTIME, no Anthropic
// key, but an OpenRouter key present).
func openRouterOnlyEnv() bool {
	if envStripped("SWE_DEFAULT_RUNTIME") != "" {
		return false
	}
	if envStripped("ANTHROPIC_API_KEY") != "" {
		return false
	}
	return envStripped("OPENROUTER_API_KEY") != ""
}

// DefaultRuntime ports _default_runtime, honoring SWE_DEFAULT_RUNTIME.
// When unset, auto-selects open_code if only an OpenRouter key is present,
// otherwise claude_code. An invalid env value falls back to claude_code.
func DefaultRuntime() string {
	value := envStripped("SWE_DEFAULT_RUNTIME")
	if value == "" {
		if openRouterOnlyEnv() {
			return "open_code"
		}
		return "claude_code"
	}
	for _, rv := range runtimex.RuntimeValues {
		if value == rv {
			return value
		}
	}
	return "claude_code"
}

// codexUsesChatGPTAuth ports _codex_uses_chatgpt_auth. api_key → false,
// chatgpt → true, auto (default) → true iff OPENAI_API_KEY is unset/empty.
func codexUsesChatGPTAuth() bool {
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("SWE_CODEX_AUTH_MODE")))
	if mode == "" {
		mode = "auto"
	}
	if mode == "api_key" {
		return false
	}
	if mode == "chatgpt" {
		return true
	}
	// auto
	return envStripped("OPENAI_API_KEY") == ""
}

// codexDefaultModel ports _codex_default_model: the base codex model for the
// active auth mode.
func codexDefaultModel() string {
	if codexUsesChatGPTAuth() {
		return codexChatGPTModel
	}
	return codexAPIKeyModel
}

// defaultModelEnvVars ports _DEFAULT_MODEL_ENV_VARS (cascade order).
var defaultModelEnvVars = []string{"SWE_DEFAULT_MODEL", "AI_MODEL", "HARNESS_MODEL"}

// defaultModelFromEnv ports _default_model_from_env: first non-empty (stripped)
// of SWE_DEFAULT_MODEL → AI_MODEL → HARNESS_MODEL, else "" (meaning None).
func defaultModelFromEnv() string {
	for _, v := range defaultModelEnvVars {
		if value := envStripped(v); value != "" {
			return value
		}
	}
	return ""
}

// DefaultPlanningModel ports _default_planning_model: env cascade, then the
// OpenRouter default when only an OpenRouter key is present, else "sonnet".
func DefaultPlanningModel() string {
	if envModel := defaultModelFromEnv(); envModel != "" {
		return envModel
	}
	if openRouterOnlyEnv() {
		return openRouterAutoDefaultModel
	}
	return "sonnet"
}

// ---------------------------------------------------------------------------
// Flat-model validation + resolution
// ---------------------------------------------------------------------------

// validateFlatModels ports _validate_flat_models: nil → empty; unknown keys →
// verbatim "Unknown model keys" error.
func validateFlatModels(models map[string]string) (map[string]string, error) {
	if models == nil {
		return map[string]string{}, nil
	}
	var unknown []string
	for k := range models {
		if _, ok := allowedModelKeys[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sortStrings(unknown)
		reprs := make([]string, len(unknown))
		for i, k := range unknown {
			reprs[i] = pyRepr(k)
		}
		valid := sortedAllowedModelKeys()
		return nil, fmt.Errorf(
			"Unknown model keys: %s. Valid keys: %s",
			strings.Join(reprs, ", "),
			strings.Join(valid, ", "),
		)
	}
	return models, nil
}

// sortedAllowedModelKeys returns the allowed model keys sorted alphabetically
// (for error messages) — ports ', '.join(sorted(_ALLOWED_MODEL_KEYS)).
func sortedAllowedModelKeys() []string {
	keys := make([]string, 0, len(allowedModelKeys))
	for k := range allowedModelKeys {
		keys = append(keys, k)
	}
	sortStrings(keys)
	return keys
}

// ResolveRuntimeModels ports resolve_runtime_models. Resolution order (lowest →
// highest precedence): runtime base defaults → env cascade → models["default"]
// → models["<role>"]. fieldNames nil defaults to AllModelFields.
func ResolveRuntimeModels(runtime string, models map[string]string, fieldNames []string) (map[string]string, error) {
	if fieldNames == nil {
		fieldNames = AllModelFields
	}

	base := runtimeBaseModels(runtime)
	if base == nil {
		return nil, fmt.Errorf(
			"Unsupported runtime %s. Valid runtimes: %s",
			pyRepr(runtime),
			strings.Join(runtimeValuesSlice(), ", "),
		)
	}

	flatModels, err := validateFlatModels(models)
	if err != nil {
		return nil, err
	}

	if runtime == "codex" {
		codexModel := codexDefaultModel()
		for field := range base {
			base[field] = codexModel
		}
	} else if runtime == "open_code" && openRouterOnlyEnv() {
		for field := range base {
			base[field] = openRouterAutoDefaultModel
		}
	}

	resolved := make(map[string]string, len(fieldNames))
	for _, field := range fieldNames {
		resolved[field] = base[field]
	}

	if envDefault := defaultModelFromEnv(); envDefault != "" {
		for _, field := range fieldNames {
			resolved[field] = envDefault
		}
	}

	if defaultModel, ok := flatModels["default"]; ok && defaultModel != "" {
		for _, field := range fieldNames {
			resolved[field] = defaultModel
		}
	}

	for role, modelName := range flatModels {
		if role == "default" {
			continue
		}
		field := RoleToModelField[role]
		if _, present := resolved[field]; present {
			resolved[field] = modelName
		}
	}

	return resolved, nil
}

// runtimeValuesSlice returns RUNTIME_VALUES as a slice in canonical order.
func runtimeValuesSlice() []string {
	return runtimex.RuntimeValues[:]
}

// runtimeToProvider ports _runtime_to_provider = runtime_to_harness_provider.
func runtimeToProvider(runtime string) string {
	provider, err := runtimex.RuntimeToHarnessProvider(runtime)
	if err != nil {
		return ""
	}
	return provider
}

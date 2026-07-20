package config

import "testing"

// Ports tests/test_model_tiers.py — the per-tier model env vars
// (SWE_MODEL_LOW / _MED / _HIGH).
//
// Validation contract:
//   - No tier envs set → resolution is unchanged for every runtime.
//   - A tier var applies to exactly the roles in its tier (see RoleToTier).
//   - Tier vars beat the SWE_DEFAULT_MODEL → AI_MODEL → HARNESS_MODEL cascade,
//     and lose to caller config (models["default"], models["<role>"]).
//   - SWE_MODEL_HIGH also wins the DefaultPlanningModel cascade (the planning
//     reasoners are high-tier roles).

// highTierFields ports _HIGH_FIELDS.
var highTierFields = map[string]bool{
	"pm_model":        true,
	"architect_model": true,
	"tech_lead_model": true,
	"replan_model":    true,
}

// openCodeBaseModel ports _OPEN_CODE_BASE.
const openCodeBaseModel = "openrouter/minimax/minimax-m2.5"

// TestModelTiers_NoTierEnvsUnchanged ports TestNoTierEnvsUnchanged: no tier
// envs set → resolution unchanged for all runtimes.
func TestModelTiers_NoTierEnvsUnchanged(t *testing.T) {
	tests := []struct {
		name    string
		runtime string
		env     map[string]string
		want    func(field string) string
	}{
		{"claude_code base defaults", "claude_code", nil, func(field string) string {
			if field == "qa_synthesizer_model" {
				return "haiku"
			}
			return "sonnet"
		}},
		{"open_code base defaults", "open_code", nil,
			func(string) string { return openCodeBaseModel }},
		{"codex base defaults", "codex", map[string]string{"SWE_CODEX_AUTH_MODE": "api_key"},
			func(string) string { return "gpt-5.3-codex" }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearProviderEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			got := mustResolve(t, tc.runtime, nil)
			for _, field := range AllModelFields {
				if want := tc.want(field); got[field] != want {
					t.Errorf("field %s = %q, want %q", field, got[field], want)
				}
			}
		})
	}
}

// TestModelTiers_TierEnvApplication ports TestTierEnvApplication: a tier var
// applies to exactly the roles in its tier.
func TestModelTiers_TierEnvApplication(t *testing.T) {
	t.Run("high only changes exactly the high-tier fields", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_MODEL_HIGH", "openrouter/z-ai/glm-5.2")
		got := mustResolve(t, "open_code", nil)
		for _, field := range AllModelFields {
			want := openCodeBaseModel
			if highTierFields[field] {
				want = "openrouter/z-ai/glm-5.2"
			}
			if got[field] != want {
				t.Errorf("field %s = %q, want %q", field, got[field], want)
			}
		}
	})

	t.Run("all three tiers resolve every field by role tier", func(t *testing.T) {
		clearProviderEnv(t)
		tierModels := map[string]string{
			"high": "openrouter/z-ai/glm-5.2",
			"med":  "openrouter/deepseek/deepseek-v4-pro",
			"low":  "openrouter/deepseek/deepseek-v4-flash",
		}
		for tier, model := range tierModels {
			t.Setenv(tierModelEnvVars[tier], model)
		}
		got := mustResolve(t, "open_code", nil)
		for role, field := range RoleToModelField {
			if want := tierModels[RoleToTier[role]]; got[field] != want {
				t.Errorf("role %s (%s) = %q, want %q", role, field, got[field], want)
			}
		}
	})

	t.Run("empty tier value treated as unset", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_MODEL_HIGH", "   ")
		got := mustResolve(t, "open_code", nil)
		for _, field := range AllModelFields {
			if got[field] != openCodeBaseModel {
				t.Errorf("field %s = %q, want %q", field, got[field], openCodeBaseModel)
			}
		}
	})
}

// TestModelTiers_Precedence ports TestTierPrecedence: tier vars beat the
// default-model env cascade and lose to caller config.
func TestModelTiers_Precedence(t *testing.T) {
	t.Run("models.default beats all tier vars", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_MODEL_HIGH", "tier-high")
		t.Setenv("SWE_MODEL_MED", "tier-med")
		t.Setenv("SWE_MODEL_LOW", "tier-low")
		got := mustResolve(t, "open_code", map[string]string{"default": "caller-default"})
		for _, field := range AllModelFields {
			if got[field] != "caller-default" {
				t.Errorf("field %s = %q, want caller-default", field, got[field])
			}
		}
	})

	t.Run("models.<role> beats everything for that role only", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_MODEL_MED", "tier-med")
		got := mustResolve(t, "open_code", map[string]string{"coder": "caller-coder"})
		if got["coder_model"] != "caller-coder" {
			t.Errorf("coder_model = %q, want caller-coder", got["coder_model"])
		}
		// Other med-tier roles still pick up the tier env value.
		if got["qa_model"] != "tier-med" {
			t.Errorf("qa_model = %q, want tier-med", got["qa_model"])
		}
	})

	t.Run("tier var beats default env cascade for its roles", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_DEFAULT_MODEL", "env-default")
		t.Setenv("AI_MODEL", "env-ai-model")
		t.Setenv("SWE_MODEL_HIGH", "tier-high")
		got := mustResolve(t, "open_code", nil)
		for _, field := range AllModelFields {
			// Unset tiers still get the cascade winner (SWE_DEFAULT_MODEL).
			want := "env-default"
			if highTierFields[field] {
				want = "tier-high"
			}
			if got[field] != want {
				t.Errorf("field %s = %q, want %q", field, got[field], want)
			}
		}
	})
}

// TestDefaultPlanningModel_HighTier ports TestDefaultPlanningModelHighTier:
// SWE_MODEL_HIGH wins the planning-model cascade; without it the prior
// behavior is unchanged.
func TestDefaultPlanningModel_HighTier(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		want string
	}{
		{"high tier var wins over default-model env",
			map[string]string{"SWE_DEFAULT_MODEL": "env-default", "SWE_MODEL_HIGH": "tier-high"}, "tier-high"},
		{"unset high tier falls back to env cascade",
			map[string]string{"SWE_DEFAULT_MODEL": "env-default"}, "env-default"},
		{"whitespace high tier treated as unset",
			map[string]string{"SWE_MODEL_HIGH": "   "}, "sonnet"},
		{"no env at all defaults to sonnet", nil, "sonnet"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearProviderEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			if got := DefaultPlanningModel(); got != tc.want {
				t.Fatalf("DefaultPlanningModel() = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestModelTiers_MappingCompleteness ports TestTierMappingCompleteness: every
// role has a tier, and every tier is a known tier.
func TestModelTiers_MappingCompleteness(t *testing.T) {
	t.Run("every role has a tier", func(t *testing.T) {
		for role := range RoleToModelField {
			if _, ok := RoleToTier[role]; !ok {
				t.Errorf("role %q missing from RoleToTier", role)
			}
		}
		for role := range RoleToTier {
			if _, ok := RoleToModelField[role]; !ok {
				t.Errorf("RoleToTier has unknown role %q", role)
			}
		}
	})

	t.Run("every tier value is known", func(t *testing.T) {
		known := make(map[string]bool, len(modelTiers))
		for _, tier := range modelTiers {
			known[tier] = true
		}
		for role, tier := range RoleToTier {
			if !known[tier] {
				t.Errorf("role %q has unknown tier %q", role, tier)
			}
		}
		if len(tierModelEnvVars) != len(modelTiers) {
			t.Errorf("tierModelEnvVars has %d tiers, want %d", len(tierModelEnvVars), len(modelTiers))
		}
		for _, tier := range modelTiers {
			if _, ok := tierModelEnvVars[tier]; !ok {
				t.Errorf("tier %q missing from tierModelEnvVars", tier)
			}
		}
	})
}

package node

import "testing"

// clearAIKeys blanks every env var ai.DefaultConfig reads so a test starts from
// "no key configured". t.Setenv restores originals at test end.
func clearAIKeys(t *testing.T) {
	t.Helper()
	for _, k := range []string{"OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_BASE_URL", "AI_MODEL"} {
		t.Setenv(k, "")
	}
}

// TestResolveAIConfig maps to the Fix-4 contract: a usable key yields a non-nil
// AIConfig; no key yields nil (so node startup is never broken by a missing key).
func TestResolveAIConfigWithKey(t *testing.T) {
	clearAIKeys(t)
	t.Setenv("OPENAI_API_KEY", "sk-test-fake")

	cfg := resolveAIConfig()
	if cfg == nil {
		t.Fatal("resolveAIConfig() = nil with a key set, want non-nil")
	}
	if cfg.APIKey != "sk-test-fake" || cfg.BaseURL == "" || cfg.Model == "" {
		t.Errorf("resolved config incomplete: %+v", cfg)
	}
}

func TestResolveAIConfigWithoutKey(t *testing.T) {
	clearAIKeys(t)
	if cfg := resolveAIConfig(); cfg != nil {
		t.Fatalf("resolveAIConfig() = %+v with no key, want nil", cfg)
	}
}

// TestBuildAgentConstructsWithoutKey: BuildAgent must still construct the node
// (AIConfig nil) when no AI key is present.
func TestBuildAgentConstructsWithoutKey(t *testing.T) {
	clearAIKeys(t)
	t.Setenv("NODE_ID", "swe-planner-go-test")

	n, err := BuildAgent("swe-planner-go", "8005", "test node")
	if err != nil {
		t.Fatalf("BuildAgent errored without an AI key: %v", err)
	}
	if n == nil || n.App == nil {
		t.Fatal("BuildAgent returned a nil node/app")
	}
}

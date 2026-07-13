package coding

import "testing"

// clearAIKeys blanks the env vars ai.DefaultConfig reads so IsOpenRouter() is
// deterministic per test. t.Setenv restores originals afterwards.
func clearAIKeys(t *testing.T) {
	t.Helper()
	for _, k := range []string{"OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_BASE_URL", "AI_MODEL"} {
		t.Setenv(k, "")
	}
}

// TestMapSynthModelOpenRouter maps to the Fix-4 contract: when the direct-LLM
// client targets OpenRouter, short aliases become provider-qualified ids and
// already-qualified ids pass through.
func TestMapSynthModelOpenRouter(t *testing.T) {
	clearAIKeys(t)
	t.Setenv("OPENROUTER_API_KEY", "sk-or-test")

	cases := map[string]string{
		"haiku":                  "anthropic/claude-haiku-4.5",
		"sonnet":                 "anthropic/claude-sonnet-4.5",
		"opus":                   "anthropic/claude-opus-4.1",
		"anthropic/claude-x":     "anthropic/claude-x", // already qualified -> passthrough
		"deepseek/deepseek-chat": "deepseek/deepseek-chat",
		"some-unknown-alias":     "some-unknown-alias", // unknown, no "/" -> passthrough
		"openrouter/z-ai/glm-5":  "openrouter/z-ai/glm-5",
	}
	for in, want := range cases {
		if got := mapSynthModel(in); got != want {
			t.Errorf("mapSynthModel(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestMapSynthModelNonOpenRouter: without OpenRouter, aliases are left as-is
// (the OpenAI-compatible endpoint / claude-code path resolves them).
func TestMapSynthModelNonOpenRouter(t *testing.T) {
	clearAIKeys(t)
	t.Setenv("OPENAI_API_KEY", "sk-openai-test")

	for _, in := range []string{"haiku", "sonnet", "opus", "gpt-4o"} {
		if got := mapSynthModel(in); got != in {
			t.Errorf("mapSynthModel(%q) = %q, want passthrough %q", in, got, in)
		}
	}
	// A "/" id still passes through unchanged.
	if got := mapSynthModel("anthropic/claude-x"); got != "anthropic/claude-x" {
		t.Errorf("qualified id must pass through, got %q", got)
	}
}

package tools

import (
	"strings"
	"testing"
)

// clearWebSearchEnv unsets both gate env vars so each test starts from a known
// baseline (mirrors the autouse _clear_env fixture in the Python test).
func clearWebSearchEnv(t *testing.T) {
	t.Helper()
	t.Setenv("OPENCODE_ENABLE_EXA", "")
	t.Setenv("EXA_API_KEY", "")
}

// Contract: no env vars set → guardrail not appended.
func TestReturnsUnchangedWhenNoEnv(t *testing.T) {
	clearWebSearchEnv(t)
	base := "You are a coder."
	if got := MaybeApplyCoderGuardrail(base); got != base {
		t.Fatalf("expected unchanged prompt, got %q", got)
	}
	if websearchEnabled() {
		t.Fatal("expected websearchEnabled() to be false")
	}
}

// Contract: OPENCODE_ENABLE_EXA alone is insufficient — Exa needs a key too.
func TestReturnsUnchangedWhenOnlyFlagSet(t *testing.T) {
	clearWebSearchEnv(t)
	t.Setenv("OPENCODE_ENABLE_EXA", "1")
	base := "You are a coder."
	if got := MaybeApplyCoderGuardrail(base); got != base {
		t.Fatalf("expected unchanged prompt, got %q", got)
	}
}

// Contract: EXA_API_KEY alone is insufficient — needs the gate flag.
func TestReturnsUnchangedWhenOnlyKeySet(t *testing.T) {
	clearWebSearchEnv(t)
	t.Setenv("EXA_API_KEY", "fake-key")
	base := "You are a coder."
	if got := MaybeApplyCoderGuardrail(base); got != base {
		t.Fatalf("expected unchanged prompt, got %q", got)
	}
}

// Contract: only OPENCODE_ENABLE_EXA=1 enables; other truthy values don't.
func TestReturnsUnchangedWhenFlagNotOne(t *testing.T) {
	clearWebSearchEnv(t)
	t.Setenv("OPENCODE_ENABLE_EXA", "true")
	t.Setenv("EXA_API_KEY", "fake-key")
	base := "You are a coder."
	if got := MaybeApplyCoderGuardrail(base); got != base {
		t.Fatalf("expected unchanged prompt, got %q", got)
	}
}

// Contract: both env vars present → guardrail appended.
func TestAppendsWhenBothEnvVarsSet(t *testing.T) {
	clearWebSearchEnv(t)
	t.Setenv("OPENCODE_ENABLE_EXA", "1")
	t.Setenv("EXA_API_KEY", "fake-key")
	if !websearchEnabled() {
		t.Fatal("expected websearchEnabled() to be true")
	}
	base := "You are a coder."
	result := MaybeApplyCoderGuardrail(base)
	if !strings.HasPrefix(result, base) {
		t.Fatalf("result should start with base prompt, got %q", result)
	}
	if !strings.Contains(result, WebSearchCoderGuardrail) {
		t.Fatal("result should contain the guardrail text")
	}
	if result != base+WebSearchCoderGuardrail {
		t.Fatal("result should be base + guardrail exactly")
	}
}

// Contract: an empty EXA_API_KEY string shouldn't pass the gate.
func TestEmptyKeyTreatedAsAbsent(t *testing.T) {
	clearWebSearchEnv(t)
	t.Setenv("OPENCODE_ENABLE_EXA", "1")
	t.Setenv("EXA_API_KEY", "")
	base := "You are a coder."
	if got := MaybeApplyCoderGuardrail(base); got != base {
		t.Fatalf("expected unchanged prompt, got %q", got)
	}
}

// Contract: the guardrail text is substantive and verbatim-shaped.
func TestGuardrailTextIsSubstantive(t *testing.T) {
	text := WebSearchCoderGuardrail
	lower := strings.ToLower(text)
	if !strings.Contains(lower, "websearch") && !strings.Contains(lower, "web_search") {
		t.Fatal("guardrail must mention websearch/web_search")
	}
	if !strings.Contains(text, "Do NOT") && !strings.Contains(text, "sparingly") {
		t.Fatal("guardrail must convey restraint")
	}
	if !strings.Contains(text, "API") && !strings.Contains(text, "library") && !strings.Contains(text, "error message") {
		t.Fatal("guardrail must give concrete when-to-use examples")
	}
	if !(len(text) > 200 && len(text) < 2000) {
		t.Fatalf("guardrail length out of expected range: %d", len(text))
	}
}

// Contract: guardrail text is byte-identical to the Python constant. Python
// len() reports 840 code points; the UTF-8 encoding (which Go's len() measures)
// is 842 bytes because the em-dash is 3 bytes. Locks verbatim parity.
func TestGuardrailByteIdentical(t *testing.T) {
	if got := len(WebSearchCoderGuardrail); got != 842 {
		t.Fatalf("guardrail byte length = %d, want 842", got)
	}
	if !strings.HasPrefix(WebSearchCoderGuardrail, "\n\n## ") {
		t.Fatal("guardrail must start with \\n\\n## ")
	}
	if !strings.HasSuffix(WebSearchCoderGuardrail, "the implementation.\n") {
		t.Fatal("guardrail must end with 'the implementation.\\n'")
	}
}

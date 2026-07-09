package runtimex

import "testing"

// Contract: RuntimeValues is exactly the Python RUNTIME_VALUES tuple, in order.
func TestRuntimeValues(t *testing.T) {
	want := [...]string{"claude_code", "open_code", "codex"}
	if RuntimeValues != want {
		t.Fatalf("RuntimeValues = %v, want %v", RuntimeValues, want)
	}
}

// Contract: aliases fold to canonical runtimes.
//   - "claude"/"claude-code"/"claude_code" -> "claude_code"
//   - "opencode"/"open_code" -> "open_code"
//   - "codex" -> "codex"
//   - case/whitespace insensitive (trim + lower)
func TestNormalizeRuntimeProvider(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"claude", "claude_code"},
		{"claude-code", "claude_code"},
		{"claude_code", "claude_code"},
		{"opencode", "open_code"},
		{"open_code", "open_code"},
		{"codex", "codex"},
		// trim + lowercase normalization
		{"  Claude  ", "claude_code"},
		{"CLAUDE-CODE", "claude_code"},
		{"OpenCode", "open_code"},
		{"\tCODEX\n", "codex"},
	}
	for _, c := range cases {
		got, err := NormalizeRuntimeProvider(c.in)
		if err != nil {
			t.Errorf("NormalizeRuntimeProvider(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("NormalizeRuntimeProvider(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Contract: an unsupported value returns an error whose message is exactly the
// Python text f"Unsupported runtime provider: {runtime}", interpolating the
// ORIGINAL (untrimmed, original-case) input.
func TestNormalizeRuntimeProviderUnsupported(t *testing.T) {
	cases := []struct {
		in      string
		wantMsg string
	}{
		{"", "Unsupported runtime provider: "},
		{"gpt4", "Unsupported runtime provider: gpt4"},
		// original input preserved verbatim in the message (not trimmed/lowered)
		{"  Foo Bar  ", "Unsupported runtime provider:   Foo Bar  "},
	}
	for _, c := range cases {
		got, err := NormalizeRuntimeProvider(c.in)
		if err == nil {
			t.Errorf("NormalizeRuntimeProvider(%q) = %q, want error", c.in, got)
			continue
		}
		if err.Error() != c.wantMsg {
			t.Errorf("NormalizeRuntimeProvider(%q) error = %q, want %q", c.in, err.Error(), c.wantMsg)
		}
	}
}

// Contract: canonical runtime -> harness provider string.
// claude_code -> "claude", open_code -> "opencode", codex -> "codex".
func TestRuntimeToHarnessProvider(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"claude_code", "claude"},
		{"claude", "claude"},
		{"claude-code", "claude"},
		{"open_code", "opencode"},
		{"opencode", "opencode"},
		{"codex", "codex"},
	}
	for _, c := range cases {
		got, err := RuntimeToHarnessProvider(c.in)
		if err != nil {
			t.Errorf("RuntimeToHarnessProvider(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("RuntimeToHarnessProvider(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Contract: canonical runtime -> harness adapter string.
// claude_code -> "claude-code", open_code -> "opencode", codex -> "codex".
func TestRuntimeToHarnessAdapter(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"claude_code", "claude-code"},
		{"claude", "claude-code"},
		{"claude-code", "claude-code"},
		{"open_code", "opencode"},
		{"opencode", "opencode"},
		{"codex", "codex"},
	}
	for _, c := range cases {
		got, err := RuntimeToHarnessAdapter(c.in)
		if err != nil {
			t.Errorf("RuntimeToHarnessAdapter(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("RuntimeToHarnessAdapter(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Contract (the asymmetry): provider vs adapter strings differ ONLY for claude.
// For every canonical runtime, compare the two mappings; they must match for
// open_code and codex and differ for claude_code.
func TestProviderAdapterAsymmetry(t *testing.T) {
	for _, rt := range RuntimeValues {
		provider, err := RuntimeToHarnessProvider(rt)
		if err != nil {
			t.Fatalf("RuntimeToHarnessProvider(%q): %v", rt, err)
		}
		adapter, err := RuntimeToHarnessAdapter(rt)
		if err != nil {
			t.Fatalf("RuntimeToHarnessAdapter(%q): %v", rt, err)
		}
		if rt == "claude_code" {
			if provider == adapter {
				t.Errorf("claude_code: provider %q and adapter %q must differ", provider, adapter)
			}
			if provider != "claude" || adapter != "claude-code" {
				t.Errorf("claude_code: got provider=%q adapter=%q, want provider=%q adapter=%q",
					provider, adapter, "claude", "claude-code")
			}
		} else if provider != adapter {
			t.Errorf("%s: provider %q and adapter %q must be identical", rt, provider, adapter)
		}
	}
}

// Contract: unsupported input propagates the normalize error through both
// mapping functions.
func TestMappingsPropagateError(t *testing.T) {
	if _, err := RuntimeToHarnessProvider("nope"); err == nil {
		t.Error("RuntimeToHarnessProvider(\"nope\") = nil error, want error")
	} else if err.Error() != "Unsupported runtime provider: nope" {
		t.Errorf("RuntimeToHarnessProvider error = %q, want %q", err.Error(), "Unsupported runtime provider: nope")
	}
	if _, err := RuntimeToHarnessAdapter("nope"); err == nil {
		t.Error("RuntimeToHarnessAdapter(\"nope\") = nil error, want error")
	} else if err.Error() != "Unsupported runtime provider: nope" {
		t.Errorf("RuntimeToHarnessAdapter error = %q, want %q", err.Error(), "Unsupported runtime provider: nope")
	}
}

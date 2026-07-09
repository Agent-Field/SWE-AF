// Package runtimex is a verbatim port of swe_af/runtime/providers.py: the
// shared runtime/provider normalization and mapping utilities.
//
// Three canonical runtimes exist (RuntimeValues). Callers pass user-facing
// aliases ("claude", "claude-code", "opencode", ...) which NormalizeRuntimeProvider
// folds to a canonical value. Two separate mappings then translate a canonical
// runtime to the string the harness expects — and they are NOT the same for
// claude_code: the harness *provider* is "claude" while the harness *adapter*
// is "claude-code" (design §4.7, "note the asymmetry"). open_code and codex map
// identically under both.
package runtimex

import (
	"fmt"
	"strings"
)

// RuntimeValues is the tuple of canonical runtime values, ported verbatim from
// Python's RUNTIME_VALUES = ("claude_code", "open_code", "codex").
var RuntimeValues = [...]string{"claude_code", "open_code", "codex"}

// NormalizeRuntimeProvider normalizes user/runtime aliases to canonical runtime
// values.
//
// Ports normalize_runtime_provider: the input is trimmed and lowercased, then
// matched against the alias sets. An unsupported value yields an error whose
// message is exactly Python's f"Unsupported runtime provider: {runtime}" —
// note it interpolates the ORIGINAL (untrimmed, original-case) input, not the
// normalized value.
func NormalizeRuntimeProvider(runtime string) (string, error) {
	value := strings.ToLower(strings.TrimSpace(runtime))
	switch value {
	case "claude_code", "claude", "claude-code":
		return "claude_code", nil
	case "open_code", "opencode":
		return "open_code", nil
	case "codex":
		return "codex", nil
	}
	return "", fmt.Errorf("Unsupported runtime provider: %s", runtime)
}

// RuntimeToHarnessProvider maps a canonical runtime to its harness provider
// value.
//
// Ports runtime_to_harness_provider: claude_code -> "claude", open_code ->
// "opencode", codex -> "codex". Normalizes first, propagating the normalize
// error for unsupported input.
func RuntimeToHarnessProvider(runtime string) (string, error) {
	normalized, err := NormalizeRuntimeProvider(runtime)
	if err != nil {
		return "", err
	}
	switch normalized {
	case "claude_code":
		return "claude", nil
	case "open_code":
		return "opencode", nil
	default:
		return "codex", nil
	}
}

// RuntimeToHarnessAdapter maps runtime aliases to AgentField harness adapter
// values.
//
// Ports runtime_to_harness_adapter: claude_code -> "claude-code", open_code ->
// "opencode", codex -> "codex". Differs from RuntimeToHarnessProvider only for
// claude_code ("claude-code" here vs "claude" there). Normalizes first,
// propagating the normalize error for unsupported input.
func RuntimeToHarnessAdapter(runtime string) (string, error) {
	normalized, err := NormalizeRuntimeProvider(runtime)
	if err != nil {
		return "", err
	}
	switch normalized {
	case "claude_code":
		return "claude-code", nil
	case "open_code":
		return "opencode", nil
	default:
		return "codex", nil
	}
}

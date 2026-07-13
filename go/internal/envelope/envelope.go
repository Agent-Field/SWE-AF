// Package envelope unwraps execution envelopes returned by agent.Call.
//
// It is a 1:1 port of swe_af/execution/envelope.py. The AgentField SDK's Call
// has two execution paths: the async path unwraps the result and surfaces
// failures, while the sync fallback path may return the full execution envelope
// (with the inner result nil) on failed executions. UnwrapCallResult normalises
// both cases so pipeline code can always expect the raw reasoner output.
package envelope

import (
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
)

// envelopeKeys is the set of keys present in the execution envelope returned by
// the control plane's _build_execute_response. Verbatim port of _ENVELOPE_KEYS.
var envelopeKeys = map[string]struct{}{
	"execution_id":  {},
	"run_id":        {},
	"node_id":       {},
	"type":          {},
	"target":        {},
	"status":        {},
	"duration_ms":   {},
	"timestamp":     {},
	"result":        {},
	"error_message": {},
	"cost":          {},
}

// failureStatuses are the terminal-failure envelope statuses.
var failureStatuses = map[string]struct{}{
	"failed":    {},
	"error":     {},
	"cancelled": {},
	"timeout":   {},
}

// UnwrapCallResult extracts the actual reasoner output from an agent.Call
// response.
//
// raw is the value returned by agent.Call (or the equivalent call_fn); label is
// a human-readable name (e.g. the target) used in error messages.
//
// Behaviour (mirrors unwrap_call_result):
//   - No envelope keys present -> already unwrapped, return raw unchanged.
//   - Envelope with a terminal-failure status -> a *fatal.FatalHarnessError when
//     the error is fatal, otherwise a plain error mirroring Python's
//     RuntimeError: "<label> failed (status=<status>): <err>".
//   - Otherwise return raw["result"] when present (and non-nil), else raw.
func UnwrapCallResult(raw map[string]any, label string) (map[string]any, error) {
	if raw == nil {
		return raw, nil
	}

	// Fast path: already unwrapped (no envelope keys present).
	if !hasEnvelopeKey(raw) {
		return raw, nil
	}

	// Looks like the execution envelope — check for errors first.
	status := ""
	if s, ok := raw["status"]; ok && s != nil {
		status = strings.ToLower(fmt.Sprintf("%v", s))
	}
	if _, isFailure := failureStatuses[status]; isFailure {
		err := errorText(raw)
		if fatal.IsFatalError(err) {
			return nil, &fatal.FatalHarnessError{OriginalMessage: err}
		}
		return nil, fmt.Errorf("%s failed (status=%s): %s", label, status, err)
	}

	inner, ok := raw["result"]
	if ok && inner != nil {
		if m, isMap := inner.(map[string]any); isMap {
			return m, nil
		}
		// Non-object result (e.g. a scalar or list). Python returns it as-is
		// via an Any return, but reasoner outputs are always JSON objects
		// (model_dump dicts); fall through to return the envelope so the caller
		// can validate rather than losing information.
	}

	// Envelope present but result is nil (or non-object) and status isn't a
	// known failure — return as-is (caller should validate).
	return raw, nil
}

// hasEnvelopeKey reports whether raw contains any envelope key (the Go
// equivalent of _ENVELOPE_KEYS.intersection(result)).
func hasEnvelopeKey(raw map[string]any) bool {
	for k := range raw {
		if _, ok := envelopeKeys[k]; ok {
			return true
		}
	}
	return false
}

// errorText reproduces Python's `result.get("error_message") or
// result.get("error") or "unknown"` truthiness chain, then str()-ifies the
// chosen value.
func errorText(raw map[string]any) string {
	if s, ok := truthy(raw["error_message"]); ok {
		return s
	}
	if s, ok := truthy(raw["error"]); ok {
		return s
	}
	return "unknown"
}

// truthy returns the string form of v and whether v is Python-truthy for the
// purpose of the `or` chain (nil and the empty string are falsy).
func truthy(v any) (string, bool) {
	if v == nil {
		return "", false
	}
	if s, ok := v.(string); ok {
		return s, s != ""
	}
	return fmt.Sprintf("%v", v), true
}

package coding

import (
	"encoding/json"
	"errors"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// jsonUnmarshal is a thin alias so the input structs' UnmarshalJSON methods can
// delegate to encoding/json without importing it under a name that shadows the
// (aliased) recursion-guard type. Keeping it here also documents that the input
// binding uses plain encoding/json (no UseNumber), matching afx.Bind.
func jsonUnmarshal(b []byte, dest any) error {
	return json.Unmarshal(b, dest)
}

// bindInput decodes a reasoner's untyped input map into a typed input value T.
// It round-trips the map through JSON (marshal then unmarshal), so T's
// UnmarshalJSON runs and seeds the Python parameter defaults for absent keys —
// the same mechanism as afx.Bind, inlined here to keep field-name matching by
// json tag and to trigger the per-input default seeding.
func bindInput[T any](input map[string]any) (T, error) {
	var out T
	b, err := json.Marshal(input)
	if err != nil {
		return out, err
	}
	if err := json.Unmarshal(b, &out); err != nil {
		return out, err
	}
	return out, nil
}

// maybeWorkspaceManifest ports _maybe_workspace_manifest: a nil/absent input map
// yields a nil manifest; otherwise the map is materialized into a
// *schemas.WorkspaceManifest by JSON round-trip (mirroring WorkspaceManifest(**raw)).
func maybeWorkspaceManifest(raw map[string]any) *schemas.WorkspaceManifest {
	if raw == nil {
		return nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var m schemas.WorkspaceManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil
	}
	return &m
}

// issueNameOf mirrors issue.get("name", "?") for the untyped issue dict.
func issueNameOf(issue map[string]any) string {
	if issue != nil {
		if v, ok := issue["name"]; ok {
			if s, ok := v.(string); ok {
				return s
			}
		}
	}
	return "?"
}

// mapBool mirrors dict.get(key, False) for a bool-valued key: returns the stored
// bool when present, otherwise false. A non-bool value is treated as false,
// matching how the Python fallback only reads genuine booleans.
func mapBool(m map[string]any, key string) bool {
	if m == nil {
		return false
	}
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

// pyBool renders a Go bool the way a Python f-string interpolates one: True/False.
// The coder/qa/reviewer "complete" notes and the qa_synthesizer FIX summary
// embed booleans, so this keeps those strings byte-identical to Python.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// isFatal reports whether err is a non-retryable API failure the coding roles
// must propagate rather than swallow into a fallback. It is fatal when err is
// (or wraps) a *fatal.FatalHarnessError — as harnessx.Run already produces for
// the coder/qa/reviewer harness path — OR when its message matches a known
// fatal pattern, which is how a billing/auth failure surfaces on the direct-LLM
// (qa_synthesizer) path where the error is a plain error, not a typed one.
func isFatal(err error) bool {
	if err == nil {
		return false
	}
	var fe *fatal.FatalHarnessError
	if errors.As(err, &fe) {
		return true
	}
	return fatal.IsFatalError(err.Error())
}

// asFatalError normalizes a fatal error into a *fatal.FatalHarnessError: an
// already-typed one is returned unchanged (preserving its OriginalMessage),
// while a plain fatal-pattern error is wrapped so callers propagate a single,
// consistent non-retryable type (parity with Python re-raising FatalHarnessError).
func asFatalError(err error) error {
	var fe *fatal.FatalHarnessError
	if errors.As(err, &fe) {
		return err
	}
	return &fatal.FatalHarnessError{OriginalMessage: err.Error()}
}

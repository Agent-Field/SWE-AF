package coding

import "fmt"

// This file holds the small dict-access helpers that reproduce Python's dynamic
// `dict.get(key, default)` / truthiness semantics on the untyped map[string]any
// values that flow through the loop (both scripted test values and
// JSON-unmarshaled reasoner outputs, where numbers arrive as float64 and slices
// as []any). The coding loop deliberately operates on maps rather than typed
// structs to match the Python source's dict-based control flow byte-for-byte.

// mapGetBool ports d.get(key, default) used in a boolean context. A present bool
// value wins; an absent/nil/non-bool value yields the default.
func mapGetBool(m map[string]any, key string, def bool) bool {
	if m == nil {
		return def
	}
	v, ok := m[key]
	if !ok || v == nil {
		return def
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return def
}

// mapGetStr ports d.get(key, default) for string-typed reads. A present string
// wins; a present non-string is stringified; absent yields the default.
func mapGetStr(m map[string]any, key, def string) string {
	if m == nil {
		return def
	}
	v, ok := m[key]
	if !ok {
		return def
	}
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return def
	}
	return fmt.Sprintf("%v", v)
}

// mapGetOr ports d.get(key, default) returning the raw value (or default when
// the key is absent).
func mapGetOr(m map[string]any, key string, def any) any {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok {
		return v
	}
	return def
}

// toStringSlice coerces a value to []string, handling both []string (test
// literals) and []any (JSON-decoded) inputs. Returns nil for anything else.
func toStringSlice(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok {
				out = append(out, s)
			} else if e != nil {
				out = append(out, fmt.Sprintf("%v", e))
			}
		}
		return out
	}
	return nil
}

// toMapSlice coerces a value to []map[string]any, handling []map[string]any and
// []any inputs. Returns nil otherwise.
func toMapSlice(v any) []map[string]any {
	switch t := v.(type) {
	case []map[string]any:
		return t
	case []any:
		out := make([]map[string]any, 0, len(t))
		for _, e := range t {
			if m, ok := e.(map[string]any); ok {
				out = append(out, m)
			}
		}
		return out
	}
	return nil
}

// toAnySlice coerces a value to []any, handling []any, []string and
// []map[string]any inputs. Returns nil otherwise.
func toAnySlice(v any) []any {
	switch t := v.(type) {
	case []any:
		return t
	case []string:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = e
		}
		return out
	case []map[string]any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = e
		}
		return out
	}
	return nil
}

// asMap returns v as a map[string]any, or nil if it is not one.
func asMap(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
}

// isTruthy mirrors Python truthiness for the value types that flow through the
// loop: nil/false/""/0/empty-collection are falsy, everything else truthy.
func isTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case int:
		return t != 0
	case int64:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case []string:
		return len(t) > 0
	case []map[string]any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

// contains reports whether s contains v.
func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

// toInt coerces a value (int / int64 / float64 / nil) to int; nil and unknown
// types yield 0. JSON numbers decode as float64, so this bridges d.get(key, 0).
func toInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	}
	return 0
}

// toIntDefault coerces to int, returning def when v is nil or an unknown type.
func toIntDefault(v any, def int) int {
	switch t := v.(type) {
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	}
	return def
}

// anyToStr stringifies a value the way a Python f-string would (nil -> "").
func anyToStr(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}

// truncate returns s limited to n bytes, mirroring Python's s[:n] for the
// ASCII summaries used here.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// lastN returns the last n elements of s (Python's s[-n:]).
func lastN(s []any, n int) []any {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

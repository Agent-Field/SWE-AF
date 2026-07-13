package config

import (
	"fmt"
	"sort"
	"strings"
)

// This file ports the V2 legacy-key rejection logic from
// execution/schemas.py (_reject_legacy_config_keys, _legacy_hint_for_model_key,
// and the legacy-equivalent tables) with verbatim error strings, plus small
// Python-parity helpers (pyRepr, sortStrings).

// pyRepr renders a Go string the way Python's repr() would for a simple string:
// wrapped in single quotes. Config keys never contain quotes, so this is a
// faithful match for the error-message interpolations that use {x!r}.
func pyRepr(s string) string {
	return "'" + s + "'"
}

// sortStrings sorts a slice of strings in place (ascending), matching Python's
// sorted().
func sortStrings(s []string) {
	sort.Strings(s)
}

// legacyGroupEquivalents ports _LEGACY_GROUP_EQUIVALENTS (order irrelevant —
// used only for membership + hint lookup).
var legacyGroupEquivalents = map[string]string{
	"planning":      "models.pm, models.architect, models.tech_lead, models.sprint_planner",
	"coding":        "models.coder, models.qa, models.code_reviewer",
	"orchestration": "models.replan, models.retry_advisor, models.issue_writer, models.issue_advisor, models.verifier, models.git, models.merger, models.integration_tester",
	"lightweight":   "models.qa_synthesizer",
}

// legacyTopLevelPair keeps _LEGACY_TOP_LEVEL_EQUIVALENTS in its Python insertion
// order so multi-hit error messages list keys identically.
type legacyTopLevelPair struct {
	Key        string
	Equivalent string
}

// legacyTopLevelEquivalents ports _LEGACY_TOP_LEVEL_EQUIVALENTS:
// ai_provider/preset/model first, then every *_model field → models.<role>.
var legacyTopLevelEquivalents = func() []legacyTopLevelPair {
	pairs := []legacyTopLevelPair{
		{"ai_provider", "runtime"},
		{"preset", "runtime + models"},
		{"model", "models.default"},
	}
	for _, p := range roleModelPairs {
		pairs = append(pairs, legacyTopLevelPair{Key: p.Field, Equivalent: "models." + p.Role})
	}
	return pairs
}()

// legacyHintForModelKey ports _legacy_hint_for_model_key.
func legacyHintForModelKey(key string) string {
	if hint, ok := legacyGroupEquivalents[key]; ok {
		return hint
	}
	if role, ok := modelFieldToRole[key]; ok {
		return "models." + role
	}
	if strings.HasSuffix(key, "_model") {
		return "models." + key[:len(key)-len("_model")]
	}
	return "models.<role>"
}

// rejectLegacyConfigKeys ports _reject_legacy_config_keys. It scans the raw
// input map for V2-forbidden keys and returns a verbatim error, matching the
// Python precedence exactly: the per-models-key group/legacy checks raise before
// the aggregated top-level "Legacy config keys" error.
func rejectLegacyConfigKeys(data map[string]any) error {
	var legacyHits []string
	for _, p := range legacyTopLevelEquivalents {
		if _, ok := data[p.Key]; ok {
			legacyHits = append(legacyHits, fmt.Sprintf("%s -> %s", pyRepr(p.Key), pyRepr(p.Equivalent)))
		}
	}

	if modelsValue, ok := data["models"]; ok {
		if modelsMap, isMap := asStringKeyedMap(modelsValue); isMap {
			for _, modelKey := range modelsMap {
				if _, isGroup := legacyGroupEquivalents[modelKey]; isGroup {
					hint := legacyHintForModelKey(modelKey)
					return fmt.Errorf(
						"Legacy model group key %s is not supported in V2. Use flat role keys: %s.",
						pyRepr(modelKey), hint,
					)
				}
				_, isField := modelFieldToRole[modelKey]
				if isField || strings.HasSuffix(modelKey, "_model") {
					hint := legacyHintForModelKey(modelKey)
					return fmt.Errorf(
						"Legacy model key %s is not supported in V2. Use %s.",
						pyRepr(modelKey), pyRepr(hint),
					)
				}
			}
		}
	}

	if len(legacyHits) > 0 {
		return fmt.Errorf(
			"Legacy config keys are not supported in V2: %s.",
			strings.Join(legacyHits, ", "),
		)
	}
	return nil
}

// asStringKeyedMap returns the ordered list of keys of a map-typed value from
// the raw config, mirroring `isinstance(models_value, dict)` + iteration. The
// key order here is not deterministic (Go map iteration), but the Python loop's
// order only affects WHICH legacy error fires when multiple legacy model keys
// coexist — every such key is individually forbidden, so any of them is a
// correct rejection. Non-map values return (nil, false), matching the Python
// isinstance guard which skips the loop.
func asStringKeyedMap(v any) ([]string, bool) {
	switch m := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		return keys, true
	case map[string]string:
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		return keys, true
	default:
		return nil, false
	}
}

// Package planning ports the planning-role prompt builders from
// swe_af/prompts/{_utils,product_manager,architect,tech_lead,sprint_planner,
// environment_scout}.py. Prompt text is verbatim; f-string interpolation is
// replicated exactly (including conditional blocks and trailing whitespace).
package planning

import (
	"fmt"
	"sort"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// WorkspaceContextBlock ports _utils.workspace_context_block.
//
// Returns an empty string when the manifest is nil or contains only a single
// repository (no additional context needed for single-repo workflows). For
// multi-repo workspaces, returns a formatted block describing each repository's
// name, role, and absolute path on disk.
func WorkspaceContextBlock(manifest *schemas.WorkspaceManifest) string {
	if manifest == nil {
		return ""
	}

	repos := manifest.Repos
	if len(repos) <= 1 {
		return ""
	}

	lines := []string{
		"## Workspace Repositories",
		"",
		"This task spans multiple repositories. Each repository is listed below with its role and local path:",
		"",
	}

	for _, repo := range repos {
		lines = append(lines, fmt.Sprintf("- **%s** (role: %s): `%s`", repo.RepoName, repo.Role, repo.AbsolutePath))
	}

	lines = append(lines, "")

	return strings.Join(lines, "\n")
}

// FormatPriorUserResponses ports hitl.ask_user.format_prior_user_responses.
//
// It is defined here (rather than imported from the hitl package) so the
// planning prompt subpackage builds independently; product_manager and
// environment_scout both need it. Exported so sibling prompt subpackages can
// reuse it.
//
// NOTE: Python renders the per-response `values` dict in JSON insertion order.
// A Go map[string]any cannot preserve that order, so keys are emitted in sorted
// order here — deterministic, but for multi-key `values` it may differ from the
// Python ordering. Single-key values (the common case) render identically.
func FormatPriorUserResponses(prior []map[string]any) string {
	if len(prior) == 0 {
		return ""
	}
	lines := []string{"## Prior Clarification From User", ""}
	for idx, entry := range prior {
		question := mapStringDefault(entry, "question", "(no title)")
		status := mapStringDefault(entry, "status", "unknown")
		lines = append(lines, fmt.Sprintf("### Question %d: %s", idx+1, question))
		lines = append(lines, fmt.Sprintf("_Status: %s_", status))
		values := mapObject(entry, "values")
		if len(values) > 0 {
			lines = append(lines, "")
			lines = append(lines, "Values submitted by user:")
			keys := make([]string, 0, len(values))
			for key := range values {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			for _, key := range keys {
				lines = append(lines, fmt.Sprintf("- **%s**: %v", key, values[key]))
			}
		}
		if feedback, ok := entry["feedback"]; ok && truthy(feedback) {
			lines = append(lines, "")
			lines = append(lines, fmt.Sprintf("User feedback: %v", feedback))
		}
		lines = append(lines, "")
	}
	lines = append(lines, "USE THESE PRIOR ANSWERS. DO NOT RE-ASK THE SAME QUESTIONS. Only "+
		"emit `ask_user_form` if you need DIFFERENT clarification not already "+
		"covered above.")
	return strings.Join(lines, "\n")
}

// KnownServiceSummaryForPrompt ports hitl.services.known_service_summary_for_prompt.
//
// Defined here so environment_scout builds independently; exported for reuse.
func KnownServiceSummaryForPrompt(specs []schemas.ServiceCredentialSpec) string {
	var lines []string
	for _, spec := range specs {
		signals := "(no static signal)"
		if len(spec.SignalFiles) > 0 {
			parts := make([]string, len(spec.SignalFiles))
			for i, s := range spec.SignalFiles {
				parts[i] = "`" + s + "`"
			}
			signals = strings.Join(parts, ", ")
		}
		lines = append(lines, fmt.Sprintf(
			"- **%s** — env `%s`; signals: %s; mint at %s; hint: %s",
			spec.ServiceName, spec.EnvVarName, signals, spec.MintURL, spec.PermissionsHint))
	}
	return strings.Join(lines, "\n")
}

// mapStringDefault returns m[key] as a string, or def if absent/not a string.
// Mirrors Python dict.get(key, default) followed by str-formatting.
func mapStringDefault(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok && v != nil {
		return fmt.Sprintf("%v", v)
	}
	return def
}

// mapObject returns m[key] as a map[string]any, or nil if absent/not an object.
func mapObject(m map[string]any, key string) map[string]any {
	if v, ok := m[key]; ok {
		if obj, ok := v.(map[string]any); ok {
			return obj
		}
	}
	return nil
}

// mapString returns m[key] coerced to a string via Python-like `.get(key, "") or ""`.
func mapString(m map[string]any, key string) string {
	if v, ok := m[key]; ok && v != nil {
		return fmt.Sprintf("%v", v)
	}
	return ""
}

// mapStringSlice returns m[key] as a []string, handling []any and []string.
// Mirrors Python `.get(key, []) or []`.
func mapStringSlice(m map[string]any, key string) []string {
	v, ok := m[key]
	if !ok || v == nil {
		return nil
	}
	switch xs := v.(type) {
	case []string:
		return xs
	case []any:
		out := make([]string, 0, len(xs))
		for _, item := range xs {
			out = append(out, fmt.Sprintf("%v", item))
		}
		return out
	}
	return nil
}

// joinBullets renders `"\n".join(f"- {c}" for c in items)`; "" for an empty slice.
func joinBullets(items []string) string {
	if len(items) == 0 {
		return ""
	}
	parts := make([]string, len(items))
	for i, c := range items {
		parts[i] = "- " + c
	}
	return strings.Join(parts, "\n")
}

// truthy replicates Python truthiness for the value types found in prompt
// inputs (nil, string, bool, numbers, slices, maps).
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case string:
		return x != ""
	case bool:
		return x
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case []any:
		return len(x) > 0
	case []string:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	default:
		return true
	}
}

// Package advisor holds the verbatim Go ports of the advisor / CI / resolve /
// fast-mode prompt builders from swe_af/prompts/*.py and swe_af/fast/prompts.py.
//
// Each module exposes its system-prompt constant plus a task-prompt function
// that takes an options struct (mirroring the Python keyword-only signatures)
// and renders byte-identical output to the Python f-string builders — including
// the Python list/dict repr formatting the originals rely on.
package advisor

import (
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Python-parity formatting helpers.
//
// The Python builders interpolate lists and dicts directly into f-strings,
// which uses Python's str()/repr() rendering (e.g. ['a', 'b'] with single
// quotes, True/False, None). These helpers reproduce that rendering so the Go
// output is byte-identical.
// ---------------------------------------------------------------------------

// pyStr reproduces Python str(v) as used in f-string interpolation of a scalar
// or container value.
func pyStr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return pyNum(x)
	case float32:
		return pyNum(float64(x))
	default:
		return pyRepr(v)
	}
}

// pyRepr reproduces Python repr(v) for the value kinds that occur in these
// prompts (strings, bools, numbers, lists, dicts). List elements are rendered
// with repr, matching Python.
func pyRepr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case string:
		return pyReprString(x)
	case bool:
		if x {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return pyNum(x)
	case float32:
		return pyNum(float64(x))
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Slice, reflect.Array:
		parts := make([]string, rv.Len())
		for i := 0; i < rv.Len(); i++ {
			parts[i] = pyRepr(rv.Index(i).Interface())
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case reflect.Map:
		return pyReprMap(v)
	case reflect.Ptr:
		if rv.IsNil() {
			return "None"
		}
		return pyRepr(rv.Elem().Interface())
	}
	return pyStr(v)
}

// pyNum renders a float the way Python str() would for whole values. Integral
// values coming from JSON (which decodes every number as float64) print without
// a trailing ".0" so they match the Python int fields they originated from.
func pyNum(f float64) string {
	if !math.IsInf(f, 0) && !math.IsNaN(f) && f == math.Trunc(f) && math.Abs(f) < 1e15 {
		return strconv.FormatInt(int64(f), 10)
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

// pyReprString reproduces Python's repr() of a string: single quotes by default,
// double quotes when the string contains a single quote but no double quote,
// with backslash, the active quote, and the common control characters escaped.
func pyReprString(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, "\"") {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// pyReprMap renders a map the way Python repr() renders a dict. Go maps are
// unordered, so keys are emitted in sorted order for determinism.
//
// TODO(parity): Python dict repr preserves insertion order; the untyped
// map[string]any port cannot recover it. No prompt in this package reprs a dict
// today, so this path is defensive only.
func pyReprMap(v any) string {
	rv := reflect.ValueOf(v)
	keys := rv.MapKeys()
	strKeys := make([]string, len(keys))
	for i, k := range keys {
		strKeys[i] = pyStr(k.Interface())
	}
	sort.Strings(strKeys)
	parts := make([]string, 0, len(keys))
	for _, sk := range strKeys {
		val := rv.MapIndex(reflect.ValueOf(sk))
		parts = append(parts, pyReprString(sk)+": "+pyRepr(val.Interface()))
	}
	return "{" + strings.Join(parts, ", ") + "}"
}

// truthy reproduces Python truthiness: empty string/list/map, zero numbers,
// nil, and false are falsy.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case float32:
		return x != 0
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Slice, reflect.Array, reflect.Map:
		return rv.Len() > 0
	case reflect.Ptr:
		return !rv.IsNil()
	}
	return true
}

// mapGet reproduces Python dict.get(key, default): the stored value when the key
// is present (even if nil), otherwise the default.
func mapGet(m map[string]any, key string, def any) any {
	if v, ok := m[key]; ok {
		return v
	}
	return def
}

// mapGetStr is mapGet followed by pyStr, for keys whose value is interpolated as
// a scalar into an f-string.
func mapGetStr(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok {
		return pyStr(v)
	}
	return def
}

// asSlice normalizes any Go slice/array (or nil) into a []any for iteration.
func asSlice(v any) []any {
	if v == nil {
		return nil
	}
	if s, ok := v.([]any); ok {
		return s
	}
	rv := reflect.ValueOf(v)
	if rv.Kind() != reflect.Slice && rv.Kind() != reflect.Array {
		return nil
	}
	out := make([]any, rv.Len())
	for i := 0; i < rv.Len(); i++ {
		out[i] = rv.Index(i).Interface()
	}
	return out
}

// asMap normalizes a value into a map[string]any, returning nil when it is not a
// map.
func asMap(v any) map[string]any {
	switch m := v.(type) {
	case nil:
		return nil
	case map[string]any:
		return m
	}
	rv := reflect.ValueOf(v)
	if rv.Kind() != reflect.Map {
		return nil
	}
	out := make(map[string]any, rv.Len())
	iter := rv.MapRange()
	for iter.Next() {
		out[pyStr(iter.Key().Interface())] = iter.Value().Interface()
	}
	return out
}

// joinStr reproduces ", ".join(list): each element rendered via pyStr (they are
// strings in practice), joined with ", ".
func joinStr(v any, sep string) string {
	items := asSlice(v)
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = pyStr(it)
	}
	return strings.Join(parts, sep)
}

// runeTruncate reproduces Python's s[:n] slice, which counts Unicode code
// points, not bytes.
func runeTruncate(s string, n int) string {
	if n < 0 {
		n = 0
	}
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

// ---------------------------------------------------------------------------
// Local copies of shared prompt helpers.
//
// TODO(wiring): the canonical implementations live in the prompts/planning
// utils package (workspace_context_block, T3.P1) and the hitl package
// (format_prior_user_responses, ask_user.py, T2.4). These unexported copies keep
// the advisor subpackage independently buildable; collapse to the shared
// versions during the Wave 6 wiring pass.
// ---------------------------------------------------------------------------

// workspaceContextBlock ports swe_af.prompts._utils.workspace_context_block.
func workspaceContextBlock(manifest *schemas.WorkspaceManifest) string {
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
		lines = append(lines, "- **"+repo.RepoName+"** (role: "+repo.Role+"): `"+repo.AbsolutePath+"`")
	}
	lines = append(lines, "")
	return strings.Join(lines, "\n")
}

// orDefault reproduces Python's `value or default` for strings: the value when
// non-empty, otherwise the default.
func orDefault(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

// ciCheckData reproduces the Python `fc.model_dump() if hasattr(fc, "model_dump")
// else dict(fc)` normalization for a failing CI check, accepting either a typed
// schemas.CIFailedCheck or a plain map[string]any.
func ciCheckData(v any) map[string]any {
	switch c := v.(type) {
	case schemas.CIFailedCheck:
		return map[string]any{
			"name":         c.Name,
			"workflow":     c.Workflow,
			"conclusion":   c.Conclusion,
			"details_url":  c.DetailsURL,
			"logs_excerpt": c.LogsExcerpt,
		}
	case *schemas.CIFailedCheck:
		if c == nil {
			return map[string]any{}
		}
		return ciCheckData(*c)
	case map[string]any:
		return c
	default:
		return asMap(v)
	}
}

// reviewCommentData normalizes a review comment (typed schemas.ReviewCommentRef
// or map[string]any) into the dict shape the Python builder consumes.
func reviewCommentData(v any) map[string]any {
	switch c := v.(type) {
	case schemas.ReviewCommentRef:
		return map[string]any{
			"comment_id": c.CommentID,
			"thread_id":  c.ThreadID,
			"path":       c.Path,
			"line":       c.Line,
			"author":     c.Author,
			"body":       c.Body,
			"url":        c.URL,
		}
	case *schemas.ReviewCommentRef:
		if c == nil {
			return map[string]any{}
		}
		return reviewCommentData(*c)
	case map[string]any:
		return c
	default:
		return asMap(v)
	}
}

// failedCheckLines renders one failing-check block, shared verbatim by the CI
// fixer and PR resolver builders.
func failedCheckLines(data map[string]any) []string {
	name := mapGetStr(data, "name", "?")
	workflow := mapGetStr(data, "workflow", "")
	conclusion := mapGetStr(data, "conclusion", "")
	url := mapGetStr(data, "details_url", "")
	logs := mapGetStr(data, "logs_excerpt", "")
	header := "#### " + name
	if workflow != "" {
		header += "  (workflow: " + workflow + ")"
	}
	if conclusion != "" {
		header += "  [" + conclusion + "]"
	}
	out := []string{header}
	if url != "" {
		out = append(out, "Details: "+url)
	}
	if logs != "" {
		out = append(out, "Log tail (last failing output):")
		out = append(out, "```")
		out = append(out, logs)
		out = append(out, "```")
	} else {
		out = append(out, "(No log captured. Run `gh run view <run-id> --log-failed` to fetch it.)")
	}
	return out
}

// formatPriorUserResponses ports swe_af.hitl.ask_user.format_prior_user_responses.
func formatPriorUserResponses(prior []map[string]any) string {
	if len(prior) == 0 {
		return ""
	}
	lines := []string{"## Prior Clarification From User", ""}
	for idx, entry := range prior {
		question := mapGetStr(entry, "question", "(no title)")
		status := mapGetStr(entry, "status", "unknown")
		lines = append(lines, "### Question "+strconv.Itoa(idx+1)+": "+question)
		lines = append(lines, "_Status: "+status+"_")
		values := asMap(mapGet(entry, "values", nil))
		if len(values) > 0 {
			lines = append(lines, "")
			lines = append(lines, "Values submitted by user:")
			// TODO(parity): Python iterates dict insertion order; sorted here for
			// determinism (see pyReprMap note).
			keys := make([]string, 0, len(values))
			for k := range values {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, key := range keys {
				lines = append(lines, "- **"+key+"**: "+pyStr(values[key]))
			}
		}
		feedback := mapGet(entry, "feedback", nil)
		if truthy(feedback) {
			lines = append(lines, "")
			lines = append(lines, "User feedback: "+pyStr(feedback))
		}
		lines = append(lines, "")
	}
	lines = append(lines,
		"USE THESE PRIOR ANSWERS. DO NOT RE-ASK THE SAME QUESTIONS. Only "+
			"emit `ask_user_form` if you need DIFFERENT clarification not already "+
			"covered above.")
	return strings.Join(lines, "\n")
}

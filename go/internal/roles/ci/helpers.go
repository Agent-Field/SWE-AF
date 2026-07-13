package ci

import (
	"encoding/json"
	"errors"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// jsonUnmarshal is a thin alias so the input structs' UnmarshalJSON methods can
// delegate to encoding/json without importing it under a name that shadows the
// (aliased) recursion-guard type. It documents that input binding uses plain
// encoding/json (no UseNumber), matching afx.Bind.
func jsonUnmarshal(b []byte, dest any) error {
	return json.Unmarshal(b, dest)
}

// bindInput decodes a reasoner's untyped input map into a typed input value T.
// It round-trips the map through JSON (marshal then unmarshal), so T's
// UnmarshalJSON runs and seeds the Python parameter defaults for absent keys —
// the same mechanism as afx.Bind, inlined here so field-name matching goes by
// json tag and triggers the per-input default seeding.
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

// isFatal reports whether err is a non-retryable API failure the CI roles must
// propagate rather than swallow into a fallback. It is fatal when err is (or
// wraps) a *fatal.FatalHarnessError — as harnessx.Run already produces for the
// harness path — OR when its message matches a known fatal pattern (defence in
// depth). Mirrors Python's `except FatalHarnessError: raise`.
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

// pyBool renders a Go bool the way a Python f-string interpolates one:
// True / False. The ci_fixer / pr_resolver "complete" notes embed booleans, so
// this keeps those strings byte-identical to Python.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// truncateRunes mirrors Python's s[:n] slice, which counts Unicode code points,
// not bytes. Used for head_sha[:10] in the ci_watcher start note.
func truncateRunes(s string, n int) string {
	if n < 0 {
		n = 0
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// countAddressed reproduces Python's
// `sum(1 for c in addressed_comments if c.addressed)` for the pr_resolver
// complete note.
func countAddressed(comments []schemas.AddressedComment) int {
	n := 0
	for _, c := range comments {
		if c.Addressed {
			n++
		}
	}
	return n
}

// toCIFailedChecks reproduces Python's
// `[fc if isinstance(fc, CIFailedCheck) else CIFailedCheck(**fc) for fc in ...]`
// normalization: each input dict is materialized into a typed
// schemas.CIFailedCheck (dropping unexpected keys and filling defaults) so the
// prompt builder sees the same normalized values Python does. On a bind failure
// the raw map is passed through — the advisor prompt helpers accept either — so
// a malformed entry still renders rather than dropping.
func toCIFailedChecks(raws []map[string]any) []any {
	out := make([]any, 0, len(raws))
	for _, r := range raws {
		if fc, err := afx.Bind[schemas.CIFailedCheck](r); err == nil {
			out = append(out, fc)
		} else {
			out = append(out, r)
		}
	}
	return out
}

// toReviewComments reproduces Python's
// `[rc if isinstance(rc, ReviewCommentRef) else ReviewCommentRef(**rc) for ...]`
// normalization for pr_resolver's review_comments, with the same raw-map
// fallback as toCIFailedChecks.
func toReviewComments(raws []map[string]any) []any {
	out := make([]any, 0, len(raws))
	for _, r := range raws {
		if rc, err := afx.Bind[schemas.ReviewCommentRef](r); err == nil {
			out = append(out, rc)
		} else {
			out = append(out, r)
		}
	}
	return out
}

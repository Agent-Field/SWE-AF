// Package dag is the DAG execution engine — the middle and outer of the
// three-nested-loop architecture. It is a 1:1 behavioural port of
// swe_af/execution/dag_executor.py.
//
// RunDAG executes a planned Issue DAG level by level (all issues in a level run
// concurrently behind a barrier), running the merge/integration/cleanup git
// gates after each level, the debt/split/replan gates, self-healing replanning,
// and durable checkpoints — returning the final DAGState.
//
// Structure (mirrors the Python module split across files):
//   - executor.go        : RunDAG main level loop + Options.
//   - gates.go           : per-issue middle loop, level execution, replan gate helpers.
//   - worktree.go        : worktree setup / merge / integration-test / cleanup / multi-repo init.
//   - checkpoint.go      : DAGState init from plan_result, save/load checkpoint.
//   - replanner_compat.go: the no-call_fn direct replanner path (_replanner_compat.py).
//   - helpers.go         : value-coercion + struct<->map helpers.
//
// Every inter-reasoner invocation flows through the injected CallFn seam (a
// closure over agent.Call + envelope.UnwrapCallResult supplied by the
// orchestrator), so the control-plane DAG renders identically and the loop
// stays testable with a scripted call function. The exact keyword-arg key names
// Python passes to each reasoner are preserved verbatim.
package dag

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Value-extraction helpers. Issue/result dicts arrive as map[string]any after
// JSON unmarshalling, so fields must be coerced from any.
// ---------------------------------------------------------------------------

func asStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// mapGetStr returns m[key] as a string, or def when absent/not-a-string,
// mirroring Python's dict.get(key, default) for string values.
func mapGetStr(m map[string]any, key, def string) string {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok {
		if s, isStr := v.(string); isStr {
			return s
		}
	}
	return def
}

// mapGetBool mirrors dict.get(key, default) truthiness for bool values.
func mapGetBool(m map[string]any, key string, def bool) bool {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok {
		if b, isBool := v.(bool); isBool {
			return b
		}
	}
	return def
}

// asBool reports the Python truthiness of a call result field (used for
// result.get("success") style checks).
func asBool(v any) bool {
	b, _ := v.(bool)
	return b
}

func asInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	case float32:
		return int(t)
	default:
		return 0
	}
}

// asStringSlice coerces an any (typically []any or []string) to []string.
func asStringSlice(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			out = append(out, asStr(e))
		}
		return out
	case string:
		// LLM shape tolerance (ports ensure_str_list): a bare string where a
		// list is expected becomes a one-element list instead of vanishing.
		if strings.TrimSpace(t) == "" {
			return nil
		}
		return []string{t}
	default:
		return nil
	}
}

// asMapSlice coerces an any (typically []any of maps, or []map[string]any) to
// []map[string]any.
func asMapSlice(v any) []map[string]any {
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
	default:
		return nil
	}
}

// asMap coerces an any to map[string]any (nil if not a map).
func asMap(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
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

// zfill2 renders n as a 2-digit zero-padded string (Python str(n).zfill(2)).
// Negative numbers are not expected here (sequence numbers are >= 0).
func zfill2(n int) string {
	return fmt.Sprintf("%02d", n)
}

// ---------------------------------------------------------------------------
// struct <-> map[string]any conversion (the Go analogue of pydantic
// model_dump() / Model(**dict)). Uses JSON round-tripping so struct JSON tags,
// UnmarshalJSON default-seeding, and MarshalJSON overrides all apply — exactly
// matching what the reasoner boundary sees.
// ---------------------------------------------------------------------------

// dumpToMap serialises a value to map[string]any via JSON (model_dump-equivalent).
func dumpToMap(v any) map[string]any {
	b, err := json.Marshal(v)
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return map[string]any{}
	}
	return m
}

// dumpToMaps serialises a slice of values to []map[string]any.
func dumpToMaps[T any](items []T) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for i := range items {
		out = append(out, dumpToMap(items[i]))
	}
	return out
}

// mapToStruct decodes a map[string]any into a typed struct via JSON
// (Model(**dict)-equivalent), applying the struct's UnmarshalJSON default seeding.
func mapToStruct[T any](m map[string]any) (T, error) {
	var out T
	b, err := json.Marshal(m)
	if err != nil {
		return out, err
	}
	if err := json.Unmarshal(b, &out); err != nil {
		return out, err
	}
	return out, nil
}

// manifestFromMap reconstructs a WorkspaceManifest from its serialised dict form
// (the Go analogue of WorkspaceManifest(**dag_state.workspace_manifest)).
func manifestFromMap(m map[string]any) (*schemas.WorkspaceManifest, error) {
	wm, err := mapToStruct[schemas.WorkspaceManifest](m)
	if err != nil {
		return nil, err
	}
	return &wm, nil
}

// ---------------------------------------------------------------------------
// Per-issue timeout wrapper (ports _call_with_timeout).
// ---------------------------------------------------------------------------

// callWithTimeout runs fn under an asyncio.wait_for-equivalent deadline. A
// deadline hit yields the exact "Agent call '<label>' timed out after <n>s"
// error (a non-fatal, generic error the callers turn into a failure/fallback);
// parent-context cancellation propagates as ctx.Err() (matching CancelledError).
func callWithTimeout(ctx context.Context, timeout int, label string, fn func(ctx context.Context) (map[string]any, error)) (map[string]any, error) {
	cctx, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()

	type result struct {
		m   map[string]any
		err error
	}
	ch := make(chan result, 1)
	go func() {
		m, err := fn(cctx)
		ch <- result{m, err}
	}()

	select {
	case <-cctx.Done():
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("Agent call '%s' timed out after %ds", label, timeout)
	case r := <-ch:
		return r.m, r.err
	}
}

// noteFunc is the nil-safe observability seam threaded through the engine. It is
// coding.NoteFn so it can be passed straight into coding.RunCodingLoop.
type noteFunc = coding.NoteFn

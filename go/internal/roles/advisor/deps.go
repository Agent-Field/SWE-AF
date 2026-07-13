// Package advisor holds the execution-phase advisor/verify role reasoners ported
// 1:1 from swe_af/reasoners/execution_agents.py: run_retry_advisor,
// run_issue_advisor, run_replanner, run_issue_writer, run_verifier, and
// generate_fix_issues.
//
// Each reasoner is an exported handler with the signature
//
//	func(ctx context.Context, deps *Deps, input map[string]any) (any, error)
//
// and is exposed by its exact Python name through Handlers(). The handlers
// invoke the structured-output harness via harnessx.Run, mirror every
// router.note() call (message + tags) verbatim, propagate fatal harness errors,
// and return the same deterministic fallback value the Python reasoner returns
// when the harness fails to produce parseable output. run_issue_advisor and
// run_replanner additionally run through the ask-user (HITL) pause/resume loop,
// engaged only when a Hax client is available (mirroring
// build_hax_client_from_env returning None to disable HITL).
package advisor

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// Handler is the shared handler signature for every advisor/verify reasoner.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Deps carries the runtime collaborators every advisor/verify reasoner needs.
// It is constructed once by the node wiring (Wave 6) and passed to each handler.
// Tests construct it directly with fakes.
type Deps struct {
	// Harness is the structured-output harness surface used by harnessx.Run. In
	// production this is the *agent.Agent; tests supply a mock. It is the single
	// choke point through which scoped credentials are injected into the
	// subprocess environment.
	Harness harnessx.HarnessCaller

	// App is the note sink (router.note in Python). A nil App drops notes, so the
	// handlers remain usable in tests without a live agent.
	App hitl.App

	// Pauser is the pause surface the ask-user loop drives (agent.Pause —
	// webhook-resumed). Used only by the HITL-wrapped reasoners; may be nil when
	// HITL is disabled.
	Pauser hitl.Pauser

	// BuildHaxClient builds the Hax request client for the HITL-wrapped reasoners,
	// mirroring build_hax_client_from_env() being called inside each Python
	// reasoner. When nil, hitl.BuildHaxClientFromEnv is used. A nil returned
	// client means HITL is disabled and any ask_user_form is ignored.
	BuildHaxClient func() *hitl.HaxClient

	// NodeID is the Python router node id, threaded into the ask-user params for
	// parity. The pause itself is issued via agent.Pause, which resolves the node
	// from the agent, so NodeID no longer drives the "waiting" transition.
	NodeID string

	// AgentFieldServer is the control-plane base URL; the approval webhook URL is
	// derived from it (approval_webhook_url(router)).
	AgentFieldServer string
}

// note fires a router.note-equivalent when a note sink is present.
func (d *Deps) note(ctx context.Context, message string, tags ...string) {
	if d != nil && d.App != nil {
		d.App.Note(ctx, message, tags...)
	}
}

// executionIDFromContext resolves the current execution id for the ask-user
// pause. It is a package var so tests can inject one — the SDK's context key is
// unexported, so external packages cannot seed an ExecutionContext otherwise.
var executionIDFromContext = func(ctx context.Context) string {
	return agent.ExecutionContextFrom(ctx).ExecutionID
}

// Handlers returns the name→handler map for this role family, keyed by the exact
// Python reasoner names. The node wiring registers each entry with app.Register.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"run_retry_advisor":   RunRetryAdvisor,
		"run_issue_advisor":   RunIssueAdvisor,
		"run_replanner":       RunReplanner,
		"run_issue_writer":    RunIssueWriter,
		"run_verifier":        RunVerifier,
		"generate_fix_issues": GenerateFixIssues,
	}
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

// decodeInput re-marshals the untyped input map into a caller-seeded struct.
// The caller pre-populates dst with the Python parameter defaults; keys present
// in input override them, absent keys keep the defaults — matching how the
// Python reasoner materializes its keyword arguments.
func decodeInput(input map[string]any, dst any) error {
	b, err := json.Marshal(input)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, dst)
}

// maybeWorkspaceManifest deserializes a workspace_manifest dict into a
// *WorkspaceManifest, or returns nil. Ports _maybe_workspace_manifest.
func maybeWorkspaceManifest(raw map[string]any) (*schemas.WorkspaceManifest, error) {
	if raw == nil {
		return nil, nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil, err
	}
	var m schemas.WorkspaceManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// structToMap converts any JSON-serializable value into a map[string]any with
// keys matching its json tags — the Go equivalent of pydantic model_dump for a
// flat model. Used to hand the ask-user wrapper a mutable result carrying
// ask_user_form.
func structToMap(v any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return m, nil
}

// getStr mirrors Python dict.get(key, default) for a string value: the default
// is returned only when the key is absent.
func getStr(m map[string]any, key, def string) string {
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
	return pyStr(v)
}

// priorResponses normalizes kwargs["prior_user_responses"] (which the ask-user
// wrapper stores as a heterogeneous slice) into []map[string]any for the prompt
// builders.
func priorResponses(kwargs map[string]any) []map[string]any {
	raw, ok := kwargs["prior_user_responses"]
	if !ok || raw == nil {
		return nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var out []map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		return nil
	}
	return out
}

// pyStr renders a value the way a Python f-string would for the note messages.
func pyStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		return pyBool(t)
	case float64:
		return pyFloat(t)
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return ""
		}
		return string(b)
	}
}

// pyBool renders a Go bool as Python would in an f-string: "True"/"False".
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// pyFloat renders a Go float64 the way Python's str(float) would — notably
// keeping a trailing ".0" for integral values (0.0, 1.0) that Go's default
// formatter drops.
func pyFloat(f float64) string {
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eEnN") {
		s += ".0"
	}
	return s
}

// pyRepr renders a string as Python repr() would: single-quoted, switching to
// double quotes only when the string contains a single quote but no double
// quote — enough to match Python list-repr output for issue names.
func pyRepr(s string) string {
	if strings.Contains(s, "'") && !strings.Contains(s, "\"") {
		return "\"" + s + "\""
	}
	return "'" + strings.ReplaceAll(s, "'", "\\'") + "'"
}

// pyStrList renders a []string the way Python's f-string renders a list literal,
// e.g. ['a', 'b'] (empty list -> []).
func pyStrList(items []string) string {
	var b strings.Builder
	b.WriteByte('[')
	for i, s := range items {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(pyRepr(s))
	}
	b.WriteByte(']')
	return b.String()
}

// runeTruncate returns the first n runes of s (Python str slicing semantics),
// mirroring text[:n] on the raw agent output.
func runeTruncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

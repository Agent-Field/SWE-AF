// Package orch ports the SWE-AF orchestrator reasoners from swe_af/app.py:
// build, plan, execute, resolve, resume_build, plus the CI-gate and plan-approval
// gates. Each orchestrator is a reasoner handler registered by its exact Python
// name; every cross-reasoner call goes through the control plane (app.Call) so
// the DAG UI renders identically to the Python pipeline.
//
// This file (common.go) holds the plumbing shared by every orchestrator — the
// Deps carrier, the unwrapping call helpers, the CI-gate/plan-approval seams,
// and the small subprocess/util helpers ported from app.py's module level. The
// sibling orchestrator files (plan.go, execute.go, resolve.go, cigate_loop.go,
// approval_gate.go, resume.go) import from here.
package orch

import (
	"bytes"
	"context"
	"encoding/json"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/envelope"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// App is the minimal agent surface the orchestrators need: control-plane-routed
// reasoner calls and fire-and-forget notes. The concrete *agent.Agent satisfies
// it; tests supply a mock. Call returns the raw execution envelope (as
// agent.Call does) — orchestrators unwrap it via envelope.UnwrapCallResult
// (see Deps.Call / Deps.NewCallFn).
type App interface {
	Call(ctx context.Context, target string, input map[string]any) (map[string]any, error)
	Note(ctx context.Context, message string, tags ...string)
}

// Handler is the exported reasoner-handler shape every orchestrator satisfies,
// mirroring the roles packages. The node-wiring wave registers each by its
// exact Python name via Handlers().
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Deps carries the collaborators an orchestrator handler needs.
//
//   - App: the control-plane-routed call + note surface (a *agent.Agent).
//   - NodeID: the node id calls are addressed to (NODE_ID, default "swe-planner-go").
//   - AgentFieldServer: the control-plane base URL — the approval webhook base.
//     (The empty-build failure carrier no longer POSTs here: build returns the
//     SDK's &agent.ReasonerFailed and the async handler posts status=failed +
//     result on its own.)
//   - CIGate: the post-PR CI watch/fix gate seam. Owned by the ci-gate task
//     (cigate_loop.go). When nil, build skips the CI gate (parity with the
//     Python gate being a no-op when check_ci is false / no PR number).
//   - ApprovalGate: the hax plan-approval gate seam. Owned by the approval-gate
//     task (approval_gate.go). When nil, build skips the approval phase (parity
//     with the Python block being skipped when HAX_API_KEY is unset).
type Deps struct {
	App              App
	NodeID           string
	AgentFieldServer string
	CIGate           CIGateRunner
	ApprovalGate     ApprovalGate
}

// ---------------------------------------------------------------------------
// Seams (overridable in tests). Mirrors the pattern in roles/planning.
// ---------------------------------------------------------------------------

// executionContextFrom is a seam over agent.ExecutionContextFrom so tests can
// inject a run_id / execution_id (the SDK's context key is unexported).
var executionContextFrom = agent.ExecutionContextFrom

// sleepFn bounds the git-init retry backoff; tests stub it to a no-op.
var sleepFn = func(ctx context.Context, d time.Duration) {
	if d <= 0 {
		return
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

func runIDFromCtx(ctx context.Context) string       { return executionContextFrom(ctx).RunID }
func executionIDFromCtx(ctx context.Context) string { return executionContextFrom(ctx).ExecutionID }

// ---------------------------------------------------------------------------
// Call helpers — the app.call + _unwrap pattern shared by every orchestrator.
// ---------------------------------------------------------------------------

// Note forwards to the App, tolerating a nil App (test convenience).
func (d *Deps) Note(ctx context.Context, message string, tags ...string) {
	if d != nil && d.App != nil {
		d.App.Note(ctx, message, tags...)
	}
}

// Call invokes the local reasoner name (addressed as "<NodeID>.<name>") and
// unwraps the envelope, mirroring Python `_unwrap(await app.call(...), label)`.
// label defaults to name when empty.
func (d *Deps) Call(ctx context.Context, name string, kwargs map[string]any, label string) (map[string]any, error) {
	if label == "" {
		label = name
	}
	raw, err := d.App.Call(ctx, d.target(name), kwargs)
	if err != nil {
		return nil, err
	}
	return envelope.UnwrapCallResult(raw, label)
}

// CallRaw invokes the local reasoner name and returns the raw envelope WITHOUT
// unwrapping — used by build's git-init loop, which unwraps separately so it can
// treat an unwrap failure as a non-fatal git-init failure (app.py:701-704).
func (d *Deps) CallRaw(ctx context.Context, name string, kwargs map[string]any) (map[string]any, error) {
	return d.App.Call(ctx, d.target(name), kwargs)
}

// target renders the fully-qualified "<NodeID>.<name>" call target. At runtime
// NodeID is always set (BuildAgent default "swe-planner-go" or the NODE_ID env);
// the fallback only guards zero-value Deps in tests.
func (d *Deps) target(name string) string {
	nodeID := d.NodeID
	if nodeID == "" {
		nodeID = "swe-planner-go"
	}
	return nodeID + "." + name
}

// NewCallFn returns a coding.CallFn closure over App.Call + envelope unwrap,
// suitable for injection into dag.RunDAG / the coding loop. target is the full
// "<node>.<reasoner>" address; the unwrap label is the reasoner segment.
func (d *Deps) NewCallFn() coding.CallFn {
	return func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		raw, err := d.App.Call(ctx, target, kwargs)
		if err != nil {
			return nil, err
		}
		label := target
		if i := strings.LastIndex(target, "."); i >= 0 {
			label = target[i+1:]
		}
		return envelope.UnwrapCallResult(raw, label)
	}
}

// NewNoteFn adapts App.Note to the coding.NoteFn signature (msg, []tags) that
// dag.RunDAG expects, binding the request ctx.
func (d *Deps) NewNoteFn(ctx context.Context) coding.NoteFn {
	return func(msg string, tags []string) {
		d.Note(ctx, msg, tags...)
	}
}

// ---------------------------------------------------------------------------
// CI-gate seam (implemented by the cigate_loop.go task).
// ---------------------------------------------------------------------------

// CIGateRequest carries everything the post-PR CI gate needs. Fields mirror the
// keyword arguments of app.py:_run_ci_gate.
type CIGateRequest struct {
	Deps              *Deps
	Cfg               *config.BuildConfig
	Resolved          map[string]string
	RepoPath          string
	PRNumber          int
	PRURL             string
	IntegrationBranch string
	BaseBranch        string
	Goal              string
	CompletedIssues   []map[string]any
	HeadSHA           string
}

// CIGateRunner watches CI on a freshly-pushed PR and fix-repushes on failure,
// returning the summary dict app.py:_run_ci_gate produces. Implemented by the
// ci-gate task; build calls it when configured.
type CIGateRunner func(ctx context.Context, req CIGateRequest) (map[string]any, error)

// ---------------------------------------------------------------------------
// Plan-approval gate seam (implemented by the approval_gate.go task).
//
// DECISION (reported to the approval-gate task): app.py inlines the entire hax
// plan-approval flow inside build()'s try block (:754-964), and that flow reuses
// the helpers the approval-gate task owns (_format_plan_for_approval :355-402,
// _create_hax_request_with_timeout :411-471). Porting it inline in build.go
// would force build.go to also own those helpers — a file-ownership conflict.
// So build.go drives the gate through this seam instead: it calls ApprovalGate
// once (when HAX_API_KEY is set and a gate is wired), acting on the returned
// outcome. The approval-gate task implements ApprovalGate (the revision loop,
// hax request, poll-based pause) and owns resume.go. build.go owns none of the
// hax specifics.
// ---------------------------------------------------------------------------

// ApprovalRequest carries the context the plan-approval gate needs to run the
// hax review loop and, on request_changes, re-run architect → tech_lead →
// sprint_planner (via Deps.Call) to revise the plan.
type ApprovalRequest struct {
	Deps            *Deps
	Cfg             *config.BuildConfig
	Resolved        map[string]string
	PlanResult      map[string]any
	ManifestMap     map[string]any
	RepoPath        string
	Goal            string
	ArtifactsDir    string
	AbsArtifactsDir string
	ExecutionID     string
}

// ApprovalOutcome is the gate's verdict. Terminal means the gate ended the build
// (rejected / expired / revision-limit reached) and Result is the BuildResult
// map build must return. Otherwise PlanResult carries the (possibly revised)
// plan build proceeds to execute.
type ApprovalOutcome struct {
	Terminal   bool
	Result     map[string]any
	PlanResult map[string]any
}

// ApprovalGate runs the hax plan-approval loop (engaged only when HAX_API_KEY is
// set). Implemented by the approval-gate task; build calls it when configured.
type ApprovalGate func(ctx context.Context, req ApprovalRequest) (ApprovalOutcome, error)

// ---------------------------------------------------------------------------
// _is_empty_build (app.py:474).
// ---------------------------------------------------------------------------

// isEmptyBuild decides whether a finished build shipped nothing and must report
// failed. Ports _is_empty_build: empty == verification did not pass AND nothing
// was ever completed or merged across the original run and every fix cycle.
func isEmptyBuild(success bool, everCompleted, everMerged int) bool {
	return !success && everCompleted == 0 && everMerged == 0
}

// ---------------------------------------------------------------------------
// _derive_repo_name (execution/schemas.py:45) — needed by build/resolve. The
// Go schemas package did not export it, so the shared copy lives here.
// ---------------------------------------------------------------------------

var reDotGitSuffix = regexp.MustCompile(`\.git$`)
var reURLSep = regexp.MustCompile(`[/:]`)

// deriveRepoName extracts a repo name from a git URL (HTTPS or SSH). Ports
// _derive_repo_name: strip a trailing ".git" (after trimming trailing "/"), then
// take the last path component split on "/" or ":".
func deriveRepoName(url string) string {
	if url == "" {
		return ""
	}
	stripped := reDotGitSuffix.ReplaceAllString(strings.TrimRight(url, "/"), "")
	parts := reURLSep.Split(stripped, -1)
	return parts[len(parts)-1]
}

// ---------------------------------------------------------------------------
// dump helper — model_dump() equivalent for passing structs as call kwargs.
// ---------------------------------------------------------------------------

// dumpToMap marshals v (typically a *WorkspaceManifest) to a map[string]any,
// mirroring Python `manifest.model_dump()`. Returns nil for a nil input so the
// `workspace_manifest=None` call kwarg is preserved for single-repo builds.
func dumpToMap(v any) map[string]any {
	if v == nil {
		return nil
	}
	raw, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	if string(raw) == "null" {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil
	}
	return m
}

// ---------------------------------------------------------------------------
// Subprocess seams — git and gh. Ported call sequences shell out through these
// so tests can mock them without a real git/gh binary.
// ---------------------------------------------------------------------------

// cmdResult mirrors subprocess.CompletedProcess: captured stdout/stderr plus an
// exit code (Python code branches on returncode, not on the raised error).
type cmdResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

// runGit runs `git <args...>` with an optional working directory (dir==""
// means inherit the process cwd, matching Python calls that omit cwd=). Tests
// override this var.
var runGit = func(ctx context.Context, dir string, args ...string) cmdResult {
	return runProc(ctx, dir, "git", args...)
}

// runGH runs `gh <args...>` with an optional working directory. Tests override.
var runGH = func(ctx context.Context, dir string, args ...string) cmdResult {
	return runProc(ctx, dir, "gh", args...)
}

// ---------------------------------------------------------------------------
// Map / value helpers — the Go analogue of Python dict.get and truthiness used
// across the orchestrators when reading unwrapped reasoner results.
// ---------------------------------------------------------------------------

// mapStr returns m[key] as a string, or def when absent/nil/non-string.
func mapStr(m map[string]any, key, def string) string {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok && v != nil {
		if s, isStr := v.(string); isStr {
			return s
		}
	}
	return def
}

// mapGet returns m[key], or def when absent/nil.
func mapGet(m map[string]any, key string, def any) any {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok && v != nil {
		return v
	}
	return def
}

// asBool mirrors Python truthiness for the JSON-decoded values orchestrators
// branch on: bool as-is, non-zero numbers, non-empty strings/collections.
func asBool(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case float64:
		return t != 0
	case int:
		return t != 0
	case json.Number:
		f, _ := t.Float64()
		return f != 0
	case []any:
		return len(t) > 0
	case []map[string]any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

// asInt coerces a JSON-decoded number (float64/json.Number/int) to int.
func asInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case float64:
		return int(t)
	case json.Number:
		i, _ := t.Int64()
		return int(i)
	default:
		return 0
	}
}

// asMapList coerces a value to []map[string]any (nil for absent/other types).
func asMapList(v any) []map[string]any {
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

// asStrList coerces a value to []string (nil for absent/other types).
func asStrList(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

// appendToMapList appends entry to m[key] (setdefault + append), storing the
// result back as []map[string]any so it survives a later JSON round-trip.
func appendToMapList(m map[string]any, key string, entry map[string]any) {
	existing := asMapList(m[key])
	m[key] = append(existing, entry)
}

// maps0 is asMapList but returns a non-nil empty slice for absent values, so
// call kwargs serialise to `[]` (matching Python's dict.get(k, [])) rather than
// JSON `null`.
func maps0(v any) []map[string]any {
	if m := asMapList(v); m != nil {
		return m
	}
	return []map[string]any{}
}

// any0 returns v, or an empty list when v is nil (Python dict.get(k, [])).
func any0(v any) any {
	if v == nil {
		return []any{}
	}
	return v
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// manifestFromMap reconstructs a *WorkspaceManifest from its model_dump() map
// (nil when the map is nil or the round-trip fails).
func manifestFromMap(m map[string]any) (*schemas.WorkspaceManifest, error) {
	if m == nil {
		return nil, nil
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	var wm schemas.WorkspaceManifest
	if err := json.Unmarshal(raw, &wm); err != nil {
		return nil, err
	}
	return &wm, nil
}

func runProc(ctx context.Context, dir, bin string, args ...string) cmdResult {
	cmd := exec.CommandContext(ctx, bin, args...)
	if dir != "" {
		cmd.Dir = dir
	}
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb
	err := cmd.Run()
	code := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			code = -1
		}
	}
	return cmdResult{Stdout: out.String(), Stderr: errb.String(), ExitCode: code}
}

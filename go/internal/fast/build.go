// Package fast is the speed-optimized single-pass build node (swe-fast). It is a
// 1:1 behavioural port of swe_af/fast/*: single-pass flat planning, strictly
// sequential single-coder execution, one verification pass (no fix cycles), and
// a thin set of delegating wrappers around the full-pipeline execution roles.
//
// The node exposes four first-class reasoners — build, fast_plan_tasks,
// fast_execute_tasks, fast_verify (see Handlers) — plus seven delegating
// wrapper names (see Wrappers) that the wiring task registers backed by the
// full-pipeline role handlers. Every inter-reasoner hop goes through the
// injected CallFn (a closure over agent.Call + envelope.UnwrapCallResult) so the
// control-plane DAG renders identically to Python's app.call graph.
package fast

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"github.com/Agent-Field/SWE-AF/go/internal/workspace"
)

// ---------------------------------------------------------------------------
// Registration surface (consumed by T6.2)
// ---------------------------------------------------------------------------

// CallFn dispatches to a reasoner by target (e.g. "swe-fast-go.run_coder") with the
// same keyword args Python passes to app.call. The wiring task supplies a
// closure over agent.Call + envelope.UnwrapCallResult (so the returned map is
// already unwrapped); tests supply a scripted function. It is structurally
// identical to coding.CallFn, so a single closure satisfies both.
type CallFn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)

// Noter is the fire-and-forget observability channel. It matches the SDK's
// *agent.Agent.Note method (and coding.Noter), so the concrete agent satisfies
// it directly while tests supply a recorder. Every Python router.note(...) /
// app.note(...) maps to Note(ctx, message, tags...) with verbatim text+tags.
type Noter interface {
	Note(ctx context.Context, message string, tags ...string)
}

// Handler is the registration signature every fast reasoner exposes, matching
// the roles packages so T6.2 wires them uniformly.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Handlers returns the name→handler map for the fast node's first-class
// reasoners, keyed by the EXACT Python reasoner names. T6.2 ranges over this to
// register each reasoner on the swe-fast node.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"build":              Build,
		"fast_plan_tasks":    FastPlanTasks,
		"fast_execute_tasks": FastExecuteTasks,
		"fast_verify":        FastVerify,
	}
}

// defaultNodeID is the Go fast node's default identity when NODE_ID is unset.
// The Go port registers as "swe-fast-go" (an opt-in sibling of the Python
// swe-fast node) so both can run against one control plane; Python's
// module-level default is os.getenv("NODE_ID", "swe-fast").
const defaultNodeID = "swe-fast-go"

// ---------------------------------------------------------------------------
// Shared collaborators
// ---------------------------------------------------------------------------

// Deps carries the seams every fast reasoner needs. Harness drives the
// structured-output subprocess (fast_plan_tasks); Call is the app.call seam used
// by fast_execute_tasks, fast_verify and the build orchestrator; Note is the
// observability channel; NodeID is the target prefix (default "swe-fast-go")
// used when composing app.call targets — mirroring Python's f"{NODE_ID}.<reasoner>".
type Deps struct {
	Harness harnessx.HarnessCaller
	Call    CallFn
	Note    Noter
	NodeID  string
}

// nodeID returns the configured node id, defaulting to "swe-fast-go" (the Go
// port's opt-in-sibling identity) when NodeID is unset.
func (d *Deps) nodeID() string {
	if d != nil && d.NodeID != "" {
		return d.NodeID
	}
	return defaultNodeID
}

// note is a nil-safe wrapper so call sites need no guard, mirroring the Python
// _note helper's tolerance of an unattached router.
func (d *Deps) note(ctx context.Context, msg string, tags ...string) {
	if d != nil && d.Note != nil {
		d.Note.Note(ctx, msg, tags...)
	}
}

// ---------------------------------------------------------------------------
// build orchestrator (ports fast/app.py::build)
// ---------------------------------------------------------------------------

type buildInput struct {
	Goal              string         `json:"goal"`
	RepoPath          string         `json:"repo_path"`
	RepoURL           string         `json:"repo_url"`
	ArtifactsDir      string         `json:"artifacts_dir"`
	AdditionalContext string         `json:"additional_context"`
	Config            map[string]any `json:"config"`
}

// UnmarshalJSON seeds the Python parameter defaults (artifacts_dir=".artifacts").
func (b *buildInput) UnmarshalJSON(data []byte) error {
	*b = buildInput{ArtifactsDir: ".artifacts"}
	type alias buildInput
	return json.Unmarshal(data, (*alias)(b))
}

// repoNameRe ports the regex in fast/app.py::_repo_name_from_url.
var repoNameRe = regexp.MustCompile(`/([^/]+?)(?:\.git)?$`)

// repoNameFromURL ports _repo_name_from_url: extract the repo name from a URL.
func repoNameFromURL(url string) string {
	m := repoNameRe.FindStringSubmatch(strings.TrimRight(url, "/"))
	if m != nil {
		return m[1]
	}
	return "repo"
}

// runtimeToProvider ports fast/app.py::_runtime_to_provider — the fast-specific
// runtime→ai_provider map (note: anything not claude_code/codex → "opencode").
func runtimeToProvider(runtime string) string {
	switch runtime {
	case "claude_code":
		return "claude"
	case "codex":
		return "codex"
	default:
		return "opencode"
	}
}

// Build ports fast/app.py::build — the speed-optimized end-to-end pipeline:
// git_init → fast_plan_tasks → fast_execute_tasks → fast_verify → repo_finalize
// → github_pr, every stage invoked via CallFn (app.call parity).
func Build(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bind[buildInput](input)
	if err != nil {
		return nil, err
	}

	cfg, err := config.LoadFastBuildConfig(in.Config)
	if err != nil {
		return nil, err
	}

	// Allow repo_url from direct parameter (overrides config).
	effectiveRepoURL := in.RepoURL
	if effectiveRepoURL == "" {
		effectiveRepoURL = cfg.RepoURL
	}

	repoPath := in.RepoPath
	// Auto-derive repo_path from repo_url when not specified.
	if effectiveRepoURL != "" && repoPath == "" {
		repoPath = filepath.Join(workspace.Root(), repoNameFromURL(effectiveRepoURL))
	}
	if repoPath == "" {
		return nil, errors.New("Either repo_path or repo_url must be provided")
	}

	if err := os.MkdirAll(repoPath, 0o755); err != nil {
		return nil, err
	}

	resolved, err := config.FastResolveModels(cfg)
	if err != nil {
		return nil, err
	}
	aiProvider := runtimeToProvider(cfg.Runtime)
	absRepo, err := filepath.Abs(repoPath)
	if err != nil {
		return nil, err
	}
	absArtifactsDir := filepath.Join(absRepo, in.ArtifactsDir)

	node := deps.nodeID()

	// ── 1. GIT INIT (1 attempt, non-fatal) ──────────────────────────────────
	deps.note(ctx, "Fast build: git init", "fast_build", "git_init")
	var gitConfig map[string]any
	rawGit, gitErr := deps.Call(ctx, node+".run_git_init", map[string]any{
		"repo_path":       repoPath,
		"goal":            in.Goal,
		"artifacts_dir":   absArtifactsDir,
		"model":           resolved["git_model"],
		"permission_mode": cfg.PermissionMode,
		"ai_provider":     aiProvider,
		"build_id":        "",
	})
	if gitErr != nil {
		deps.note(ctx, fmt.Sprintf("Git init exception (non-fatal): %s", gitErr),
			"fast_build", "git_init", "error")
	} else {
		gitInit := rawGit
		if mapBool(gitInit, "success") {
			gitConfig = map[string]any{
				"integration_branch":    gitInit["integration_branch"],
				"original_branch":       gitInit["original_branch"],
				"initial_commit_sha":    gitInit["initial_commit_sha"],
				"mode":                  gitInit["mode"],
				"remote_url":            getString(gitInit, "remote_url", ""),
				"remote_default_branch": getString(gitInit, "remote_default_branch", ""),
			}
			deps.note(ctx, fmt.Sprintf("Git init: mode=%v, branch=%v",
				gitInit["mode"], gitInit["integration_branch"]),
				"fast_build", "git_init", "complete")
		} else {
			deps.note(ctx, fmt.Sprintf("Git init failed (non-fatal): %s",
				getString(gitInit, "error_message", "unknown")),
				"fast_build", "git_init", "error")
		}
	}

	// ── 2. PLAN + EXECUTE (wrapped in build_timeout) ────────────────────────
	deps.note(ctx, fmt.Sprintf("Fast build: plan + execute (timeout=%ds)", cfg.BuildTimeoutSeconds),
		"fast_build", "plan_execute")

	planAndExecute := func(pctx context.Context) (map[string]any, map[string]any, error) {
		// 2a. PLAN
		planResult, err := deps.Call(pctx, node+".fast_plan_tasks", map[string]any{
			"goal":               in.Goal,
			"repo_path":          repoPath,
			"max_tasks":          cfg.MaxTasks,
			"pm_model":           resolved["pm_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        aiProvider,
			"additional_context": in.AdditionalContext,
			"artifacts_dir":      absArtifactsDir,
		})
		if err != nil {
			return nil, nil, err
		}
		tasks := planResult["tasks"]
		if tasks == nil {
			tasks = []any{}
		}
		deps.note(ctx, fmt.Sprintf("Plan complete: %d tasks", listLen(tasks)),
			"fast_build", "plan", "complete")

		// 2b. EXECUTE
		executionResult, err := deps.Call(pctx, node+".fast_execute_tasks", map[string]any{
			"tasks":                tasks,
			"repo_path":            repoPath,
			"coder_model":          resolved["coder_model"],
			"permission_mode":      cfg.PermissionMode,
			"ai_provider":          aiProvider,
			"task_timeout_seconds": cfg.TaskTimeoutSeconds,
			"artifacts_dir":        absArtifactsDir,
			"agent_max_turns":      cfg.AgentMaxTurns,
		})
		if err != nil {
			return nil, nil, err
		}
		return planResult, executionResult, nil
	}

	planResult, executionResult, timedOut, err := runWithBuildTimeout(
		ctx, planAndExecute, time.Duration(cfg.BuildTimeoutSeconds)*time.Second)
	if err != nil {
		return nil, err
	}
	if timedOut {
		deps.note(ctx, fmt.Sprintf("Build timed out after %ds", cfg.BuildTimeoutSeconds),
			"fast_build", "timeout")
		return &schemas.FastBuildResult{
			PlanResult: map[string]any{},
			ExecutionResult: map[string]any{
				"timed_out":       true,
				"task_results":    []any{},
				"completed_count": 0,
				"failed_count":    0,
			},
			Success: false,
			Summary: fmt.Sprintf("Build timed out after %ds", cfg.BuildTimeoutSeconds),
		}, nil
	}

	// ── 3. VERIFY (one pass, no fix cycles) ─────────────────────────────────
	deps.note(ctx, "Fast build: verify", "fast_build", "verify")
	prdDict, ok := planResult["prd"].(map[string]any)
	if !ok || len(prdDict) == 0 {
		prdDict = map[string]any{
			"validated_description": in.Goal,
			"acceptance_criteria":   []any{},
			"must_have":             []any{},
			"nice_to_have":          []any{},
			"out_of_scope":          []any{},
		}
	}
	taskResults := executionResult["task_results"]
	if taskResults == nil {
		taskResults = []any{}
	}
	var verification map[string]any
	rawVerify, verifyErr := deps.Call(ctx, node+".fast_verify", map[string]any{
		"prd":             prdDict,
		"repo_path":       repoPath,
		"task_results":    taskResults,
		"verifier_model":  resolved["verifier_model"],
		"permission_mode": cfg.PermissionMode,
		"ai_provider":     aiProvider,
		"artifacts_dir":   absArtifactsDir,
	})
	if verifyErr != nil {
		deps.note(ctx, fmt.Sprintf("Verify failed (non-fatal): %s", verifyErr),
			"fast_build", "verify", "error")
		verification = map[string]any{
			"passed":  false,
			"summary": fmt.Sprintf("Verification failed: %s", verifyErr),
		}
	} else {
		verification = rawVerify
	}
	success := mapBool(verification, "passed")

	// ── 4. REPO FINALIZE (non-fatal) ────────────────────────────────────────
	deps.note(ctx, "Fast build: finalize", "fast_build", "finalize")
	if _, finErr := deps.Call(ctx, node+".run_repo_finalize", map[string]any{
		"repo_path":       repoPath,
		"artifacts_dir":   absArtifactsDir,
		"model":           resolved["git_model"],
		"permission_mode": cfg.PermissionMode,
		"ai_provider":     aiProvider,
	}); finErr != nil {
		deps.note(ctx, fmt.Sprintf("Finalize failed (non-fatal): %s", finErr),
			"fast_build", "finalize", "error")
	}

	// ── 5. GITHUB PR (if enabled and remote present) ────────────────────────
	prURL := ""
	remoteURL := ""
	if gitConfig != nil {
		remoteURL = getString(gitConfig, "remote_url", "")
	}
	if remoteURL != "" && cfg.EnableGithubPR {
		deps.note(ctx, "Fast build: GitHub PR", "fast_build", "github_pr")
		baseBranch := cfg.GithubPRBase
		if baseBranch == "" && gitConfig != nil {
			baseBranch = getString(gitConfig, "remote_default_branch", "")
		}
		if baseBranch == "" {
			baseBranch = "main"
		}
		completedCount := intOf(executionResult["completed_count"])
		totalCount := listLen(executionResult["task_results"])
		buildSummary := fmt.Sprintf("%s: %d/%d tasks completed, verification: %s",
			partialOrSuccess(success), completedCount, totalCount, getString(verification, "summary", ""))

		completedIssues := completedIssuesFromResults(taskResults)
		rawPR, prErr := deps.Call(ctx, node+".run_github_pr", map[string]any{
			"repo_path":          repoPath,
			"integration_branch": gitConfig["integration_branch"],
			"base_branch":        baseBranch,
			"goal":               in.Goal,
			"build_summary":      buildSummary,
			"completed_issues":   completedIssues,
			"accumulated_debt":   []any{},
			"artifacts_dir":      absArtifactsDir,
			"model":              resolved["git_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        aiProvider,
		})
		if prErr != nil {
			deps.note(ctx, fmt.Sprintf("PR creation failed (non-fatal): %s", prErr),
				"fast_build", "github_pr", "error")
		} else {
			prURL = getString(rawPR, "pr_url", "")
			if prURL != "" {
				deps.note(ctx, fmt.Sprintf("Draft PR: %s", prURL),
					"fast_build", "github_pr", "complete")
			}
		}
	}

	completedCount := intOf(executionResult["completed_count"])
	totalCount := listLen(executionResult["task_results"])
	summary := fmt.Sprintf("%s: %d/%d tasks completed",
		partialOrSuccess(success), completedCount, totalCount)
	if len(verification) > 0 {
		summary += fmt.Sprintf(", verification: %s", getString(verification, "summary", ""))
	}

	return &schemas.FastBuildResult{
		PlanResult:      planResult,
		ExecutionResult: executionResult,
		Verification:    verification,
		Success:         success,
		Summary:         summary,
		PRURL:           prURL,
	}, nil
}

// runWithBuildTimeout runs fn under a deadline context, mirroring the Python
// asyncio.wait_for around _plan_and_execute. It returns (plan, exec, timedOut,
// err): timedOut is true only when the build deadline (not parent cancellation)
// fired; a non-timeout error from planning/execution propagates via err (as
// Python's non-TimeoutError exceptions propagate past the except clause).
func runWithBuildTimeout(
	ctx context.Context,
	fn func(context.Context) (map[string]any, map[string]any, error),
	timeout time.Duration,
) (plan, exec map[string]any, timedOut bool, err error) {
	bctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	type res struct {
		plan, exec map[string]any
		err        error
	}
	ch := make(chan res, 1)
	go func() {
		p, e, err := fn(bctx)
		ch <- res{p, e, err}
	}()

	select {
	case <-bctx.Done():
		if ctx.Err() != nil {
			return nil, nil, false, ctx.Err() // parent cancelled → propagate
		}
		return nil, nil, true, nil // build-level timeout
	case r := <-ch:
		if r.err != nil {
			return nil, nil, false, r.err
		}
		return r.plan, r.exec, false, nil
	}
}

// partialOrSuccess mirrors Python's f"{'Success' if success else 'Partial'}".
func partialOrSuccess(success bool) string {
	if success {
		return "Success"
	}
	return "Partial"
}

// completedIssuesFromResults ports the list comprehension building the PR's
// completed_issues from the execution task_results (only outcome=="completed").
func completedIssuesFromResults(taskResults any) []map[string]any {
	out := []map[string]any{}
	items, ok := taskResults.([]any)
	if !ok {
		return out
	}
	for _, it := range items {
		r, ok := it.(map[string]any)
		if !ok {
			continue
		}
		if getString(r, "outcome", "") == "completed" {
			out = append(out, map[string]any{
				"issue_name":     getString(r, "task_name", ""),
				"result_summary": getString(r, "summary", ""),
			})
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Shared helpers (used across the fast package's reasoners)
// ---------------------------------------------------------------------------

// bind decodes an untyped reasoner input map into a typed value T, round-tripping
// through JSON so T's UnmarshalJSON runs and seeds the Python parameter defaults
// for absent keys (same mechanism as afx.Bind).
func bind[T any](input map[string]any) (T, error) {
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

// mapBool mirrors dict.get(key, False) for a bool-valued key.
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

// getString mirrors dict.get(key, default) for a string-valued key.
func getString(m map[string]any, key, def string) string {
	if m == nil {
		return def
	}
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return def
}

// intOf coerces a JSON number (int or float64) to int; anything else → 0.
// Mirrors Python's dict.get(key, 0) where the value is an integer count.
func intOf(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	}
	return 0
}

// listLen mirrors len(x) for a JSON list held as []any (or a typed slice); a
// non-list (or nil) yields 0, matching len([]).
func listLen(v any) int {
	switch s := v.(type) {
	case []any:
		return len(s)
	case []map[string]any:
		return len(s)
	case []string:
		return len(s)
	}
	return 0
}

// stringSlice coerces a JSON list value into []string, always returning a
// non-nil slice so it marshals to [] (not null), matching Pydantic's list[str]
// default of []. Non-string elements are skipped.
func stringSlice(v any) []string {
	out := []string{}
	switch s := v.(type) {
	case []string:
		return append(out, s...)
	case []any:
		for _, e := range s {
			if str, ok := e.(string); ok {
				out = append(out, str)
			}
		}
	}
	return out
}

// pyRepr renders a string the way Python's repr() does for the common case:
// wrapped in single quotes with backslash and single-quote escaped. Used to keep
// note strings that interpolate {value!r} byte-identical to Python.
func pyRepr(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `'`, `\'`)
	return "'" + s + "'"
}

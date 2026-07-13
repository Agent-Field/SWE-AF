// Package ci holds the post-PR CI-gate / PR-resolve role reasoners of the
// SWE-AF port: run_ci_watcher, run_ci_fixer and run_pr_resolver. Each is a 1:1
// port of the corresponding @router.reasoner in
// swe_af/reasoners/execution_agents.py (Phase C, execution_agents.py:1539-1780).
//
// Every handler has the shape
//
//	func(ctx context.Context, deps *Deps, input map[string]any) (any, error)
//
// so the wiring wave (T6.2) can register it under the exact Python reasoner name
// via Handlers(). The handlers mirror the Python functions 1:1:
//
//   - inputs are bound from the untyped map with the SAME parameter names and
//     defaults as the Python signatures (wait_seconds=1500, poll_seconds=30,
//     iteration=1, max_iterations=2, merge_state="skipped", model="sonnet",
//     ai_provider="claude", …);
//   - run_ci_watcher invokes NO LLM — it is pure delegation to
//     cigate.WatchPRChecks, returning its result unchanged;
//   - run_ci_fixer / run_pr_resolver invoke the structured-output harness through
//     the single choke point harnessx.Run[T] (which injects run-scoped
//     credentials and classifies fatal API errors);
//   - every router.note(...) call is ported verbatim (message text + tag list);
//   - fatal (non-retryable) harness/API errors propagate as *FatalHarnessError
//     and are never swallowed into a fallback;
//   - on a harness parse failure (Parsed==nil) each LLM role returns its
//     deterministic fallback struct — NOT an error — byte-identical to the
//     Python fallback (model_dump()).
//
// Model resolution is input-driven exactly as in Python: the reasoner reads the
// model from its input (default "sonnet"). The caller resolves the per-role
// model — for run_ci_fixer the CI-gate loop passes ci_fixer_model with a
// fallback to coder_model (app.py:326: resolved_models.get("ci_fixer_model",
// resolved_models.get("coder_model", ""))) — and hands it in; the reasoner does
// not re-resolve it here.
package ci

import (
	"context"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/cigate"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/prompts/advisor"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// Note is the fire-and-forget observability channel each reasoner uses. It
// mirrors the Python router.note(message, tags=[...]) call. Declaring it as an
// interface (rather than depending on *agent.Agent directly) lets tests capture
// the exact note messages and tags the roles emit — the same seam the Python
// tests get from a fake router.
type Note interface {
	Note(ctx context.Context, message string, tags ...string)
}

// App is the full dependency surface a CI role needs: the harness choke point
// (satisfied by *agent.Agent via its Harness method, the same as
// harnessx.HarnessCaller) plus Note. *agent.Agent satisfies App.
type App interface {
	harnessx.HarnessCaller
	Note
}

// Deps carries the shared dependencies threaded into every handler. It is a
// struct (not a bare App) so the wiring wave can extend it without touching
// every handler signature.
type Deps struct {
	App App
}

// Handler is the common signature every CI role reasoner exposes.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Handlers returns the name→handler registration surface for wiring (design
// §8). The names are the exact Python reasoner names, addressable at the same
// node.reasoner path.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"run_ci_watcher":  RunCIWatcher,
		"run_ci_fixer":    RunCIFixer,
		"run_pr_resolver": RunPRResolver,
	}
}

// resolveTools is the allowed-tool list shared verbatim by run_ci_fixer and
// run_pr_resolver (execution_agents.py:1651, :1751). Note Bash leads the list —
// kept in the Python order.
var resolveTools = []string{"Bash", "Read", "Edit", "Write", "Glob", "Grep"}

// watchFn is the package-level seam onto cigate.WatchPRChecks. Production uses
// the real deterministic gh poller; tests replace it with a scripted watcher so
// run_ci_watcher's plumbing (parameter forwarding, result pass-through, notes)
// is exercised without a live GitHub remote. cigate keeps its own execCommand
// seam unexported, so this indirection is the only way an external package can
// stub the watcher — mirroring harnessx's runIDFromContext seam.
var watchFn = cigate.WatchPRChecks

// ---------------------------------------------------------------------------
// run_ci_watcher (NO LLM — deterministic gh poller)
// ---------------------------------------------------------------------------

type ciWatcherInput struct {
	RepoPath    string `json:"repo_path"`
	PRNumber    int    `json:"pr_number"`
	WaitSeconds int    `json:"wait_seconds"`
	PollSeconds int    `json:"poll_seconds"`
	HeadSHA     string `json:"head_sha"`
}

// UnmarshalJSON seeds the Python parameter defaults (wait_seconds=1500,
// poll_seconds=30) so keys absent from the input map keep those defaults.
// head_sha defaults to "" (the Go zero), matching the Python signature.
func (c *ciWatcherInput) UnmarshalJSON(b []byte) error {
	*c = ciWatcherInput{WaitSeconds: 1500, PollSeconds: 30}
	type alias ciWatcherInput
	return jsonUnmarshal(b, (*alias)(c))
}

// RunCIWatcher ports run_ci_watcher (execution_agents.py:1539). It polls
// `gh pr checks` until conclusive via cigate.WatchPRChecks and returns the
// CIWatchResult unchanged. It invokes no LLM.
//
// The Python body wraps watch_pr_checks in a try/except that converts an
// exception into a status="error" CIWatchResult. The Go cigate.WatchPRChecks
// returns a CIWatchResult with no error (internal failures already surface as
// status="error" inside the poller), so that except branch is unreachable here
// and is deliberately not reproduced.
func RunCIWatcher(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[ciWatcherInput](input)
	if err != nil {
		return nil, err
	}

	startMsg := fmt.Sprintf("CI watcher: PR #%d, wait_cap=%ds, poll=%ds",
		in.PRNumber, in.WaitSeconds, in.PollSeconds)
	if in.HeadSHA != "" {
		startMsg += fmt.Sprintf(", anchored to %s", truncateRunes(in.HeadSHA, 10))
	}
	deps.App.Note(ctx, startMsg, "ci_watcher", "start")

	result := watchFn(ctx, in.RepoPath, in.PRNumber, in.WaitSeconds, in.PollSeconds, in.HeadSHA)

	deps.App.Note(ctx,
		fmt.Sprintf("CI watcher: status=%s (%s)", result.Status, result.Summary),
		"ci_watcher", "complete", result.Status)
	return result, nil
}

// ---------------------------------------------------------------------------
// run_ci_fixer
// ---------------------------------------------------------------------------

type ciFixerInput struct {
	RepoPath          string           `json:"repo_path"`
	PRNumber          int              `json:"pr_number"`
	PRURL             string           `json:"pr_url"`
	IntegrationBranch string           `json:"integration_branch"`
	BaseBranch        string           `json:"base_branch"`
	FailedChecks      []map[string]any `json:"failed_checks"`
	Iteration         int              `json:"iteration"`
	MaxIterations     int              `json:"max_iterations"`
	Goal              string           `json:"goal"`
	CompletedIssues   []map[string]any `json:"completed_issues"`
	PreviousAttempts  []map[string]any `json:"previous_attempts"`
	Model             string           `json:"model"`
	PermissionMode    string           `json:"permission_mode"`
	AIProvider        string           `json:"ai_provider"`
}

// UnmarshalJSON seeds the Python parameter defaults (iteration=1,
// max_iterations=2, model="sonnet", ai_provider="claude").
func (c *ciFixerInput) UnmarshalJSON(b []byte) error {
	*c = ciFixerInput{Iteration: 1, MaxIterations: 2, Model: "sonnet", AIProvider: "claude"}
	type alias ciFixerInput
	return jsonUnmarshal(b, (*alias)(c))
}

// RunCIFixer ports run_ci_fixer (execution_agents.py:1591). It diagnoses the
// failing CI checks, fixes the production code, and pushes a new commit to the
// integration branch, returning a CIFixResult-shaped result.
func RunCIFixer(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[ciFixerInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, fmt.Sprintf(
		"CI fixer: PR #%d, attempt %d/%d, %d failing check(s)",
		in.PRNumber, in.Iteration, in.MaxIterations, len(in.FailedChecks)),
		"ci_fixer", "start")

	taskPrompt := advisor.CIFixerTaskPrompt(advisor.CIFixerTaskOptions{
		RepoPath:          in.RepoPath,
		PRNumber:          in.PRNumber,
		PRURL:             in.PRURL,
		IntegrationBranch: in.IntegrationBranch,
		BaseBranch:        in.BaseBranch,
		FailedChecks:      toCIFailedChecks(in.FailedChecks),
		Iteration:         in.Iteration,
		MaxIterations:     in.MaxIterations,
		Goal:              in.Goal,
		CompletedIssues:   in.CompletedIssues,
		PreviousAttempts:  in.PreviousAttempts,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          resolveTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   advisor.CIFixerSystemPrompt,
		Cwd:            in.RepoPath,
	}.ToOptions()

	parsed, result, hErr := harnessx.Run[schemas.CIFixResult](ctx, deps.App, taskPrompt, opts)
	switch {
	case hErr != nil:
		if isFatal(hErr) {
			return nil, hErr // Non-retryable — propagate immediately.
		}
		deps.App.Note(ctx, "CI fixer agent failed: "+hErr.Error(), "ci_fixer", "error")
	case result != nil && result.Parsed != nil:
		deps.App.Note(ctx, fmt.Sprintf(
			"CI fixer complete: fixed=%s, pushed=%s, %d file(s) changed",
			pyBool(parsed.Fixed), pyBool(parsed.Pushed), len(parsed.FilesChanged)),
			"ci_fixer", "complete")
		return parsed, nil
	}

	return &schemas.CIFixResult{
		Fixed:               false,
		FilesChanged:        []string{},
		Summary:             "CI fixer agent failed to produce a valid result.",
		RejectedWorkarounds: []string{},
		ErrorMessage:        "CI fixer agent failed to produce a valid result.",
	}, nil
}

// ---------------------------------------------------------------------------
// run_pr_resolver
// ---------------------------------------------------------------------------

type prResolverInput struct {
	RepoPath          string           `json:"repo_path"`
	PRNumber          int              `json:"pr_number"`
	PRURL             string           `json:"pr_url"`
	HeadBranch        string           `json:"head_branch"`
	BaseBranch        string           `json:"base_branch"`
	MergeState        string           `json:"merge_state"`
	ConflictedFiles   []string         `json:"conflicted_files"`
	FailedChecks      []map[string]any `json:"failed_checks"`
	ReviewComments    []map[string]any `json:"review_comments"`
	Goal              string           `json:"goal"`
	AdditionalContext string           `json:"additional_context"`
	Model             string           `json:"model"`
	PermissionMode    string           `json:"permission_mode"`
	AIProvider        string           `json:"ai_provider"`
}

// UnmarshalJSON seeds the Python parameter defaults (merge_state="skipped",
// model="sonnet", ai_provider="claude"). conflicted_files/failed_checks/
// review_comments default to nil (the Python `x or []` normalization is applied
// at use — a nil slice has len 0 and iterates zero times, matching []).
func (p *prResolverInput) UnmarshalJSON(b []byte) error {
	*p = prResolverInput{MergeState: "skipped", Model: "sonnet", AIProvider: "claude"}
	type alias prResolverInput
	return jsonUnmarshal(b, (*alias)(p))
}

// RunPRResolver ports run_pr_resolver (execution_agents.py:1680). It resolves an
// open PR — completing an in-progress merge, fixing CI, and addressing review
// comments — and returns a PRResolveResult-shaped result. The orchestrator
// consumes addressed_comments (each carrying comment_id/thread_id/addressed/
// note) to drive the post-resolve thread-reply pass.
func RunPRResolver(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[prResolverInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, fmt.Sprintf(
		"PR resolver: PR #%d, merge_state=%s, %d failing check(s), %d review comment(s)",
		in.PRNumber, in.MergeState, len(in.FailedChecks), len(in.ReviewComments)),
		"pr_resolver", "start")

	taskPrompt := advisor.PRResolverTaskPrompt(advisor.PRResolverTaskOptions{
		RepoPath:          in.RepoPath,
		PRNumber:          in.PRNumber,
		PRURL:             in.PRURL,
		HeadBranch:        in.HeadBranch,
		BaseBranch:        in.BaseBranch,
		MergeState:        in.MergeState,
		ConflictedFiles:   in.ConflictedFiles,
		FailedChecks:      toCIFailedChecks(in.FailedChecks),
		ReviewComments:    toReviewComments(in.ReviewComments),
		Goal:              in.Goal,
		AdditionalContext: in.AdditionalContext,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          resolveTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   advisor.PRResolverSystemPrompt,
		Cwd:            in.RepoPath,
	}.ToOptions()

	parsed, result, hErr := harnessx.Run[schemas.PRResolveResult](ctx, deps.App, taskPrompt, opts)
	switch {
	case hErr != nil:
		if isFatal(hErr) {
			return nil, hErr // Non-retryable — propagate immediately.
		}
		deps.App.Note(ctx, "PR resolver agent failed: "+hErr.Error(), "pr_resolver", "error")
	case result != nil && result.Parsed != nil:
		deps.App.Note(ctx, fmt.Sprintf(
			"PR resolver complete: fixed=%s, pushed=%s, merge_resolved=%s, "+
				"%d file(s) changed, %d/%d comment(s) addressed",
			pyBool(parsed.Fixed), pyBool(parsed.Pushed), pyBool(parsed.MergeResolved),
			len(parsed.FilesChanged), countAddressed(parsed.AddressedComments),
			len(parsed.AddressedComments)),
			"pr_resolver", "complete")
		return parsed, nil
	}

	return &schemas.PRResolveResult{
		Fixed:               false,
		FilesChanged:        []string{},
		CommitSHAs:          []string{},
		AddressedComments:   []schemas.AddressedComment{},
		Summary:             "PR resolver agent failed to produce a valid result.",
		RejectedWorkarounds: []string{},
		ErrorMessage:        "PR resolver agent failed to produce a valid result.",
	}, nil
}

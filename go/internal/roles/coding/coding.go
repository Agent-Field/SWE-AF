// Package coding ports the four coding-loop role reasoners from
// swe_af/reasoners/execution_agents.py: run_coder, run_qa, run_code_reviewer
// and run_qa_synthesizer.
//
// Each role is an exported handler with the signature
//
//	func(ctx context.Context, deps *Deps, input map[string]any) (any, error)
//
// so the wiring task (T6.2) can register it under the exact Python reasoner name
// via Handlers(). The handlers mirror the Python functions 1:1:
//
//   - inputs are bound from the untyped map with the SAME parameter names and
//     defaults as the Python function signatures (model="sonnet"/"haiku",
//     ai_provider="claude", iteration=1, qa_ran=false, …);
//   - run_coder/run_qa/run_code_reviewer call the structured-output harness via
//     harnessx.Run; run_qa_synthesizer uses the direct-LLM path (Deps.AI), the
//     Go equivalent of Python's router.ai (NOT the coding harness);
//   - every .note() call is ported verbatim (message text + tag list);
//   - fatal (non-retryable) harness/API errors propagate as *FatalHarnessError
//     and are never swallowed into a fallback;
//   - on a schema parse failure (harness Parsed==nil, or an unparseable AI
//     response) each role returns its deterministic fallback struct — NOT an
//     error — with field values byte-identical to the Python fallback;
//   - iteration_id is threaded through and overwrites the parsed result's value
//     on the success path, exactly as Python does (`out["iteration_id"] = …`).
package coding

import (
	"context"
	"fmt"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/ai"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	prompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"github.com/Agent-Field/SWE-AF/go/internal/tools"
)

// ---------------------------------------------------------------------------
// Handler wiring surface (consumed by T6.2)
// ---------------------------------------------------------------------------

// Noter is the fire-and-forget observability channel. It matches the SDK's
// *agent.Agent.Note method so the concrete agent satisfies it directly, while
// tests can supply a recorder. Every Python `router.note(...)` maps to a
// deps.Note.Note(ctx, message, tags...) call with verbatim text and tags.
type Noter interface {
	Note(ctx context.Context, message string, tags ...string)
}

// AICaller is the direct-LLM seam used only by run_qa_synthesizer — the Go
// equivalent of Python's router.ai. *agent.Agent satisfies it via its AI method
// (sdk/go/agent/agent.go:1877); tests supply a mock so the synthesizer's
// decision logic is exercised without a live provider.
type AICaller interface {
	AI(ctx context.Context, prompt string, opts ...ai.Option) (*ai.Response, error)
}

// Deps carries the collaborators every coding role needs. Harness drives the
// structured-output subprocess (coder/qa/reviewer); AI drives the direct-LLM
// call (qa_synthesizer); Note is the observability channel. The role-level
// parameters (model, provider, permission_mode, …) come from the reasoner
// INPUT map — mirroring Python, where the coding loop passes them per call —
// so Deps deliberately holds no config.
type Deps struct {
	Harness harnessx.HarnessCaller
	AI      AICaller
	Note    Noter
}

// Handler is the registration signature every coding role exposes.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Handlers returns the name→handler map for the coding roles, keyed by the
// EXACT Python reasoner names. T6.2 ranges over this to register each reasoner.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"run_coder":          RunCoder,
		"run_qa":             RunQA,
		"run_code_reviewer":  RunCodeReviewer,
		"run_qa_synthesizer": RunQASynthesizer,
	}
}

// codingTools is the coder/qa allowed-tool list (Python passes this verbatim).
var codingTools = []string{"Read", "Write", "Edit", "Bash", "Glob", "Grep"}

// reviewerTools is the code-reviewer allowed-tool list. Note the ordering
// differs from codingTools (Bash last) — kept verbatim from the Python call.
var reviewerTools = []string{"Read", "Write", "Glob", "Grep", "Bash"}

// ---------------------------------------------------------------------------
// run_coder
// ---------------------------------------------------------------------------

type coderInput struct {
	Issue             map[string]any `json:"issue"`
	WorktreePath      string         `json:"worktree_path"`
	Feedback          string         `json:"feedback"`
	Iteration         int            `json:"iteration"`
	IterationID       string         `json:"iteration_id"`
	ProjectContext    map[string]any `json:"project_context"`
	MemoryContext     map[string]any `json:"memory_context"`
	Model             string         `json:"model"`
	PermissionMode    string         `json:"permission_mode"`
	AIProvider        string         `json:"ai_provider"`
	WorkspaceManifest map[string]any `json:"workspace_manifest"`
	TargetRepo        string         `json:"target_repo"`
}

// UnmarshalJSON seeds the Python parameter defaults (iteration=1, model="sonnet",
// ai_provider="claude") so keys absent from the input map keep those defaults.
func (c *coderInput) UnmarshalJSON(b []byte) error {
	*c = coderInput{Iteration: 1, Model: "sonnet", AIProvider: "claude"}
	type alias coderInput
	return jsonUnmarshal(b, (*alias)(c))
}

// RunCoder ports run_coder (execution_agents.py:963). Implements an issue and
// returns a CoderResult-shaped result.
func RunCoder(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[coderInput](input)
	if err != nil {
		return nil, err
	}

	issueName := issueNameOf(in.Issue)
	deps.Note.Note(ctx, fmt.Sprintf("Coder starting: %s (iteration %d)", issueName, in.Iteration), "coder", "start")

	wsManifest := maybeWorkspaceManifest(in.WorkspaceManifest)

	taskPrompt := prompts.CoderTaskPrompt(prompts.CoderTaskPromptOpts{
		Issue:             in.Issue,
		WorktreePath:      in.WorktreePath,
		Feedback:          in.Feedback,
		Iteration:         in.Iteration,
		ProjectContext:    in.ProjectContext,
		MemoryContext:     in.MemoryContext,
		WorkspaceManifest: wsManifest,
		TargetRepo:        in.TargetRepo,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          codingTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   tools.MaybeApplyCoderGuardrail(prompts.CoderSystemPrompt),
		Cwd:            in.WorktreePath,
	}.ToOptions()

	harnessError := ""
	parsed, result, hErr := harnessx.Run[schemas.CoderResult](ctx, deps.Harness, taskPrompt, opts)
	switch {
	case hErr != nil:
		if isFatal(hErr) {
			return nil, hErr // Non-retryable — propagate immediately.
		}
		harnessError = hErr.Error()
		deps.Note.Note(ctx, fmt.Sprintf("Coder agent failed: %s: %s", issueName, harnessError), "coder", "error")
	case result != nil && result.Parsed != nil:
		deps.Note.Note(ctx, fmt.Sprintf("Coder complete: %s, files=%d, complete=%s",
			issueName, len(parsed.FilesChanged), pyBool(parsed.Complete)), "coder", "complete")
		parsed.IterationID = in.IterationID
		return parsed, nil
	default:
		// Harness returned but produced no parseable CoderResult. Surface the
		// underlying error so the empty result carries *why*.
		if result != nil {
			harnessError = result.ErrorMessage
		}
		if harnessError == "" {
			harnessError = "no structured output returned"
		}
		deps.Note.Note(ctx, fmt.Sprintf("Coder produced no result: %s: %s", issueName, harnessError), "coder", "error")
	}

	summary := fmt.Sprintf("Coder agent failed for %s", issueName)
	if harnessError != "" {
		summary += ": " + harnessError
	}
	return &schemas.CoderResult{
		FilesChanged:      []string{},
		Summary:           summary,
		Complete:          false,
		IterationID:       in.IterationID,
		CodebaseLearnings: []string{},
		AgentRetro:        map[string]any{},
	}, nil
}

// ---------------------------------------------------------------------------
// run_qa
// ---------------------------------------------------------------------------

type qaInput struct {
	WorktreePath      string         `json:"worktree_path"`
	CoderResult       map[string]any `json:"coder_result"`
	Issue             map[string]any `json:"issue"`
	IterationID       string         `json:"iteration_id"`
	ProjectContext    map[string]any `json:"project_context"`
	Model             string         `json:"model"`
	PermissionMode    string         `json:"permission_mode"`
	AIProvider        string         `json:"ai_provider"`
	WorkspaceManifest map[string]any `json:"workspace_manifest"`
	TargetRepo        string         `json:"target_repo"`
}

// UnmarshalJSON seeds the Python parameter defaults (model="sonnet",
// ai_provider="claude").
func (q *qaInput) UnmarshalJSON(b []byte) error {
	*q = qaInput{Model: "sonnet", AIProvider: "claude"}
	type alias qaInput
	return jsonUnmarshal(b, (*alias)(q))
}

// RunQA ports run_qa (execution_agents.py:1060). Reviews/augments tests and runs
// the suite, returning a QAResult-shaped result.
func RunQA(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[qaInput](input)
	if err != nil {
		return nil, err
	}

	issueName := issueNameOf(in.Issue)
	deps.Note.Note(ctx, fmt.Sprintf("QA starting: %s", issueName), "qa", "start")

	wsManifest := maybeWorkspaceManifest(in.WorkspaceManifest)

	taskPrompt := prompts.QATaskPrompt(prompts.QATaskPromptOpts{
		WorktreePath:      in.WorktreePath,
		CoderResult:       in.CoderResult,
		Issue:             in.Issue,
		IterationID:       in.IterationID,
		ProjectContext:    in.ProjectContext,
		WorkspaceManifest: wsManifest,
		TargetRepo:        in.TargetRepo,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          codingTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   prompts.QASystemPrompt,
		Cwd:            in.WorktreePath,
	}.ToOptions()

	parsed, result, hErr := harnessx.Run[schemas.QAResult](ctx, deps.Harness, taskPrompt, opts)
	switch {
	case hErr != nil:
		if isFatal(hErr) {
			return nil, hErr
		}
		deps.Note.Note(ctx, fmt.Sprintf("QA agent failed: %s: %s", issueName, hErr.Error()), "qa", "error")
	case result != nil && result.Parsed != nil:
		deps.Note.Note(ctx, fmt.Sprintf("QA complete: %s, passed=%s", issueName, pyBool(parsed.Passed)), "qa", "complete")
		parsed.IterationID = in.IterationID
		return parsed, nil
	}

	return &schemas.QAResult{
		Passed:       false,
		Summary:      fmt.Sprintf("QA agent failed for %s", issueName),
		TestFailures: []map[string]any{},
		CoverageGaps: []string{},
		IterationID:  in.IterationID,
	}, nil
}

// ---------------------------------------------------------------------------
// run_code_reviewer
// ---------------------------------------------------------------------------

type codeReviewerInput struct {
	WorktreePath      string         `json:"worktree_path"`
	CoderResult       map[string]any `json:"coder_result"`
	Issue             map[string]any `json:"issue"`
	IterationID       string         `json:"iteration_id"`
	ProjectContext    map[string]any `json:"project_context"`
	QARan             bool           `json:"qa_ran"`
	MemoryContext     map[string]any `json:"memory_context"`
	Model             string         `json:"model"`
	PermissionMode    string         `json:"permission_mode"`
	AIProvider        string         `json:"ai_provider"`
	WorkspaceManifest map[string]any `json:"workspace_manifest"`
	TargetRepo        string         `json:"target_repo"`
}

// UnmarshalJSON seeds the Python parameter defaults (qa_ran=false is the Go zero
// value; model="sonnet", ai_provider="claude").
func (c *codeReviewerInput) UnmarshalJSON(b []byte) error {
	*c = codeReviewerInput{Model: "sonnet", AIProvider: "claude"}
	type alias codeReviewerInput
	return jsonUnmarshal(b, (*alias)(c))
}

// RunCodeReviewer ports run_code_reviewer (execution_agents.py:1134). Reviews
// code quality/security/requirements and threads qa_ran through to the prompt.
func RunCodeReviewer(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[codeReviewerInput](input)
	if err != nil {
		return nil, err
	}

	issueName := issueNameOf(in.Issue)
	deps.Note.Note(ctx, fmt.Sprintf("Code reviewer starting: %s", issueName), "code_reviewer", "start")

	wsManifest := maybeWorkspaceManifest(in.WorkspaceManifest)

	taskPrompt := prompts.CodeReviewerTaskPrompt(prompts.CodeReviewerTaskPromptOpts{
		WorktreePath:      in.WorktreePath,
		CoderResult:       in.CoderResult,
		Issue:             in.Issue,
		IterationID:       in.IterationID,
		ProjectContext:    in.ProjectContext,
		QARan:             in.QARan,
		MemoryContext:     in.MemoryContext,
		WorkspaceManifest: wsManifest,
		TargetRepo:        in.TargetRepo,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          reviewerTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   prompts.CodeReviewerSystemPrompt,
		Cwd:            in.WorktreePath,
	}.ToOptions()

	parsed, result, hErr := harnessx.Run[schemas.CodeReviewResult](ctx, deps.Harness, taskPrompt, opts)
	switch {
	case hErr != nil:
		if isFatal(hErr) {
			return nil, hErr
		}
		deps.Note.Note(ctx, fmt.Sprintf("Code reviewer agent failed: %s: %s", issueName, hErr.Error()), "code_reviewer", "error")
	case result != nil && result.Parsed != nil:
		deps.Note.Note(ctx, fmt.Sprintf("Code reviewer complete: %s, approved=%s, blocking=%s",
			issueName, pyBool(parsed.Approved), pyBool(parsed.Blocking)), "code_reviewer", "complete")
		parsed.IterationID = in.IterationID
		return parsed, nil
	}

	return &schemas.CodeReviewResult{
		Approved:    true, // don't block on reviewer failure
		Summary:     fmt.Sprintf("Code reviewer agent failed for %s — not blocking", issueName),
		Blocking:    false,
		DebtItems:   []map[string]any{},
		IterationID: in.IterationID,
	}, nil
}

// ---------------------------------------------------------------------------
// run_qa_synthesizer (direct-LLM path, NOT the coding harness)
// ---------------------------------------------------------------------------

type qaSynthInput struct {
	QAResult          map[string]any   `json:"qa_result"`
	ReviewResult      map[string]any   `json:"review_result"`
	IterationHistory  []map[string]any `json:"iteration_history"`
	IterationID       string           `json:"iteration_id"`
	WorktreePath      string           `json:"worktree_path"`
	IssueSummary      map[string]any   `json:"issue_summary"`
	ArtifactsDir      string           `json:"artifacts_dir"`
	Model             string           `json:"model"`
	PermissionMode    string           `json:"permission_mode"`
	AIProvider        string           `json:"ai_provider"`
	WorkspaceManifest map[string]any   `json:"workspace_manifest"`
	TargetRepo        string           `json:"target_repo"`
}

// UnmarshalJSON seeds the Python parameter defaults (model="haiku",
// ai_provider="claude").
func (q *qaSynthInput) UnmarshalJSON(b []byte) error {
	*q = qaSynthInput{Model: "haiku", AIProvider: "claude"}
	type alias qaSynthInput
	return jsonUnmarshal(b, (*alias)(q))
}

// RunQASynthesizer ports run_qa_synthesizer (execution_agents.py:1216). Merges
// QA + review feedback into a fix/approve/block decision. Uses the direct-LLM
// path (Deps.AI, = Python's router.ai) rather than the coding harness, and on
// any failure falls back to a deterministic decision derived from the raw QA and
// review results (execution_agents.py:1277-1304, verbatim).
func RunQASynthesizer(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bindInput[qaSynthInput](input)
	if err != nil {
		return nil, err
	}

	deps.Note.Note(ctx, "QA synthesizer starting", "qa_synthesizer", "start")

	wsManifest := maybeWorkspaceManifest(in.WorkspaceManifest)

	taskPrompt := prompts.QASynthesizerTaskPrompt(prompts.QASynthesizerTaskPromptOpts{
		QAResult:          in.QAResult,
		ReviewResult:      in.ReviewResult,
		IterationHistory:  in.IterationHistory,
		IterationID:       in.IterationID,
		WorktreePath:      in.WorktreePath,
		IssueSummary:      in.IssueSummary,
		WorkspaceManifest: wsManifest,
	})

	resp, aiErr := deps.AI.AI(ctx, taskPrompt,
		ai.WithSystem(prompts.QASynthesizerSystemPrompt),
		ai.WithModel(mapSynthModel(in.Model)),
		ai.WithSchema(schemas.QASynthesisResult{}),
	)
	if aiErr != nil {
		if isFatal(aiErr) {
			// Non-retryable — surface as a FatalHarnessError (parity with the
			// Python `except FatalHarnessError: raise`).
			return nil, asFatalError(aiErr)
		}
		deps.Note.Note(ctx, fmt.Sprintf("QA synthesizer agent failed: %s", aiErr.Error()), "qa_synthesizer", "error")
	} else if parsed, ok := parseSynthesis(resp); ok {
		deps.Note.Note(ctx, fmt.Sprintf("QA synthesizer complete: action=%s, stuck=%s",
			string(parsed.Action), pyBool(parsed.Stuck)), "qa_synthesizer", "complete")
		parsed.IterationID = in.IterationID
		return parsed, nil
	}

	// Fallback: inspect the raw results to make a safe decision.
	testsPassed := mapBool(in.QAResult, "passed")
	reviewApproved := mapBool(in.ReviewResult, "approved")
	reviewBlocking := mapBool(in.ReviewResult, "blocking")

	var fallbackAction, fallbackSummary string
	switch {
	case testsPassed && reviewApproved && !reviewBlocking:
		fallbackAction = "approve"
		fallbackSummary = "Synthesizer failed but QA passed and review approved — approving."
	case reviewBlocking:
		fallbackAction = "block"
		fallbackSummary = "Synthesizer failed and review has blocking issues — blocking."
	default:
		fallbackAction = "fix"
		fallbackSummary = fmt.Sprintf(
			"Synthesizer failed — defaulting to FIX. QA passed=%s, review approved=%s.",
			pyBool(testsPassed), pyBool(reviewApproved))
	}

	return &schemas.QASynthesisResult{
		Action:      schemas.QASynthesisAction(fallbackAction),
		Summary:     fallbackSummary,
		Stuck:       false,
		IterationID: in.IterationID,
	}, nil
}

// mapSynthModel maps the short role model alias (haiku/sonnet/opus, the coding
// loop's cfg.QASynthesizerModel() default is "haiku") to a provider-qualified
// model id when the direct-LLM client targets OpenRouter — its OpenAI-compatible
// endpoint has no "haiku" model, so an unmapped alias 400s. Ids already carrying
// a "/" (already provider-qualified) pass through unchanged, and when the client
// is not OpenRouter (OpenAI-compatible or no key configured) the alias is left
// as-is. The OpenRouter decision is re-derived from the same env ai.DefaultConfig
// reads, matching the AIConfig wired onto the agent in node.BuildAgent.
func mapSynthModel(model string) string {
	if strings.Contains(model, "/") {
		return model
	}
	if !ai.DefaultConfig().IsOpenRouter() {
		return model
	}
	switch model {
	case "haiku":
		return "anthropic/claude-haiku-4.5"
	case "sonnet":
		return "anthropic/claude-sonnet-4.5"
	case "opus":
		return "anthropic/claude-opus-4.1"
	default:
		return model
	}
}

// parseSynthesis mirrors Python's `result.parsed is not None`: a synthesis is
// usable only when the AI produced parseable JSON carrying a valid action enum
// (Python's pydantic validation returns parsed=None on a missing/invalid
// required field). On any parse failure or empty response it reports ok=false so
// the caller falls through to the deterministic fallback (no error note — Python
// only notes when router.ai itself raises).
func parseSynthesis(resp *ai.Response) (*schemas.QASynthesisResult, bool) {
	if resp == nil {
		return nil, false
	}
	var out schemas.QASynthesisResult
	if err := resp.JSON(&out); err != nil {
		return nil, false
	}
	// The system prompt names the actions in uppercase (FIX/APPROVE/BLOCK) and
	// the reflected request schema carries no enum, so models routinely answer
	// in uppercase. Normalize before the enum check.
	out.Action = schemas.QASynthesisAction(strings.ToLower(strings.TrimSpace(string(out.Action))))
	switch out.Action {
	case schemas.QASynthesisActionFix, schemas.QASynthesisActionApprove, schemas.QASynthesisActionBlock:
		return &out, true
	default:
		return nil, false
	}
}

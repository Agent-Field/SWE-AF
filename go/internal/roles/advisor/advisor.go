package advisor

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/prompts/advisor"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// advisorTools is the tool set shared by the advisor/verify harness reasoners
// (Read/Write/Glob/Grep/Bash), matching the Python tools= lists.
var advisorTools = []string{"Read", "Write", "Glob", "Grep", "Bash"}

// isFatal reports whether err is a non-retryable fatal harness error, which the
// reasoners must propagate rather than swallow into a fallback.
func isFatal(err error) bool {
	var fErr *fatal.FatalHarnessError
	return errors.As(err, &fErr)
}

// ---------------------------------------------------------------------------
// run_retry_advisor
// ---------------------------------------------------------------------------

type retryAdvisorInput struct {
	Issue               map[string]any `json:"issue"`
	ErrorMessage        string         `json:"error_message"`
	ErrorContext        string         `json:"error_context"`
	AttemptNumber       int            `json:"attempt_number"`
	RepoPath            string         `json:"repo_path"`
	PRDSummary          string         `json:"prd_summary"`
	ArchitectureSummary string         `json:"architecture_summary"`
	PRDPath             string         `json:"prd_path"`
	ArchitecturePath    string         `json:"architecture_path"`
	ArtifactsDir        string         `json:"artifacts_dir"`
	Model               string         `json:"model"`
	PermissionMode      string         `json:"permission_mode"`
	AIProvider          string         `json:"ai_provider"`
	WorkspaceManifest   map[string]any `json:"workspace_manifest"`
}

// RunRetryAdvisor diagnoses a coding agent failure and advises whether to retry.
// Ports run_retry_advisor. On agent failure it returns a safe default
// (should_retry=false).
func RunRetryAdvisor(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := retryAdvisorInput{Model: "sonnet", AIProvider: "claude"}
	if err := decodeInput(input, &in); err != nil {
		return nil, err
	}

	deps.note(ctx, fmt.Sprintf(
		"Retry advisor analyzing %s (attempt %d)", getStr(in.Issue, "name", "?"), in.AttemptNumber),
		"retry_advisor", "start")

	wsManifest, err := maybeWorkspaceManifest(in.WorkspaceManifest)
	if err != nil {
		return nil, err
	}

	taskPrompt := advisor.RetryAdvisorTaskPrompt(advisor.RetryAdvisorTaskOptions{
		Issue:               in.Issue,
		ErrorMessage:        in.ErrorMessage,
		ErrorContext:        in.ErrorContext,
		AttemptNumber:       in.AttemptNumber,
		PRDSummary:          in.PRDSummary,
		ArchitectureSummary: in.ArchitectureSummary,
		PRDPath:             in.PRDPath,
		ArchitecturePath:    in.ArchitecturePath,
		WorkspaceManifest:   wsManifest,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.Model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          advisorTools,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   advisor.RetryAdvisorSystemPrompt,
		Cwd:            in.RepoPath,
	}.ToOptions()

	parsed, result, err := harnessx.Run[schemas.RetryAdvice](ctx, deps.Harness, taskPrompt, opts)
	if err != nil {
		if isFatal(err) {
			return nil, err
		}
		deps.note(ctx, fmt.Sprintf("Retry advisor agent failed: %v", err), "retry_advisor", "error")
	} else if result != nil && result.Parsed != nil {
		deps.note(ctx, fmt.Sprintf(
			"Retry advisor: should_retry=%s, confidence=%s", pyBool(parsed.ShouldRetry), pyFloat(parsed.Confidence)),
			"retry_advisor", "complete")
		return parsed, nil
	}

	// Fallback: don't retry if the advisor itself failed.
	return schemas.RetryAdvice{
		ShouldRetry:     false,
		Diagnosis:       "Retry advisor agent failed to produce a valid analysis.",
		Strategy:        "Cannot advise — advisor failure.",
		ModifiedContext: "",
		Confidence:      0.0,
	}, nil
}

// ---------------------------------------------------------------------------
// run_issue_advisor (HITL-wrapped)
// ---------------------------------------------------------------------------

type issueAdvisorInput struct {
	Issue                 map[string]any   `json:"issue"`
	OriginalIssue         map[string]any   `json:"original_issue"`
	FailureResult         map[string]any   `json:"failure_result"`
	IterationHistory      []map[string]any `json:"iteration_history"`
	DAGStateSummary       map[string]any   `json:"dag_state_summary"`
	AdvisorInvocation     int              `json:"advisor_invocation"`
	MaxAdvisorInvocations int              `json:"max_advisor_invocations"`
	PreviousAdaptations   []map[string]any `json:"previous_adaptations"`
	WorktreePath          string           `json:"worktree_path"`
	Model                 string           `json:"model"`
	PermissionMode        string           `json:"permission_mode"`
	AIProvider            string           `json:"ai_provider"`
	WorkspaceManifest     map[string]any   `json:"workspace_manifest"`
	PriorUserResponses    []map[string]any `json:"prior_user_responses"`
}

// RunIssueAdvisor analyzes a coding-loop failure and decides how to adapt.
// Ports run_issue_advisor. It is wrapped in the ask-user (HITL) loop and, on
// agent failure, falls back to ACCEPT_WITH_DEBT (never blocking the pipeline).
func RunIssueAdvisor(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := issueAdvisorInput{AdvisorInvocation: 1, MaxAdvisorInvocations: 2, Model: "sonnet", AIProvider: "claude"}
	if err := decodeInput(input, &in); err != nil {
		return nil, err
	}

	issueName := getStr(in.Issue, "name", "?")
	deps.note(ctx, fmt.Sprintf(
		"Issue advisor analyzing %s (invocation %d/%d)", issueName, in.AdvisorInvocation, in.MaxAdvisorInvocations),
		"issue_advisor", "start")

	wsManifest, err := maybeWorkspaceManifest(in.WorkspaceManifest)
	if err != nil {
		return nil, err
	}

	cwd := in.WorktreePath
	if cwd == "" {
		cwd = getStr(in.DAGStateSummary, "repo_path", ".")
	}
	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	invoke := func(ctx context.Context, kwargs map[string]any) (map[string]any, error) {
		taskPrompt := advisor.IssueAdvisorTaskPrompt(advisor.IssueAdvisorTaskOptions{
			Issue:                 in.Issue,
			OriginalIssue:         in.OriginalIssue,
			FailureResult:         in.FailureResult,
			IterationHistory:      in.IterationHistory,
			DAGStateSummary:       in.DAGStateSummary,
			AdvisorInvocation:     in.AdvisorInvocation,
			MaxAdvisorInvocations: in.MaxAdvisorInvocations,
			PreviousAdaptations:   in.PreviousAdaptations,
			WorktreePath:          in.WorktreePath,
			WorkspaceManifest:     wsManifest,
			PriorUserResponses:    priorResponses(kwargs),
		})
		opts := harnessx.RoleOptions{
			Provider:       provider,
			Model:          in.Model,
			MaxTurns:       config.DefaultAgentMaxTurns,
			Tools:          advisorTools,
			PermissionMode: in.PermissionMode,
			SystemPrompt:   advisor.IssueAdvisorSystemPrompt,
			Cwd:            cwd,
		}.ToOptions()

		parsed, result, err := harnessx.Run[schemas.IssueAdvisorDecision](ctx, deps.Harness, taskPrompt, opts)
		if err != nil {
			return nil, err
		}
		if result != nil && result.Parsed != nil {
			return structToMap(parsed)
		}
		return nil, nil
	}

	resultMap, err := deps.runWithAskUser(ctx, invoke, initialKwargs(in.PriorUserResponses), "issue_advisor")
	if err != nil {
		if isFatal(err) {
			return nil, err
		}
		deps.note(ctx, fmt.Sprintf("Issue advisor agent failed: %v", err), "issue_advisor", "error")
	} else if resultMap != nil {
		deps.note(ctx, fmt.Sprintf(
			"Issue advisor decision: %s — %s", getStr(resultMap, "action", ""), getStr(resultMap, "summary", "")),
			"issue_advisor", "complete")
		return resultMap, nil
	}

	// Fallback: accept with debt rather than blocking the pipeline.
	fallback := schemas.IssueAdvisorDecision{
		Action:               schemas.AdvisorActionAcceptWithDebt,
		FailureDiagnosis:     "Issue Advisor agent failed to produce a valid analysis.",
		FailureCategory:      "environment",
		Rationale:            "Advisor failure — accepting with debt to avoid pipeline stall.",
		Confidence:           0.1,
		MissingFunctionality: []string{fmt.Sprintf("Full implementation of %s", issueName)},
		DebtSeverity:         "high",
		Summary:              fmt.Sprintf("Issue advisor failed — accepting %s with debt", issueName),
	}
	deps.note(ctx, "Issue advisor failed — falling back to ACCEPT_WITH_DEBT", "issue_advisor", "fallback")
	return fallback, nil
}

// ---------------------------------------------------------------------------
// run_replanner (HITL-wrapped)
// ---------------------------------------------------------------------------

type replannerInput struct {
	DAGState           map[string]any   `json:"dag_state"`
	FailedIssues       []map[string]any `json:"failed_issues"`
	ReplanModel        string           `json:"replan_model"`
	PermissionMode     string           `json:"permission_mode"`
	AIProvider         string           `json:"ai_provider"`
	EscalationNotes    []map[string]any `json:"escalation_notes"`
	PriorUserResponses []map[string]any `json:"prior_user_responses"`
}

// RunReplanner decides how to handle unrecoverable failures. Ports run_replanner.
// It is wrapped in the ask-user (HITL) loop and, on agent failure, falls back to
// CONTINUE (not ABORT) so a replanner crash does not kill the pipeline.
func RunReplanner(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := replannerInput{ReplanModel: "sonnet", AIProvider: "claude"}
	if err := decodeInput(input, &in); err != nil {
		return nil, err
	}

	state, err := buildDAGState(in.DAGState)
	if err != nil {
		return nil, err
	}
	failures, err := buildIssueResults(in.FailedIssues)
	if err != nil {
		return nil, err
	}
	failedNames := issueNames(failures)

	deps.note(ctx, fmt.Sprintf(
		"Replanner starting (attempt %d/%d): failed = %s", state.ReplanCount+1, state.MaxReplans, pyStrList(failedNames)),
		"replanner", "start")

	logDir := ""
	if state.ArtifactsDir != "" {
		logDir = filepath.Join(state.ArtifactsDir, "logs")
	}
	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	cwd := state.RepoPath
	if cwd == "" {
		cwd = "."
	}

	invoke := func(ctx context.Context, kwargs map[string]any) (map[string]any, error) {
		taskPrompt := advisor.ReplannerTaskPrompt(advisor.ReplannerTaskOptions{
			DAGState:           state,
			FailedIssues:       failures,
			EscalationNotes:    in.EscalationNotes,
			AdaptationHistory:  state.AdaptationHistory,
			PriorUserResponses: priorResponses(kwargs),
		})
		currentPrompt := taskPrompt
		for attempt := 0; attempt < 2; attempt++ {
			opts := harnessx.RoleOptions{
				Provider:       provider,
				Model:          in.ReplanModel,
				MaxTurns:       config.DefaultAgentMaxTurns,
				Tools:          advisorTools,
				PermissionMode: in.PermissionMode,
				SystemPrompt:   advisor.ReplannerSystemPrompt,
				Cwd:            cwd,
			}.ToOptions()

			parsed, result, err := harnessx.Run[schemas.ReplanDecision](ctx, deps.Harness, currentPrompt, opts)
			if err != nil {
				return nil, err
			}
			if logDir != "" {
				text := "(empty)"
				if result != nil {
					if t := result.Text(); t != "" {
						text = t
					}
				}
				rawLog := filepath.Join(logDir, fmt.Sprintf("replanner_%d_raw_%d.txt", state.ReplanCount, attempt))
				if mkErr := os.MkdirAll(logDir, 0o755); mkErr == nil {
					_ = os.WriteFile(rawLog, []byte(text), 0o644)
				}
			}
			if result != nil && result.Parsed != nil {
				return structToMap(parsed)
			}
			rawText := ""
			if result != nil {
				rawText = result.Text()
			}
			deps.note(ctx, fmt.Sprintf(
				"Replanner produced unparseable output (attempt %d): %s", attempt+1, runeTruncate(rawText, 500)),
				"replanner", "parse_error")
			currentPrompt = "YOUR PREVIOUS RESPONSE COULD NOT BE PARSED. " +
				"Output ONLY valid JSON conforming to the ReplanDecision schema.\n\n" + taskPrompt
		}
		return nil, nil
	}

	resultMap, err := deps.runWithAskUser(ctx, invoke, initialKwargs(in.PriorUserResponses), "replanner")
	if err != nil {
		if isFatal(err) {
			return nil, err
		}
		deps.note(ctx, fmt.Sprintf("Replanner agent failed: %v", err), "replanner", "error")
	} else if resultMap != nil {
		deps.note(ctx, fmt.Sprintf(
			"Replan decision: %s — %s", getStr(resultMap, "action", ""), getStr(resultMap, "summary", "")),
			"replanner", "complete")
		return resultMap, nil
	}

	// Pitfall 5 fix: fall back to CONTINUE, not ABORT.
	fallback := schemas.ReplanDecision{
		Action: schemas.ReplanActionContinue,
		Rationale: "Replanner agent failed to produce a valid decision. " +
			"Falling back to CONTINUE — downstream of failed issues will be " +
			"notified of the gap but the pipeline will proceed.",
		SkippedIssueNames: []string{},
		Summary:           fmt.Sprintf("Replanner failure — continuing with gap notification for: %s", pyStrList(failedNames)),
	}
	deps.note(ctx, "Replanner failed — falling back to CONTINUE (not ABORT)", "replanner", "fallback")
	return fallback, nil
}

// ---------------------------------------------------------------------------
// HITL + reconstruction helpers
// ---------------------------------------------------------------------------

// initialKwargs seeds the ask-user kwargs with prior_user_responses, mirroring
// Python's `initial_prior = list(prior_user_responses or [])`.
func initialKwargs(prior []map[string]any) map[string]any {
	var v any
	if prior == nil {
		v = []any{}
	} else {
		v = prior
	}
	return map[string]any{"prior_user_responses": v}
}

// runWithAskUser drives the shared ask-user loop for the HITL reasoners with a
// fresh per-call budget of 2 (matching AskUserBudget(remaining=2) in Python).
// The Hax client is (re)built per call from env unless Deps.BuildHaxClient
// overrides it — a nil client disables HITL (build_hax_client_from_env → None).
func (d *Deps) runWithAskUser(
	ctx context.Context,
	invoke hitl.ReasonerInvoke,
	kwargs map[string]any,
	label string,
) (map[string]any, error) {
	var hax *hitl.HaxClient
	if d.BuildHaxClient != nil {
		hax = d.BuildHaxClient()
	} else {
		hax = hitl.BuildHaxClientFromEnv()
	}
	return hitl.RunWithAskUser(ctx, invoke, kwargs, hitl.RunWithAskUserParams{
		App:         d.App,
		Pauser:      d.Pauser,
		Hax:         hax,
		Budget:      &hitl.AskUserBudget{Remaining: 2},
		NodeID:      d.NodeID,
		ExecutionID: executionIDFromContext(ctx),
		WebhookURL:  hitl.ApprovalWebhookURL(d.AgentFieldServer),
		NoteLabel:   label,
	})
}

// buildDAGState reconstructs a DAGState from the input dict. Ports
// _build_dag_state (DAGState(**dag_state_dict)); DAGState.UnmarshalJSON seeds its
// non-zero defaults (e.g. max_replans=2).
func buildDAGState(raw map[string]any) (schemas.DAGState, error) {
	var state schemas.DAGState
	if err := decodeInput(raw, &state); err != nil {
		return state, err
	}
	return state, nil
}

// buildIssueResults reconstructs an IssueResult list from dicts. Ports
// _build_issue_results.
func buildIssueResults(raw []map[string]any) ([]schemas.IssueResult, error) {
	out := make([]schemas.IssueResult, 0, len(raw))
	for _, r := range raw {
		var ir schemas.IssueResult
		if err := decodeInput(r, &ir); err != nil {
			return nil, err
		}
		out = append(out, ir)
	}
	return out, nil
}

// issueNames returns the issue_name of each failure, mirroring
// [f.issue_name for f in failures].
func issueNames(failures []schemas.IssueResult) []string {
	names := make([]string, 0, len(failures))
	for _, f := range failures {
		names = append(names, f.IssueName)
	}
	return names
}

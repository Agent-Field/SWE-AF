// Package planning ports the five planning-pipeline role reasoners from
// swe_af/reasoners/pipeline.py (:158-549): run_product_manager,
// run_environment_scout, run_architect, run_tech_lead and run_sprint_planner.
//
// Each role is an exported handler of the shape
//
//	func(ctx context.Context, deps *Deps, input map[string]any) (any, error)
//
// mirroring the Python @router.reasoner() signatures: the input keys, defaults
// and tool lists are byte-for-byte matches so the async API body stays
// compatible, and every note()/tag, fatal propagation and deterministic
// fallback is preserved verbatim.
//
// The single choke-point for harness calls is harnessx.Run[T]; the HITL-wrapped
// roles (PM, scout) drive hitl.RunWithAskUser, engaging the ask-user loop only
// when a hax client is configured (Deps.Hax != nil) — the Go analogue of
// build_hax_client_from_env() returning None.
package planning

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dagutil"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	prompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/planning"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// Handler is the exported reasoner-handler shape every planning role satisfies.
// The node-wiring wave registers each by its exact Python name via Handlers().
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Deps carries the collaborators a planning handler needs. The concrete
// *agent.Agent satisfies Harness, App and Pauser; tests supply mocks.
//
// Hax is the hax REST client. When nil the ask-user loop is DISABLED (the LLM's
// ask_user_form is stripped and the current decision proceeds) — matching
// Python's build_hax_client_from_env() returning None when HAX_API_KEY is unset.
// The node wiring builds it once via hitl.BuildHaxClientFromEnv().
type Deps struct {
	Harness          harnessx.HarnessCaller
	App              hitl.App
	Pauser           hitl.Pauser
	Hax              *hitl.HaxClient
	NodeID           string
	AgentFieldServer string
}

// executionContextFrom is a seam over agent.ExecutionContextFrom so tests can
// inject a run_id / execution_id (the SDK's context key is unexported, so an
// external test cannot seed an ExecutionContext into a ctx directly).
var executionContextFrom = agent.ExecutionContextFrom

// Handlers is the name→handler registration surface consumed by node wiring.
// The keys are the exact Python reasoner names.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"run_product_manager":   RunProductManager,
		"run_environment_scout": RunEnvironmentScout,
		"run_architect":         RunArchitect,
		"run_tech_lead":         RunTechLead,
		"run_sprint_planner":    RunSprintPlanner,
	}
}

// ---------------------------------------------------------------------------
// run_product_manager (HITL-wrapped, budget 2)
// ---------------------------------------------------------------------------

// RunProductManager scopes a goal into a PRD. Ports pipeline.py:158-237.
func RunProductManager(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	deps.App.Note(ctx, "PM starting", "pm", "start")

	goal := getString(input, "goal", "")
	repoPath := getString(input, "repo_path", "")
	artifactsDir := getString(input, "artifacts_dir", ".artifacts")
	additionalContext := getString(input, "additional_context", "")
	model := orResolvedDefault(getString(input, "model", ""), config.DefaultPlanningModel())
	maxTurns := getInt(input, "max_turns", config.DefaultAgentMaxTurns)
	permissionMode := getString(input, "permission_mode", "")
	aiProvider := orResolvedDefault(getString(input, "ai_provider", ""), config.DefaultRuntime())
	initialPrior := getPriorResponses(input)

	_, paths, err := ensurePaths(repoPath, artifactsDir)
	if err != nil {
		return nil, err
	}

	wsManifest, err := workspaceManifestFrom(getMap(input, "workspace_manifest"))
	if err != nil {
		return nil, err
	}

	systemPrompt, _ := prompts.ProductManagerPrompts(prompts.ProductManagerPromptsOpts{
		Goal:               goal,
		RepoPath:           repoPath,
		PRDPath:            paths["prd"],
		AdditionalContext:  additionalContext,
		PriorUserResponses: initialPrior,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(aiProvider)
	if err != nil {
		return nil, err
	}

	invoke := func(ctx context.Context, kwargs map[string]any) (map[string]any, error) {
		prior := toPriorList(kwargs["prior_user_responses"])
		taskPrompt := prompts.PMTaskPrompt(prompts.PMTaskPromptOpts{
			Goal:               goal,
			RepoPath:           repoPath,
			PRDPath:            paths["prd"],
			AdditionalContext:  additionalContext,
			WorkspaceManifest:  wsManifest,
			PriorUserResponses: prior,
		})
		opts := harnessx.RoleOptions{
			Provider:       provider,
			Model:          model,
			MaxTurns:       maxTurns,
			Tools:          []string{"Read", "Write", "Glob", "Grep", "Bash"},
			PermissionMode: permissionMode,
			SystemPrompt:   systemPrompt,
			Cwd:            repoPath,
		}.ToOptions()
		parsed, res, err := harnessx.Run[schemas.PRD](ctx, deps.Harness, taskPrompt, opts)
		if err != nil {
			return nil, err
		}
		if res == nil || res.Parsed == nil {
			// Parse failure: Python's _invoke_pm returns None. Signal that to
			// the wrapper with a nil map (run_with_ask_user then returns nil).
			return nil, nil
		}
		return toMap(parsed)
	}

	ec := executionContextFrom(ctx)
	result, err := hitl.RunWithAskUser(ctx, invoke,
		map[string]any{"prior_user_responses": initialPrior},
		hitl.RunWithAskUserParams{
			App:         deps.App,
			Pauser:      deps.Pauser,
			Hax:         deps.Hax,
			Budget:      &hitl.AskUserBudget{Remaining: 2},
			WebhookURL:  hitl.ApprovalWebhookURL(deps.AgentFieldServer),
			NodeID:      deps.NodeID,
			ExecutionID: ec.ExecutionID,
			NoteLabel:   "product_manager",
		})
	if err != nil {
		return nil, err
	}
	if result == nil {
		return nil, errors.New("Product manager failed to produce a valid PRD")
	}

	deps.App.Note(ctx, "PM complete", "pm", "complete")
	return result, nil
}

// ---------------------------------------------------------------------------
// run_environment_scout (HITL-wrapped; excludes + stashes scoped_credentials)
// ---------------------------------------------------------------------------

// RunEnvironmentScout negotiates scoped third-party credentials before the
// architect. Ports pipeline.py:240-354. The returned dict EXCLUDES
// scoped_credentials (the control plane logs reasoner returns); the values are
// stashed in the process-local store keyed by the build's run_id instead.
func RunEnvironmentScout(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	deps.App.Note(ctx, "Environment scout starting", "scout", "start")

	prd := getMap(input, "prd")
	repoPath := getString(input, "repo_path", "")
	artifactsDir := getString(input, "artifacts_dir", ".artifacts")
	model := orResolvedDefault(getString(input, "model", ""), config.DefaultPlanningModel())
	maxTurns := getInt(input, "max_turns", config.DefaultAgentMaxTurns)
	permissionMode := getString(input, "permission_mode", "")
	aiProvider := orResolvedDefault(getString(input, "ai_provider", ""), config.DefaultRuntime())
	initialPrior := getPriorResponses(input)

	// Ensure the artifact dirs exist; the scout writes no artifacts of its own.
	if _, _, err := ensurePaths(repoPath, artifactsDir); err != nil {
		return nil, err
	}

	wsManifest, err := workspaceManifestFrom(getMap(input, "workspace_manifest"))
	if err != nil {
		return nil, err
	}

	provider, err := runtimex.RuntimeToHarnessAdapter(aiProvider)
	if err != nil {
		return nil, err
	}

	invoke := func(ctx context.Context, kwargs map[string]any) (map[string]any, error) {
		prior := toPriorList(kwargs["prior_user_responses"])
		taskPrompt := prompts.EnvironmentScoutTaskPrompt(prompts.EnvironmentScoutTaskPromptOpts{
			PRD:                prd,
			RepoPath:           repoPath,
			WorkspaceManifest:  wsManifest,
			PriorUserResponses: prior,
		})
		opts := harnessx.RoleOptions{
			Provider:       provider,
			Model:          model,
			MaxTurns:       maxTurns,
			Tools:          []string{"Read", "Glob", "Grep", "Bash"},
			PermissionMode: permissionMode,
			SystemPrompt:   prompts.EnvironmentScoutSystemPrompt,
			Cwd:            repoPath,
		}.ToOptions()
		parsed, res, err := harnessx.Run[schemas.ScoutResult](ctx, deps.Harness, taskPrompt, opts)
		if err != nil {
			return nil, err
		}
		if res == nil || res.Parsed == nil {
			return nil, nil
		}
		return toMap(parsed)
	}

	ec := executionContextFrom(ctx)
	result, err := hitl.RunWithAskUser(ctx, invoke,
		map[string]any{"prior_user_responses": initialPrior},
		hitl.RunWithAskUserParams{
			App:         deps.App,
			Pauser:      deps.Pauser,
			Hax:         deps.Hax,
			Budget:      &hitl.AskUserBudget{Remaining: 2},
			WebhookURL:  hitl.ApprovalWebhookURL(deps.AgentFieldServer),
			NodeID:      deps.NodeID,
			ExecutionID: ec.ExecutionID,
			NoteLabel:   "environment_scout",
		})
	if err != nil {
		return nil, err
	}

	if result == nil {
		deps.App.Note(ctx,
			"Scout produced no parseable result — proceeding without credentials",
			"scout", "fallback")
		fallback, err := toMap(&schemas.ScoutResult{
			DetectedServices: []schemas.ServiceCredentialSpec{},
			SkippedServices:  []string{},
			Summary:          "Scout produced no parseable result; proceeding without credentials.",
		})
		if err != nil {
			return nil, err
		}
		delete(fallback, "scoped_credentials")
		return fallback, nil
	}

	// Stash credentials in the process-local store under the build's run_id
	// (shared across every reasoner in this build). This MUST happen before we
	// strip them from the return value — otherwise the build() caller has no way
	// to retrieve them.
	scopeID := ec.RunID
	if scopeID == "" {
		scopeID = ec.RootWorkflowID
	}
	creds := scopedCredentialsFrom(result["scoped_credentials"])
	if scopeID != "" && len(creds) > 0 {
		hitl.StoreScopedCredentials(scopeID, creds)
	}

	credsCount := len(creds)
	skippedCount := len(toStringSlice(result["skipped_services"]))
	deps.App.Note(ctx, fmt.Sprintf(
		"Scout complete: %d credential(s) negotiated, %d skipped", credsCount, skippedCount),
		"scout", "complete")

	// SAFETY: scoped_credentials is EXCLUDED from the returned dict. Downstream
	// reasoners retrieve the values from the process-local store using the same
	// scope_id (execution-context run_id).
	delete(result, "scoped_credentials")
	return result, nil
}

// ---------------------------------------------------------------------------
// run_architect (feedback param for revision loops)
// ---------------------------------------------------------------------------

// RunArchitect produces a technical architecture from the PRD. Ports
// pipeline.py:357-414.
func RunArchitect(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	deps.App.Note(ctx, "Architect starting", "architect", "start")

	repoPath := getString(input, "repo_path", "")
	artifactsDir := getString(input, "artifacts_dir", ".artifacts")
	feedback := getString(input, "feedback", "")
	model := orResolvedDefault(getString(input, "model", ""), config.DefaultPlanningModel())
	maxTurns := getInt(input, "max_turns", config.DefaultAgentMaxTurns)
	permissionMode := getString(input, "permission_mode", "")
	aiProvider := orResolvedDefault(getString(input, "ai_provider", ""), config.DefaultRuntime())

	_, paths, err := ensurePaths(repoPath, artifactsDir)
	if err != nil {
		return nil, err
	}

	prdObj, err := prdFrom(getMap(input, "prd"))
	if err != nil {
		return nil, err
	}
	wsManifest, err := workspaceManifestFrom(getMap(input, "workspace_manifest"))
	if err != nil {
		return nil, err
	}

	systemPrompt, _ := prompts.ArchitectPrompts(prompts.ArchitectPromptsOpts{
		PRD:              prdObj,
		RepoPath:         repoPath,
		PRDPath:          paths["prd"],
		ArchitecturePath: paths["architecture"],
		Feedback:         feedback,
	})
	taskPrompt := prompts.ArchitectTaskPrompt(prompts.ArchitectTaskPromptOpts{
		PRD:               prdObj,
		RepoPath:          repoPath,
		PRDPath:           paths["prd"],
		ArchitecturePath:  paths["architecture"],
		Feedback:          feedback,
		WorkspaceManifest: wsManifest,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(aiProvider)
	if err != nil {
		return nil, err
	}
	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          model,
		MaxTurns:       maxTurns,
		Tools:          []string{"Read", "Write", "Glob", "Grep", "Bash"},
		PermissionMode: permissionMode,
		SystemPrompt:   systemPrompt,
		Cwd:            repoPath,
	}.ToOptions()
	parsed, res, err := harnessx.Run[schemas.Architecture](ctx, deps.Harness, taskPrompt, opts)
	if err != nil {
		return nil, err
	}
	if res == nil || res.Parsed == nil {
		return nil, errors.New("Architect failed to produce a valid architecture")
	}

	deps.App.Note(ctx, "Architect complete", "architect", "complete")
	return toMap(parsed)
}

// ---------------------------------------------------------------------------
// run_tech_lead (writes plan/review.json)
// ---------------------------------------------------------------------------

// RunTechLead reviews the architecture against the PRD. Ports pipeline.py:417-474.
// The review is persisted to <base>/plan/review.json (indent=2) before return.
func RunTechLead(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	deps.App.Note(ctx, "Tech Lead starting", "tech_lead", "start")

	repoPath := getString(input, "repo_path", "")
	artifactsDir := getString(input, "artifacts_dir", ".artifacts")
	revisionNumber := getInt(input, "revision_number", 0)
	model := orResolvedDefault(getString(input, "model", ""), config.DefaultPlanningModel())
	maxTurns := getInt(input, "max_turns", config.DefaultAgentMaxTurns)
	permissionMode := getString(input, "permission_mode", "")
	aiProvider := orResolvedDefault(getString(input, "ai_provider", ""), config.DefaultRuntime())

	base, paths, err := ensurePaths(repoPath, artifactsDir)
	if err != nil {
		return nil, err
	}

	wsManifest, err := workspaceManifestFrom(getMap(input, "workspace_manifest"))
	if err != nil {
		return nil, err
	}

	systemPrompt, _ := prompts.TechLeadPrompts(prompts.TechLeadPromptsOpts{
		PRDPath:          paths["prd"],
		ArchitecturePath: paths["architecture"],
		RevisionNumber:   revisionNumber,
	})
	taskPrompt := prompts.TechLeadTaskPrompt(prompts.TechLeadTaskPromptOpts{
		PRDPath:           paths["prd"],
		ArchitecturePath:  paths["architecture"],
		RevisionNumber:    revisionNumber,
		WorkspaceManifest: wsManifest,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(aiProvider)
	if err != nil {
		return nil, err
	}
	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          model,
		MaxTurns:       maxTurns,
		Tools:          []string{"Read", "Write", "Glob", "Grep"},
		PermissionMode: permissionMode,
		SystemPrompt:   systemPrompt,
		Cwd:            repoPath,
	}.ToOptions()
	parsed, res, err := harnessx.Run[schemas.ReviewResult](ctx, deps.Harness, taskPrompt, opts)
	if err != nil {
		return nil, err
	}
	if res == nil || res.Parsed == nil {
		return nil, errors.New("Tech lead failed to produce a valid review")
	}

	review, err := toMap(parsed)
	if err != nil {
		return nil, err
	}
	reviewJSONPath := filepath.Join(base, "plan", "review.json")
	blob, err := json.MarshalIndent(review, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(reviewJSONPath, blob, 0o644); err != nil {
		return nil, err
	}

	deps.App.Note(ctx, "Tech Lead complete", "tech_lead", "complete")
	return review, nil
}

// ---------------------------------------------------------------------------
// run_sprint_planner (inline SprintPlanOutput{issues, rationale})
// ---------------------------------------------------------------------------

// sprintPlanOutput is the inline schema Python declares inside
// run_sprint_planner (issues + rationale).
type sprintPlanOutput struct {
	Issues    []schemas.PlannedIssue `json:"issues"`
	Rationale string                 `json:"rationale"`
}

// defaultPlanningSchemaRetries bounds the extra harness calls the sprint
// planner makes when its structured output does not parse/validate. Small on
// purpose: each attempt is a full planner run. Mirrors Python's
// DEFAULT_PLANNING_SCHEMA_RETRIES (swe_af/reasoners/pipeline.py, #146).
const defaultPlanningSchemaRetries = 2

// schemaRetryContext feeds a failed attempt's parse/validation error back into
// the retry prompt. Mirrors pipeline._schema_retry_context.
const schemaRetryContext = "## Retry Context\n" +
	"Your previous response could not be parsed into the required " +
	"structured output. The validation error was:\n\n%s\n\n" +
	"Produce the complete structured output again, correcting that error. " +
	"Include every required field."

// isEmptyCompletion reports whether a schema-bound harness call returned with
// neither a parsed object nor any raw text. Python's
// check_empty_harness_completion classifies that shape as a provider/model
// mismatch rather than a schema-quality failure, so it must fail fast instead
// of burning the retry bound. A terminal failure_type=schema is exempt: the
// agent did produce output that failed validation, the raw text just was not
// surfaced on the result.
func isEmptyCompletion(result *harness.Result) bool {
	if result == nil {
		return true
	}
	if result.Parsed != nil {
		return false
	}
	if strings.TrimSpace(result.Result) != "" {
		return false
	}
	return result.FailureType != harness.FailureSchema
}

// describeSchemaFailure names the parser error or the failing fields for a
// schema-bound call that produced no parsed result. Re-decoding the raw text
// into dst makes the decode error name the offending field; the harness's own
// ErrorMessage is appended when present. Mirrors
// pipeline._describe_schema_failure.
func describeSchemaFailure(result *harness.Result, dst any) string {
	raw := ""
	detail := ""
	if result != nil {
		raw = result.Result
		detail = strings.TrimSpace(result.ErrorMessage)
	}
	if strings.TrimSpace(raw) == "" {
		if detail != "" {
			return detail
		}
		return "the harness returned no parsed result and no error detail"
	}
	failure := "the harness returned no parsed result"
	if err := json.Unmarshal([]byte(raw), dst); err != nil {
		failure = fmt.Sprintf("raw response failed schema validation (%v)", err)
	}
	if detail != "" {
		return fmt.Sprintf("%s; harness reported: %s", failure, detail)
	}
	return failure
}

// persistRawResponse writes a failed attempt's raw completion next to the
// stage's artifacts. Mirrors pipeline._persist_raw_response.
func persistRawResponse(path string, result *harness.Result, attempt int, failure string) error {
	raw := ""
	if result != nil {
		raw = result.Result
	}
	body := strings.TrimSpace(raw)
	if body == "" {
		body = "(the harness returned no raw completion text)"
	}
	content := fmt.Sprintf(
		"# attempt %d: unparseable structured output\n# error: %s\n# raw completion text follows\n%s\n",
		attempt, failure, body,
	)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(content), 0o644)
}

// RunSprintPlanner decomposes the work into executable issues. Ports
// pipeline.py:477-549. Returns {"issues": [...], "rationale": "..."}. The pure
// level/conflict/sequence helpers (_compute_levels, _validate_file_conflicts,
// _assign_sequence_numbers) are applied by the plan orchestrator, NOT here — the
// reasoner only surfaces the raw issues + rationale.
//
// input["max_schema_retries"] bounds how many extra harness calls are made
// when the planner responds with output that does not parse/validate; each
// retry feeds the validation error back into the task prompt and the raw
// response is written to plan/sprint_planner_raw_response.txt.
func RunSprintPlanner(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	deps.App.Note(ctx, "Sprint Planner starting", "sprint_planner", "start")

	repoPath := getString(input, "repo_path", "")
	artifactsDir := getString(input, "artifacts_dir", ".artifacts")
	model := orResolvedDefault(getString(input, "model", ""), config.DefaultPlanningModel())
	maxTurns := getInt(input, "max_turns", config.DefaultAgentMaxTurns)
	permissionMode := getString(input, "permission_mode", "")
	aiProvider := orResolvedDefault(getString(input, "ai_provider", ""), config.DefaultRuntime())

	base, paths, err := ensurePaths(repoPath, artifactsDir)
	if err != nil {
		return nil, err
	}

	prdObj, err := prdFrom(getMap(input, "prd"))
	if err != nil {
		return nil, err
	}
	archObj, err := architectureFrom(getMap(input, "architecture"))
	if err != nil {
		return nil, err
	}
	wsManifest, err := workspaceManifestFrom(getMap(input, "workspace_manifest"))
	if err != nil {
		return nil, err
	}

	systemPrompt, _ := prompts.SprintPlannerPrompts(prompts.SprintPlannerPromptsOpts{
		PRD:              prdObj,
		Architecture:     archObj,
		RepoPath:         repoPath,
		PRDPath:          paths["prd"],
		ArchitecturePath: paths["architecture"],
	})

	prdMap, err := toMap(&prdObj)
	if err != nil {
		return nil, err
	}
	archMap, err := toMap(&archObj)
	if err != nil {
		return nil, err
	}
	taskPrompt := prompts.SprintPlannerTaskPrompt(prompts.SprintPlannerTaskPromptOpts{
		Goal:              prdObj.ValidatedDescription,
		PRD:               prdMap,
		Architecture:      archMap,
		WorkspaceManifest: wsManifest,
		RepoPath:          repoPath,
		PRDPath:           paths["prd"],
		ArchitecturePath:  paths["architecture"],
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(aiProvider)
	if err != nil {
		return nil, err
	}
	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          model,
		MaxTurns:       maxTurns,
		Tools:          []string{"Read", "Write", "Glob", "Grep"},
		PermissionMode: permissionMode,
		SystemPrompt:   systemPrompt,
		Cwd:            repoPath,
	}.ToOptions()
	maxSchemaRetries := getInt(input, "max_schema_retries", defaultPlanningSchemaRetries)
	if maxSchemaRetries < 0 {
		maxSchemaRetries = 0
	}
	attempts := maxSchemaRetries + 1
	rawResponsePath := filepath.Join(base, "plan", "sprint_planner_raw_response.txt")

	var parsed *sprintPlanOutput
	succeeded := false
	lastFailure := ""
	for attempt := 1; attempt <= attempts; attempt++ {
		prompt := taskPrompt
		if lastFailure != "" {
			prompt += "\n\n" + fmt.Sprintf(schemaRetryContext, lastFailure)
		}
		var res *harness.Result
		var runErr error
		parsed, res, runErr = harnessx.Run[sprintPlanOutput](ctx, deps.Harness, prompt, opts)
		if runErr != nil {
			return nil, runErr
		}
		if res != nil && res.Parsed != nil {
			succeeded = true
			break
		}
		if isEmptyCompletion(res) {
			return nil, fmt.Errorf(
				"Sprint planner harness returned an empty completion "+
					"(provider=%s, model=%s) — check provider auth/model compatibility",
				provider, model,
			)
		}
		lastFailure = describeSchemaFailure(res, &sprintPlanOutput{})
		if err := persistRawResponse(rawResponsePath, res, attempt, lastFailure); err != nil {
			return nil, err
		}
		if attempt < attempts {
			deps.App.Note(ctx, fmt.Sprintf(
				"Sprint planner structured output invalid on attempt %d/%d — retrying with the validation error",
				attempt, attempts), "planning", "schema_retry")
		}
	}
	if !succeeded {
		return nil, fmt.Errorf(
			"Sprint planner failed to produce valid issues after %d attempt(s) "+
				"(provider=%s, model=%s; raw response: %s) — %s",
			attempts, provider, model, rawResponsePath, lastFailure,
		)
	}
	issues := make([]any, 0, len(parsed.Issues))
	for i := range parsed.Issues {
		m, err := toMap(&parsed.Issues[i])
		if err != nil {
			return nil, err
		}
		issues = append(issues, m)
	}

	deps.App.Note(ctx, "Sprint Planner complete", "sprint_planner", "complete")
	return map[string]any{
		"issues":    issues,
		"rationale": parsed.Rationale,
	}, nil
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// ensurePaths mirrors pipeline._ensure_paths: base = abspath(repo_path)/artifacts_dir,
// creating logs/plan/issues under it. Delegates to dagutil.EnsurePaths (the
// shared verbatim port).
func ensurePaths(repoPath, artifactsDir string) (string, map[string]string, error) {
	abs, err := filepath.Abs(repoPath)
	if err != nil {
		return "", nil, err
	}
	base := filepath.Join(abs, artifactsDir)
	paths, err := dagutil.EnsurePaths(base)
	if err != nil {
		return "", nil, err
	}
	return base, paths, nil
}

// toMap serializes a value to a model_dump()-equivalent map[string]any via a
// JSON round-trip (no omitempty on the schema structs → every key present).
func toMap(v any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// prdFrom materializes a schemas.PRD from an input dict (PRD(**prd) analogue).
func prdFrom(m map[string]any) (schemas.PRD, error) {
	var prd schemas.PRD
	if m == nil {
		return prd, nil
	}
	if err := remarshal(m, &prd); err != nil {
		return prd, err
	}
	return prd, nil
}

// architectureFrom materializes a schemas.Architecture from an input dict.
func architectureFrom(m map[string]any) (schemas.Architecture, error) {
	var arch schemas.Architecture
	if m == nil {
		return arch, nil
	}
	if err := remarshal(m, &arch); err != nil {
		return arch, err
	}
	return arch, nil
}

// workspaceManifestFrom materializes a *schemas.WorkspaceManifest, or nil when
// no manifest was supplied (WorkspaceManifest(**m) if m else None).
func workspaceManifestFrom(m map[string]any) (*schemas.WorkspaceManifest, error) {
	if len(m) == 0 {
		return nil, nil
	}
	var ws schemas.WorkspaceManifest
	if err := remarshal(m, &ws); err != nil {
		return nil, err
	}
	return &ws, nil
}

// remarshal round-trips a map through JSON into a typed destination, applying
// any UnmarshalJSON default-seeding on the destination type.
func remarshal(m map[string]any, dest any) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, dest)
}

// getString returns input[key] when present and a string, else def — matching
// Python kwargs-default semantics (the default applies only when the key is
// absent, not when it is present-but-empty).
func getString(input map[string]any, key, def string) string {
	if v, ok := input[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return def
}

func orResolvedDefault(value, def string) string {
	if value == "" {
		return def
	}
	return value
}

// getInt returns input[key] as an int when present, else def. Tolerates the
// float64 that JSON numbers decode to.
func getInt(input map[string]any, key string, def int) int {
	v, ok := input[key]
	if !ok {
		return def
	}
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	case json.Number:
		if i, err := n.Int64(); err == nil {
			return int(i)
		}
	}
	return def
}

// getMap returns input[key] as a map[string]any, or nil when absent/mistyped.
func getMap(input map[string]any, key string) map[string]any {
	if v, ok := input[key]; ok {
		if m, ok := v.(map[string]any); ok {
			return m
		}
	}
	return nil
}

// getPriorResponses reads prior_user_responses off the handler input as a
// []map[string]any (Python: list(prior_user_responses or [])).
func getPriorResponses(input map[string]any) []map[string]any {
	return toPriorList(input["prior_user_responses"])
}

// toPriorList normalizes the several shapes prior_user_responses can arrive as
// ([]map[string]any, []any of maps, or nil) into []map[string]any.
func toPriorList(v any) []map[string]any {
	switch list := v.(type) {
	case []map[string]any:
		return list
	case []any:
		out := make([]map[string]any, 0, len(list))
		for _, item := range list {
			if m, ok := item.(map[string]any); ok {
				out = append(out, m)
			}
		}
		return out
	default:
		return []map[string]any{}
	}
}

// scopedCredentialsFrom projects a scoped_credentials value (map[string]any with
// string values) into a string→string credential map.
func scopedCredentialsFrom(v any) map[string]string {
	out := map[string]string{}
	m, ok := v.(map[string]any)
	if !ok {
		return out
	}
	for k, val := range m {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}

// toStringSlice projects a JSON array value into a []string, dropping non-strings.
func toStringSlice(v any) []string {
	switch list := v.(type) {
	case []string:
		return list
	case []any:
		out := make([]string, 0, len(list))
		for _, item := range list {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

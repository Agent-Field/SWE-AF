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
	"sort"
	"strings"
	"time"

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

	base, paths, err := ensurePaths(repoPath, artifactsDir)
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
		parsed, err := runSchemaBoundRole[schemas.PRD](
			ctx, deps, opts, taskPrompt,
			filepath.Join(base, "plan", "product_manager_raw_response.txt"),
			"PM",
			"Product manager failed to produce a valid PRD",
			provider, model,
			planningRoleSchemaRetries,
		)
		if err != nil {
			return nil, err
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

	base, paths, err := ensurePaths(repoPath, artifactsDir)
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
	parsed, err := runSchemaBoundRole[schemas.Architecture](
		ctx, deps, opts, taskPrompt,
		filepath.Join(base, "plan", "architect_raw_response.txt"),
		"Architect",
		"Architect failed to produce a valid architecture",
		provider, model,
		planningRoleSchemaRetries,
	)
	if err != nil {
		return nil, err
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
	parsed, err := runSchemaBoundRole[schemas.ReviewResult](
		ctx, deps, opts, taskPrompt,
		filepath.Join(base, "plan", "tech_lead_raw_response.txt"),
		"Tech lead",
		"Tech lead failed to produce a valid review",
		provider, model,
		planningRoleSchemaRetries,
	)
	if err != nil {
		return nil, err
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

// planningRoleSchemaRetries is the number of extra *outer* schema-bound harness
// calls the product manager, architect and tech lead get when their structured
// output does not parse/validate. These count harness() calls, not model runs:
// the SDK retries schema failures inside one call (DEFAULT_SCHEMA_RETRIES = 2),
// so one outer attempt can be up to three subprocess runs. Kept small on
// purpose: each attempt is a full stage run over the PRD and architecture.
const planningRoleSchemaRetries = 1

// sprintPlannerSchemaRetries is the outer-call bound for the sprint planner.
// Its response is a large issue set that feeds every downstream issue, and one
// extra outer attempt than the other stages is enough; each outer attempt is
// itself up to three subprocess runs via the SDK's in-call schema retries, so
// the subprocess ceiling is three times these constants. Mirrors Python's
// PLANNING_ROLE_SCHEMA_RETRIES / SPRINT_PLANNER_SCHEMA_RETRIES
// (swe_af/reasoners/pipeline.py, #146). These are internal constants rather
// than handler inputs because nothing passes a different value.
const sprintPlannerSchemaRetries = 2

// maxRawResponseChars caps one failed attempt's raw text in the retry log. The
// first and last halves are kept — output-limit truncation shows at the tail,
// malformed-JSON evidence usually at the head — and the middle is elided, so
// the default three attempts cannot grow the log without bound.
const maxRawResponseChars = 200_000

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
// ErrorMessage is appended when present. encoding/json only reports decode and
// type errors — it does not enforce required fields — so for a structurally
// valid response that is missing a field this leans on the harness's own
// message. Mirrors pipeline._describe_schema_failure.
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

// truncateRawResponse keeps the head and tail of an oversized raw response.
func truncateRawResponse(raw string) string {
	if len(raw) <= maxRawResponseChars {
		return raw
	}
	half := maxRawResponseChars / 2
	omitted := len(raw) - maxRawResponseChars
	return raw[:half] + fmt.Sprintf(
		"\n\n# ... %d characters omitted (full response was %d chars) ...\n\n",
		omitted, len(raw),
	) + raw[len(raw)-half:]
}

// appendArtifact appends text to a run artifact, creating its parent directory.
func appendArtifact(path, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	if _, err := f.WriteString(text); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}

// redactScopedCredentials replaces any negotiated credential value with a
// marker so a response that echoes one cannot land in the archived retry log.
// Values are replaced longest-first so a shorter value cannot split a longer
// one. Mirrors pipeline._redact_scoped_credentials.
func redactScopedCredentials(runID, text string) string {
	if text == "" || runID == "" {
		return text
	}
	creds := hitl.GetScopedCredentials(runID)
	if len(creds) == 0 {
		return text
	}
	names := make([]string, 0, len(creds))
	for name := range creds {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool {
		return len(creds[names[i]]) > len(creds[names[j]])
	})
	for _, name := range names {
		if value := creds[name]; value != "" {
			text = strings.ReplaceAll(text, value, "[REDACTED:"+name+"]")
		}
	}
	return text
}

// persistRawResponse appends one failed attempt's raw completion to the
// stage's retry log. Mirrors pipeline._persist_raw_response.
func persistRawResponse(path, runID string, result *harness.Result, attempt, attempts int, failure string) error {
	raw := ""
	if result != nil {
		raw = result.Result
	}
	redacted := redactScopedCredentials(runID, raw)
	body := strings.TrimSpace(redacted)
	if body != "" {
		body = truncateRawResponse(redacted)
	} else {
		body = "(the harness returned no raw completion text)"
	}
	block := fmt.Sprintf(
		"===== attempt %d/%d failed: %s =====\n# raw completion text follows\n%s",
		attempt, attempts, redactScopedCredentials(runID, failure), body,
	)
	return appendArtifact(path, redactScopedCredentials(runID, block))
}

// recordRetryOutcome appends the terminal retry outcome to the stage's retry
// log.
func recordRetryOutcome(path, runID, outcome string) error {
	return appendArtifact(path, redactScopedCredentials(runID,
		"===== outcome: "+outcome+" ====="))
}

// recordOutcomeBestEffort records a terminal retry outcome without ever failing
// the stage.
func recordOutcomeBestEffort(ctx context.Context, deps *Deps, stage, path, runID, outcome string) {
	if err := recordRetryOutcome(path, runID, outcome); err != nil {
		deps.App.Note(ctx, fmt.Sprintf(
			"%s could not write the retry outcome to %s: %v",
			stage, path, err), "planning", "schema_retry", "artifact_error")
	}
}

// recordRunHeaderBestEffort starts a self-describing section for this
// invocation of the retry log. The log is append-only and keyed by repo path,
// so a second build against the same path appends after the first; the header
// makes each build's section identifiable without a reader having to guess
// which outcome is current.
func recordRunHeaderBestEffort(ctx context.Context, deps *Deps, stage, path, runID string) {
	label := runID
	if label == "" {
		label = "unknown-run"
	}
	if err := appendArtifact(path, fmt.Sprintf(
		"===== run %s | %s | started %s =====",
		label, stage, time.Now().UTC().Format(time.RFC3339))); err != nil {
		deps.App.Note(ctx, fmt.Sprintf(
			"%s could not write the retry-log header to %s: %v",
			stage, path, err), "planning", "schema_retry", "artifact_error")
	}
}

// runSchemaBoundRole drives the bounded schema-retry loop shared by the four
// planning stages (PM, architect, tech lead, sprint planner). It re-issues the
// harness call with the validation error fed back into the prompt when the
// output does not parse/validate. Each invocation starts a self-describing run
// section and always ends it with a terminal outcome line, including when a
// fatal API error or an empty completion ends the loop early, so the log never
// trails off mid-sequence. Run-scoped credential values are redacted before
// anything reaches the log. maxSchemaRetries is set per call site from
// planningRoleSchemaRetries / sprintPlannerSchemaRetries.
func runSchemaBoundRole[T any](
	ctx context.Context,
	deps *Deps,
	opts harness.Options,
	taskPrompt string,
	rawResponsePath string,
	stage string,
	failureLabel string,
	provider string,
	model string,
	maxSchemaRetries int,
) (*T, error) {
	attempts := maxSchemaRetries + 1
	if attempts < 1 {
		attempts = 1
	}
	runID := executionContextFrom(ctx).RunID
	lastFailure := ""
	persistenceError := ""
	recordRunHeaderBestEffort(ctx, deps, stage, rawResponsePath, runID)
	for attempt := 1; attempt <= attempts; attempt++ {
		prompt := taskPrompt
		if lastFailure != "" {
			prompt += "\n\n" + fmt.Sprintf(schemaRetryContext, lastFailure)
		}
		parsed, res, err := harnessx.Run[T](ctx, deps.Harness, prompt, opts)
		if err != nil {
			recordOutcomeBestEffort(ctx, deps, stage, rawResponsePath, runID,
				fmt.Sprintf("FAILED after attempt %d/%d: %v", attempt, attempts, err))
			return nil, err
		}
		if res != nil && res.Parsed != nil {
			recordOutcomeBestEffort(ctx, deps, stage, rawResponsePath, runID,
				fmt.Sprintf("succeeded on attempt %d/%d", attempt, attempts))
			return parsed, nil
		}
		if isEmptyCompletion(res) {
			reason := fmt.Sprintf("empty completion (provider=%s, model=%s)", provider, model)
			recordOutcomeBestEffort(ctx, deps, stage, rawResponsePath, runID,
				fmt.Sprintf("FAILED after attempt %d/%d: %s", attempt, attempts, reason))
			return nil, fmt.Errorf(
				"%s harness returned an empty completion "+
					"(provider=%s, model=%s) — check provider auth/model compatibility",
				stage, provider, model,
			)
		}
		var zero T
		lastFailure = describeSchemaFailure(res, &zero)
		if err := persistRawResponse(rawResponsePath, runID, res, attempt, attempts, lastFailure); err != nil {
			persistenceError = err.Error()
			deps.App.Note(ctx, fmt.Sprintf(
				"%s could not write the raw response to %s: %v",
				stage, rawResponsePath, err), "planning", "schema_retry", "artifact_error")
		}
		if attempt < attempts {
			deps.App.Note(ctx, fmt.Sprintf(
				"%s structured output invalid on attempt %d/%d — retrying with the validation error",
				stage, attempt, attempts), "planning", "schema_retry")
		}
	}
	recordOutcomeBestEffort(ctx, deps, stage, rawResponsePath, runID,
		fmt.Sprintf("FAILED after %d attempt(s): %s", attempts, lastFailure))
	persistenceDetail := ""
	if persistenceError != "" {
		persistenceDetail = "; raw response could not be written: " + persistenceError
	}
	return nil, fmt.Errorf(
		"%s after %d attempt(s) (provider=%s, model=%s; raw response: %s) — %s%s",
		failureLabel, attempts, provider, model, rawResponsePath,
		redactScopedCredentials(runID, lastFailure), persistenceDetail,
	)
}

// RunSprintPlanner decomposes the work into executable issues. Ports
// pipeline.py:477-549. Returns {"issues": [...], "rationale": "..."}. The pure
// level/conflict/sequence helpers (_compute_levels, _validate_file_conflicts,
// _assign_sequence_numbers) are applied by the plan orchestrator, NOT here — the
// reasoner only surfaces the raw issues + rationale.
//
// A response that does not parse/validate is retried up to
// sprintPlannerSchemaRetries times, each retry feeding the validation error
// back into the task prompt. Every failed attempt and the terminal outcome are
// appended to plan/sprint_planner_raw_response.txt.
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
	parsed, err := runSchemaBoundRole[sprintPlanOutput](
		ctx, deps, opts, taskPrompt,
		filepath.Join(base, "plan", "sprint_planner_raw_response.txt"),
		"Sprint planner",
		"Sprint planner failed to produce valid issues",
		provider, model,
		sprintPlannerSchemaRetries,
	)
	if err != nil {
		return nil, err
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

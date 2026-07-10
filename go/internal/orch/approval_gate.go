// This file (approval_gate.go) ports the hax plan-approval gate from
// swe_af/app.py: the _format_plan_for_approval formatter (:355-402), the
// _create_hax_request_with_timeout wrapper (:411-471), and the inline
// plan-approval revision loop build() runs when HAX_API_KEY is set (:754-964).
//
// build.go drives the gate through the ApprovalGate seam (common.go): it calls
// PlanApprovalGate once, acting on the returned ApprovalOutcome. The gate owns
// the entire hax flow — request the plan review, pause the execution
// (agent.Pause, webhook-resumed, design §4.6), and on request_changes re-run
// Architect → Tech Lead → Sprint Planner with the reviewer feedback, bounded by
// cfg.max_plan_revision_iterations.
package orch

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// PlanApprovalGate satisfies the ApprovalGate seam (common.go). build.go assigns
// it to Deps.ApprovalGate; it engages only when a hax client can be built from
// the environment (HAX_API_KEY set) AND a pauser is wired.
var _ ApprovalGate = PlanApprovalGate

// ---------------------------------------------------------------------------
// Seams (overridable in tests; wired by node/register in production).
// ---------------------------------------------------------------------------

// haxClientProvider builds the hax REST client. Returns nil when HAX_API_KEY is
// unset (HITL disabled). Package seam so tests point it at a stub server.
var haxClientProvider = hitl.BuildHaxClientFromEnv

// pauserProvider yields the pause surface the gate drives (agent.Pause — the
// direct port of app.pause, design §4.6; webhook-resumed, no polling). Nil until
// node/register wires it to the *agent.Agent; when nil (or it returns nil) the
// gate no-ops and the build proceeds unreviewed, matching the Python block being
// skipped when the pause substrate is unavailable.
var pauserProvider func(req ApprovalRequest) hitl.Pauser

// SetPauserProvider wires the pause surface factory. The node-wiring wave calls
// this once with a closure returning the *agent.Agent.
func SetPauserProvider(f func(req ApprovalRequest) hitl.Pauser) {
	pauserProvider = f
}

// PlanApprovalGate runs the hax plan-approval revision loop. Ports
// app.py:754-964. Returns a terminal ApprovalOutcome (rejected / expired /
// error / revision-limit reached) carrying the BuildResult build must return, or
// a non-terminal outcome carrying the (possibly revised) plan to execute.
func PlanApprovalGate(ctx context.Context, req ApprovalRequest) (ApprovalOutcome, error) {
	hax := haxClientProvider()
	if hax == nil || pauserProvider == nil {
		// HITL disabled (no HAX_API_KEY) or no pauser wired — skip approval and
		// proceed with the plan unchanged.
		return ApprovalOutcome{Terminal: false, PlanResult: req.PlanResult}, nil
	}
	pauser := pauserProvider(req)
	if pauser == nil {
		return ApprovalOutcome{Terminal: false, PlanResult: req.PlanResult}, nil
	}

	deps := req.Deps
	cfg := req.Cfg
	planResult := req.PlanResult

	cpBase := strings.TrimRight(strings.TrimSpace(deps.AgentFieldServer), "/")
	if cpBase == "" {
		cpBase = "http://localhost:8080"
	}
	webhookURL := cpBase + "/api/v1/webhooks/approval-response"

	// Best-effort approval_state.json trail (app.py:770-771, :825-848).
	approvalStatePath := filepath.Join(req.AbsArtifactsDir, "approval_state.json")
	_ = os.MkdirAll(filepath.Dir(approvalStatePath), 0o755)

	revisionHistory := []map[string]any{}
	maxRev := cfg.MaxPlanRevisionIterations
	expiresHours := cfg.ApprovalExpiresInHours
	userID := strings.TrimSpace(os.Getenv("AGENTFIELD_APPROVAL_USER_ID"))
	nodeID := deps.NodeID
	if nodeID == "" {
		nodeID = "swe-planner-go"
	}

	for revisionIter := 0; revisionIter <= maxRev; revisionIter++ {
		deps.Note(ctx, fmt.Sprintf("Phase 1.5: Requesting plan approval (iteration %d)", revisionIter),
			"build", "approval")

		planSummary, prdMd, archMd, issuesForTemplate := formatPlanForApproval(planResult)

		title := "SWE-AF Plan Review"
		if revisionIter > 0 {
			title = fmt.Sprintf("SWE-AF Plan Review (Revision %d)", revisionIter)
		}

		haxPayload := map[string]any{
			"planSummary":  planSummary,
			"issues":       issuesForTemplate,
			"architecture": archMd,
			"prd":          prdMd,
			"metadata": map[string]any{
				"repoUrl":         cfg.RepoURL,
				"goalDescription": req.Goal,
				"agentNodeId":     nodeID,
				"executionId":     req.ExecutionID,
			},
			"revisionNumber":  revisionIter,
			"revisionHistory": revisionHistory,
		}

		descr := "Review the proposed implementation plan before execution begins"
		created, err := createHaxRequestWithTimeout(ctx, deps, hax, hitl.CreateRequestParams{
			Type:             "plan-review-v2",
			Title:            title,
			Description:      &descr,
			Payload:          haxPayload,
			WebhookURL:       webhookURL,
			ExpiresInSeconds: expiresHours * 3600,
			UserID:           userID,
		}, revisionIter)
		if err != nil {
			return ApprovalOutcome{}, err
		}

		writeApprovalState(approvalStatePath, map[string]any{
			"decision":        "pending",
			"feedback":        "",
			"request_id":      created.ID,
			"request_url":     created.URL,
			"revision_number": revisionIter,
		})

		decision, feedback, err := pauseForApproval(ctx, pauser, req.ExecutionID, created, expiresHours)
		if err != nil {
			return ApprovalOutcome{}, err
		}

		writeApprovalState(approvalStatePath, map[string]any{
			"decision":         decision,
			"feedback":         feedback,
			"request_id":       created.ID,
			"request_url":      created.URL,
			"revision_number":  revisionIter,
			"revision_history": revisionHistory,
		})

		if decision == "approved" {
			deps.Note(ctx, "Plan approved — proceeding to execution",
				"build", "approval", "approved")
			return ApprovalOutcome{Terminal: false, PlanResult: planResult}, nil
		}

		if decision == "request_changes" {
			if revisionIter >= maxRev {
				deps.Note(ctx, fmt.Sprintf("Max plan revision iterations (%d) reached", maxRev),
					"build", "approval", "exhausted")
				return terminalOutcome(planResult,
					fmt.Sprintf("Plan revision limit reached after %d iterations", revisionIter+1)), nil
			}

			revisionHistory = append(revisionHistory, map[string]any{
				"iteration": revisionIter,
				"feedback":  feedback,
			})

			deps.Note(ctx, fmt.Sprintf("Changes requested (iteration %d): %s",
				revisionIter, runeTruncate(feedback, 200)),
				"build", "approval", "request_changes")

			revised, err := replanWithFeedback(ctx, req, planResult, feedback)
			if err != nil {
				return ApprovalOutcome{}, err
			}
			planResult = revised
			continue
		}

		// Terminal: rejected, expired, or error.
		reason := feedback
		if reason == "" {
			reason = decision
		}
		deps.Note(ctx, fmt.Sprintf("Plan %s by human reviewer: %s", decision, reason),
			"build", "approval", decision)
		return terminalOutcome(planResult, fmt.Sprintf("Plan %s: %s", decision, reason)), nil
	}

	// Unreachable in practice: every loop iteration either approves (return),
	// terminates (return), or requests changes (continue, bounded by maxRev).
	// Proceed with the latest plan as a defensive fallthrough.
	return ApprovalOutcome{Terminal: false, PlanResult: planResult}, nil
}

// createHaxRequestWithTimeout submits the hax approval request, emitting the
// three timeline notes (entry / success / error) from
// _create_hax_request_with_timeout (app.py:411-471). The hard 120s timeout that
// bounds a wedged hax-sdk lives inside HaxClient.CreateRequest, so this wrapper
// only adds observability + error routing.
func createHaxRequestWithTimeout(
	ctx context.Context,
	deps *Deps,
	hax *hitl.HaxClient,
	params hitl.CreateRequestParams,
	revisionIter int,
) (*hitl.CreatedRequest, error) {
	deps.Note(ctx, fmt.Sprintf("Phase 1.5: Submitting hax create_request (iteration %d)", revisionIter),
		"build", "approval", "hax", "create_request")

	created, err := hax.CreateRequest(ctx, params)
	if err != nil {
		if strings.Contains(err.Error(), "timed out") {
			deps.Note(ctx, fmt.Sprintf("hax_client.create_request timed out (iteration %d)", revisionIter),
				"build", "approval", "hax", "timeout")
		} else {
			deps.Note(ctx, fmt.Sprintf("hax_client.create_request raised %v (iteration %d)", err, revisionIter),
				"build", "approval", "hax", "error")
		}
		return nil, err
	}

	deps.Note(ctx, fmt.Sprintf("hax create_request succeeded (request_id=%s, iteration %d)",
		created.ID, revisionIter),
		"build", "approval", "hax", "submitted")
	return created, nil
}

// pauseForApproval pauses the execution for plan approval via agent.Pause: it
// transitions the execution to "waiting" on the control plane and blocks until
// the /webhooks/approval callback resolves it (or it expires — decision
// "expired") — the direct port of app.pause (design §4.6, webhook-resumed, no
// polling). Returns the decision string (approved / request_changes / rejected /
// expired / error) plus any free-text feedback. The callback URL is derived by
// the SDK from the agent's public URL, so no CallbackURL is passed here.
func pauseForApproval(
	ctx context.Context,
	pauser hitl.Pauser,
	executionID string,
	created *hitl.CreatedRequest,
	expiresHours int,
) (decision string, feedback string, err error) {
	result, err := pauser.Pause(ctx, agent.PauseOptions{
		ApprovalRequestID:  created.ID,
		ApprovalRequestURL: created.URL,
		ExpiresInHours:     expiresHours,
		ExecutionID:        executionID,
	})
	if err != nil {
		return "", "", err
	}
	return result.Decision, result.Feedback, nil
}

// replanWithFeedback re-runs Architect → Tech Lead loop → Sprint Planner with
// the reviewer feedback and returns the revised plan_result (app.py:881-951).
// PM is skipped (the PRD/scope is fixed).
func replanWithFeedback(ctx context.Context, req ApprovalRequest, planResult map[string]any, feedback string) (map[string]any, error) {
	deps := req.Deps
	cfg := req.Cfg
	resolved := req.Resolved
	prd := mapGet(planResult, "prd", map[string]any{})
	manifest := req.ManifestMap
	provider := cfg.AIProvider()

	arch, err := deps.Call(ctx, "run_architect", map[string]any{
		"prd":                prd,
		"repo_path":          req.RepoPath,
		"artifacts_dir":      req.ArtifactsDir,
		"feedback":           feedback,
		"model":              resolved["architect_model"],
		"permission_mode":    cfg.PermissionMode,
		"ai_provider":        provider,
		"workspace_manifest": manifest,
	}, "run_architect (human revision)")
	if err != nil {
		return nil, err
	}

	var review map[string]any
	for tlIter := 0; tlIter <= cfg.MaxReviewIterations; tlIter++ {
		review, err = deps.Call(ctx, "run_tech_lead", map[string]any{
			"prd":                prd,
			"repo_path":          req.RepoPath,
			"artifacts_dir":      req.ArtifactsDir,
			"revision_number":    tlIter,
			"model":              resolved["tech_lead_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        provider,
			"workspace_manifest": manifest,
		}, "run_tech_lead")
		if err != nil {
			return nil, err
		}
		if asBool(review["approved"]) {
			break
		}
		if tlIter < cfg.MaxReviewIterations {
			arch, err = deps.Call(ctx, "run_architect", map[string]any{
				"prd":                prd,
				"repo_path":          req.RepoPath,
				"artifacts_dir":      req.ArtifactsDir,
				"feedback":           mapStr(review, "feedback", ""),
				"model":              resolved["architect_model"],
				"permission_mode":    cfg.PermissionMode,
				"ai_provider":        provider,
				"workspace_manifest": manifest,
			}, "run_architect (tech lead revision)")
			if err != nil {
				return nil, err
			}
		}
	}

	// Auto-approve on exhaustion, mirroring the ReviewResult(...).model_dump()
	// override at app.py:923-930.
	if review != nil && !asBool(review["approved"]) {
		auto := schemas.ReviewResult{
			Approved:             true,
			Feedback:             mapStr(review, "feedback", ""),
			ScopeIssues:          asStrList(mapGet(review, "scope_issues", []any{})),
			ComplexityAssessment: mapStr(review, "complexity_assessment", "appropriate"),
			Summary:              mapStr(review, "summary", "") + " [auto-approved after max iterations]",
		}
		review = dumpToMap(auto)
	}

	sprint, err := deps.Call(ctx, "run_sprint_planner", map[string]any{
		"prd":                prd,
		"architecture":       arch,
		"repo_path":          req.RepoPath,
		"artifacts_dir":      req.ArtifactsDir,
		"model":              resolved["sprint_planner_model"],
		"permission_mode":    cfg.PermissionMode,
		"ai_provider":        provider,
		"workspace_manifest": manifest,
	}, "run_sprint_planner (revision)")
	if err != nil {
		return nil, err
	}

	// plan_result = {**plan_result, architecture, review, issues, rationale}.
	revised := make(map[string]any, len(planResult)+4)
	for k, v := range planResult {
		revised[k] = v
	}
	revised["architecture"] = arch
	revised["review"] = review
	revised["issues"] = mapGet(sprint, "issues", []any{})
	revised["rationale"] = mapGet(sprint, "rationale", "")
	return revised, nil
}

// terminalOutcome builds the terminal ApprovalOutcome carrying the failure
// BuildResult build returns verbatim (app.py:863-868, :959-964): plan_result +
// empty dag_state + success=false + the summary.
func terminalOutcome(planResult map[string]any, summary string) ApprovalOutcome {
	br := schemas.BuildResult{
		PlanResult: planResult,
		DAGState:   map[string]any{},
		Success:    false,
		Summary:    summary,
	}
	return ApprovalOutcome{Terminal: true, Result: dumpToMap(br)}
}

// formatPlanForApproval ports _format_plan_for_approval (app.py:355-402): render
// the plan_result into (planSummary, prdMarkdown, architectureMarkdown,
// issuesForTemplate) for the hax plan-review-v2 template.
func formatPlanForApproval(planResult map[string]any) (string, string, string, []map[string]any) {
	planSummary := mapStr(planResult, "rationale", "")
	prdData, _ := mapGet(planResult, "prd", map[string]any{}).(map[string]any)
	archData, _ := mapGet(planResult, "architecture", map[string]any{}).(map[string]any)

	var prdParts []string
	if vd := mapStr(prdData, "validated_description", ""); vd != "" {
		prdParts = append(prdParts, "## Description\n"+vd)
	}
	if b, ok := bulletBlock(prdData["must_have"]); ok {
		prdParts = append(prdParts, "## Must Have\n"+b)
	}
	if b, ok := bulletBlock(prdData["nice_to_have"]); ok {
		prdParts = append(prdParts, "## Nice to Have\n"+b)
	}
	if b, ok := bulletBlock(prdData["acceptance_criteria"]); ok {
		prdParts = append(prdParts, "## Acceptance Criteria\n"+b)
	}
	prdMarkdown := strings.Join(prdParts, "\n\n")

	var archParts []string
	if s := mapStr(archData, "summary", ""); s != "" {
		archParts = append(archParts, "## Summary\n"+s)
	}
	if comps := asMapList(mapGet(archData, "components", nil)); len(comps) > 0 {
		archParts = append(archParts, "## Components")
		for _, comp := range comps {
			name := mapStr(comp, "name", "Component")
			archParts = append(archParts, fmt.Sprintf("### %s\n%s", name, mapStr(comp, "responsibility", "")))
			if files := coerceAnyList(comp["touches_files"]); len(files) > 0 {
				quoted := make([]string, len(files))
				for i, f := range files {
					quoted[i] = fmt.Sprintf("`%v`", f)
				}
				archParts = append(archParts, "Files: "+strings.Join(quoted, ", "))
			}
		}
	}
	if decs := asMapList(mapGet(archData, "decisions", nil)); len(decs) > 0 {
		archParts = append(archParts, "## Key Decisions")
		for _, dec := range decs {
			archParts = append(archParts, fmt.Sprintf("- **%s**: %s",
				mapStr(dec, "decision", ""), mapStr(dec, "rationale", "")))
		}
	}
	architectureMarkdown := strings.Join(archParts, "\n\n")

	issues := []map[string]any{}
	for _, iss := range asMapList(mapGet(planResult, "issues", nil)) {
		issues = append(issues, map[string]any{
			"name":               mapStr(iss, "name", ""),
			"title":              mapStr(iss, "title", ""),
			"description":        mapStr(iss, "description", ""),
			"dependsOn":          any0(iss["depends_on"]),
			"filesToModify":      any0(iss["files_to_modify"]),
			"filesToCreate":      any0(iss["files_to_create"]),
			"acceptanceCriteria": any0(iss["acceptance_criteria"]),
		})
	}

	return planSummary, prdMarkdown, architectureMarkdown, issues
}

// bulletBlock renders a list value as "- item\n- item…", returning ok=false when
// the value is absent/empty (mirrors the Python `if prd_data.get(k):` guard).
func bulletBlock(v any) (string, bool) {
	items := coerceAnyList(v)
	if len(items) == 0 {
		return "", false
	}
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = fmt.Sprintf("- %v", it)
	}
	return strings.Join(parts, "\n"), true
}

// coerceAnyList coerces a JSON-decoded list value to []any (nil for non-lists).
func coerceAnyList(v any) []any {
	switch t := v.(type) {
	case []any:
		return t
	case []string:
		out := make([]any, len(t))
		for i, s := range t {
			out[i] = s
		}
		return out
	case []map[string]any:
		out := make([]any, len(t))
		for i, m := range t {
			out[i] = m
		}
		return out
	default:
		return nil
	}
}

// runeTruncate returns the first n runes of s (Python-style s[:n]). Used only
// for a diagnostic note, so exact rune parity with Python slicing matters more
// than byte counting.
func runeTruncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// writeApprovalState persists the approval_state.json trail (best-effort;
// failures are ignored, matching the Python open()/json.dump not being guarded
// against the caller). Uses indent=2 like the Python json.dump.
func writeApprovalState(path string, state map[string]any) {
	raw, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(path, raw, 0o644)
}

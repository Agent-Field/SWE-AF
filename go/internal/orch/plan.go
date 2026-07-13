package orch

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sync/errgroup"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dagutil"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// PlanHandler is the exported reasoner handler for the "plan" reasoner. The
// node-wiring wave (T6.2) registers it under its exact Python name "plan"; it is
// intentionally NOT added to build.go's Handlers() here to keep file ownership
// disjoint. Use RegisterPlan to merge it into a handler map.
var PlanHandler Handler = Plan

// RegisterPlan adds the "plan" reasoner handler to m. The wiring wave calls this
// (alongside orch.Handlers()) so build.go need not be edited to reference Plan.
func RegisterPlan(m map[string]Handler) {
	m["plan"] = PlanHandler
}

// planInput mirrors the Python plan() signature (param names + defaults).
// The nil-defaulting model/provider params (Python None) are represented as
// empty strings; an empty value means "resolve from the environment", matching
// Python's `x or default` truthiness (both None and "" fall through).
type planInput struct {
	Goal                string         `json:"goal"`
	RepoPath            string         `json:"repo_path"`
	ArtifactsDir        string         `json:"artifacts_dir"`
	AdditionalContext   string         `json:"additional_context"`
	MaxReviewIterations int            `json:"max_review_iterations"`
	PMModel             string         `json:"pm_model"`
	ArchitectModel      string         `json:"architect_model"`
	TechLeadModel       string         `json:"tech_lead_model"`
	SprintPlannerModel  string         `json:"sprint_planner_model"`
	IssueWriterModel    string         `json:"issue_writer_model"`
	PermissionMode      string         `json:"permission_mode"`
	AIProvider          string         `json:"ai_provider"`
	WorkspaceManifest   map[string]any `json:"workspace_manifest"`
}

// Plan runs the full planning pipeline. Ports plan() (app.py:1413-1622):
// product_manager → (environment_scout) → architect ↔ tech_lead review loop →
// sprint_planner → compute levels / conflicts / sequence numbers → parallel
// issue writers → assemble PlanResult. Every sub-reasoner call is routed through
// the control plane via Deps.Call (envelope-unwrapped), so the DAG UI renders
// identically to the Python pipeline.
func Plan(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	// Defaults applied BEFORE unmarshal so an absent key keeps the default and a
	// present key (even 0/"") overrides it.
	in := planInput{ArtifactsDir: ".artifacts", MaxReviewIterations: 2}
	if raw, err := json.Marshal(input); err == nil {
		_ = json.Unmarshal(raw, &in)
	}

	// Resolve provider/model defaults from the environment (docstring parity):
	// with only an OPENROUTER_API_KEY present the pipeline runs on open_code with
	// the default OpenRouter model; explicit args always win.
	aiProvider := in.AIProvider
	if aiProvider == "" {
		aiProvider = config.DefaultRuntime()
	}
	defaultModel := config.DefaultPlanningModel()
	pmModel := firstNonEmpty(in.PMModel, defaultModel)
	architectModel := firstNonEmpty(in.ArchitectModel, defaultModel)
	techLeadModel := firstNonEmpty(in.TechLeadModel, defaultModel)
	sprintPlannerModel := firstNonEmpty(in.SprintPlannerModel, defaultModel)
	issueWriterModel := firstNonEmpty(in.IssueWriterModel, defaultModel)

	deps.Note(ctx, "Pipeline starting", "pipeline", "start")

	// 1. PM scopes the goal into a PRD.
	deps.Note(ctx, "Phase 1: Product Manager", "pipeline", "pm")
	prd, err := deps.Call(ctx, "run_product_manager", map[string]any{
		"goal":               in.Goal,
		"repo_path":          in.RepoPath,
		"artifacts_dir":      in.ArtifactsDir,
		"additional_context": in.AdditionalContext,
		"model":              pmModel,
		"permission_mode":    in.PermissionMode,
		"ai_provider":        aiProvider,
		"workspace_manifest": in.WorkspaceManifest,
	}, "run_product_manager")
	if err != nil {
		return nil, err
	}

	// 1.5. Environment Scout — negotiate scoped credentials before architecture.
	// Only engages when HAX is enabled (auto-skipped at the reasoner level when
	// HAX_API_KEY is unset); the scout stashes negotiated values in the in-memory
	// credentials store keyed by run_id. No-op when HAX is disabled.
	if strings.TrimSpace(os.Getenv("HAX_API_KEY")) != "" {
		deps.Note(ctx, "Phase 1.5: Environment Scout", "pipeline", "scout")
		if _, serr := deps.Call(ctx, "run_environment_scout", map[string]any{
			"prd":                prd,
			"repo_path":          in.RepoPath,
			"artifacts_dir":      in.ArtifactsDir,
			"model":              pmModel,
			"permission_mode":    in.PermissionMode,
			"ai_provider":        aiProvider,
			"workspace_manifest": in.WorkspaceManifest,
		}, "run_environment_scout"); serr != nil {
			return nil, serr
		}
	}

	// 2. Architect designs the solution.
	deps.Note(ctx, "Phase 2: Architect", "pipeline", "architect")
	arch, err := deps.Call(ctx, "run_architect", map[string]any{
		"prd":                prd,
		"repo_path":          in.RepoPath,
		"artifacts_dir":      in.ArtifactsDir,
		"model":              architectModel,
		"permission_mode":    in.PermissionMode,
		"ai_provider":        aiProvider,
		"workspace_manifest": in.WorkspaceManifest,
	}, "run_architect")
	if err != nil {
		return nil, err
	}

	// 3. Tech Lead review loop (bounded: max_review_iterations + 1 passes).
	var review map[string]any
	for i := 0; i <= in.MaxReviewIterations; i++ {
		deps.Note(ctx, fmt.Sprintf("Phase 3: Tech Lead review (iteration %d)", i),
			"pipeline", "tech_lead")
		review, err = deps.Call(ctx, "run_tech_lead", map[string]any{
			"prd":                prd,
			"repo_path":          in.RepoPath,
			"artifacts_dir":      in.ArtifactsDir,
			"revision_number":    i,
			"model":              techLeadModel,
			"permission_mode":    in.PermissionMode,
			"ai_provider":        aiProvider,
			"workspace_manifest": in.WorkspaceManifest,
		}, "run_tech_lead")
		if err != nil {
			return nil, err
		}
		if asBool(review["approved"]) {
			break
		}
		if i < in.MaxReviewIterations {
			deps.Note(ctx, fmt.Sprintf("Architecture revision %d", i+1),
				"pipeline", "revision")
			arch, err = deps.Call(ctx, "run_architect", map[string]any{
				"prd":                prd,
				"repo_path":          in.RepoPath,
				"artifacts_dir":      in.ArtifactsDir,
				"feedback":           review["feedback"],
				"model":              architectModel,
				"permission_mode":    in.PermissionMode,
				"ai_provider":        aiProvider,
				"workspace_manifest": in.WorkspaceManifest,
			}, "run_architect (revision)")
			if err != nil {
				return nil, err
			}
		}
	}

	// Force-approve if we exhausted iterations (mirrors the ReviewResult rebuild).
	if review == nil {
		return nil, fmt.Errorf("plan: tech lead review is nil")
	}
	if !asBool(review["approved"]) {
		review = map[string]any{
			"approved":              true,
			"feedback":              mapGet(review, "feedback", ""),
			"scope_issues":          any0(review["scope_issues"]),
			"complexity_assessment": mapStr(review, "complexity_assessment", "appropriate"),
			"summary":               mapStr(review, "summary", "") + " [auto-approved after max iterations]",
		}
	}

	// 4. Sprint planner decomposes into issues.
	deps.Note(ctx, "Phase 4: Sprint Planner", "pipeline", "sprint_planner")
	sprintResult, err := deps.Call(ctx, "run_sprint_planner", map[string]any{
		"prd":                prd,
		"architecture":       arch,
		"repo_path":          in.RepoPath,
		"artifacts_dir":      in.ArtifactsDir,
		"model":              sprintPlannerModel,
		"permission_mode":    in.PermissionMode,
		"ai_provider":        aiProvider,
		"workspace_manifest": in.WorkspaceManifest,
	}, "run_sprint_planner")
	if err != nil {
		return nil, err
	}
	issues := asMapList(sprintResult["issues"])
	rationale := mapStr(sprintResult, "rationale", "")

	// 5. Compute parallel execution levels & assign sequence numbers BEFORE
	// issue writing. A dependency cycle propagates as an error.
	levels, err := dagutil.ComputeLevels(issues)
	if err != nil {
		return nil, err
	}
	issues = dagutil.AssignSequenceNumbers(issues, levels)
	fileConflicts := dagutil.ValidateFileConflicts(issues, levels)

	// 4b. Parallel issue writing (issues now have sequence_number set).
	absRepo, aerr := filepath.Abs(in.RepoPath)
	if aerr != nil {
		absRepo = in.RepoPath
	}
	base := filepath.Join(absRepo, in.ArtifactsDir)
	issuesDir := filepath.Join(base, "plan", "issues")
	prdPath := filepath.Join(base, "plan", "prd.md")
	architecturePath := filepath.Join(base, "plan", "architecture.md")
	if err := os.MkdirAll(issuesDir, 0o755); err != nil {
		return nil, err
	}

	prdSummary := mapStr(prd, "validated_description", "")
	prdAC := asStrList(prd["acceptance_criteria"])
	if len(prdAC) > 0 {
		var b strings.Builder
		for i, c := range prdAC {
			if i > 0 {
				b.WriteString("\n")
			}
			b.WriteString("- " + c)
		}
		prdSummary += "\n\nAcceptance Criteria:\n" + b.String()
	}

	deps.Note(ctx, fmt.Sprintf("Phase 4b: Writing %d issue files in parallel", len(issues)),
		"pipeline", "issue_writers")

	architectureSummary := mapStr(arch, "summary", "")
	succeededFlags := make([]bool, len(issues))
	var g errgroup.Group
	for idx, issue := range issues {
		idx, issue := idx, issue
		name := mapStr(issue, "name", "")
		siblings := make([]map[string]any, 0, len(issues))
		for _, other := range issues {
			if mapStr(other, "name", "") != name {
				siblings = append(siblings, map[string]any{
					"name":     other["name"],
					"title":    mapGet(other, "title", ""),
					"provides": any0(other["provides"]),
				})
			}
		}
		g.Go(func() error {
			// return_exceptions=True: a failed writer never aborts the others; it
			// simply does not count as a success.
			res, cerr := deps.Call(ctx, "run_issue_writer", map[string]any{
				"issue":                issue,
				"prd_summary":          prdSummary,
				"architecture_summary": architectureSummary,
				"issues_dir":           issuesDir,
				"repo_path":            in.RepoPath,
				"prd_path":             prdPath,
				"architecture_path":    architecturePath,
				"sibling_issues":       siblings,
				"model":                issueWriterModel,
				"permission_mode":      in.PermissionMode,
				"ai_provider":          aiProvider,
				"workspace_manifest":   in.WorkspaceManifest,
			}, "run_issue_writer")
			if cerr == nil && res != nil && asBool(res["success"]) {
				succeededFlags[idx] = true
			}
			return nil
		})
	}
	_ = g.Wait()

	succeeded := 0
	for _, ok := range succeededFlags {
		if ok {
			succeeded++
		}
	}
	failed := len(succeededFlags) - succeeded
	deps.Note(ctx, fmt.Sprintf("Issue writers complete: %d succeeded, %d failed", succeeded, failed),
		"pipeline", "issue_writers", "complete")

	// 6. Write rationale to disk.
	rationalePath := filepath.Join(base, "rationale.md")
	if err := os.WriteFile(rationalePath, []byte(rationale), 0o644); err != nil {
		return nil, err
	}

	deps.Note(ctx, "Pipeline complete", "pipeline", "complete")

	// Assemble the PlanResult, validating/normalizing the collected artifacts
	// exactly as Python's PlanResult(...).model_dump() does (required-field
	// validation + default seeding + drop of unknown keys).
	result, err := buildPlanResult(prd, arch, review, issues, levels, fileConflicts, base, rationale)
	if err != nil {
		return nil, err
	}
	return result, nil
}

// buildPlanResult coerces the collected reasoner dicts into the typed
// schemas.PlanResult (validating required fields, matching Pydantic) and returns
// its model_dump()-equivalent map. A missing required field (e.g. a malformed PM
// result lacking validated_description) surfaces as an error — parity with the
// PlanResult(prd=prd) ValidationError in Python.
func buildPlanResult(
	prd, arch, review map[string]any,
	issues []map[string]any,
	levels [][]string,
	fileConflicts []map[string]any,
	base, rationale string,
) (map[string]any, error) {
	var prdTyped schemas.PRD
	if err := coerce(prd, &prdTyped,
		[]string{"validated_description", "acceptance_criteria", "must_have", "nice_to_have", "out_of_scope"},
		"PRD"); err != nil {
		return nil, err
	}

	var archTyped schemas.Architecture
	if err := coerce(arch, &archTyped,
		[]string{"summary", "components", "interfaces", "decisions", "file_changes_overview"},
		"Architecture"); err != nil {
		return nil, err
	}

	var reviewTyped schemas.ReviewResult
	if err := coerce(review, &reviewTyped,
		[]string{"approved", "feedback", "summary"},
		"ReviewResult"); err != nil {
		return nil, err
	}

	issuesTyped := make([]schemas.PlannedIssue, 0, len(issues))
	for _, issue := range issues {
		var pi schemas.PlannedIssue
		if err := coerce(issue, &pi,
			[]string{"name", "title", "description", "acceptance_criteria"},
			"PlannedIssue"); err != nil {
			return nil, err
		}
		issuesTyped = append(issuesTyped, pi)
	}

	if levels == nil {
		levels = [][]string{}
	}
	if fileConflicts == nil {
		fileConflicts = []map[string]any{}
	}

	result := schemas.PlanResult{
		PRD:           prdTyped,
		Architecture:  archTyped,
		Review:        reviewTyped,
		Issues:        issuesTyped,
		Levels:        levels,
		FileConflicts: fileConflicts,
		ArtifactsDir:  base,
		Rationale:     rationale,
	}
	out := dumpToMap(result)
	if out == nil {
		return nil, fmt.Errorf("plan: failed to serialize PlanResult")
	}
	return out, nil
}

// coerce validates that every required key is present in m (mirroring Pydantic's
// required-field check) and then decodes m into dst via a JSON round-trip (which
// also surfaces type mismatches and seeds struct defaults). label prefixes the
// error message.
func coerce(m map[string]any, dst any, required []string, label string) error {
	if m == nil {
		return fmt.Errorf("%s: missing (nil)", label)
	}
	for _, k := range required {
		if _, ok := m[k]; !ok {
			return fmt.Errorf("%s: missing required field %q", label, k)
		}
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return fmt.Errorf("%s: %w", label, err)
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		return fmt.Errorf("%s: %w", label, err)
	}
	return nil
}

// firstNonEmpty returns a if non-empty, else b (Python's `a or b` for strings).
func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

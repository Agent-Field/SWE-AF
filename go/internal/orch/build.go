package orch

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/envelope"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"github.com/Agent-Field/SWE-AF/go/internal/workspace"
)

// Handlers is the name→handler registration surface consumed by node wiring.
// The keys are the exact Python reasoner names owned by this package's build
// pipeline. Sibling orchestrator files contribute their own entries when the
// wiring wave merges them.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"build": Build,
	}
}

// buildInput mirrors the Python build() signature (param names + defaults). The
// async API body binds by these exact keys.
type buildInput struct {
	Goal              string         `json:"goal"`
	RepoPath          string         `json:"repo_path"`
	RepoURL           string         `json:"repo_url"`
	ArtifactsDir      string         `json:"artifacts_dir"`
	AdditionalContext string         `json:"additional_context"`
	Config            map[string]any `json:"config"`
	ExecuteFnTarget   string         `json:"execute_fn_target"`
	MaxTurns          int            `json:"max_turns"`
	PermissionMode    string         `json:"permission_mode"`
	EnableLearning    bool           `json:"enable_learning"`
}

// Build is the end-to-end orchestrator: clone → plan (+ git init) → approval →
// execute → verify/fix → finalize → PR (+ CI gate). Ports build() (app.py:490).
func Build(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := buildInput{ArtifactsDir: ".artifacts"}
	if raw, err := json.Marshal(input); err == nil {
		_ = json.Unmarshal(raw, &in)
	}

	// cfg = BuildConfig(**config) if config else BuildConfig()
	cfg, err := config.LoadBuildConfig(in.Config)
	if err != nil {
		return nil, err
	}

	// Allow repo_url from direct parameter.
	if in.RepoURL != "" {
		cfg.RepoURL = in.RepoURL
	}

	// build_id BEFORE workspace setup → per-build isolation (issue #43).
	buildID := newBuildID()

	repoPath := in.RepoPath

	// Auto-derive repo_path from repo_url, build-scoped.
	if cfg.RepoURL != "" && repoPath == "" {
		repoName := deriveRepoName(cfg.RepoURL)
		repoPath = filepath.Join(workspace.Root(), fmt.Sprintf("%s-%s", repoName, buildID))
	}

	// Multi-repo: derive repo_path from the primary repo.
	if repoPath == "" && len(cfg.Repos) > 1 {
		primary := cfg.PrimaryRepo()
		if primary == nil {
			primary = &cfg.Repos[0]
		}
		repoName := deriveRepoName(primary.RepoURL)
		repoPath = filepath.Join(workspace.Root(), fmt.Sprintf("%s-%s", repoName, buildID))
	}

	if repoPath == "" {
		return nil, errors.New("Either repo_path or repo_url must be provided")
	}

	deps.Note(ctx, fmt.Sprintf("Build starting (build_id=%s)", buildID), "build", "start")

	// Scope key for the credentials store; cleared in the deferred finally so
	// even an error leaves no secrets in process memory.
	scopeID := runIDFromCtx(ctx)
	defer func() {
		if scopeID != "" {
			hitl.ClearScopedCredentials(scopeID)
		}
	}()

	// --- Clone / reset the single-repo working tree ---
	if err := prepareSingleRepo(ctx, deps, cfg, repoPath); err != nil {
		return nil, err
	}

	if in.ExecuteFnTarget != "" {
		cfg.ExecuteFnTarget = in.ExecuteFnTarget
	}
	if in.PermissionMode != "" {
		cfg.PermissionMode = in.PermissionMode
	}
	if in.EnableLearning {
		cfg.EnableLearning = true
	}
	if in.MaxTurns > 0 {
		cfg.AgentMaxTurns = in.MaxTurns
	}

	resolved, err := cfg.ResolvedModels()
	if err != nil {
		return nil, err
	}

	absRepo, err := filepath.Abs(repoPath)
	if err != nil {
		absRepo = repoPath
	}
	absArtifactsDir := filepath.Join(absRepo, in.ArtifactsDir)

	// --- Multi-repo clone ---
	var manifest *schemas.WorkspaceManifest
	if len(cfg.Repos) > 1 {
		deps.Note(ctx, fmt.Sprintf("Cloning %d repos concurrently", len(cfg.Repos)),
			"build", "clone", "multi-repo")
		manifest, err = cloneRepos(ctx, cfg, absArtifactsDir)
		if err != nil {
			return nil, err
		}
		if primary := manifest.PrimaryRepo(); primary != nil {
			repoPath = primary.AbsolutePath
		}
		deps.Note(ctx, fmt.Sprintf("Multi-repo workspace ready: %s", manifest.WorkspaceRoot),
			"build", "clone", "multi-repo", "complete")
	}
	manifestMap := dumpToMap(manifest)

	// 1. PLAN + GIT INIT (concurrent — no data dependency).
	deps.Note(ctx, "Phase 1: Planning + Git init (parallel)", "build", "parallel")

	planKwargs := map[string]any{
		"goal":                  in.Goal,
		"repo_path":             repoPath,
		"artifacts_dir":         in.ArtifactsDir,
		"additional_context":    in.AdditionalContext,
		"max_review_iterations": cfg.MaxReviewIterations,
		"pm_model":              resolved["pm_model"],
		"architect_model":       resolved["architect_model"],
		"tech_lead_model":       resolved["tech_lead_model"],
		"sprint_planner_model":  resolved["sprint_planner_model"],
		"issue_writer_model":    resolved["issue_writer_model"],
		"permission_mode":       cfg.PermissionMode,
		"ai_provider":           cfg.AIProvider(),
		"workspace_manifest":    manifestMap,
	}

	type callRes struct {
		raw map[string]any
		err error
	}
	planCh := make(chan callRes, 1)
	go func() {
		raw, perr := deps.App.Call(ctx, deps.target("plan"), planKwargs)
		planCh <- callRes{raw: raw, err: perr}
	}()

	maxGitInitRetries := cfg.GitInitMaxRetries
	var gitInit map[string]any
	var previousError any // None on first attempt, string thereafter
	var rawPlan callRes
	planReceived := false

	for attempt := 1; attempt <= maxGitInitRetries; attempt++ {
		note := fmt.Sprintf("Git init attempt %d/%d", attempt, maxGitInitRetries)
		if s, ok := previousError.(string); ok && s != "" {
			note += fmt.Sprintf(" (previous error: %s)", s)
		}
		deps.Note(ctx, note, "build", "git_init", "retry")

		gitKwargs := map[string]any{
			"repo_path":       repoPath,
			"goal":            in.Goal,
			"artifacts_dir":   absArtifactsDir,
			"model":           resolved["git_model"],
			"permission_mode": cfg.PermissionMode,
			"ai_provider":     cfg.AIProvider(),
			"previous_error":  previousError,
			"build_id":        buildID,
		}

		rawGit, gerr := deps.CallRaw(ctx, "run_git_init", gitKwargs)
		if attempt == 1 {
			rawPlan = <-planCh // gather: wait for both
			planReceived = true
		}
		if gerr != nil {
			return nil, gerr // transport error propagates (Python: gather raises)
		}

		// git_init failures are non-fatal — unwrap but don't propagate.
		gi, uerr := envelope.UnwrapCallResult(rawGit, "run_git_init")
		if uerr != nil {
			gi = rawGit // except RuntimeError: git_init = raw_git (the envelope dict)
		}
		gitInit = gi

		if asBool(gitInit["success"]) {
			deps.Note(ctx, fmt.Sprintf("Git init succeeded on attempt %d", attempt),
				"build", "git_init", "success")
			break
		}

		previousError = mapStr(gitInit, "error_message", "unknown error")
		deps.Note(ctx, fmt.Sprintf("Git init attempt %d failed: %s", attempt, previousError),
			"build", "git_init", "failed")

		if attempt == maxGitInitRetries {
			deps.Note(ctx, fmt.Sprintf(
				"Git init failed after %d attempts — proceeding without git workflow",
				maxGitInitRetries), "build", "git_init", "exhausted")
		}
		if attempt < maxGitInitRetries {
			sleepFn(ctx, time.Duration(cfg.GitInitRetryDelay*float64(time.Second)))
		}
	}

	if !planReceived {
		rawPlan = <-planCh
	}
	if rawPlan.err != nil {
		return nil, rawPlan.err
	}
	planResult, err := envelope.UnwrapCallResult(rawPlan.raw, "plan")
	if err != nil {
		return nil, err
	}

	var gitConfig map[string]any
	if asBool(gitInit["success"]) {
		gitConfig = map[string]any{
			"integration_branch":    gitInit["integration_branch"],
			"original_branch":       gitInit["original_branch"],
			"initial_commit_sha":    gitInit["initial_commit_sha"],
			"mode":                  gitInit["mode"],
			"remote_url":            mapStr(gitInit, "remote_url", ""),
			"remote_default_branch": mapStr(gitInit, "remote_default_branch", ""),
		}
		deps.Note(ctx, fmt.Sprintf("Git init: mode=%v, branch=%v",
			gitInit["mode"], gitInit["integration_branch"]), "build", "git_init", "complete")
	} else {
		deps.Note(ctx, fmt.Sprintf("Git init failed: %s — proceeding without git workflow",
			mapStr(gitInit, "error_message", "unknown")), "build", "git_init", "error")
	}

	// 1.5 APPROVAL CHECKPOINT (hax plan review; engaged only when HAX_API_KEY set
	// and a gate is wired — see the ApprovalGate seam in common.go).
	haxAPIKey := strings.TrimSpace(os.Getenv("HAX_API_KEY"))
	execID := executionIDFromCtx(ctx)
	if haxAPIKey != "" && execID != "" && deps.ApprovalGate != nil {
		outcome, aerr := deps.ApprovalGate(ctx, ApprovalRequest{
			Deps:            deps,
			Cfg:             cfg,
			Resolved:        resolved,
			PlanResult:      planResult,
			ManifestMap:     manifestMap,
			RepoPath:        repoPath,
			Goal:            in.Goal,
			ArtifactsDir:    in.ArtifactsDir,
			AbsArtifactsDir: absArtifactsDir,
			ExecutionID:     execID,
		})
		if aerr != nil {
			return nil, aerr
		}
		if outcome.Terminal {
			return outcome.Result, nil
		}
		if outcome.PlanResult != nil {
			planResult = outcome.PlanResult
		}
	}

	// 2. EXECUTE
	execConfig := cfg.ToExecutionConfigDict()
	dagResult, err := deps.Call(ctx, "execute", map[string]any{
		"plan_result":        planResult,
		"repo_path":          repoPath,
		"execute_fn_target":  cfg.ExecuteFnTarget,
		"config":             execConfig,
		"git_config":         gitConfig,
		"build_id":           buildID,
		"workspace_manifest": manifestMap,
	}, "execute")
	if err != nil {
		return nil, err
	}

	// High-water marks of shipped work across the original run + fix cycles.
	everCompleted := len(asMapList(dagResult["completed_issues"]))
	everMerged := len(asStrList(dagResult["merged_branches"]))

	// Refresh manifest with git_init_result populated by _init_all_repos().
	if manifest != nil {
		if wm, ok := dagResult["workspace_manifest"].(map[string]any); ok && wm != nil {
			if refreshed, merr := manifestFromMap(wm); merr == nil && refreshed != nil {
				manifest = refreshed
			}
		}
	}

	planArtifactsDir := mapStr(planResult, "artifacts_dir", in.ArtifactsDir)

	// 3. VERIFY (with bounded fix cycles).
	var verification map[string]any
	for cycle := 0; cycle <= cfg.MaxVerifyFixCycles; cycle++ {
		deps.Note(ctx, fmt.Sprintf("Verification cycle %d", cycle), "build", "verify")
		verification, err = deps.Call(ctx, "run_verifier", map[string]any{
			"prd":                planResult["prd"],
			"repo_path":          repoPath,
			"artifacts_dir":      planArtifactsDir,
			"completed_issues":   maps0(dagResult["completed_issues"]),
			"failed_issues":      maps0(dagResult["failed_issues"]),
			"skipped_issues":     any0(dagResult["skipped_issues"]),
			"model":              resolved["verifier_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        cfg.AIProvider(),
			"workspace_manifest": dumpToMap(manifest),
		}, "run_verifier")
		if err != nil {
			return nil, err
		}

		if asBool(verification["passed"]) || cycle >= cfg.MaxVerifyFixCycles {
			break
		}

		failedCriteria := failedCriteriaOf(verification)
		if len(failedCriteria) == 0 {
			deps.Note(ctx, "Verification failed but no specific criteria failures found", "build", "verify")
			break
		}

		deps.Note(ctx, fmt.Sprintf("Verification failed (%d criteria), %d fix cycles remaining",
			len(failedCriteria), cfg.MaxVerifyFixCycles-cycle), "build", "verify", "retry")

		fixResult, ferr := deps.Call(ctx, "generate_fix_issues", map[string]any{
			"failed_criteria":    failedCriteria,
			"dag_state":          dagResult,
			"prd":                planResult["prd"],
			"artifacts_dir":      planArtifactsDir,
			"model":              resolved["verifier_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        cfg.AIProvider(),
			"workspace_manifest": dumpToMap(manifest),
		}, "generate_fix_issues")
		if ferr != nil {
			return nil, ferr
		}

		fixIssues := asMapList(fixResult["fix_issues"])
		fixDebt := asMapList(fixResult["debt_items"])

		// Record unfixable criteria as debt on the dag_result.
		for _, debt := range fixDebt {
			appendToMapList(dagResult, "accumulated_debt", map[string]any{
				"type":      "unmet_acceptance_criterion",
				"criterion": mapStr(debt, "criterion", ""),
				"reason":    mapStr(debt, "reason", ""),
				"severity":  mapStr(debt, "severity", "high"),
			})
		}

		if len(fixIssues) > 0 {
			levelNames := make([]string, len(fixIssues))
			for i, fi := range fixIssues {
				levelNames[i] = mapStr(fi, "name", fmt.Sprintf("fix-%d", i))
			}
			fixPlan := map[string]any{
				"prd":            planResult["prd"],
				"architecture":   mapGet(planResult, "architecture", map[string]any{}),
				"review":         mapGet(planResult, "review", map[string]any{}),
				"issues":         fixIssues,
				"levels":         [][]string{levelNames},
				"file_conflicts": []any{},
				"artifacts_dir":  planArtifactsDir,
				"rationale":      fmt.Sprintf("Fix issues for verification cycle %d", cycle+1),
			}
			dagResult, err = deps.Call(ctx, "execute", map[string]any{
				"plan_result":        fixPlan,
				"repo_path":          repoPath,
				"config":             execConfig,
				"git_config":         gitConfig,
				"workspace_manifest": dumpToMap(manifest),
			}, "execute_fixes")
			if err != nil {
				return nil, err
			}
			everCompleted = maxInt(everCompleted, len(asMapList(dagResult["completed_issues"])))
			everMerged = maxInt(everMerged, len(asStrList(dagResult["merged_branches"])))
			continue // re-verify
		}

		deps.Note(ctx, "No fixable issues generated — accepting with debt", "build", "verify")
		break
	}

	success := asBool(verification["passed"])
	completed := len(asMapList(dagResult["completed_issues"]))
	total := len(asMapList(dagResult["all_issues"]))

	verb := "completed with issues"
	verifWord := "failed"
	if success {
		verb = "succeeded"
		verifWord = "passed"
	}
	deps.Note(ctx, fmt.Sprintf("Build %s: %d/%d issues, verification=%s",
		verb, completed, total, verifWord), "build", "complete")

	// Capture plan docs before finalize cleans up .artifacts/.
	prdMarkdown, architectureMarkdown := readPlanDocs(planResult)

	// 3b. FINALIZE.
	finalizeRepos(ctx, deps, cfg, resolved, manifest, repoPath, planArtifactsDir)

	// 4. PUSH & PR (+ CI gate).
	prResults := []schemas.RepoPRResult{}
	ciGateResults := []map[string]any{}
	buildSummary := buildSummaryText(success, completed, total, verification)

	if manifest != nil && len(manifest.Repos) > 1 {
		prResults, ciGateResults = multiRepoPRs(ctx, deps, cfg, resolved, manifest,
			dagResult, planArtifactsDir, in.Goal, buildSummary)
	} else {
		prResults, ciGateResults = singleRepoPR(ctx, deps, cfg, resolved, gitConfig,
			dagResult, repoPath, planArtifactsDir, in.Goal, buildSummary,
			prdMarkdown, architectureMarkdown)
	}

	// 5. WORKSPACE CLEANUP (non-blocking).
	if manifest != nil && manifest.WorkspaceRoot != "" {
		_ = os.RemoveAll(manifest.WorkspaceRoot)
		deps.Note(ctx, fmt.Sprintf("Workspace cleaned up: %s", manifest.WorkspaceRoot),
			"build", "cleanup")
	}

	buildResult := schemas.BuildResult{
		PlanResult:    planResult,
		DAGState:      dagResult,
		Verification:  verification,
		Success:       success,
		Summary:       buildSummaryText(success, completed, total, verification),
		PRResults:     prResults,
		CIGateResults: ciGateResults,
	}
	buildResultMap := dumpToMap(buildResult)

	// Empty-build guard: nothing shipped AND verification failed → report failed.
	// Return the SDK's result-carrying &agent.ReasonerFailed so the async handler
	// records status=failed while preserving the BuildResult on the execution
	// record (it posts result + error in a single 5×-retried status update). The
	// result is attached only when it is a JSON object (a non-nil map), mirroring
	// the JSON-object guard of the retired reasonerfail.buildBody.
	if isEmptyBuild(success, everCompleted, everMerged) {
		msg := fmt.Sprintf("Build failed: 0/%d issues completed, no branches merged", total)
		rf := &agent.ReasonerFailed{Message: msg}
		if buildResultMap != nil {
			rf.Result = buildResultMap
		}
		return buildResultMap, rf
	}

	return buildResultMap, nil
}

// prepareSingleRepo ports the single-repo clone/reset block (app.py:548-617).
func prepareSingleRepo(ctx context.Context, deps *Deps, cfg *config.BuildConfig, repoPath string) error {
	gitDir := filepath.Join(repoPath, ".git")
	switch {
	case cfg.RepoURL != "" && !pathExists(gitDir):
		deps.Note(ctx, fmt.Sprintf("Cloning %s → %s", cfg.RepoURL, repoPath), "build", "clone")
		// Create only the parent; git clone creates the leaf itself. Pre-creating
		// the destination leaf makes git refuse it as "already exists and is not
		// an empty directory" on Windows, where it cannot re-open the dir the node
		// just made (issue #107).
		_ = os.MkdirAll(filepath.Dir(repoPath), 0o755)
		r := runGit(ctx, "", "clone", cfg.RepoURL, repoPath)
		if r.ExitCode != 0 {
			errMsg := strings.TrimSpace(r.Stderr)
			deps.Note(ctx, fmt.Sprintf("Clone failed (exit %d): %s", r.ExitCode, errMsg),
				"build", "clone", "error")
			return fmt.Errorf("git clone failed (exit %d): %s", r.ExitCode, errMsg)
		}
	case cfg.RepoURL != "" && pathExists(gitDir):
		defaultBranch := cfg.GithubPRBase
		if defaultBranch == "" {
			defaultBranch = "main"
		}
		deps.Note(ctx, fmt.Sprintf("Repo already exists at %s — resetting to origin/%s",
			repoPath, defaultBranch), "build", "clone", "reset")

		worktreesDir := filepath.Join(repoPath, ".worktrees")
		if isDir(worktreesDir) {
			_ = os.RemoveAll(worktreesDir)
		}
		runGit(ctx, repoPath, "worktree", "prune")

		if fetch := runGit(ctx, repoPath, "fetch", "origin"); fetch.ExitCode != 0 {
			deps.Note(ctx, fmt.Sprintf("git fetch failed: %s", strings.TrimSpace(fetch.Stderr)),
				"build", "clone", "error")
		}

		runGit(ctx, repoPath, "checkout", "-f", defaultBranch)
		reset := runGit(ctx, repoPath, "reset", "--hard", "origin/"+defaultBranch)
		if reset.ExitCode != 0 {
			deps.Note(ctx, fmt.Sprintf("Reset to origin/%s failed — re-cloning", defaultBranch),
				"build", "clone", "reclone")
			_ = os.RemoveAll(repoPath)
			// Parent-only: git clone re-creates the leaf (issue #107).
			_ = os.MkdirAll(filepath.Dir(repoPath), 0o755)
			clone := runGit(ctx, "", "clone", cfg.RepoURL, repoPath)
			if clone.ExitCode != 0 {
				return fmt.Errorf("git re-clone failed: %s", strings.TrimSpace(clone.Stderr))
			}
		}
	default:
		_ = os.MkdirAll(repoPath, 0o755)
	}
	return nil
}

// finalizeRepos ports the FINALIZE phase (app.py:1117-1175) for multi- and
// single-repo builds. A finalize failure is non-blocking (noted, not raised).
func finalizeRepos(ctx context.Context, deps *Deps, cfg *config.BuildConfig,
	resolved map[string]string, manifest *schemas.WorkspaceManifest, repoPath, artifactsDir string) {

	callFinalize := func(path, repoName string) {
		label := "run_repo_finalize"
		if repoName != "" {
			label = fmt.Sprintf("run_repo_finalize (%s)", repoName)
		}
		res, ferr := deps.Call(ctx, "run_repo_finalize", map[string]any{
			"repo_path":       path,
			"artifacts_dir":   artifactsDir,
			"model":           resolved["git_model"],
			"permission_mode": cfg.PermissionMode,
			"ai_provider":     cfg.AIProvider(),
		}, label)
		if ferr != nil {
			suffix := ""
			if repoName != "" {
				suffix = " for " + repoName
			}
			deps.Note(ctx, fmt.Sprintf("Repo finalize failed%s (non-blocking): %v", suffix, ferr),
				"build", "finalize", "error")
			return
		}
		tag := "complete"
		word := "finalized"
		if !asBool(res["success"]) {
			tag = "warning"
			word = "finalize incomplete"
		}
		prefix := "Repo " + word
		if repoName != "" {
			prefix = fmt.Sprintf("Repo %s (%s)", word, repoName)
		}
		deps.Note(ctx, fmt.Sprintf("%s: %s", prefix, mapStr(res, "summary", "")),
			"build", "finalize", tag)
	}

	if manifest != nil && len(manifest.Repos) > 1 {
		deps.Note(ctx, fmt.Sprintf("Phase 3b: Multi-repo finalization (%d repos)", len(manifest.Repos)),
			"build", "finalize", "multi-repo")
		for i := range manifest.Repos {
			callFinalize(manifest.Repos[i].AbsolutePath, manifest.Repos[i].RepoName)
		}
		return
	}
	deps.Note(ctx, "Phase 3b: Repo finalization", "build", "finalize")
	callFinalize(repoPath, "")
}

// newBuildID returns an 8-hex-char build id (uuid.uuid4().hex[:8] parity).
func newBuildID() string {
	var b [4]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// buildSummaryText ports the shared summary f-string.
func buildSummaryText(success bool, completed, total int, verification map[string]any) string {
	word := "Partial"
	if success {
		word = "Success"
	}
	s := fmt.Sprintf("%s: %d/%d issues completed", word, completed, total)
	if verification != nil {
		s += fmt.Sprintf(", verification: %s", mapStr(verification, "summary", ""))
	}
	return s
}

// readPlanDocs reads prd.md / architecture.md from <artifacts_dir>/plan.
func readPlanDocs(planResult map[string]any) (prd, arch string) {
	planDir := filepath.Join(mapStr(planResult, "artifacts_dir", ""), "plan")
	if b, err := os.ReadFile(filepath.Join(planDir, "prd.md")); err == nil {
		prd = string(b)
	}
	if b, err := os.ReadFile(filepath.Join(planDir, "architecture.md")); err == nil {
		arch = string(b)
	}
	return prd, arch
}

// failedCriteriaOf extracts the failed criteria_results entries (passed != true).
func failedCriteriaOf(verification map[string]any) []map[string]any {
	var out []map[string]any
	for _, c := range asMapList(verification["criteria_results"]) {
		passed := true
		if v, ok := c["passed"]; ok {
			passed = asBool(v)
		}
		if !passed {
			out = append(out, c)
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// PR phases.
// ---------------------------------------------------------------------------

// singleRepoPR ports the single-repo push + PR + CI-gate block (app.py:1264-1367).
func singleRepoPR(ctx context.Context, deps *Deps, cfg *config.BuildConfig,
	resolved map[string]string, gitConfig, dagResult map[string]any,
	repoPath, artifactsDir, goal, buildSummary, prdMarkdown, architectureMarkdown string,
) ([]schemas.RepoPRResult, []map[string]any) {

	prResults := []schemas.RepoPRResult{}
	ciGateResults := []map[string]any{}

	remoteURL := ""
	if gitConfig != nil {
		remoteURL = mapStr(gitConfig, "remote_url", "")
	}
	if remoteURL == "" || !cfg.EnableGithubPR {
		return prResults, ciGateResults
	}

	deps.Note(ctx, "Phase 4: Push + PR", "build", "github_pr")
	baseBranch := cfg.GithubPRBase
	if baseBranch == "" && gitConfig != nil {
		baseBranch = mapStr(gitConfig, "remote_default_branch", "")
	}
	if baseBranch == "" {
		baseBranch = "main"
	}

	prResult, perr := deps.Call(ctx, "run_github_pr", map[string]any{
		"repo_path":          repoPath,
		"integration_branch": mapGet(gitConfig, "integration_branch", ""),
		"base_branch":        baseBranch,
		"goal":               goal,
		"build_summary":      buildSummary,
		"completed_issues":   maps0(dagResult["completed_issues"]),
		"accumulated_debt":   maps0(dagResult["accumulated_debt"]),
		"artifacts_dir":      artifactsDir,
		"model":              resolved["git_model"],
		"permission_mode":    cfg.PermissionMode,
		"ai_provider":        cfg.AIProvider(),
	}, "run_github_pr")
	if perr != nil {
		deps.Note(ctx, fmt.Sprintf("PR creation failed: %v", perr), "build", "github_pr", "error")
		return prResults, ciGateResults
	}

	prURL := mapStr(prResult, "pr_url", "")
	prNumber := asInt(prResult["pr_number"])
	if prURL != "" {
		deps.Note(ctx, fmt.Sprintf("PR created: %s", prURL), "build", "github_pr", "complete")
		appendPlanDocsToPR(ctx, deps, repoPath, prNumber, prdMarkdown, architectureMarkdown)
	} else {
		deps.Note(ctx, fmt.Sprintf("PR creation failed: %s",
			mapStr(prResult, "error_message", "unknown")), "build", "github_pr", "error")
	}

	if prURL != "" {
		repoName := "repo"
		if cfg.RepoURL != "" {
			repoName = deriveRepoName(cfg.RepoURL)
		}
		prResults = append(prResults, schemas.RepoPRResult{
			RepoName: repoName,
			RepoURL:  cfg.RepoURL,
			Success:  true,
			PRURL:    prURL,
			PRNumber: prNumber,
		})
		if cfg.CheckCI && prNumber != 0 && deps.CIGate != nil {
			gate, gerr := deps.CIGate(ctx, CIGateRequest{
				Deps:              deps,
				Cfg:               cfg,
				Resolved:          resolved,
				RepoPath:          repoPath,
				PRNumber:          prNumber,
				PRURL:             prURL,
				IntegrationBranch: mapStr(gitConfig, "integration_branch", ""),
				BaseBranch:        baseBranch,
				Goal:              goal,
				CompletedIssues:   asMapList(dagResult["completed_issues"]),
			})
			if gerr == nil {
				entry := map[string]any{"repo_name": repoName}
				for k, v := range gate {
					entry[k] = v
				}
				ciGateResults = append(ciGateResults, entry)
			}
		}
	}

	return prResults, ciGateResults
}

// multiRepoPRs ports the multi-repo push + PR + CI-gate block (app.py:1185-1263).
func multiRepoPRs(ctx context.Context, deps *Deps, cfg *config.BuildConfig,
	resolved map[string]string, manifest *schemas.WorkspaceManifest,
	dagResult map[string]any, artifactsDir, goal, buildSummary string,
) ([]schemas.RepoPRResult, []map[string]any) {

	prResults := []schemas.RepoPRResult{}
	ciGateResults := []map[string]any{}

	deps.Note(ctx, "Phase 4: Multi-repo Push + PRs", "build", "github_pr", "multi-repo")
	for i := range manifest.Repos {
		wsRepo := manifest.Repos[i]
		if !wsRepo.CreatePR || !cfg.EnableGithubPR {
			continue
		}
		repoGitInit := wsRepo.GitInitResult
		if repoGitInit == nil {
			repoGitInit = map[string]any{}
		}
		repoRemoteURL := mapStr(repoGitInit, "remote_url", "")
		if repoRemoteURL == "" {
			repoRemoteURL = wsRepo.RepoURL
		}
		if repoRemoteURL == "" {
			continue
		}
		repoIntegrationBranch := mapStr(repoGitInit, "integration_branch", "")
		if repoIntegrationBranch == "" {
			continue
		}
		repoBaseBranch := cfg.GithubPRBase
		if repoBaseBranch == "" {
			repoBaseBranch = mapStr(repoGitInit, "remote_default_branch", "")
		}
		if repoBaseBranch == "" {
			repoBaseBranch = "main"
		}

		completedForRepo := filterCompletedForRepo(dagResult, wsRepo.RepoName)

		prR, perr := deps.Call(ctx, "run_github_pr", map[string]any{
			"repo_path":          wsRepo.AbsolutePath,
			"integration_branch": repoIntegrationBranch,
			"base_branch":        repoBaseBranch,
			"goal":               goal,
			"build_summary":      buildSummary,
			"completed_issues":   completedForRepo,
			"accumulated_debt":   maps0(dagResult["accumulated_debt"]),
			"artifacts_dir":      artifactsDir,
			"model":              resolved["git_model"],
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        cfg.AIProvider(),
		}, "run_github_pr")
		if perr != nil {
			prResults = append(prResults, schemas.RepoPRResult{
				RepoName:     wsRepo.RepoName,
				RepoURL:      wsRepo.RepoURL,
				Success:      false,
				ErrorMessage: perr.Error(),
			})
			deps.Note(ctx, fmt.Sprintf("PR creation failed for %s: %v", wsRepo.RepoName, perr),
				"build", "github_pr", "error")
			continue
		}

		prResults = append(prResults, schemas.RepoPRResult{
			RepoName:     wsRepo.RepoName,
			RepoURL:      wsRepo.RepoURL,
			Success:      asBool(prR["success"]),
			PRURL:        mapStr(prR, "pr_url", ""),
			PRNumber:     asInt(prR["pr_number"]),
			ErrorMessage: mapStr(prR, "error_message", ""),
		})

		prURL := mapStr(prR, "pr_url", "")
		prNumber := asInt(prR["pr_number"])
		if prURL != "" {
			deps.Note(ctx, fmt.Sprintf("PR created for %s: %s", wsRepo.RepoName, prURL),
				"build", "github_pr", "complete")
			if cfg.CheckCI && prNumber != 0 && deps.CIGate != nil {
				gate, gerr := deps.CIGate(ctx, CIGateRequest{
					Deps:              deps,
					Cfg:               cfg,
					Resolved:          resolved,
					RepoPath:          wsRepo.AbsolutePath,
					PRNumber:          prNumber,
					PRURL:             prURL,
					IntegrationBranch: repoIntegrationBranch,
					BaseBranch:        repoBaseBranch,
					Goal:              goal,
					CompletedIssues:   completedForRepo,
				})
				if gerr == nil {
					entry := map[string]any{"repo_name": wsRepo.RepoName}
					for k, v := range gate {
						entry[k] = v
					}
					ciGateResults = append(ciGateResults, entry)
				}
			}
		}
	}

	return prResults, ciGateResults
}

// filterCompletedForRepo keeps completed issues with no repo_name or matching
// repo_name (app.py:1211-1214,1244-1247).
func filterCompletedForRepo(dagResult map[string]any, repoName string) []map[string]any {
	out := []map[string]any{}
	for _, r := range asMapList(dagResult["completed_issues"]) {
		rn := mapStr(r, "repo_name", "")
		if rn == "" || rn == repoName {
			out = append(out, r)
		}
	}
	return out
}

// appendPlanDocsToPR programmatically appends PRD/Architecture to the PR body via
// `gh pr view/edit` (app.py:1294-1333). Non-fatal on failure.
func appendPlanDocsToPR(ctx context.Context, deps *Deps, repoPath string, prNumber int, prdMarkdown, architectureMarkdown string) {
	if prdMarkdown == "" && architectureMarkdown == "" {
		return
	}
	num := strconv.Itoa(prNumber)
	view := runGH(ctx, repoPath, "pr", "view", num, "--json", "body", "--jq", ".body")
	if view.ExitCode != 0 {
		deps.Note(ctx, fmt.Sprintf("Failed to append plan docs to PR (non-fatal): %s",
			strings.TrimSpace(view.Stderr)), "build", "github_pr", "plan_docs", "warning")
		return
	}
	currentBody := strings.TrimSpace(view.Stdout)

	planSections := "\n\n---\n"
	if prdMarkdown != "" {
		planSections += "\n<details><summary>📋 PRD (Product Requirements Document)</summary>\n\n" +
			prdMarkdown + "\n\n</details>\n"
	}
	if architectureMarkdown != "" {
		planSections += "\n<details><summary>🏗️ Architecture</summary>\n\n" +
			architectureMarkdown + "\n\n</details>\n"
	}
	newBody := currentBody + planSections

	edit := runGH(ctx, repoPath, "pr", "edit", num, "--body", newBody)
	if edit.ExitCode != 0 {
		deps.Note(ctx, fmt.Sprintf("Failed to append plan docs to PR (non-fatal): %s",
			strings.TrimSpace(edit.Stderr)), "build", "github_pr", "plan_docs", "warning")
		return
	}
	deps.Note(ctx, "Plan docs appended to PR body", "build", "github_pr", "plan_docs")
}

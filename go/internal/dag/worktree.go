package dag

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"regexp"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"golang.org/x/sync/errgroup"
)

var seqPrefixRE = regexp.MustCompile(`^\d{2}-`)

// setupWorktrees creates git worktrees for parallel issue isolation, returning
// the active_issues list with worktree_path and branch_name injected. Ports
// _setup_worktrees (single-repo + multi-repo grouping by target_repo).
//
// A returned error propagates out of RunDAG (matching Python, where call_fn
// exceptions in the directly-awaited setup are not caught).
func setupWorktrees(
	ctx context.Context,
	dagState *schemas.DAGState,
	activeIssues []map[string]any,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	note noteFunc,
	buildID string,
) ([]map[string]any, error) {
	if note != nil {
		names := issueNames(activeIssues)
		note(fmt.Sprintf("Setting up worktrees for %s", pyStrList(names)),
			[]string{"execution", "worktree_setup", "start"})
	}

	// --- Single-repo path ---
	if dagState.WorkspaceManifest == nil {
		setup, err := dispatchWorkspaceSetup(
			ctx, callFn, nodeID, cfg,
			dagState.RepoPath, dagState.GitIntegrationBranch,
			activeIssues, dagState.WorktreesDir, dagState.ArtifactsDir,
			dagState.CurrentLevel, buildID, note,
		)
		if err != nil {
			return nil, err
		}
		if !asBool(setup["success"]) {
			if note != nil {
				note("Worktree setup failed — issues will run without isolation",
					[]string{"execution", "worktree_setup", "error"})
			}
			return activeIssues, nil
		}
		return enrichIssuesFromSetup(activeIssues, setup, dagState.GitIntegrationBranch), nil
	}

	// --- Multi-repo path: group issues by target_repo (insertion-ordered) ---
	manifest, err := manifestFromMap(dagState.WorkspaceManifest)
	if err != nil {
		return nil, err
	}
	byRepo := map[string][]map[string]any{}
	var byRepoOrder []string
	for _, issue := range activeIssues {
		repo := mapGetStr(issue, "target_repo", "")
		if repo == "" {
			repo = manifest.PrimaryRepoName
		}
		if _, seen := byRepo[repo]; !seen {
			byRepoOrder = append(byRepoOrder, repo)
		}
		byRepo[repo] = append(byRepo[repo], issue)
	}

	if note != nil {
		note(fmt.Sprintf("Multi-repo worktree setup: dispatching to %s", pyStrList(byRepoOrder)),
			[]string{"execution", "worktree_setup", "multi-repo"})
	}

	var allEnriched []map[string]any
	for _, repoName := range byRepoOrder {
		repoIssues := byRepo[repoName]
		wsRepo := findRepo(manifest, repoName)
		if wsRepo == nil {
			if note != nil {
				note(fmt.Sprintf("WARNING: target_repo '%s' not found in workspace manifest. "+
					"Issues %s will run without worktree isolation.", repoName, pyStrList(issueNames(repoIssues))),
					[]string{"execution", "worktree_setup", "warning"})
			}
			allEnriched = append(allEnriched, repoIssues...)
			continue
		}
		integrationBranch := mapGetStr(wsRepo.GitInitResult, "integration_branch", "")
		if integrationBranch == "" {
			if note != nil {
				note(fmt.Sprintf("WARNING: repo '%s' has no integration branch (git_init incomplete). "+
					"Issues %s will run without worktree isolation.", repoName, pyStrList(issueNames(repoIssues))),
					[]string{"execution", "worktree_setup", "warning"})
			}
			allEnriched = append(allEnriched, repoIssues...)
			continue
		}

		repoWorktreesDir := filepath.Join(wsRepo.AbsolutePath, ".worktrees")
		setup, err := dispatchWorkspaceSetup(
			ctx, callFn, nodeID, cfg,
			wsRepo.AbsolutePath, integrationBranch,
			repoIssues, repoWorktreesDir, dagState.ArtifactsDir,
			dagState.CurrentLevel, buildID, note,
		)
		if err != nil {
			return nil, err
		}
		if !asBool(setup["success"]) {
			allEnriched = append(allEnriched, repoIssues...)
			continue
		}
		allEnriched = append(allEnriched, enrichIssuesFromSetup(repoIssues, setup, integrationBranch)...)
	}

	if note != nil {
		note(fmt.Sprintf("Worktree setup complete: %d issues enriched", len(allEnriched)),
			[]string{"execution", "worktree_setup", "complete"})
	}
	return allEnriched, nil
}

// enrichIssuesFromSetup matches worktree setup results back to issues. Verbatim
// port of _enrich_issues_from_setup.
func enrichIssuesFromSetup(issues []map[string]any, setup map[string]any, integrationBranch string) []map[string]any {
	worktreeMap := map[string]map[string]any{}
	for _, w := range asMapSlice(setup["workspaces"]) {
		rawName := asStr(w["issue_name"])
		worktreeMap[rawName] = w
		stripped := seqPrefixRE.ReplaceAllString(rawName, "")
		if stripped != rawName {
			worktreeMap[stripped] = w
		}
	}

	enriched := make([]map[string]any, 0, len(issues))
	for _, issue := range issues {
		ws, ok := worktreeMap[asStr(issue["name"])]
		if ok {
			merged := copyIssue(issue)
			merged["worktree_path"] = ws["worktree_path"]
			merged["branch_name"] = ws["branch_name"]
			merged["integration_branch"] = integrationBranch
			enriched = append(enriched, merged)
		} else {
			enriched = append(enriched, issue)
		}
	}
	return enriched
}

// mergeLevelBranches merges completed branches into the integration branch,
// returning the MergeResult dict (or nil when nothing to merge). Ports
// _merge_level_branches (single-repo + multi-repo per-repo dispatch).
func mergeLevelBranches(
	ctx context.Context,
	dagState *schemas.DAGState,
	levelResult *schemas.LevelResult,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	issueByName map[string]map[string]any,
	fileConflicts []map[string]any,
	note noteFunc,
) (map[string]any, error) {
	// --- Single-repo path ---
	if dagState.WorkspaceManifest == nil {
		var completedBranches []map[string]any
		for _, r := range levelResult.Completed {
			if r.BranchName != "" {
				issueDesc := mapGetStr(issueByName[r.IssueName], "description", "")
				completedBranches = append(completedBranches, map[string]any{
					"branch_name":       r.BranchName,
					"issue_name":        r.IssueName,
					"result_summary":    r.ResultSummary,
					"files_changed":     r.FilesChanged,
					"issue_description": issueDesc,
				})
			}
		}
		if len(completedBranches) == 0 {
			return nil, nil
		}

		if note != nil {
			note(fmt.Sprintf("Merging %d branches: %s", len(completedBranches), pyStrList(branchNames(completedBranches))),
				[]string{"execution", "merge", "start"})
		}

		mergeKwargs := map[string]any{
			"repo_path":            dagState.RepoPath,
			"integration_branch":   dagState.GitIntegrationBranch,
			"branches_to_merge":    completedBranches,
			"file_conflicts":       fileConflicts,
			"prd_summary":          dagState.PRDSummary,
			"architecture_summary": dagState.ArchitectureSummary,
			"artifacts_dir":        dagState.ArtifactsDir,
			"level":                levelResult.LevelIndex,
			"model":                cfg.MergerModel(),
			"ai_provider":          cfg.AIProvider(),
		}

		mergeResult, err := dispatchMerge(
			ctx, callFn, nodeID, cfg,
			dagState.RepoPath, dagState.GitIntegrationBranch,
			completedBranches, mergeKwargs, levelResult.LevelIndex, note,
		)
		if err != nil {
			return nil, err
		}

		dagState.MergeResults = append(dagState.MergeResults, mergeResult)
		for _, b := range asStringSlice(mergeResult["merged_branches"]) {
			if !contains(dagState.MergedBranches, b) {
				dagState.MergedBranches = append(dagState.MergedBranches, b)
			}
		}
		for _, b := range asStringSlice(mergeResult["failed_branches"]) {
			if !contains(dagState.UnmergedBranches, b) {
				dagState.UnmergedBranches = append(dagState.UnmergedBranches, b)
			}
		}

		if note != nil {
			note(fmt.Sprintf("Merge complete: merged=%s, failed=%s",
				pyStrList(asStringSlice(mergeResult["merged_branches"])), pyStrList(asStringSlice(mergeResult["failed_branches"]))),
				[]string{"execution", "merge", "complete"})
		}
		return mergeResult, nil
	}

	// --- Multi-repo path: group by repo_name, one merger call per repo ---
	manifest, err := manifestFromMap(dagState.WorkspaceManifest)
	if err != nil {
		return nil, err
	}

	byRepo := map[string][]schemas.IssueResult{}
	var byRepoOrder []string
	for _, r := range levelResult.Completed {
		if r.BranchName != "" {
			repo := r.RepoName
			if repo == "" {
				repo = manifest.PrimaryRepoName
			}
			if _, seen := byRepo[repo]; !seen {
				byRepoOrder = append(byRepoOrder, repo)
			}
			byRepo[repo] = append(byRepo[repo], r)
		}
	}
	if len(byRepoOrder) == 0 {
		return nil, nil
	}

	if note != nil {
		note(fmt.Sprintf("Multi-repo merge: dispatching to %s", pyStrList(byRepoOrder)),
			[]string{"execution", "merge", "start"})
	}

	// Dispatch all repo merges concurrently (asyncio.gather(return_exceptions=True)).
	type mergeOutcome struct {
		result map[string]any
		err    error
	}
	outcomes := make([]mergeOutcome, len(byRepoOrder))
	g, gctx := errgroup.WithContext(ctx)
	for i, repoName := range byRepoOrder {
		i, repoName := i, repoName
		issueResults := byRepo[repoName]
		g.Go(func() error {
			res, cerr := callMergerForRepo(gctx, manifest, repoName, issueResults, dagState, callFn, nodeID, cfg, issueByName, fileConflicts, levelResult.LevelIndex)
			outcomes[i] = mergeOutcome{res, cerr}
			return nil
		})
	}
	_ = g.Wait()

	var lastGood map[string]any
	for i, repoName := range byRepoOrder {
		oc := outcomes[i]
		if oc.err != nil {
			if note != nil {
				note(fmt.Sprintf("Merge failed for repo '%s': %v", repoName, oc.err),
					[]string{"execution", "merge", "error"})
			}
			continue
		}
		result := oc.result
		withRepo := copyIssue(result)
		withRepo["repo_name"] = repoName
		dagState.MergeResults = append(dagState.MergeResults, withRepo)
		for _, b := range asStringSlice(result["merged_branches"]) {
			if !contains(dagState.MergedBranches, b) {
				dagState.MergedBranches = append(dagState.MergedBranches, b)
			}
		}
		for _, b := range asStringSlice(result["failed_branches"]) {
			if !contains(dagState.UnmergedBranches, b) {
				dagState.UnmergedBranches = append(dagState.UnmergedBranches, b)
			}
		}
		if asBool(result["success"]) {
			lastGood = result
		}
	}

	if note != nil {
		note(fmt.Sprintf("Multi-repo merge complete: repos=%s, merged=%s",
			pyStrList(byRepoOrder), pyStrList(dagState.MergedBranches)),
			[]string{"execution", "merge", "complete"})
	}
	return lastGood, nil
}

// callMergerForRepo invokes run_merger for a single repo. Ports the inner
// _call_merger_for_repo closure. Returns ({success:false,...}, nil) when the
// repo has no usable git_init_result (matching Python's early-return dicts).
func callMergerForRepo(
	ctx context.Context,
	manifest *schemas.WorkspaceManifest,
	repoName string,
	issueResults []schemas.IssueResult,
	dagState *schemas.DAGState,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	issueByName map[string]map[string]any,
	fileConflicts []map[string]any,
	levelIndex int,
) (map[string]any, error) {
	wsRepo := findRepo(manifest, repoName)
	if wsRepo == nil || wsRepo.GitInitResult == nil {
		return map[string]any{"success": false, "merged_branches": []any{}, "failed_branches": []any{}}, nil
	}
	integrationBranch := mapGetStr(wsRepo.GitInitResult, "integration_branch", "")
	if integrationBranch == "" {
		return map[string]any{"success": false, "merged_branches": []any{}, "failed_branches": []any{}}, nil
	}

	branchesToMerge := make([]map[string]any, 0, len(issueResults))
	for _, r := range issueResults {
		branchesToMerge = append(branchesToMerge, map[string]any{
			"branch_name":       r.BranchName,
			"issue_name":        r.IssueName,
			"result_summary":    r.ResultSummary,
			"files_changed":     r.FilesChanged,
			"issue_description": mapGetStr(issueByName[r.IssueName], "description", ""),
		})
	}

	mergeKwargs := map[string]any{
		"repo_path":            wsRepo.AbsolutePath,
		"integration_branch":   integrationBranch,
		"branches_to_merge":    branchesToMerge,
		"file_conflicts":       fileConflicts,
		"prd_summary":          dagState.PRDSummary,
		"architecture_summary": dagState.ArchitectureSummary,
		"artifacts_dir":        dagState.ArtifactsDir,
		"level":                levelIndex,
		"model":                cfg.MergerModel(),
		"ai_provider":          cfg.AIProvider(),
	}
	return dispatchMerge(
		ctx, callFn, nodeID, cfg,
		wsRepo.AbsolutePath, integrationBranch,
		branchesToMerge, mergeKwargs, levelIndex, nil,
	)
}

// runIntegrationTests runs integration tests after a merge if the merger
// requested it and it is enabled, returning the IntegrationTestResult dict (or
// nil when skipped). Ports _run_integration_tests.
func runIntegrationTests(
	ctx context.Context,
	dagState *schemas.DAGState,
	mergeResult map[string]any,
	levelResult *schemas.LevelResult,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	issueByName map[string]map[string]any,
	note noteFunc,
) (map[string]any, error) {
	if !asBool(mergeResult["needs_integration_test"]) {
		return nil, nil
	}
	if !cfg.EnableIntegrationTesting {
		return nil, nil
	}

	mergedSet := map[string]bool{}
	for _, b := range asStringSlice(mergeResult["merged_branches"]) {
		mergedSet[b] = true
	}
	var mergedBranches []map[string]any
	for _, r := range levelResult.Completed {
		if r.BranchName != "" && mergedSet[r.BranchName] {
			mergedBranches = append(mergedBranches, map[string]any{
				"branch_name":    r.BranchName,
				"issue_name":     r.IssueName,
				"result_summary": r.ResultSummary,
				"files_changed":  r.FilesChanged,
				"repo_name":      r.RepoName,
			})
		}
	}

	if note != nil {
		reposTouched := map[string]bool{}
		var reposOrder []string
		for _, b := range mergedBranches {
			if rn := asStr(b["repo_name"]); rn != "" && !reposTouched[rn] {
				reposTouched[rn] = true
				reposOrder = append(reposOrder, rn)
			}
		}
		label := ""
		if len(reposOrder) > 0 {
			label = fmt.Sprintf(" (repos: %s)", pySet(reposOrder))
		}
		note("Running integration tests"+label, []string{"execution", "integration_test", "start"})
	}

	// Determine the best repo_path for integration tests.
	integrationTestRepoPath := dagState.RepoPath
	if dagState.WorkspaceManifest != nil {
		reposWithMerges := map[string]bool{}
		var order []string
		for _, b := range mergedBranches {
			if rn := asStr(b["repo_name"]); rn != "" && !reposWithMerges[rn] {
				reposWithMerges[rn] = true
				order = append(order, rn)
			}
		}
		if len(order) == 1 {
			manifest, err := manifestFromMap(dagState.WorkspaceManifest)
			if err == nil {
				if wsRepo := findRepo(manifest, order[0]); wsRepo != nil && wsRepo.AbsolutePath != "" {
					integrationTestRepoPath = wsRepo.AbsolutePath
				}
			}
		}
	}

	var testResult map[string]any
	for attempt := 0; attempt < cfg.MaxIntegrationTestRetries+1; attempt++ {
		res, err := callFn(ctx, nodeID+".run_integration_tester", map[string]any{
			"repo_path":            integrationTestRepoPath,
			"integration_branch":   dagState.GitIntegrationBranch,
			"merged_branches":      mergedBranches,
			"prd_summary":          dagState.PRDSummary,
			"architecture_summary": dagState.ArchitectureSummary,
			"conflict_resolutions": mergeResult["conflict_resolutions"],
			"artifacts_dir":        dagState.ArtifactsDir,
			"level":                levelResult.LevelIndex,
			"model":                cfg.IntegrationTesterModel(),
			"ai_provider":          cfg.AIProvider(),
			"workspace_manifest":   dagState.WorkspaceManifest,
		})
		if err != nil {
			return nil, err
		}
		testResult = res
		if asBool(testResult["passed"]) {
			break
		}
		if note != nil && attempt < cfg.MaxIntegrationTestRetries {
			note(fmt.Sprintf("Integration test failed (attempt %d), retrying...", attempt+1),
				[]string{"execution", "integration_test", "retry"})
		}
	}

	if testResult != nil {
		dagState.IntegrationTestResults = append(dagState.IntegrationTestResults, testResult)
		if note != nil {
			status := "failed"
			if asBool(testResult["passed"]) {
				status = "passed"
			}
			note(fmt.Sprintf("Integration test %s: %s", status, mapGetStr(testResult, "summary", "")),
				[]string{"execution", "integration_test", "complete"})
		}
	}
	return testResult, nil
}

// cleanupWorktrees removes worktrees and cleans up branches after merge. Ports
// _cleanup_worktrees. Returns an error only for a fatal harness error (which
// propagates when the cleanup task is awaited).
func cleanupWorktrees(
	ctx context.Context,
	dagState *schemas.DAGState,
	branchesToClean []string,
	callFn coding.CallFn,
	nodeID string,
	note noteFunc,
	level int,
	model, aiProvider string,
	deterministicGit bool,
	completedResults []schemas.IssueResult,
) error {
	if len(branchesToClean) == 0 {
		return nil
	}
	if note != nil {
		note(fmt.Sprintf("Cleaning up %d worktrees", len(branchesToClean)),
			[]string{"execution", "worktree_cleanup", "start"})
	}

	cleanSet := map[string]bool{}
	for _, b := range branchesToClean {
		cleanSet[b] = true
	}

	// --- Multi-repo path: group by repo and clean per-repo ---
	if dagState.WorkspaceManifest != nil && len(completedResults) > 0 {
		manifest, err := manifestFromMap(dagState.WorkspaceManifest)
		if err != nil {
			return err
		}
		byRepo := map[string][]string{}
		var byRepoOrder []string
		for _, r := range completedResults {
			repo := r.RepoName
			if repo == "" {
				repo = manifest.PrimaryRepoName
			}
			if r.BranchName != "" && cleanSet[r.BranchName] {
				if _, seen := byRepo[repo]; !seen {
					byRepoOrder = append(byRepoOrder, repo)
				}
				byRepo[repo] = append(byRepo[repo], r.BranchName)
			}
		}
		for _, repoName := range byRepoOrder {
			wsRepo := findRepo(manifest, repoName)
			if wsRepo == nil {
				continue
			}
			repoWorktreesDir := filepath.Join(wsRepo.AbsolutePath, ".worktrees")
			if err := cleanupSingleRepo(ctx, callFn, nodeID, wsRepo.AbsolutePath, repoWorktreesDir,
				byRepo[repoName], dagState.ArtifactsDir, level, model, aiProvider, deterministicGit, note); err != nil {
				return err
			}
		}
		return nil
	}

	// --- Single-repo path ---
	return cleanupSingleRepo(ctx, callFn, nodeID, dagState.RepoPath, dagState.WorktreesDir,
		branchesToClean, dagState.ArtifactsDir, level, model, aiProvider, deterministicGit, note)
}

// cleanupSingleRepo cleans up worktrees for a single repo, retrying once on
// failure. Ports _cleanup_single_repo. A FatalHarnessError propagates; any other
// error is logged and retried.
func cleanupSingleRepo(
	ctx context.Context,
	callFn coding.CallFn,
	nodeID, repoPath, worktreesDir string,
	branchesToClean []string,
	artifactsDir string,
	level int,
	model, aiProvider string,
	deterministicGit bool,
	note noteFunc,
) error {
	if deterministicGit {
		result, err := fastCleanupWorktrees(repoPath, worktreesDir, branchesToClean)
		if err == nil {
			if note != nil {
				note(fmt.Sprintf("Worktree cleanup complete (deterministic): %s",
					pyStrList(asStringSlice(result["cleaned"]))),
					[]string{"execution", "worktree_cleanup", "fast_path"})
			}
			return nil
		}
		if note != nil {
			note(fmt.Sprintf("Deterministic cleanup failed (%v) — falling back to the cleanup agent", err),
				[]string{"execution", "worktree_cleanup", "fallback"})
		}
	}

	for attempt := 0; attempt < 2; attempt++ { // up to 1 retry
		result, err := callFn(ctx, nodeID+".run_workspace_cleanup", map[string]any{
			"repo_path":         repoPath,
			"worktrees_dir":     worktreesDir,
			"branches_to_clean": branchesToClean,
			"artifacts_dir":     artifactsDir,
			"level":             level,
			"model":             model,
			"ai_provider":       aiProvider,
		})
		if err != nil {
			var fhe *fatal.FatalHarnessError
			if errors.As(err, &fhe) {
				return err // fatal — propagate (matches `except FatalHarnessError: raise`)
			}
			if note != nil {
				note(fmt.Sprintf("Worktree cleanup error (attempt %d/2): %v", attempt+1, err),
					[]string{"execution", "worktree_cleanup", "error"})
			}
			continue
		}
		if asBool(result["success"]) {
			if note != nil {
				note(fmt.Sprintf("Worktree cleanup complete: %s", pyStrList(asStringSlice(result["cleaned"]))),
					[]string{"execution", "worktree_cleanup", "complete"})
			}
			return nil
		}
		if note != nil {
			note(fmt.Sprintf("Worktree cleanup returned success=false (attempt %d/2), cleaned=%s",
				attempt+1, pyStrList(asStringSlice(result["cleaned"]))),
				[]string{"execution", "worktree_cleanup", "warning"})
		}
	}

	if note != nil {
		note(fmt.Sprintf("Worktree cleanup failed after retries for: %s", pyStrList(branchesToClean)),
			[]string{"execution", "worktree_cleanup", "error"})
	}
	return nil
}

// initAllRepos runs git_init concurrently for all repos in workspace_manifest,
// then writes git_init_result back onto each repo. No-op when the manifest is
// nil (single-repo path). Ports _init_all_repos. Exceptions are non-fatal
// (asyncio.gather(return_exceptions=True)).
func initAllRepos(
	ctx context.Context,
	dagState *schemas.DAGState,
	callFn coding.CallFn,
	nodeID, gitModel, aiProvider, permissionMode, buildID string,
	note noteFunc,
) error {
	if dagState.WorkspaceManifest == nil {
		return nil // single-repo path: git_init already ran in build()
	}

	manifest, err := manifestFromMap(dagState.WorkspaceManifest)
	if err != nil {
		return err
	}

	if note != nil {
		var repoNames []string
		for _, r := range manifest.Repos {
			repoNames = append(repoNames, r.RepoName)
		}
		note(fmt.Sprintf("Initialising git for %d repos: %s", len(manifest.Repos), pyStrList(repoNames)),
			[]string{"execution", "init_all_repos", "start"})
	}

	type initOutcome struct {
		name   string
		result map[string]any
		err    error
	}
	outcomes := make([]initOutcome, len(manifest.Repos))
	g, gctx := errgroup.WithContext(ctx)
	for i := range manifest.Repos {
		i := i
		wsRepo := manifest.Repos[i]
		g.Go(func() error {
			res, cerr := callFn(gctx, nodeID+".run_git_init", map[string]any{
				"repo_path":       wsRepo.AbsolutePath,
				"goal":            "", // goal not needed for dependency repos
				"artifacts_dir":   dagState.ArtifactsDir,
				"model":           gitModel,
				"permission_mode": permissionMode,
				"ai_provider":     aiProvider,
				"build_id":        buildID,
			})
			outcomes[i] = initOutcome{name: wsRepo.RepoName, result: res, err: cerr}
			return nil
		})
	}
	_ = g.Wait()

	// Write results back onto the manifest repos.
	for i := range manifest.Repos {
		oc := outcomes[i]
		if oc.err != nil {
			if note != nil {
				note(fmt.Sprintf("git_init failed for a repo (non-fatal): %v", oc.err),
					[]string{"execution", "init_all_repos", "error"})
			}
			continue
		}
		// repo_map[name].git_init_result = git_init_dict
		for j := range manifest.Repos {
			if manifest.Repos[j].RepoName == oc.name {
				manifest.Repos[j].GitInitResult = oc.result
				break
			}
		}
	}

	// Replace dag_state manifest dict with the updated version.
	dagState.WorkspaceManifest = dumpToMap(manifest)

	if note != nil {
		note("git init complete for all repos", []string{"execution", "init_all_repos", "complete"})
	}
	return nil
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

// findRepo returns the WorkspaceRepo with the given name, or nil.
func findRepo(manifest *schemas.WorkspaceManifest, name string) *schemas.WorkspaceRepo {
	for i := range manifest.Repos {
		if manifest.Repos[i].RepoName == name {
			return &manifest.Repos[i]
		}
	}
	return nil
}

// copyIssue makes a shallow copy of an issue/result dict (for the {**issue, ...}
// spread pattern).
func copyIssue(m map[string]any) map[string]any {
	c := make(map[string]any, len(m)+3)
	for k, v := range m {
		c[k] = v
	}
	return c
}

func issueNames(issues []map[string]any) []string {
	out := make([]string, 0, len(issues))
	for _, i := range issues {
		out = append(out, mapGetStr(i, "name", "?"))
	}
	return out
}

func branchNames(branches []map[string]any) []string {
	out := make([]string, 0, len(branches))
	for _, b := range branches {
		out = append(out, asStr(b["branch_name"]))
	}
	return out
}

// pyStrList renders a []string the way Python renders a list of strings in an
// f-string — e.g. ['a', 'b'] — so note messages are byte-identical to Python.
func pyStrList(items []string) string {
	out := "["
	for i, n := range items {
		if i > 0 {
			out += ", "
		}
		out += "'" + n + "'"
	}
	return out + "]"
}

// pySet renders a []string as Python renders a set literal in an f-string —
// e.g. {'a', 'b'}. Order is the caller's insertion order (Python set order is
// non-deterministic, so exact parity is not required here; only used in a note).
func pySet(items []string) string {
	out := "{"
	for i, n := range items {
		if i > 0 {
			out += ", "
		}
		out += "'" + n + "'"
	}
	return out + "}"
}

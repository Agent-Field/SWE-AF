package dag

// gitfast.go ports swe_af/execution/git_fast_path.py — deterministic git
// fast-paths for the mechanical steps between levels (worktree setup,
// conflict-free merges, cleanup). The reasoner agents remain the fallback
// (and the conflict-resolution path for merges);
// ExecutionConfig.DeterministicGit=false restores the agent-driven path.
// Branch/worktree naming matches prompts/workspace.py exactly.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
)

// gitFastPathError signals the caller to fall back to the agent-driven path.
type gitFastPathError struct{ msg string }

func (e *gitFastPathError) Error() string { return e.msg }

func gitFastErrf(format string, args ...any) *gitFastPathError {
	return &gitFastPathError{msg: fmt.Sprintf(format, args...)}
}

func runGitCmd(repoPath string, args ...string) (string, string, int) {
	cmd := exec.Command("git", append([]string{"-C", repoPath}, args...)...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	code := 0
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			code = exitErr.ExitCode()
		} else {
			code = -1
		}
	}
	out := strings.TrimSpace(stdout.String())
	detail := strings.TrimSpace(stderr.String())
	if detail == "" {
		detail = out
	}
	if detail == "" && err != nil {
		detail = err.Error()
	}
	return out, detail, code
}

func gitIdentityArgs(repoPath string) []string {
	if out, _, code := runGitCmd(repoPath, "config", "user.email"); code == 0 && out != "" {
		return nil
	}
	return []string{"-c", "user.name=SWE-AF", "-c", "user.email=swe-af@agentfield.local"}
}

// fastBranchCore is the shared naming core: [<build_id>-]<NN>-<name>.
func fastBranchCore(issue map[string]any, buildID string) string {
	name := mapGetStr(issue, "name", "issue")
	seq := asInt(issue["sequence_number"])
	core := fmt.Sprintf("%02d-%s", seq, name)
	if buildID != "" {
		return buildID + "-" + core
	}
	return core
}

// fastSetupWorktrees ports git_fast_path.setup_worktrees. Returns the exact
// shape run_workspace_setup returns.
func fastSetupWorktrees(
	repoPath, integrationBranch string,
	issues []map[string]any,
	worktreesDir, buildID string,
) (map[string]any, error) {
	if integrationBranch == "" {
		return nil, gitFastErrf("no integration branch — cannot create worktrees")
	}
	if err := os.MkdirAll(worktreesDir, 0o755); err != nil {
		return nil, gitFastErrf("mkdir worktrees dir failed: %v", err)
	}

	workspaces := make([]any, 0, len(issues))
	for _, issue := range issues {
		core := fastBranchCore(issue, buildID)
		branch := "issue/" + core
		worktreePath := filepath.Join(worktreesDir, "issue-"+core)

		created := false
		lastDetail := ""
		for attempt := 1; attempt <= 3; attempt++ {
			// Fresh branch first; if the branch survived a prior attempt or a
			// resume, attach a worktree to it instead.
			_, detail, code := runGitCmd(repoPath, "worktree", "add", "-b", branch, worktreePath, integrationBranch)
			if code == 0 {
				created = true
				break
			}
			if _, _, vcode := runGitCmd(repoPath, "rev-parse", "--verify", branch); vcode == 0 {
				if _, statErr := os.Stat(worktreePath); statErr == nil {
					created = true // both already exist (resume) — reuse
					break
				}
				if _, _, acode := runGitCmd(repoPath, "worktree", "add", worktreePath, branch); acode == 0 {
					created = true
					break
				}
			}
			lastDetail = detail
			time.Sleep(time.Duration(attempt) * 300 * time.Millisecond)
		}
		if !created {
			return nil, gitFastErrf("worktree add failed for %s: %s", branch, lastDetail)
		}
		workspaces = append(workspaces, map[string]any{
			"issue_name":    mapGetStr(issue, "name", ""),
			"branch_name":   branch,
			"worktree_path": worktreePath,
		})
	}
	return map[string]any{"success": true, "workspaces": workspaces}, nil
}

// fastMergeBranches ports git_fast_path.merge_branches: sequential --no-ff
// merges; conflicts are aborted (integration branch stays clean) and reported
// in failed_branches. needs_integration_test only when >1 branch merged.
func fastMergeBranches(
	repoPath, integrationBranch string,
	branchNamesToMerge []string,
	level int,
) (map[string]any, error) {
	if _, detail, code := runGitCmd(repoPath, "checkout", integrationBranch); code != 0 {
		return nil, gitFastErrf("checkout %s failed: %s", integrationBranch, detail)
	}
	preMergeSHA, _, _ := runGitCmd(repoPath, "rev-parse", "HEAD")
	identity := gitIdentityArgs(repoPath)

	merged := []string{}
	failed := []string{}
	for _, branch := range branchNamesToMerge {
		args := append(append([]string{}, identity...),
			"merge", "--no-ff", branch, "-m", fmt.Sprintf("merge(level-%d): %s", level, branch))
		if _, _, code := runGitCmd(repoPath, args...); code == 0 {
			merged = append(merged, branch)
		} else {
			runGitCmd(repoPath, "merge", "--abort")
			failed = append(failed, branch)
		}
	}

	summary := fmt.Sprintf("Fast-path merged %d/%d branch(es)", len(merged), len(branchNamesToMerge))
	if len(failed) > 0 {
		summary += fmt.Sprintf("; conflicts need the merger agent: %s", pyStrList(failed))
	}
	rationale := "single branch merged; per-issue tests already covered it"
	if len(merged) > 1 {
		rationale = "multiple branches merged this level; they have not run together"
	}
	mergeSHA, _, _ := runGitCmd(repoPath, "rev-parse", "HEAD")
	return map[string]any{
		"success":                    len(failed) == 0,
		"merged_branches":            merged,
		"failed_branches":            failed,
		"conflict_resolutions":       []any{},
		"merge_commit_sha":           mergeSHA,
		"pre_merge_sha":              preMergeSHA,
		"needs_integration_test":     len(merged) > 1,
		"integration_test_rationale": rationale,
		"summary":                    summary,
	}, nil
}

// fastCleanupWorktrees ports git_fast_path.cleanup_worktrees. Best-effort per
// entry; worktree dir is the branch name with "/" -> "-". Errors when repoPath
// is not a git repository at all — the caller falls back to the agent path
// rather than reporting a silent no-op success.
func fastCleanupWorktrees(repoPath, worktreesDir string, branches []string) (map[string]any, error) {
	if _, _, code := runGitCmd(repoPath, "rev-parse", "--git-dir"); code != 0 {
		return nil, gitFastErrf("not a git repository: %s", repoPath)
	}
	cleaned := make([]string, 0, len(branches))
	for _, branch := range branches {
		worktreePath := filepath.Join(worktreesDir, strings.ReplaceAll(branch, "/", "-"))
		if _, _, code := runGitCmd(repoPath, "worktree", "remove", "--force", worktreePath); code != 0 {
			if _, err := os.Stat(worktreePath); err == nil {
				_ = os.RemoveAll(worktreePath)
			}
		}
		runGitCmd(repoPath, "branch", "-D", branch)
		cleaned = append(cleaned, branch)
	}
	runGitCmd(repoPath, "worktree", "prune")
	return map[string]any{"success": true, "cleaned": cleaned}, nil
}

// combineMergeResults ports git_fast_path.combine_merge_results.
func combineMergeResults(fast, agent map[string]any) map[string]any {
	merged := append([]string{}, asStringSlice(fast["merged_branches"])...)
	for _, b := range asStringSlice(agent["merged_branches"]) {
		if !contains(merged, b) {
			merged = append(merged, b)
		}
	}
	failed := []string{}
	for _, b := range asStringSlice(agent["failed_branches"]) {
		if !contains(merged, b) {
			failed = append(failed, b)
		}
	}
	mergeSHA := mapGetStr(agent, "merge_commit_sha", "")
	if mergeSHA == "" {
		mergeSHA = mapGetStr(fast, "merge_commit_sha", "")
	}
	conflictRes := agent["conflict_resolutions"]
	if conflictRes == nil {
		conflictRes = []any{}
	}
	return map[string]any{
		"success":                    len(failed) == 0,
		"merged_branches":            merged,
		"failed_branches":            failed,
		"conflict_resolutions":       conflictRes,
		"merge_commit_sha":           mergeSHA,
		"pre_merge_sha":              mapGetStr(fast, "pre_merge_sha", ""),
		"needs_integration_test":     true, // conflicts were resolved — always retest
		"integration_test_rationale": "merger agent resolved conflicts this level",
		"summary": fmt.Sprintf("%s | merger agent: %s",
			mapGetStr(fast, "summary", ""), mapGetStr(agent, "summary", "")),
	}
}

// dispatchWorkspaceSetup ports _dispatch_workspace_setup: deterministic
// worktree creation first, agent fallback on failure.
func dispatchWorkspaceSetup(
	ctx context.Context,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	repoPath, integrationBranch string,
	issues []map[string]any,
	worktreesDir, artifactsDir string,
	level int,
	buildID string,
	note noteFunc,
) (map[string]any, error) {
	if cfg.DeterministicGit {
		setup, err := fastSetupWorktrees(repoPath, integrationBranch, issues, worktreesDir, buildID)
		if err == nil {
			if note != nil {
				note(fmt.Sprintf("Worktrees created deterministically: %d (no agent call)",
					len(setup["workspaces"].([]any))),
					[]string{"execution", "worktree_setup", "fast_path"})
			}
			return setup, nil
		}
		if note != nil {
			note(fmt.Sprintf("Deterministic worktree setup failed (%v) — falling back to the workspace agent", err),
				[]string{"execution", "worktree_setup", "fallback"})
		}
	}
	return callFn(ctx, nodeID+".run_workspace_setup", map[string]any{
		"repo_path":          repoPath,
		"integration_branch": integrationBranch,
		"issues":             issues,
		"worktrees_dir":      worktreesDir,
		"artifacts_dir":      artifactsDir,
		"level":              level,
		"model":              cfg.GitModel(),
		"ai_provider":        cfg.AIProvider(),
		"build_id":           buildID,
	})
}

// dispatchMerge ports _dispatch_merge: deterministic --no-ff merges first, the
// merger agent handles only conflicted branches; with DeterministicGit off the
// agent merges everything (with the historical one-retry on failure).
func dispatchMerge(
	ctx context.Context,
	callFn coding.CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	repoPath, integrationBranch string,
	completedBranches []map[string]any,
	mergeKwargs map[string]any,
	level int,
	note noteFunc,
) (map[string]any, error) {
	if cfg.DeterministicGit {
		fast, err := fastMergeBranches(repoPath, integrationBranch, branchNames(completedBranches), level)
		if err != nil {
			if note != nil {
				note(fmt.Sprintf("Deterministic merge failed (%v) — falling back to the merger agent", err),
					[]string{"execution", "merge", "fallback"})
			}
		} else {
			failedSet := toStringSet(asStringSlice(fast["failed_branches"]))
			if len(failedSet) == 0 {
				if note != nil {
					note(fmt.Sprintf("Merged %d branch(es) deterministically (no agent call)",
						len(asStringSlice(fast["merged_branches"]))),
						[]string{"execution", "merge", "fast_path"})
				}
				return fast, nil
			}
			var conflicted []map[string]any
			for _, b := range completedBranches {
				if failedSet[mapGetStr(b, "branch_name", "")] {
					conflicted = append(conflicted, b)
				}
			}
			if note != nil {
				note(fmt.Sprintf("%d branch(es) conflict — merger agent takes over: %s",
					len(conflicted), pyStrList(branchNames(conflicted))),
					[]string{"execution", "merge", "fallback"})
			}
			agentKwargs := map[string]any{}
			for k, v := range mergeKwargs {
				agentKwargs[k] = v
			}
			agentKwargs["branches_to_merge"] = conflicted
			agentResult, err := callFn(ctx, nodeID+".run_merger", agentKwargs)
			if err != nil {
				return nil, err
			}
			return combineMergeResults(fast, agentResult), nil
		}
	}

	mergeResult, err := callFn(ctx, nodeID+".run_merger", mergeKwargs)
	if err != nil {
		return nil, err
	}
	// Retry once on failure (handles transient auth errors, network blips).
	if !asBool(mergeResult["success"]) && len(asStringSlice(mergeResult["failed_branches"])) > 0 {
		if note != nil {
			note("Merge failed, retrying once...", []string{"execution", "merge", "retry"})
		}
		mergeResult, err = callFn(ctx, nodeID+".run_merger", mergeKwargs)
		if err != nil {
			return nil, err
		}
	}
	return mergeResult, nil
}

func toStringSet(items []string) map[string]bool {
	s := make(map[string]bool, len(items))
	for _, it := range items {
		s[it] = true
	}
	return s
}

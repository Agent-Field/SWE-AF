package gitops

import (
	"context"
	"fmt"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
	gitprompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/gitops"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// run_repo_finalize
// ---------------------------------------------------------------------------

// repoFinalizeInput mirrors run_repo_finalize's signature
// (execution_agents.py:1409).
type repoFinalizeInput struct {
	RepoPath       string `json:"repo_path"`
	ArtifactsDir   string `json:"artifacts_dir"`
	Model          string `json:"model"`
	PermissionMode string `json:"permission_mode"`
	AIProvider     string `json:"ai_provider"`
	// BaseRef is the commit the run started from. The cleanup may remove only
	// tracked files that did not exist there (see finalizeguard.go).
	BaseRef string `json:"base_ref"`
}

// RunRepoFinalize cleans up the repository after verification — removes
// artifacts and fortifies .gitignore. Non-blocking: failure does not affect
// build success. Returns a RepoFinalizeResult dict.
func RunRepoFinalize(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[repoFinalizeInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, "Repo finalize starting", "repo_finalize", "start")

	scope, guarded := openFinalizeScope(ctx, in.RepoPath, in.BaseRef)
	taskPrompt := gitprompts.RepoFinalizeTaskPrompt(in.RepoPath, scope.created...)

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	model, err := resolveModel(in.Model, "git", provider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, model, gitprompts.RepoFinalizeSystemPrompt, in.RepoPath,
		[]string{"Bash", "Read", "Write", "Glob", "Grep"}, in.PermissionMode)

	val, ok, err := runRole[schemas.RepoFinalizeResult](ctx, deps, taskPrompt, opts, "repo_finalize", "Repo finalize agent failed")
	if err != nil {
		return nil, err
	}

	// The guard runs whatever the agent answered: a cleanup that deleted a
	// file and then failed to report is exactly the one to check.
	var restored []string
	if guarded {
		restored = scope.restore(ctx)
		if len(restored) > 0 {
			deps.App.Note(ctx, fmt.Sprintf(
				"Repo finalize removed or changed %d file(s) that existed before it ran; restored: %s",
				len(restored), strings.Join(restored, ", ")), "repo_finalize", "restored")
		}
	}

	if ok {
		res := *val
		applyRestored(&res, restored)
		deps.App.Note(ctx, fmt.Sprintf("Repo finalize complete: %d files removed, gitignore_updated=%s",
			len(res.FilesRemoved), pyBool(res.GitignoreUpdated)), "repo_finalize", "complete")
		return res, nil
	}

	res := schemas.RepoFinalizeResult{
		Success: false,
		Summary: "Repo finalize agent failed to produce a valid result.",
	}
	applyRestored(&res, restored)
	return res, nil
}

// applyRestored reports the guard's restorations on the result: the restored
// paths leave files_removed (they were not removed in the end), join
// files_restored, and are named in the summary.
func applyRestored(res *schemas.RepoFinalizeResult, restored []string) {
	if len(restored) == 0 {
		return
	}
	back := make(map[string]bool, len(restored))
	for _, p := range restored {
		back[p] = true
	}
	kept := res.FilesRemoved[:0:0]
	for _, p := range res.FilesRemoved {
		if !back[strings.TrimSuffix(p, "/")] && !restoredUnder(p, restored) {
			kept = append(kept, p)
		}
	}
	res.FilesRemoved = kept
	res.FilesRestored = restored
	res.Summary = strings.TrimSpace(res.Summary + " Restored files that existed before the cleanup and are not its to remove: " +
		strings.Join(restored, ", ") + ".")
}

// restoredUnder reports whether a removed directory entry (".codeaf/") held
// any restored path.
func restoredUnder(removed string, restored []string) bool {
	dir := strings.TrimSuffix(removed, "/") + "/"
	for _, p := range restored {
		if strings.HasPrefix(p, dir) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// run_github_pr
// ---------------------------------------------------------------------------

// githubPRInput mirrors run_github_pr's signature (execution_agents.py:1467).
type githubPRInput struct {
	RepoPath          string           `json:"repo_path"`
	IntegrationBranch string           `json:"integration_branch"`
	BaseBranch        string           `json:"base_branch"`
	Goal              string           `json:"goal"`
	BuildSummary      string           `json:"build_summary"`
	CompletedIssues   []map[string]any `json:"completed_issues"`
	AccumulatedDebt   []map[string]any `json:"accumulated_debt"`
	ArtifactsDir      string           `json:"artifacts_dir"`
	Model             string           `json:"model"`
	PermissionMode    string           `json:"permission_mode"`
	AIProvider        string           `json:"ai_provider"`
}

// RunGitHubPR pushes the integration branch and creates a PR (opened ready
// for review — the github_pr prompt forbids --draft) on GitHub.
// Returns a GitHubPRResult dict.
func RunGitHubPR(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[githubPRInput](input)
	if err != nil {
		return nil, err
	}

	// A BRANCH THAT CHANGES NO FILE OPENS NO PULL REQUEST, whatever commits it
	// carries (issue #309: two empty commits opened the 0-file PR #315). The
	// orchestrators check this before pushing; the role checks again so no
	// caller can open one.
	if v := nochange.Check(ctx, in.RepoPath, in.IntegrationBranch, in.BaseBranch); v.Known && v.Empty {
		deps.App.Note(ctx, nochange.Note, "github_pr", "skipped", "no_changes")
		return schemas.GitHubPRResult{Success: false, ErrorMessage: nochange.Note}, nil
	}

	deps.App.Note(ctx, fmt.Sprintf("GitHub PR: pushing %s and creating PR",
		in.IntegrationBranch), "github_pr", "start")

	taskPrompt := gitprompts.GitHubPRTaskPrompt(gitprompts.GitHubPROptions{
		RepoPath:          in.RepoPath,
		IntegrationBranch: in.IntegrationBranch,
		BaseBranch:        in.BaseBranch,
		Goal:              in.Goal,
		BuildSummary:      in.BuildSummary,
		CompletedIssues:   in.CompletedIssues,
		AccumulatedDebt:   in.AccumulatedDebt,
	})

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	model, err := resolveModel(in.Model, "git", provider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, model, gitprompts.GitHubPRSystemPrompt, in.RepoPath,
		[]string{"Bash", "Write"}, in.PermissionMode)

	val, ok, err := runRole[schemas.GitHubPRResult](ctx, deps, taskPrompt, opts, "github_pr", "GitHub PR agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("GitHub PR complete: %s", val.PRURL), "github_pr", "complete")
		return *val, nil
	}

	return schemas.GitHubPRResult{
		Success:      false,
		ErrorMessage: "GitHub PR agent failed to produce a valid result.",
	}, nil
}

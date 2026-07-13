package gitops

import (
	"context"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
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

	taskPrompt := gitprompts.RepoFinalizeTaskPrompt(in.RepoPath)

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.RepoFinalizeSystemPrompt, in.RepoPath,
		[]string{"Bash", "Read", "Write", "Glob", "Grep"}, in.PermissionMode)

	val, ok, err := runRole[schemas.RepoFinalizeResult](ctx, deps, taskPrompt, opts, "repo_finalize", "Repo finalize agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Repo finalize complete: %d files removed, gitignore_updated=%s",
			len(val.FilesRemoved), pyBool(val.GitignoreUpdated)), "repo_finalize", "complete")
		return *val, nil
	}

	return schemas.RepoFinalizeResult{
		Success: false,
		Summary: "Repo finalize agent failed to produce a valid result.",
	}, nil
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
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.GitHubPRSystemPrompt, in.RepoPath,
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

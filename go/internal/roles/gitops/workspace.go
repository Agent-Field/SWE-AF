package gitops

import (
	"context"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	gitprompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/gitops"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// run_git_init
// ---------------------------------------------------------------------------

// gitInitInput mirrors run_git_init's signature (execution_agents.py:599).
type gitInitInput struct {
	RepoPath       string `json:"repo_path"`
	Goal           string `json:"goal"`
	ArtifactsDir   string `json:"artifacts_dir"`
	Model          string `json:"model"`
	PermissionMode string `json:"permission_mode"`
	AIProvider     string `json:"ai_provider"`
	PreviousError  string `json:"previous_error"`
	BuildID        string `json:"build_id"`
}

// RunGitInit initializes the git repo and creates the integration branch.
//
// previous_error: when non-empty this is a retry attempt, and the error context
// is appended to the system prompt so the agent learns from the prior failure.
// Returns a GitInitResult dict; on parse failure returns a deterministic
// failure fallback.
func RunGitInit(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[gitInitInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, fmt.Sprintf("Git init starting for: %s", truncateRunes(in.Goal, 80)), "git_init", "start")

	taskPrompt := gitprompts.GitInitTaskPrompt(gitprompts.GitInitOptions{
		RepoPath: in.RepoPath,
		Goal:     in.Goal,
		BuildID:  in.BuildID,
	})

	// Build system prompt with error context if retrying.
	systemPrompt := gitprompts.GitInitSystemPrompt
	if in.PreviousError != "" {
		systemPrompt += "\n\n## IMPORTANT: Retry Context\n\n" +
			fmt.Sprintf("The previous attempt failed with error: '%s'\n\n", in.PreviousError) +
			"Please carefully review what went wrong and adjust your approach:\n" +
			"- Ensure you provide ALL required fields in the correct format\n" +
			"- Double-check your git commands are valid\n" +
			"- Verify the GitInitResult JSON structure is complete\n" +
			"- If the error indicates a parsing issue, ensure your output is valid JSON\n"
	}

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), systemPrompt, in.RepoPath,
		[]string{"Bash", "Write"}, in.PermissionMode)

	val, ok, err := runRole[schemas.GitInitResult](ctx, deps, taskPrompt, opts, "git_init", "Git init agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Git init complete: mode=%s, integration_branch=%s",
			val.Mode, val.IntegrationBranch), "git_init", "complete")
		return *val, nil
	}

	// Fallback: report failure.
	return schemas.GitInitResult{
		Mode:              "unknown",
		OriginalBranch:    "",
		IntegrationBranch: "",
		InitialCommitSHA:  "",
		Success:           false,
		ErrorMessage:      "Git init agent failed to produce a valid result.",
	}, nil
}

// ---------------------------------------------------------------------------
// run_workspace_setup
// ---------------------------------------------------------------------------

// workspaceSetupInput mirrors run_workspace_setup's signature
// (execution_agents.py:682).
type workspaceSetupInput struct {
	RepoPath          string           `json:"repo_path"`
	IntegrationBranch string           `json:"integration_branch"`
	Issues            []map[string]any `json:"issues"`
	WorktreesDir      string           `json:"worktrees_dir"`
	ArtifactsDir      string           `json:"artifacts_dir"`
	Level             int              `json:"level"`
	Model             string           `json:"model"`
	PermissionMode    string           `json:"permission_mode"`
	AIProvider        string           `json:"ai_provider"`
	BuildID           string           `json:"build_id"`
}

// workspaceSetupResult is the inline BaseModel run_workspace_setup returns:
// {workspaces: [WorkspaceInfo, ...], success: bool}.
type workspaceSetupResult struct {
	Workspaces []schemas.WorkspaceInfo `json:"workspaces"`
	Success    bool                    `json:"success"`
}

// RunWorkspaceSetup creates git worktrees for parallel issue isolation. Each
// worktree gets branch issue/{build_id}-{seq}-{name} (the prompt encodes the
// build-id prefixing). Returns {workspaces, success}.
func RunWorkspaceSetup(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[workspaceSetupInput](input)
	if err != nil {
		return nil, err
	}

	issueNames := make([]string, len(in.Issues))
	for i, issue := range in.Issues {
		issueNames[i] = mapStr(issue, "name", "?")
	}
	deps.App.Note(ctx, fmt.Sprintf("Workspace setup: creating %d worktrees for %s",
		len(in.Issues), pyListReprStrs(issueNames)), "workspace_setup", "start")

	taskPrompt := gitprompts.WorkspaceSetupTaskPrompt(gitprompts.WorkspaceSetupOptions{
		RepoPath:          in.RepoPath,
		IntegrationBranch: in.IntegrationBranch,
		Issues:            in.Issues,
		WorktreesDir:      in.WorktreesDir,
		BuildID:           in.BuildID,
	})

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.SetupSystemPrompt, in.RepoPath,
		[]string{"Bash", "Write"}, in.PermissionMode)

	val, ok, err := runRole[workspaceSetupResult](ctx, deps, taskPrompt, opts, "workspace_setup", "Workspace setup agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Workspace setup complete: %d worktrees created",
			len(val.Workspaces)), "workspace_setup", "complete")
		return *val, nil
	}

	return workspaceSetupResult{Workspaces: []schemas.WorkspaceInfo{}, Success: false}, nil
}

// ---------------------------------------------------------------------------
// run_workspace_cleanup
// ---------------------------------------------------------------------------

// workspaceCleanupInput mirrors run_workspace_cleanup's signature
// (execution_agents.py:897).
type workspaceCleanupInput struct {
	RepoPath        string   `json:"repo_path"`
	WorktreesDir    string   `json:"worktrees_dir"`
	BranchesToClean []string `json:"branches_to_clean"`
	ArtifactsDir    string   `json:"artifacts_dir"`
	Level           int      `json:"level"`
	Model           string   `json:"model"`
	PermissionMode  string   `json:"permission_mode"`
	AIProvider      string   `json:"ai_provider"`
}

// workspaceCleanupResult is the inline BaseModel run_workspace_cleanup returns:
// {success: bool, cleaned: list[str]} (cleaned defaults to []).
type workspaceCleanupResult struct {
	Success bool     `json:"success"`
	Cleaned []string `json:"cleaned"`
}

// RunWorkspaceCleanup removes worktrees and optionally deletes merged branches.
// Returns {success, cleaned}.
func RunWorkspaceCleanup(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[workspaceCleanupInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, fmt.Sprintf("Workspace cleanup: %d branches to clean",
		len(in.BranchesToClean)), "workspace_cleanup", "start")

	taskPrompt := gitprompts.WorkspaceCleanupTaskPrompt(gitprompts.WorkspaceCleanupOptions{
		RepoPath:        in.RepoPath,
		WorktreesDir:    in.WorktreesDir,
		BranchesToClean: in.BranchesToClean,
	})

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.CleanupSystemPrompt, in.RepoPath,
		[]string{"Bash", "Write"}, in.PermissionMode)

	val, ok, err := runRole[workspaceCleanupResult](ctx, deps, taskPrompt, opts, "workspace_cleanup", "Workspace cleanup agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Workspace cleanup complete: %d cleaned",
			len(val.Cleaned)), "workspace_cleanup", "complete")
		return *val, nil
	}

	return workspaceCleanupResult{Success: false, Cleaned: []string{}}, nil
}

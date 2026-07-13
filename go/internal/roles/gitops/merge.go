package gitops

import (
	"context"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	gitprompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/gitops"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// run_merger
// ---------------------------------------------------------------------------

// mergerInput mirrors run_merger's signature (execution_agents.py:749).
type mergerInput struct {
	RepoPath            string           `json:"repo_path"`
	IntegrationBranch   string           `json:"integration_branch"`
	BranchesToMerge     []map[string]any `json:"branches_to_merge"`
	FileConflicts       []map[string]any `json:"file_conflicts"`
	PRDSummary          string           `json:"prd_summary"`
	ArchitectureSummary string           `json:"architecture_summary"`
	ArtifactsDir        string           `json:"artifacts_dir"`
	Level               int              `json:"level"`
	Model               string           `json:"model"`
	PermissionMode      string           `json:"permission_mode"`
	AIProvider          string           `json:"ai_provider"`
}

// RunMerger merges level branches into the integration branch with AI conflict
// resolution. Returns a MergeResult dict; on parse failure the fallback marks
// every branch as failed.
func RunMerger(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[mergerInput](input)
	if err != nil {
		return nil, err
	}

	branchNames := make([]string, len(in.BranchesToMerge))
	for i, b := range in.BranchesToMerge {
		branchNames[i] = mapStr(b, "branch_name", "?")
	}
	deps.App.Note(ctx, fmt.Sprintf("Merger starting: %d branches %s",
		len(in.BranchesToMerge), pyListReprStrs(branchNames)), "merger", "start")

	taskPrompt := gitprompts.MergerTaskPrompt(gitprompts.MergerOptions{
		RepoPath:            in.RepoPath,
		IntegrationBranch:   in.IntegrationBranch,
		BranchesToMerge:     in.BranchesToMerge,
		FileConflicts:       in.FileConflicts,
		PRDSummary:          in.PRDSummary,
		ArchitectureSummary: in.ArchitectureSummary,
	})

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.MergerSystemPrompt, in.RepoPath,
		[]string{"Bash", "Read", "Write", "Glob", "Grep"}, in.PermissionMode)

	val, ok, err := runRole[schemas.MergeResult](ctx, deps, taskPrompt, opts, "merger", "Merger agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Merger complete: merged=%s, failed=%s, needs_test=%s",
			pyListReprStrs(val.MergedBranches), pyListReprStrs(val.FailedBranches),
			pyBool(val.NeedsIntegrationTest)), "merger", "complete")
		return *val, nil
	}

	return schemas.MergeResult{
		Success:              false,
		MergedBranches:       []string{},
		FailedBranches:       branchNames,
		NeedsIntegrationTest: false,
		Summary:              "Merger agent failed to produce a valid result.",
	}, nil
}

// ---------------------------------------------------------------------------
// run_integration_tester
// ---------------------------------------------------------------------------

// integrationTesterInput mirrors run_integration_tester's signature
// (execution_agents.py:822).
type integrationTesterInput struct {
	RepoPath            string           `json:"repo_path"`
	IntegrationBranch   string           `json:"integration_branch"`
	MergedBranches      []map[string]any `json:"merged_branches"`
	PRDSummary          string           `json:"prd_summary"`
	ArchitectureSummary string           `json:"architecture_summary"`
	ConflictResolutions []map[string]any `json:"conflict_resolutions"`
	ArtifactsDir        string           `json:"artifacts_dir"`
	Level               int              `json:"level"`
	Model               string           `json:"model"`
	PermissionMode      string           `json:"permission_mode"`
	AIProvider          string           `json:"ai_provider"`
	WorkspaceManifest   map[string]any   `json:"workspace_manifest"`
}

// RunIntegrationTester runs integration tests on merged code to verify
// cross-feature interactions. Returns an IntegrationTestResult dict.
func RunIntegrationTester(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[integrationTesterInput](input)
	if err != nil {
		return nil, err
	}

	deps.App.Note(ctx, fmt.Sprintf("Integration tester starting: %d merged branches",
		len(in.MergedBranches)), "integration_tester", "start")

	wsManifest := maybeWorkspaceManifest(in.WorkspaceManifest)

	taskPrompt := gitprompts.IntegrationTesterTaskPrompt(gitprompts.IntegrationTesterOptions{
		RepoPath:            in.RepoPath,
		IntegrationBranch:   in.IntegrationBranch,
		MergedBranches:      in.MergedBranches,
		PRDSummary:          in.PRDSummary,
		ArchitectureSummary: in.ArchitectureSummary,
		ConflictResolutions: in.ConflictResolutions,
		WorkspaceManifest:   wsManifest,
	})

	provider, err := resolveProvider(in.AIProvider)
	if err != nil {
		return nil, err
	}
	opts := roleOptions(provider, orDefault(in.Model, "sonnet"), gitprompts.IntegrationTesterSystemPrompt, in.RepoPath,
		[]string{"Bash", "Read", "Write", "Glob", "Grep"}, in.PermissionMode)

	val, ok, err := runRole[schemas.IntegrationTestResult](ctx, deps, taskPrompt, opts, "integration_tester", "Integration tester agent failed")
	if err != nil {
		return nil, err
	}
	if ok {
		deps.App.Note(ctx, fmt.Sprintf("Integration tester complete: passed=%s, %d/%d tests passed",
			pyBool(val.Passed), val.TestsPassed, val.TestsRun), "integration_tester", "complete")
		return *val, nil
	}

	return schemas.IntegrationTestResult{
		Passed:      false,
		TestsRun:    0,
		TestsPassed: 0,
		TestsFailed: 0,
		Summary:     "Integration tester agent failed to produce a valid result.",
	}, nil
}

package dag

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// checkpointPath returns the path to the checkpoint file, or "" when there is
// no artifacts_dir. Ports _checkpoint_path.
func checkpointPath(dagState *schemas.DAGState) string {
	if dagState.ArtifactsDir == "" {
		return ""
	}
	return filepath.Join(dagState.ArtifactsDir, "execution", "checkpoint.json")
}

// saveCheckpoint persists DAGState to a checkpoint file for crash recovery.
// Ports _save_checkpoint: a no-op when there is no artifacts_dir; otherwise
// writes <artifacts>/execution/checkpoint.json with indent=2 (matching Python's
// json.dump(..., indent=2, default=str)). The write is not atomic — Python's is
// a plain open("w")+json.dump, so this matches byte-for-byte semantics.
func saveCheckpoint(dagState *schemas.DAGState, note noteFunc) {
	path := checkpointPath(dagState)
	if path == "" {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	b, err := json.MarshalIndent(dagState, "", "  ")
	if err != nil {
		return
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		return
	}
	if note != nil {
		note("Checkpoint saved: level="+itoa(dagState.CurrentLevel), []string{"execution", "checkpoint"})
	}
}

// loadCheckpoint loads DAGState from a checkpoint file, or returns nil when not
// found. Ports _load_checkpoint. It round-trips a checkpoint written by either
// the Python or Go executor (DAGState.UnmarshalJSON seeds max_replans=2).
func loadCheckpoint(artifactsDir string) *schemas.DAGState {
	path := filepath.Join(artifactsDir, "execution", "checkpoint.json")
	b, err := os.ReadFile(path)
	if err != nil {
		return nil // not found (or unreadable) -> None
	}
	var state schemas.DAGState
	if err := json.Unmarshal(b, &state); err != nil {
		return nil
	}
	return &state
}

// initDAGState extracts a DAGState from a PlanResult dict, populating all
// artifact paths, plan-context summaries, and issue/level data. Optionally
// populates git fields from gitConfig. Verbatim port of _init_dag_state.
func initDAGState(planResult map[string]any, repoPath string, gitConfig map[string]any, buildID string) *schemas.DAGState {
	artifactsDir := mapGetStr(planResult, "artifacts_dir", "")

	// Artifact paths.
	var prdPath, architecturePath, issuesDir string
	if artifactsDir != "" {
		prdPath = filepath.Join(artifactsDir, "plan", "prd.md")
		architecturePath = filepath.Join(artifactsDir, "plan", "architecture.md")
		issuesDir = filepath.Join(artifactsDir, "plan", "issues")
	}

	// PRD summary: validated_description + acceptance criteria.
	prd := asMap(planResult["prd"])
	prdSummaryParts := []string{mapGetStr(prd, "validated_description", "")}
	ac := asStringSlice(prd["acceptance_criteria"])
	if len(ac) > 0 {
		prdSummaryParts = append(prdSummaryParts, "\nAcceptance Criteria:")
		for _, c := range ac {
			prdSummaryParts = append(prdSummaryParts, "- "+c)
		}
	}
	prdSummary := strings.Join(prdSummaryParts, "\n")

	// Architecture summary.
	architecture := asMap(planResult["architecture"])
	architectureSummary := mapGetStr(architecture, "summary", "")

	// Issues and levels. Issues arrive as dicts already; coerce to
	// []map[string]any (the Python isinstance/model_dump branch is a no-op here
	// because the plan result is a JSON dict by the time it reaches Go).
	allIssues := asMapSlice(planResult["issues"])
	if allIssues == nil {
		allIssues = []map[string]any{}
	}
	levels := coerceLevels(planResult["levels"])

	state := &schemas.DAGState{
		RepoPath:            repoPath,
		ArtifactsDir:        artifactsDir,
		PRDPath:             prdPath,
		ArchitecturePath:    architecturePath,
		IssuesDir:           issuesDir,
		OriginalPlanSummary: mapGetStr(planResult, "rationale", ""),
		PRDSummary:          prdSummary,
		ArchitectureSummary: architectureSummary,
		AllIssues:           allIssues,
		Levels:              levels,
		BuildID:             buildID,
		MaxReplans:          2, // Pydantic default (overwritten by run_dag from config).
	}

	// Git fields (populated when the git workflow is active).
	if gitConfig != nil {
		state.GitIntegrationBranch = mapGetStr(gitConfig, "integration_branch", "")
		state.GitOriginalBranch = mapGetStr(gitConfig, "original_branch", "")
		state.GitInitialCommit = mapGetStr(gitConfig, "initial_commit_sha", "")
		state.GitMode = mapGetStr(gitConfig, "mode", "")
		state.WorktreesDir = filepath.Join(repoPath, ".worktrees")
	}

	return state
}

// coerceLevels converts a plan_result "levels" value ([][]string, or []any of
// []any) into [][]string, preserving order.
func coerceLevels(v any) [][]string {
	switch t := v.(type) {
	case [][]string:
		return t
	case []any:
		out := make([][]string, 0, len(t))
		for _, lvl := range t {
			out = append(out, asStringSlice(lvl))
		}
		return out
	default:
		return [][]string{}
	}
}

// itoa is a tiny strconv.Itoa shim to keep the imports minimal at call sites.
func itoa(n int) string {
	// Local implementation avoids importing strconv solely for one call in the
	// note message; behaviour is identical for the small non-negative ints used
	// as level indices.
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

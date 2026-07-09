// This file (resume.go) ports the resume_build reasoner from
// swe_af/app.py:2081-2134: reload the last DAG checkpoint, reconstruct the
// minimal plan_result the executor needs, and re-invoke execute with
// resume=true. The node-wiring wave registers ResumeBuildHandler under the exact
// Python name "resume_build".
package orch

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// ResumeBuildHandler satisfies the orchestrator Handler shape.
var _ Handler = ResumeBuildHandler

// ResumeBuildHandler resumes a crashed build from the last checkpoint. Ports
// resume_build (app.py:2082): load <repo>/<artifacts>/execution/checkpoint.json,
// reconstruct plan_result from the checkpoint's DAGState, and call execute with
// resume=true. Returns the raw execute envelope unchanged (Python returns the
// app.call result WITHOUT unwrapping).
func ResumeBuildHandler(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	repoPath := mapStr(input, "repo_path", "")
	artifactsDir := mapStr(input, "artifacts_dir", ".artifacts")

	absRepo, err := filepath.Abs(repoPath)
	if err != nil {
		absRepo = repoPath
	}
	base := filepath.Join(absRepo, artifactsDir)
	planPath := filepath.Join(base, "execution", "checkpoint.json")

	if _, statErr := os.Stat(planPath); statErr != nil {
		return nil, fmt.Errorf("No checkpoint found at %s. Cannot resume.", planPath)
	}

	raw, err := os.ReadFile(planPath)
	if err != nil {
		return nil, fmt.Errorf("No checkpoint found at %s. Cannot resume.", planPath)
	}
	var checkpoint map[string]any
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		return nil, err
	}

	// Reconstruct plan_result exactly as app.py:2112-2121. The PRD / architecture
	// / review are not needed for resume — the DAGState carries the issue graph.
	planResult := map[string]any{
		"prd":            map[string]any{},
		"architecture":   map[string]any{},
		"review":         map[string]any{},
		"issues":         any0(checkpoint["all_issues"]),
		"levels":         any0(checkpoint["levels"]),
		"file_conflicts": []any{},
		"artifacts_dir":  mapStr(checkpoint, "artifacts_dir", base),
		"rationale":      mapStr(checkpoint, "original_plan_summary", ""),
	}

	deps.Note(ctx, "Resuming build from checkpoint", "build", "resume")

	// Python returns the raw app.call result (no _unwrap), so use CallRaw.
	result, err := deps.CallRaw(ctx, "execute", map[string]any{
		"plan_result": planResult,
		"repo_path":   repoPath,
		"config":      input["config"],
		"git_config":  input["git_config"],
		"resume":      true,
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

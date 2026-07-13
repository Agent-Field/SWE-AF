package orch

import (
	"context"
	"encoding/json"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dag"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// runDAG is the dag.RunDAG seam. Production runs the real DAG executor; tests
// override this var to capture the arguments execute forwards (config, resume,
// git config, workspace manifest, execute_fn) and to return a canned DAGState
// without spinning up the whole engine. Mirrors the Python tests that patch
// swe_af.execution.dag_executor.run_dag.
var runDAG = dag.RunDAG

// executeInput mirrors the Python execute() signature (param names + defaults):
//
//	execute(plan_result, repo_path, execute_fn_target="", config=None,
//	        git_config=None, resume=False, build_id="", workspace_manifest=None)
//
// The async API body binds request kwargs by these exact JSON keys. Optional
// object params are map[string]any so None/absent decodes to nil (the Python
// default), preserving the single-repo backward-compat path for
// workspace_manifest and the "no overrides" path for config/git_config.
type executeInput struct {
	PlanResult        map[string]any `json:"plan_result"`
	RepoPath          string         `json:"repo_path"`
	ExecuteFnTarget   string         `json:"execute_fn_target"`
	Config            map[string]any `json:"config"`
	GitConfig         map[string]any `json:"git_config"`
	Resume            bool           `json:"resume"`
	BuildID           string         `json:"build_id"`
	WorkspaceManifest map[string]any `json:"workspace_manifest"`
}

// ExecuteHandler is the "execute" reasoner: a thin wrapper that builds an
// ExecutionConfig from the config dict, wires the control-plane-routed call_fn
// (and, when an external coder target is set, an execute_fn closing over it),
// invokes dag.RunDAG with the full Python option set, and returns the final
// DAGState.model_dump() map. Ports execute() (app.py:1625-1682).
//
// The node-wiring wave (T6.2) registers this handler under the exact Python
// name "execute".
func ExecuteHandler(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := executeInput{}
	if raw, err := json.Marshal(input); err == nil {
		_ = json.Unmarshal(raw, &in)
	}

	// exec_config = ExecutionConfig(**config) if config else ExecutionConfig().
	// LoadExecutionConfig(nil) and LoadExecutionConfig({}) both yield defaults,
	// so an absent/empty config dict reproduces the bare ExecutionConfig() path.
	execConfig, err := config.LoadExecutionConfig(in.Config)
	if err != nil {
		return nil, err
	}

	// call_fn = app.call (unwrapped) — used by the built-in coding loop and the
	// reasoner-driven gates inside run_dag.
	callFn := deps.NewCallFn()

	// execute_fn_target handling: when a remote coder target is supplied, run_dag
	// takes the external-executor path. The closure calls the target directly
	// (full "<node>.<reasoner>" address, NOT node-local) with the issue dict and
	// the DAG state's repo_path, mirroring:
	//   await app.call(execute_fn_target, issue=issue, repo_path=dag_state.repo_path)
	// callFn already dispatches to the raw target and unwraps the envelope, so it
	// is the correct primitive for an external (non-node-local) call.
	var executeFn dag.ExecuteFn
	if in.ExecuteFnTarget != "" {
		target := in.ExecuteFnTarget
		executeFn = func(ctx context.Context, issue map[string]any, dagState *schemas.DAGState) (map[string]any, error) {
			return callFn(ctx, target, map[string]any{
				"issue":     issue,
				"repo_path": dagState.RepoPath,
			})
		}
	}

	opts := []dag.Option{
		dag.WithNoteFn(deps.NewNoteFn(ctx)),
		dag.WithGitConfig(in.GitConfig),
		dag.WithResume(in.Resume),
		dag.WithBuildID(in.BuildID),
		dag.WithWorkspaceManifest(in.WorkspaceManifest),
	}
	if executeFn != nil {
		opts = append(opts, dag.WithExecuteFn(executeFn))
	}

	state, err := runDAG(ctx, in.PlanResult, in.RepoPath, callFn, deps.NodeID, execConfig, opts...)
	if err != nil {
		return nil, err
	}

	// return state.model_dump()
	return dumpToMap(state), nil
}

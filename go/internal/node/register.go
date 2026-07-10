package node

// register.go wires every reasoner onto the agent by its exact Python name
// (design §8). The two entry points mirror the two Python nodes:
//
//   - RegisterPlanner mounts swe_af.app: the 5 orchestrators (build, plan,
//     execute, resolve, resume_build) plus the 25 role reasoners from
//     swe_af.reasoners.router.
//   - RegisterFast mounts swe_af.fast.app: the 4 fast reasoners (build,
//     fast_plan_tasks, fast_execute_tasks, fast_verify) plus the SAME 25 role
//     reasoners — fast/app.py:39 does app.include_router(_execution_router), so
//     the seven thin wrappers (run_git_init, run_coder, run_verifier,
//     run_repo_finalize, run_github_pr, run_ci_watcher, run_ci_fixer) are just
//     those role names, backed by the full-pipeline role handlers (fast.Wrappers
//     is the identity delegation map that documents this).
//
// Tags: the Go port registers under a distinct identity from the Python node
// (swe-planner-go / swe-fast-go) so both stacks can run against one control
// plane. Role reasoners carry ["swe-planner-go"] on BOTH nodes — mirroring the
// Python structure where they are registered through the swe-planner-tagged
// AgentRouter, but grouped under the Go node's -go identity. The four fast-node
// reasoners carry ["swe-fast-go"] (Python: fast_router tags=["swe-fast"]). The
// five orchestrators carry ["swe-planner-go"] to group them with the node in the
// control-plane UI (design §8).

import (
	"context"
	"encoding/json"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/orch"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/advisor"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/ci"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/gitops"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/planning"

	"github.com/Agent-Field/SWE-AF/go/internal/fast"
)

const (
	tagPlanner = "swe-planner-go"
	tagFast    = "swe-fast-go"
)

// RegisterPlanner registers the full swe-planner surface: 25 role reasoners +
// 5 orchestrators (30 total). Ports swe_af/app.py.
func (n *Node) RegisterPlanner() {
	n.registerRoles()
	n.registerOrchestrators()
}

// RegisterFast registers the swe-fast surface: the same 25 role reasoners + the
// 4 fast reasoners (29 total). Ports swe_af/fast/app.py. It deliberately does
// NOT register the orchestrators — fast/app.py only defines its own build.
func (n *Node) RegisterFast() {
	n.registerRoles()
	n.registerFastReasoners()
}

// ---------------------------------------------------------------------------
// Role reasoners (identical on both nodes)
// ---------------------------------------------------------------------------

// registerRoles wires the 25 execution/planning role reasoners, each backed by
// its package handler and threaded with the Deps built from the agent. All are
// tagged ["swe-planner-go"] (Python groups them under the swe-planner router).
func (n *Node) registerRoles() {
	tag := agent.WithReasonerTags(tagPlanner)

	planningDeps := &planning.Deps{
		Harness:          n.App,
		App:              n.App,
		Pauser:           n.App,
		Hax:              n.hax,
		NodeID:           n.NodeID,
		AgentFieldServer: n.AgentFieldServer,
	}
	for name, h := range planning.Handlers() {
		regHandler(n, name, planningDeps, h, tag)
	}

	codingDeps := &coding.Deps{Harness: n.App, AI: n.App, Note: n.App}
	for name, h := range coding.Handlers() {
		regHandler(n, name, codingDeps, h, tag)
	}

	gitopsDeps := &gitops.Deps{App: n.App}
	for name, h := range gitops.Handlers() {
		regHandler(n, name, gitopsDeps, h, tag)
	}

	advisorDeps := &advisor.Deps{
		Harness:          n.App,
		App:              n.App,
		Pauser:           n.App,
		BuildHaxClient:   hitl.BuildHaxClientFromEnv,
		NodeID:           n.NodeID,
		AgentFieldServer: n.AgentFieldServer,
	}
	for name, h := range advisor.Handlers() {
		regHandler(n, name, advisorDeps, h, tag)
	}

	ciDeps := &ci.Deps{App: n.App}
	for name, h := range ci.Handlers() {
		regHandler(n, name, ciDeps, h, tag)
	}
}

// ---------------------------------------------------------------------------
// Orchestrators (swe-planner only)
// ---------------------------------------------------------------------------

// registerOrchestrators wires build, plan, execute, resolve and resume_build.
// The CI-gate and plan-approval gate seams are set on the shared orch.Deps so
// build/resolve drive them (RunCIGate / PlanApprovalGate); the approval client
// provider is wired in BuildAgent.
func (n *Node) registerOrchestrators() {
	deps := &orch.Deps{
		App:              n.App,
		NodeID:           n.NodeID,
		AgentFieldServer: n.AgentFieldServer,
		Token:            n.Token,
		CIGate:           orch.RunCIGate,
		ApprovalGate:     orch.PlanApprovalGate,
	}

	handlers := orch.Handlers() // {"build": Build}
	orch.RegisterPlan(handlers) // adds {"plan": Plan}
	handlers["execute"] = orch.ExecuteHandler
	handlers["resolve"] = orch.ResolveHandler
	handlers["resume_build"] = orch.ResumeBuildHandler

	// Python registers the orchestrators via @app.reasoner() with NO tags
	// (only router-registered roles carry tags) — keep the registration
	// payload identical.
	for name, h := range handlers {
		var opts []agent.ReasonerOption
		if s, ok := orchestratorSchemas[name]; ok {
			opts = append(opts, agent.WithInputSchema(s))
		}
		regHandler(n, name, deps, h, opts...)
	}
}

// ---------------------------------------------------------------------------
// Fast reasoners (swe-fast only)
// ---------------------------------------------------------------------------

// registerFastReasoners wires the fast node's four first-class reasoners.
func (n *Node) registerFastReasoners() {
	deps := &fast.Deps{
		Harness: n.App,
		Call:    newCallFn(n.App),
		Note:    n.App,
		NodeID:  n.NodeID,
	}

	// Python tags: fast_plan_tasks/fast_execute_tasks/fast_verify come from
	// fast_router (tags=["swe-fast"]); the fast `build` is @app.reasoner()
	// with NO tags. Mirror that exactly.
	tag := agent.WithReasonerTags(tagFast)
	for name, h := range fast.Handlers() {
		var opts []agent.ReasonerOption
		if name != "build" {
			opts = append(opts, tag)
		}
		if s, ok := fastSchemas[name]; ok {
			opts = append(opts, agent.WithInputSchema(s))
		}
		regHandler(n, name, deps, h, opts...)
	}
}

// ---------------------------------------------------------------------------
// Registration helper
// ---------------------------------------------------------------------------

// regHandler adapts a package handler (func(ctx, *Deps, input) (any, error)) to
// the SDK's HandlerFunc (func(ctx, input) (any, error)) by capturing deps, then
// registers it under name and records the name on the node. D is inferred from
// deps; the package Handler types are assignable to the parameter's unnamed func
// type.
func regHandler[D any](
	n *Node,
	name string,
	deps *D,
	h func(context.Context, *D, map[string]any) (any, error),
	opts ...agent.ReasonerOption,
) {
	n.registered = append(n.registered, name)
	n.App.RegisterReasoner(name, func(ctx context.Context, input map[string]any) (any, error) {
		return h(ctx, deps, input)
	}, opts...)
}

// ---------------------------------------------------------------------------
// Input schemas — derived from the Python reasoner signatures so the
// control-plane UI reasoner cards show the real fields (the SDK default is a
// bare {"type":"object","additionalProperties":true} with no properties). Each
// keeps additionalProperties:true so the async API body stays byte-compatible
// with Python (extra keys are still accepted).
// ---------------------------------------------------------------------------

func schema(raw string) json.RawMessage { return json.RawMessage(raw) }

// orchestratorSchemas maps the 5 orchestrator names to their input schemas.
var orchestratorSchemas = map[string]json.RawMessage{
	// build(goal, repo_path="", repo_url="", artifacts_dir=".artifacts",
	//       additional_context="", config=None, execute_fn_target="",
	//       max_turns=0, permission_mode="", enable_learning=False)
	"build": schema(`{"type":"object","additionalProperties":true,"required":["goal"],"properties":{` +
		`"goal":{"type":"string"},"repo_path":{"type":"string"},"repo_url":{"type":"string"},` +
		`"artifacts_dir":{"type":"string"},"additional_context":{"type":"string"},"config":{"type":"object"},` +
		`"execute_fn_target":{"type":"string"},"max_turns":{"type":"integer"},"permission_mode":{"type":"string"},` +
		`"enable_learning":{"type":"boolean"}}}`),

	// plan(goal, repo_path, artifacts_dir=".artifacts", additional_context="",
	//      max_review_iterations=2, pm_model=None, architect_model=None,
	//      tech_lead_model=None, sprint_planner_model=None, issue_writer_model=None,
	//      permission_mode="", ai_provider=None, workspace_manifest=None)
	"plan": schema(`{"type":"object","additionalProperties":true,"required":["goal","repo_path"],"properties":{` +
		`"goal":{"type":"string"},"repo_path":{"type":"string"},"artifacts_dir":{"type":"string"},` +
		`"additional_context":{"type":"string"},"max_review_iterations":{"type":"integer"},` +
		`"pm_model":{"type":"string"},"architect_model":{"type":"string"},"tech_lead_model":{"type":"string"},` +
		`"sprint_planner_model":{"type":"string"},"issue_writer_model":{"type":"string"},` +
		`"permission_mode":{"type":"string"},"ai_provider":{"type":"string"},"workspace_manifest":{"type":"object"}}}`),

	// execute(plan_result, repo_path, execute_fn_target="", config=None,
	//         git_config=None, resume=False, build_id="", workspace_manifest=None)
	"execute": schema(`{"type":"object","additionalProperties":true,"required":["plan_result","repo_path"],"properties":{` +
		`"plan_result":{"type":"object"},"repo_path":{"type":"string"},"execute_fn_target":{"type":"string"},` +
		`"config":{"type":"object"},"git_config":{"type":"object"},"resume":{"type":"boolean"},` +
		`"build_id":{"type":"string"},"workspace_manifest":{"type":"object"}}}`),

	// resolve(pr_url, pr_number, repo_url, head_branch, base_branch="main",
	//         ci_failures=None, review_comments=None, goal="", additional_context="",
	//         config=None)
	"resolve": schema(`{"type":"object","additionalProperties":true,"required":["pr_url","pr_number","repo_url","head_branch"],"properties":{` +
		`"pr_url":{"type":"string"},"pr_number":{"type":"integer"},"repo_url":{"type":"string"},` +
		`"head_branch":{"type":"string"},"base_branch":{"type":"string"},"ci_failures":{"type":"array"},` +
		`"review_comments":{"type":"array"},"goal":{"type":"string"},"additional_context":{"type":"string"},` +
		`"config":{"type":"object"}}}`),

	// resume_build(repo_path, artifacts_dir=".artifacts", config=None, git_config=None)
	"resume_build": schema(`{"type":"object","additionalProperties":true,"required":["repo_path"],"properties":{` +
		`"repo_path":{"type":"string"},"artifacts_dir":{"type":"string"},"config":{"type":"object"},` +
		`"git_config":{"type":"object"}}}`),
}

// fastSchemas maps the 4 fast reasoner names to their input schemas.
var fastSchemas = map[string]json.RawMessage{
	// build(goal, repo_path="", repo_url="", artifacts_dir=".artifacts",
	//       additional_context="", config=None)
	"build": schema(`{"type":"object","additionalProperties":true,"required":["goal"],"properties":{` +
		`"goal":{"type":"string"},"repo_path":{"type":"string"},"repo_url":{"type":"string"},` +
		`"artifacts_dir":{"type":"string"},"additional_context":{"type":"string"},"config":{"type":"object"}}}`),

	// fast_plan_tasks(goal, repo_path, max_tasks=10, pm_model="haiku",
	//                 permission_mode="", ai_provider="claude",
	//                 additional_context="", artifacts_dir="")
	"fast_plan_tasks": schema(`{"type":"object","additionalProperties":true,"required":["goal","repo_path"],"properties":{` +
		`"goal":{"type":"string"},"repo_path":{"type":"string"},"max_tasks":{"type":"integer"},` +
		`"pm_model":{"type":"string"},"permission_mode":{"type":"string"},"ai_provider":{"type":"string"},` +
		`"additional_context":{"type":"string"},"artifacts_dir":{"type":"string"}}}`),

	// fast_execute_tasks(tasks, repo_path, coder_model="haiku", permission_mode="",
	//                    ai_provider="claude", task_timeout_seconds=300,
	//                    artifacts_dir="", agent_max_turns=50)
	"fast_execute_tasks": schema(`{"type":"object","additionalProperties":true,"required":["tasks","repo_path"],"properties":{` +
		`"tasks":{"type":"array"},"repo_path":{"type":"string"},"coder_model":{"type":"string"},` +
		`"permission_mode":{"type":"string"},"ai_provider":{"type":"string"},"task_timeout_seconds":{"type":"integer"},` +
		`"artifacts_dir":{"type":"string"},"agent_max_turns":{"type":"integer"}}}`),

	// fast_verify(prd, repo_path, task_results, verifier_model="sonnet",
	//             permission_mode="", ai_provider="claude", artifacts_dir="")
	"fast_verify": schema(`{"type":"object","additionalProperties":true,"required":["prd","repo_path","task_results"],"properties":{` +
		`"prd":{"type":"object"},"repo_path":{"type":"string"},"task_results":{"type":"array"},` +
		`"verifier_model":{"type":"string"},"permission_mode":{"type":"string"},"ai_provider":{"type":"string"},` +
		`"artifacts_dir":{"type":"string"}}}`),
}

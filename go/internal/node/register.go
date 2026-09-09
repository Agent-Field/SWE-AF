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
// Tags match the Python node's exactly, because this registers under the same
// identity: a caller's trigger does not change when the implementation does.
// Role reasoners carry ["swe-planner"] on BOTH nodes — mirroring the Python
// structure where they are registered through the swe-planner-tagged
// AgentRouter. The four fast-node reasoners carry ["swe-fast"] (Python:
// fast_router tags=["swe-fast"]). The five orchestrators carry ["swe-planner"]
// to group them with the node in the control-plane UI (design §8).
//
// Two further tags are load-bearing for discovery rather than grouping:
//
//   - "entrypoint" marks a reasoner a caller may legitimately start from —
//     build, implement_issue, plan, resolve, resume_build. `af ls --entrypoints`
//     and GET /api/v1/discovery/capabilities filter on it. execute is NOT one:
//     its plan_result input is produced by plan, not hand-written.
//   - "internal" marks the pipeline stages an orchestrator drives and nothing
//     else should call — every role reasoner plus pro_execute. Each also carries
//     a description saying so, because a discovering coding agent that sees a
//     bare name (run_product_manager) will otherwise invoke it directly and get
//     a failure that reads like a broken node.
//
// Running this alongside the Python node against one control plane therefore
// needs an explicit NODE_ID on one of them; docker-compose.go.yml does that.

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
	"github.com/Agent-Field/SWE-AF/go/internal/issue"
	"github.com/Agent-Field/SWE-AF/go/internal/pro"
)

const (
	tagPlanner    = "swe-planner"
	tagFast       = "swe-fast"
	tagEntrypoint = "entrypoint"
	tagInternal   = "internal"
)

// RegisterPlanner registers the swe-planner surface. With the pro engine off it
// is the full classic surface: 25 role reasoners + 5 orchestrators + the
// issue-level entry point (31 total), porting swe_af/app.py. With the pro engine
// on (pro.Available()), the classic entry points are withheld and only the pro
// executor is added — see the body for why.
func (n *Node) RegisterPlanner() {
	n.registerRoles()
	// Pro engine on (the af-install / desktop default): the bundled swe-pro
	// sidecar is the coding surface and needs no opencode, so register only the
	// pro executor and withhold the classic opencode-driven entry points. The
	// orchestrators (plan/build/execute/resolve/resume_build) and implement_issue
	// drive the opencode role harness and fail wherever opencode is absent — the
	// desktop bundle ships aforge, not opencode — so advertising them just
	// surfaces broken entries next to swe-pro's working code_task. The role
	// reasoners stay registered (internal, undiscoverable) so pro_execute can
	// still call them. SWE_PRO_ENGINE=0 restores the full classic surface.
	if pro.Available() {
		n.registerProReasoners()
		return
	}
	n.registerOrchestrators()
	n.registerIssueReasoner()
}

// RegisterFast registers the swe-fast surface: the same 25 role reasoners + the
// 4 fast reasoners + the issue-level entry point (30 total). Ports
// swe_af/fast/app.py. It deliberately does NOT register the orchestrators —
// fast/app.py only defines its own build.
func (n *Node) RegisterFast() {
	n.registerRoles()
	n.registerFastReasoners()
	n.registerIssueReasoner()
}

// ---------------------------------------------------------------------------
// Role reasoners (identical on both nodes)
// ---------------------------------------------------------------------------

// internalRoleOpts is the single source of the registration metadata every role
// reasoner carries: the swe-planner group tag, the "internal" marker, and the
// one-line description that tells a discovering caller this stage is driven by
// an orchestrator. area names the role package's domain (planning, coding,
// gitops, advisor, ci) — the only part that varies across the 25.
func internalRoleOpts(area string) []agent.ReasonerOption {
	return []agent.ReasonerOption{
		agent.WithReasonerTags(tagPlanner, tagInternal),
		agent.WithDescription("Internal " + area + " pipeline stage invoked by the orchestrators " +
			"(build/plan/execute) — do not call directly."),
	}
}

// registerRoles wires the 25 execution/planning role reasoners, each backed by
// its package handler and threaded with the Deps built from the agent. All are
// tagged ["swe-planner","internal"] (Python groups them under the swe-planner
// router) and described per internalRoleOpts.
func (n *Node) registerRoles() {
	planningDeps := &planning.Deps{
		Harness:          n.App,
		App:              n.App,
		Pauser:           n.App,
		Hax:              n.hax,
		NodeID:           n.NodeID,
		AgentFieldServer: n.AgentFieldServer,
	}
	planningOpts := internalRoleOpts("planning")
	for name, h := range planning.Handlers() {
		regHandler(n, name, planningDeps, h, planningOpts...)
	}

	codingDeps := &coding.Deps{Harness: n.App, AI: n.App, Note: n.App}
	codingOpts := internalRoleOpts("coding")
	for name, h := range coding.Handlers() {
		regHandler(n, name, codingDeps, h, codingOpts...)
	}

	gitopsDeps := &gitops.Deps{App: n.App}
	gitopsOpts := internalRoleOpts("gitops")
	for name, h := range gitops.Handlers() {
		regHandler(n, name, gitopsDeps, h, gitopsOpts...)
	}

	advisorDeps := &advisor.Deps{
		Harness:          n.App,
		App:              n.App,
		Pauser:           n.App,
		BuildHaxClient:   hitl.BuildHaxClientFromEnv,
		NodeID:           n.NodeID,
		AgentFieldServer: n.AgentFieldServer,
	}
	advisorOpts := internalRoleOpts("advisor")
	for name, h := range advisor.Handlers() {
		regHandler(n, name, advisorDeps, h, advisorOpts...)
	}

	ciDeps := &ci.Deps{App: n.App}
	ciOpts := internalRoleOpts("ci")
	for name, h := range ci.Handlers() {
		regHandler(n, name, ciDeps, h, ciOpts...)
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
		CIGate:           orch.RunCIGate,
		ApprovalGate:     orch.PlanApprovalGate,
	}
	// Engine default routing (seamless path): with the flag truthy AND the
	// binary present, builds and execute calls that name no execute_fn_target
	// route per-issue coding through pro_execute on this node. Callers that pass
	// a target keep full control. Flag-on with a missing binary degrades to the
	// classic loop
	// (pro.Start logs the warning) instead of routing to a node that never
	// joined.
	if pro.Available() {
		deps.DefaultExecuteFnTarget = n.NodeID + ".pro_execute"
	}

	handlers := orch.Handlers() // {"build": Build}
	orch.RegisterPlan(handlers) // adds {"plan": Plan}
	handlers["execute"] = orch.ExecuteHandler
	handlers["resolve"] = orch.ResolveHandler
	handlers["resume_build"] = orch.ResumeBuildHandler

	// Python registers the orchestrators via @app.reasoner(): only `build`
	// carries an explicit routing description, the others get their docstring
	// summaries. The "entrypoint" tag goes on every orchestrator a caller may
	// legitimately start from (orchestratorEntrypoints).
	for name, h := range handlers {
		var opts []agent.ReasonerOption
		if orchestratorEntrypoints[name] {
			opts = append(opts, agent.WithReasonerTags(tagEntrypoint))
		}
		if d, ok := orchestratorDescriptions[name]; ok {
			opts = append(opts, agent.WithDescription(d))
		}
		if s, ok := orchestratorSchemas[name]; ok {
			opts = append(opts, agent.WithInputSchema(s))
		}
		regHandler(n, name, deps, h, opts...)
	}
}

// orchestratorEntrypoints is the set of orchestrators a caller may start a run
// from, and therefore the ones tagged "entrypoint" for discovery. plan, resolve
// and resume_build are advanced but legitimate entries (a goal, a PR URL and a
// checkpointed repo respectively). execute is deliberately absent: its
// plan_result input is only producible by a prior plan call, so surfacing it as
// an entry point invites hand-written garbage.
var orchestratorEntrypoints = map[string]bool{
	"build":        true,
	"plan":         true,
	"resolve":      true,
	"resume_build": true,
}

// orchestratorDescriptions mirrors the Python side: build's explicit
// description= kwarg, and the docstring first paragraphs the Python SDK
// auto-registers for the other orchestrators (swe_af/app.py).
var orchestratorDescriptions = map[string]string{
	"build": "Feature-level build: plans a PRD → architecture → issue DAG, then codes, " +
		"reviews, merges and verifies end-to-end. Give it a goal plus repo_path or " +
		"repo_url; returns a verified feature branch (optionally a draft PR). " +
		"Typical wall-clock 25-60 min. For one well-scoped change with known files, " +
		"prefer implement_issue.",
	"plan": "Run the full planning pipeline.",
	"execute": "Execute a planned DAG with self-healing replanning. Input plan_result comes " +
		"from a prior plan call — not a hand-written object; prefer build unless you are " +
		"resuming a custom pipeline.",
	"resolve":      "Update an existing PR: merge base, fix CI, address review comments, push.",
	"resume_build": "Resume a crashed build from the last checkpoint.",
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
	// tagged ["entrypoint"] with a routing description. Mirror that exactly.
	tag := agent.WithReasonerTags(tagFast)
	for name, h := range fast.Handlers() {
		var opts []agent.ReasonerOption
		if name == "build" {
			opts = append(opts,
				agent.WithReasonerTags(tagEntrypoint),
				agent.WithDescription(
					"Fast-mode build: one planning pass into a small task list, then code and "+
						"verify with tight timeouts. Same goal/repo_path interface as "+
						"swe-planner.build, but lighter and cheaper — suited to small features "+
						"where full DAG planning is overkill."),
			)
		} else {
			opts = append(opts, tag)
		}
		if s, ok := fastSchemas[name]; ok {
			opts = append(opts, agent.WithInputSchema(s))
		}
		regHandler(n, name, deps, h, opts...)
	}
}

// ---------------------------------------------------------------------------
// Issue-level entry point (both nodes)
// ---------------------------------------------------------------------------

// registerIssueReasoner wires implement_issue — the sub-harness entry point a
// main coding harness delegates fully-scoped issues to. Python includes the
// swe-issue-tagged issue_router in BOTH apps; the Go port mirrors that on both
// nodes under the -go tag convention.
func (n *Node) registerIssueReasoner() {
	deps := &issue.Deps{
		Call:   newCallFn(n.App),
		Note:   n.App,
		NodeID: n.NodeID,
	}
	tag := agent.WithReasonerTags("swe-issue-go", tagEntrypoint)
	for name, h := range issue.Handlers() {
		opts := []agent.ReasonerOption{tag, agent.WithDescription(
			"Issue-level build (sub-harness entry): implements ONE fully-scoped issue " +
				"on an isolated branch of a local repo — no planning agents, ~4-8 LLM " +
				"calls, minutes not hours. Give it issue{title, description, " +
				"acceptance_criteria, files_to_*} plus repo_path; returns the deliverable " +
				"branch. Prefer this over build when you already know exactly what to change."),
		}
		if s, ok := issueSchemas[name]; ok {
			opts = append(opts, agent.WithInputSchema(s))
		}
		regHandler(n, name, deps, h, opts...)
	}
}

// ---------------------------------------------------------------------------
// Pro-engine surface (SWE_PRO_ENGINE-gated, swe-planner only)
// ---------------------------------------------------------------------------

// registerProReasoners wires the pro-engine adapter. Called only when
// pro.Available(), so the classic surface — and the parity test asserting it —
// is unchanged whenever SWE_PRO_ENGINE is falsy or the binary is missing.
func (n *Node) registerProReasoners() {
	deps := &pro.Deps{
		Call:       newCallFn(n.App),
		Note:       n.App,
		EngineNode: pro.NodeID(),
	}
	for name, h := range pro.Handlers() {
		opts := []agent.ReasonerOption{
			// "internal": pro_execute is an execute_fn_target, reached by
			// build/execute routing per-issue coding through it — not a surface a
			// caller starts a run from.
			agent.WithReasonerTags(tagPlanner, tagInternal),
			agent.WithDescription(
				"Pro-engine executor: implements ONE fully-scoped issue via the " +
					"bundled pro coding engine. Matches the execute_fn_target contract — " +
					"set config.execute_fn_target to \"<node>.pro_execute\" on build/execute " +
					"to route per-issue coding through it."),
		}
		if s, ok := proSchemas[name]; ok {
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
// registers it under name and records the name plus its resolved discovery
// metadata on the node. D is inferred from deps; the package Handler types are
// assignable to the parameter's unnamed func type.
func regHandler[D any](
	n *Node,
	name string,
	deps *D,
	h func(context.Context, *D, map[string]any) (any, error),
	opts ...agent.ReasonerOption,
) {
	n.registered = append(n.registered, name)
	n.recordMeta(name, opts)
	n.App.RegisterReasoner(name, func(ctx context.Context, input map[string]any) (any, error) {
		return h(ctx, deps, input)
	}, opts...)
}

// recordMeta resolves opts the same way RegisterReasoner does — by applying them
// to a zero agent.Reasoner — and keeps the tags/description under name. The SDK
// exposes no reader for its registered reasoners, so this mirror is what lets
// the surface tests assert what a caller discovers.
func (n *Node) recordMeta(name string, opts []agent.ReasonerOption) {
	var r agent.Reasoner
	for _, opt := range opts {
		opt(&r)
	}
	if n.meta == nil {
		n.meta = make(map[string]ReasonerMeta)
	}
	n.meta[name] = ReasonerMeta{Tags: r.Tags, Description: r.Description}
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

// issueSchemas maps the issue-level reasoner to its input schema.
var issueSchemas = map[string]json.RawMessage{
	// implement_issue(issue, repo_path, base_branch="", artifacts_dir=".artifacts",
	//                 additional_context="", config=None)
	"implement_issue": schema(`{"type":"object","additionalProperties":true,"required":["issue","repo_path"],"properties":{` +
		`"issue":{"type":"object"},"repo_path":{"type":"string"},"base_branch":{"type":"string"},` +
		`"artifacts_dir":{"type":"string"},"additional_context":{"type":"string"},"config":{"type":"object"}}}`),
}

// proSchemas maps the opt-in pro-engine reasoners to their input schemas.
var proSchemas = map[string]json.RawMessage{
	// pro_execute(issue, repo_path) — the execute_fn_target calling convention.
	"pro_execute": schema(`{"type":"object","additionalProperties":true,"required":["issue","repo_path"],"properties":{` +
		`"issue":{"type":"object"},"repo_path":{"type":"string"}}}`),
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

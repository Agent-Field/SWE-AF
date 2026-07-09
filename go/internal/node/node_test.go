package node

import (
	"context"
	"sort"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/fast"
)

// pythonRoleSurface is the independent parity checklist: the exact 25 role
// reasoner names the Python swe_af.reasoners.router registers (pipeline.py's 5
// planning roles + execution_agents.py's 20 execution roles). It is written from
// the Python inventory — NOT derived from the Go Handlers() maps — so the test
// catches a drift in either direction (a missing or an extra Go registration).
var pythonRoleSurface = []string{
	// pipeline.py — planning roles
	"run_product_manager",
	"run_environment_scout",
	"run_architect",
	"run_tech_lead",
	"run_sprint_planner",
	// execution_agents.py — coding roles
	"run_coder",
	"run_qa",
	"run_code_reviewer",
	"run_qa_synthesizer",
	// execution_agents.py — git/workspace roles
	"run_git_init",
	"run_workspace_setup",
	"run_workspace_cleanup",
	"run_merger",
	"run_integration_tester",
	"run_repo_finalize",
	"run_github_pr",
	// execution_agents.py — advisor/verify roles
	"run_retry_advisor",
	"run_issue_advisor",
	"run_replanner",
	"run_issue_writer",
	"run_verifier",
	"generate_fix_issues",
	// execution_agents.py — CI/resolve roles
	"run_ci_watcher",
	"run_ci_fixer",
	"run_pr_resolver",
}

// pythonOrchestrators is the 5 orchestrator reasoners defined on swe_af.app
// (app.py @app.reasoner()): build, plan, execute, resolve, resume_build.
var pythonOrchestrators = []string{"build", "plan", "execute", "resolve", "resume_build"}

// pythonFastReasoners is the 4 first-class fast reasoners: fast/app.py's build
// plus fast_plan_tasks / fast_execute_tasks / fast_verify.
var pythonFastReasoners = []string{"build", "fast_plan_tasks", "fast_execute_tasks", "fast_verify"}

func TestRegisterPlannerExactSurface(t *testing.T) {
	n, err := BuildAgent("swe-planner", "8003", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	// swe-planner surface = 25 roles + 5 orchestrators = 30 unique names.
	want := append(append([]string(nil), pythonRoleSurface...), pythonOrchestrators...)
	assertSurface(t, "swe-planner", n.RegisteredNames(), want)
}

func TestRegisterFastExactSurface(t *testing.T) {
	n, err := BuildAgent("swe-fast", "8004", "fast desc")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterFast()

	// swe-fast surface = 25 roles + 4 fast reasoners = 29 unique names.
	// It must NOT contain plan/execute/resolve/resume_build (those live only on
	// swe-planner) — assertSurface's extra-name check enforces that.
	want := append(append([]string(nil), pythonRoleSurface...), pythonFastReasoners...)
	assertSurface(t, "swe-fast", n.RegisteredNames(), want)
}

// TestFastWrappersAreBackedByRoles verifies the seven delegating wrappers
// (fast/__init__.py) are present on the swe-fast surface — each is one of the
// role names, backed by the full-pipeline role handler (fast.Wrappers identity).
func TestFastWrappersAreBackedByRoles(t *testing.T) {
	n, err := BuildAgent("swe-fast", "8004", "fast desc")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterFast()

	got := toSet(n.RegisteredNames())
	for _, w := range fast.WrapperNames() {
		if !got[w] {
			t.Errorf("fast wrapper %q not registered on swe-fast surface", w)
		}
	}
	// Every wrapper must also be one of the role names (identity delegation).
	roleSet := toSet(pythonRoleSurface)
	for _, w := range fast.WrapperNames() {
		if !roleSet[w] {
			t.Errorf("fast wrapper %q is not a role name — delegation is not identity", w)
		}
	}
}

// TestRegHandlerRoutesToPackageHandler validates the adapter closure: it must
// invoke the package handler with the captured Deps and the request input, and
// propagate the handler's return value. This exercises the single routing seam
// every reasoner registration flows through, without a control plane.
func TestRegHandlerRoutesToPackageHandler(t *testing.T) {
	app, err := agent.New(agent.Config{NodeID: "test", Version: "1.0.0", ListenAddress: ":0"})
	if err != nil {
		t.Fatalf("agent.New: %v", err)
	}
	n := &Node{App: app}

	type fakeDeps struct{ marker string }
	deps := &fakeDeps{marker: "wired"}

	var gotDeps *fakeDeps
	var gotInput map[string]any
	h := func(_ context.Context, d *fakeDeps, in map[string]any) (any, error) {
		gotDeps = d
		gotInput = in
		return map[string]any{"echo": in["x"]}, nil
	}

	regHandler(n, "probe", deps, h)

	if len(n.registered) != 1 || n.registered[0] != "probe" {
		t.Fatalf("regHandler did not record name: %v", n.registered)
	}

	out, err := app.Execute(context.Background(), "probe", map[string]any{"x": 42})
	if err != nil {
		t.Fatalf("Execute(probe): %v", err)
	}
	if gotDeps != deps {
		t.Errorf("handler received deps %v, want the captured %v", gotDeps, deps)
	}
	if gotInput["x"] != 42 {
		t.Errorf("handler received input %v, want x=42", gotInput)
	}
	m, ok := out.(map[string]any)
	if !ok || m["echo"] != 42 {
		t.Errorf("handler return not propagated: got %#v", out)
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// assertSurface fails if got (the registered names) does not equal want as a
// set, or if got contains duplicates. Reporting missing and extra names
// separately makes a parity drift immediately diagnosable.
func assertSurface(t *testing.T, node string, got, want []string) {
	t.Helper()

	// Duplicate guard: RegisterReasoner dedupes by name in its map, so a
	// duplicate in the recorded slice means two registrations collided on one
	// name (a silent surface bug the set comparison would otherwise hide).
	seen := map[string]int{}
	for _, name := range got {
		seen[name]++
	}
	for name, c := range seen {
		if c > 1 {
			t.Errorf("[%s] reasoner %q registered %d times (collision)", node, name, c)
		}
	}

	gotSet := toSet(got)
	wantSet := toSet(want)

	var missing, extra []string
	for name := range wantSet {
		if !gotSet[name] {
			missing = append(missing, name)
		}
	}
	for name := range gotSet {
		if !wantSet[name] {
			extra = append(extra, name)
		}
	}
	sort.Strings(missing)
	sort.Strings(extra)

	if len(missing) > 0 {
		t.Errorf("[%s] missing reasoners (in Python, not registered): %v", node, missing)
	}
	if len(extra) > 0 {
		t.Errorf("[%s] extra reasoners (registered, not in Python): %v", node, extra)
	}
	if len(gotSet) != len(wantSet) {
		t.Errorf("[%s] surface size = %d, want %d", node, len(gotSet), len(wantSet))
	}
}

func toSet(names []string) map[string]bool {
	s := make(map[string]bool, len(names))
	for _, n := range names {
		s[n] = true
	}
	return s
}

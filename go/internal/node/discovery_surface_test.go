package node

import (
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/pro"
)

// roleAreas is the independent checklist of the pipeline area each internal role
// reasoner belongs to. Like pythonRoleSurface it is written from the roles'
// jobs — NOT read back out of register.go — so a role that moves package
// without its description following fails here.
var roleAreas = map[string]string{
	// planning roles
	"run_product_manager":   "planning",
	"run_environment_scout": "planning",
	"run_architect":         "planning",
	"run_tech_lead":         "planning",
	"run_sprint_planner":    "planning",
	// coding roles
	"run_coder":          "coding",
	"run_qa":             "coding",
	"run_code_reviewer":  "coding",
	"run_qa_synthesizer": "coding",
	// git/workspace roles
	"run_git_init":           "gitops",
	"run_workspace_setup":    "gitops",
	"run_workspace_cleanup":  "gitops",
	"run_merger":             "gitops",
	"run_integration_tester": "gitops",
	"run_repo_finalize":      "gitops",
	"run_github_pr":          "gitops",
	// advisor/verify roles
	"run_retry_advisor":   "advisor",
	"run_issue_advisor":   "advisor",
	"run_replanner":       "advisor",
	"run_issue_writer":    "advisor",
	"run_verifier":        "advisor",
	"generate_fix_issues": "advisor",
	// CI/resolve roles
	"run_ci_watcher":  "ci",
	"run_ci_fixer":    "ci",
	"run_pr_resolver": "ci",
}

// wantEntrypoints is the exact set of swe-planner reasoners a caller may start a
// run from. execute is deliberately absent (its plan_result comes from plan) and
// so is pro_execute (an execute_fn_target). A caller-facing utility landing on
// this node — e.g. get_workspace_handle — joins this list when it does.
var wantEntrypoints = []string{"build", "implement_issue", "plan", "resolve", "resume_build"}

// TestRoleReasonersAreMarkedInternal: every role reasoner must carry the
// "internal" tag AND a description saying an orchestrator drives it. Without
// both, a coding agent discovering the node sees a bare name like
// run_product_manager and invokes it directly — which fails, because the stage
// has no orchestrator context.
func TestRoleReasonersAreMarkedInternal(t *testing.T) {
	t.Setenv("SWE_PRO_ENGINE", "")
	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()
	meta := n.RegisteredMeta()

	for _, name := range pythonRoleSurface {
		area, ok := roleAreas[name]
		if !ok {
			t.Errorf("role %q has no area in roleAreas — extend the checklist", name)
			continue
		}
		m, ok := meta[name]
		if !ok {
			t.Errorf("role %q not registered", name)
			continue
		}
		if !hasTag(m.Tags, tagInternal) {
			t.Errorf("role %q tags = %v, want the %q marker", name, m.Tags, tagInternal)
		}
		if !hasTag(m.Tags, tagPlanner) {
			t.Errorf("role %q tags = %v, lost the %q group tag", name, m.Tags, tagPlanner)
		}
		if m.Description == "" {
			t.Errorf("role %q has no description — discovery shows a bare name", name)
			continue
		}
		if want := "Internal " + area + " pipeline stage"; !strings.HasPrefix(m.Description, want) {
			t.Errorf("role %q description = %q, want it to start with %q", name, m.Description, want)
		}
		if !strings.Contains(m.Description, "do not call directly") {
			t.Errorf("role %q description = %q, want the do-not-call-directly warning", name, m.Description)
		}
	}
}

// TestEntrypointTagIsExactSet: the "entrypoint" tag is what `af ls --entrypoints`
// and GET /api/v1/discovery/capabilities filter on, so the tagged set must be
// exactly the reasoners a caller can legitimately start from — no internal stage
// leaking in, no real entry point missing.
func TestEntrypointTagIsExactSet(t *testing.T) {
	t.Setenv("SWE_PRO_ENGINE", "")
	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	assertSurface(t, "swe-planner[entrypoint]", entrypointNames(n), wantEntrypoints)

	// Every entry point must also say what it is for — the tag routes a caller
	// to it, the description tells them whether to pick it.
	meta := n.RegisteredMeta()
	for _, name := range wantEntrypoints {
		if meta[name].Description == "" {
			t.Errorf("entrypoint %q has no description", name)
		}
	}

	// The fast node exposes only its own build plus implement_issue.
	f, err := BuildAgent("swe-fast", "8006", "fast desc")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	f.RegisterFast()
	assertSurface(t, "swe-fast[entrypoint]", entrypointNames(f), []string{"build", "implement_issue"})
}

// TestProExecuteIsInternal: pro_execute is reached through
// config.execute_fn_target, never started by a caller — it must be tagged
// internal and must not appear as an entry point.
func TestProExecuteIsInternal(t *testing.T) {
	t.Setenv(pro.EnvEnabled, "1")
	fakeEngineBin(t)

	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	m, ok := n.RegisteredMeta()["pro_execute"]
	if !ok {
		t.Fatal("pro_execute not registered with the engine enabled")
	}
	if !hasTag(m.Tags, tagInternal) {
		t.Errorf("pro_execute tags = %v, want the %q marker", m.Tags, tagInternal)
	}
	if hasTag(m.Tags, tagEntrypoint) {
		t.Errorf("pro_execute tags = %v, must not be an entry point", m.Tags)
	}
	if m.Description == "" {
		t.Error("pro_execute lost its description")
	}
	// With the engine on the classic entry points are withheld, so the node
	// advertises no entry points of its own — the swe-pro sidecar carries the
	// coding entry (code_task/code_resume).
	assertSurface(t, "swe-planner[pro][entrypoint]", entrypointNames(n), nil)
}

// TestExecuteDescribesItsPlanResultInput: execute's plan_result is the one input
// on the surface a caller cannot write by hand — the description has to say so,
// since execute is (correctly) not tagged as an entry point but is still visible.
func TestExecuteDescribesItsPlanResultInput(t *testing.T) {
	t.Setenv("SWE_PRO_ENGINE", "")
	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	got := n.RegisteredMeta()["execute"].Description
	for _, want := range []string{"plan_result comes from a prior plan call", "prefer build"} {
		if !strings.Contains(got, want) {
			t.Errorf("execute description = %q, want it to contain %q", got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// entrypointNames returns the names of every reasoner registered with the
// "entrypoint" tag.
func entrypointNames(n *Node) []string {
	var out []string
	for name, m := range n.RegisteredMeta() {
		if hasTag(m.Tags, tagEntrypoint) {
			out = append(out, name)
		}
	}
	return out
}

func hasTag(tags []string, want string) bool {
	for _, t := range tags {
		if t == want {
			return true
		}
	}
	return false
}

package node

import (
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/pro"
)

// TestRegisterPlannerProSurfaceGated: with SWE_PRO_ENGINE set, the planner
// surface is the default 31 names plus exactly the pro handlers — and nothing
// on the fast node changes (the pro surface is planner-only).
func TestRegisterPlannerProSurfaceGated(t *testing.T) {
	t.Setenv(pro.EnvEnabled, "1")

	n, err := BuildAgent("swe-planner-go", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	want := append(append([]string(nil), pythonRoleSurface...), pythonOrchestrators...)
	want = append(want, pythonIssueReasoners...)
	for name := range pro.Handlers() {
		want = append(want, name)
	}
	assertSurface(t, "swe-planner-go[pro]", n.RegisteredNames(), want)

	f, err := BuildAgent("swe-fast-go", "8006", "fast desc")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	f.RegisterFast()
	if toSet(f.RegisteredNames())["pro_execute"] {
		t.Error("pro_execute must not register on the fast node")
	}
}

// TestProSurfaceOffByDefault: with the flag unset the planner registers no pro
// reasoner — the complement of the exact-surface parity test, stated directly.
func TestProSurfaceOffByDefault(t *testing.T) {
	t.Setenv(pro.EnvEnabled, "")

	n, err := BuildAgent("swe-planner-go", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()
	if toSet(n.RegisteredNames())["pro_execute"] {
		t.Error("pro_execute registered without SWE_PRO_ENGINE — the surface must be opt-in")
	}
}

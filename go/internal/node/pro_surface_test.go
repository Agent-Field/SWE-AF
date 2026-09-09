package node

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/pro"
)

// fakeEngineBin creates an existing dummy engine binary and points SWE_PRO_BIN
// at it, so pro.Available() sees the flag-on-and-binary-present state.
func fakeEngineBin(t *testing.T) {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "engine")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatalf("fake engine bin: %v", err)
	}
	t.Setenv(pro.EnvBin, bin)
}

// TestRegisterPlannerProSurfaceGated: with SWE_PRO_ENGINE set and the engine
// binary present, the pro engine REPLACES the classic surface — the internal
// role reasoners and the pro handlers register, but the opencode-driven
// orchestrators and implement_issue are withheld (they'd fail wherever opencode
// is absent; the swe-pro sidecar is the working coding entry). Nothing on the
// fast node changes (the pro surface is planner-only).
func TestRegisterPlannerProSurfaceGated(t *testing.T) {
	t.Setenv(pro.EnvEnabled, "1")
	fakeEngineBin(t)

	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()

	want := append([]string(nil), pythonRoleSurface...)
	for name := range pro.Handlers() {
		want = append(want, name)
	}
	assertSurface(t, "swe-planner[pro]", n.RegisteredNames(), want)

	f, err := BuildAgent("swe-fast", "8006", "fast desc")
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

	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()
	if toSet(n.RegisteredNames())["pro_execute"] {
		t.Error("pro_execute registered without SWE_PRO_ENGINE — the surface must stay flag-gated")
	}
}

// TestProSurfaceEnabledButBinaryMissing: the flag alone is not enough — with
// no engine binary on disk the planner must keep the classic surface (and the
// classic coding loop) instead of routing every issue to a node that never
// joins the control plane.
func TestProSurfaceEnabledButBinaryMissing(t *testing.T) {
	t.Setenv(pro.EnvEnabled, "1")
	t.Setenv(pro.EnvBin, filepath.Join(t.TempDir(), "missing"))

	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatalf("BuildAgent: %v", err)
	}
	n.RegisterPlanner()
	if toSet(n.RegisteredNames())["pro_execute"] {
		t.Error("pro_execute registered with SWE_PRO_ENGINE set but no binary — must degrade to the classic loop")
	}
}

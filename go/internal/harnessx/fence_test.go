package harnessx

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fence"
)

// Every codeaf call is started through the fence, scoped to the build's own
// folder; another harness is left alone; SWE_FENCE=off is the only way out.
func TestACodeAFCallIsFencedToItsBuild(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", root)
	bin := filepath.Join(t.TempDir(), "codeaf")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEAF_BIN", bin)
	build := filepath.Join(root, "factory-demo-aaaa")
	cwd := filepath.Join(build, ".worktrees", "issue-01")
	_ = os.MkdirAll(cwd, 0o755)

	got := fenceCodeAF(harness.Options{Provider: "aforge", Cwd: cwd, Env: map[string]string{"K": "v"}})
	self, _ := os.Executable()
	if got.BinPath != self {
		t.Fatalf("codeaf is not started through the fence: BinPath %q", got.BinPath)
	}
	realRoot, _ := filepath.EvalSymlinks(root)
	if got.Env[fence.EnvExec] != "1" || got.Env[fence.EnvReal] != bin || got.Env[fence.EnvRoot] != realRoot ||
		got.Env[fence.EnvDir] != filepath.Join(realRoot, "factory-demo-aaaa") || got.Env["K"] != "v" {
		t.Fatalf("fence env = %v", got.Env)
	}

	if o := fenceCodeAF(harness.Options{Provider: "claude-code", Cwd: cwd}); o.BinPath != "" {
		t.Fatal("another harness must be left alone")
	}
	t.Setenv("SWE_FENCE", "off")
	if o := fenceCodeAF(harness.Options{Provider: "aforge", Cwd: cwd}); o.BinPath != "" {
		t.Fatal("SWE_FENCE=off must opt out")
	}
}

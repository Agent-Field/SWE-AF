package harnessx

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fence"
)

// TestMain lets this test binary stand in for swe-planner as the fence
// launcher: the SDK starts os.Executable() with the fence's variables set, and
// main hands that process to fence.Main exactly as cmd/swe-planner does.
func TestMain(m *testing.M) {
	if os.Getenv(fence.EnvExec) == "1" {
		os.Exit(fence.Main(os.Args[1:]))
	}
	os.Exit(m.Run())
}

// Job 70 (2026-11-07): a fenced codeaf step ran the legacy `exec --json -w`
// command, so its step had no live chat, no steer and no questions for the
// person. A fenced run is named codeaf and, watched, is the session command
// that carries --events and --steer, started through the launcher.
func TestAFencedRunIsWatchedAndSteerable(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", root)
	tools := t.TempDir()
	argvFile := filepath.Join(tools, "argv")
	bin := filepath.Join(tools, "codeaf")
	script := "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > " + argvFile + "\nprintf '{}\\n'\n"
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEAF_BIN", bin)
	cwd := filepath.Join(root, "factory-demo-aaaa", ".worktrees", "issue-01")
	_ = os.MkdirAll(cwd, 0o755)

	opts := fenceCodeAF(harness.Options{Provider: "aforge", Cwd: cwd,
		Env: map[string]string{"OPENROUTER_API_KEY": "sk-test", "CODEAF_HOME": t.TempDir()}})
	if opts.Provider != harness.ProviderCodeAF {
		t.Fatalf("a fenced run is named %q, not codeaf", opts.Provider)
	}
	provider, err := harness.BuildProvider(opts.Provider, opts.BinPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := provider.(*harness.CodeAFProvider); !ok {
		t.Fatalf("a fenced run builds %T, the legacy provider", provider)
	}
	// The launcher still runs; only the bubblewrap layer is set aside here so
	// the test does not depend on the host's user namespaces.
	t.Setenv("SWE_FENCE", "off")
	opts.Live = &harness.Live{Event: func(json.RawMessage) {}, Steer: make(chan string)}
	if _, err := provider.Execute(context.Background(), "do the thing", opts); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(argvFile)
	if err != nil {
		t.Fatalf("the fake codeaf was never started through the launcher: %v", err)
	}
	argv := strings.Split(strings.TrimSpace(string(raw)), "\n")
	has := map[string]bool{}
	for _, a := range argv {
		has[a] = true
	}
	for _, want := range []string{"chat", "--once", "--events", "--steer"} {
		if !has[want] {
			t.Errorf("fenced argv has no %s: %q", want, argv)
		}
	}
	if has["exec"] || has["-w"] {
		t.Errorf("fenced argv is the legacy exec command: %q", argv)
	}
}

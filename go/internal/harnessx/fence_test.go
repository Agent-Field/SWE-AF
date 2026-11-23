package harnessx

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fence"
)

// V2: With SWE_FENCE unset (or false), a codeaf run gets no fence: the options
// and command carry no fence launcher, wrapper, flags, or fence env vars, and
// nothing is refused. With SWE_FENCE=1 it is fenced to its build's folder as
// before.
func TestACodeAFCallIsFencedToItsBuild(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", root)
	tools := t.TempDir()
	argvFile := filepath.Join(tools, "argv")
	envFile := filepath.Join(tools, "env")
	bin := filepath.Join(tools, "codeaf")
	script := "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > " + argvFile +
		"\nenv > " + envFile + "\nprintf '{}\\n'\n"
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEAF_BIN", bin)
	build := filepath.Join(root, "factory-demo-aaaa")
	cwd := filepath.Join(build, ".worktrees", "issue-01")
	_ = os.MkdirAll(cwd, 0o755)

	// Unset (default) and false values: no launcher, no fence env, nothing refused.
	for _, val := range []string{"", "0", "false", "off"} {
		t.Setenv(fence.EnvFence, val)
		for _, prov := range []string{"aforge", "codeaf"} {
			unfenced := fenceCodeAF(harness.Options{Provider: prov, Cwd: cwd, Env: map[string]string{"K": "v"}})
			if unfenced.BinPath != "" || unfenced.Provider != prov {
				t.Fatalf("SWE_FENCE=%q provider=%q modified opts: BinPath=%q Provider=%q",
					val, prov, unfenced.BinPath, unfenced.Provider)
			}
			for k := range unfenced.Env {
				if strings.HasPrefix(k, "SWE_FENCE") {
					t.Fatalf("SWE_FENCE=%q leaked fence variable %q into opts.Env: %v", val, k, unfenced.Env)
				}
			}
		}
	}

	// Execute a codeaf run with SWE_FENCE unset and PATH stripped of bwrap so
	// any fence attempt would refuse: the fake codeaf runs directly with no
	// fence wrapper, flags, or SWE_FENCE_* environment.
	t.Setenv(fence.EnvFence, "")
	t.Setenv("PATH", "/bin:/usr/bin")
	unfencedOpts := fenceCodeAF(harness.Options{
		Provider: "codeaf",
		BinPath:  bin,
		Cwd:      cwd,
		Env:      map[string]string{"OPENROUTER_API_KEY": "sk-test", "CODEAF_HOME": t.TempDir()},
	})
	if unfencedOpts.BinPath != bin {
		t.Fatalf("unfenced BinPath = %q, want %q", unfencedOpts.BinPath, bin)
	}
	provider, err := harness.BuildProvider(unfencedOpts.Provider, unfencedOpts.BinPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Execute(context.Background(), "do the thing", unfencedOpts); err != nil {
		t.Fatalf("unfenced codeaf run was refused: %v", err)
	}
	rawArgv, err := os.ReadFile(argvFile)
	if err != nil {
		t.Fatalf("unfenced codeaf did not run: %v", err)
	}
	for _, a := range strings.Split(strings.TrimSpace(string(rawArgv)), "\n") {
		if strings.Contains(a, "bwrap") || strings.Contains(a, "sandbox-exec") || a == "--tmpfs" || a == "--die-with-parent" {
			t.Fatalf("unfenced argv carried fence wrapper/flag %q: %s", a, string(rawArgv))
		}
	}
	rawEnv, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(rawEnv), "\n") {
		if strings.HasPrefix(line, "SWE_FENCE_") {
			t.Fatalf("unfenced run saw fence env %q", line)
		}
	}

	// With SWE_FENCE=1: fenced through the launcher as before.
	t.Setenv(fence.EnvFence, "1")
	got := fenceCodeAF(harness.Options{Provider: "aforge", Cwd: cwd, Env: map[string]string{"K": "v"}})
	self, _ := os.Executable()
	if got.BinPath != self {
		t.Fatalf("codeaf is not started through the fence: BinPath %q", got.BinPath)
	}
	if got.Provider != harness.ProviderCodeAF {
		t.Fatalf("fenced provider = %q, want %q", got.Provider, harness.ProviderCodeAF)
	}
	realRoot, _ := filepath.EvalSymlinks(root)
	if got.Env[fence.EnvExec] != "1" || got.Env[fence.EnvReal] != bin || got.Env[fence.EnvRoot] != realRoot ||
		got.Env[fence.EnvDir] != filepath.Join(realRoot, "factory-demo-aaaa") || got.Env["K"] != "v" {
		t.Fatalf("fence env = %v", got.Env)
	}

	if o := fenceCodeAF(harness.Options{Provider: "claude-code", Cwd: cwd}); o.BinPath != "" {
		t.Fatal("another harness must be left alone")
	}
}

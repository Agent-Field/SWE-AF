package pro

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestEnabled(t *testing.T) {
	cases := map[string]bool{
		"":      false,
		"0":     false,
		"false": false,
		"off":   false,
		"nope":  false,
		"1":     true,
		"true":  true,
		"TRUE":  true,
		"yes":   true,
		"on":    true,
	}
	for val, want := range cases {
		t.Setenv(EnvEnabled, val)
		if got := Enabled(); got != want {
			t.Errorf("Enabled() with %s=%q = %v, want %v", EnvEnabled, val, got, want)
		}
	}
}

func TestDefaults(t *testing.T) {
	t.Setenv(EnvBin, "")
	t.Setenv(EnvNodeID, "")
	t.Setenv(EnvPort, "")
	if BinPath() != DefaultBin {
		t.Errorf("BinPath() = %q, want %q", BinPath(), DefaultBin)
	}
	if NodeID() != DefaultNodeID {
		t.Errorf("NodeID() = %q, want %q", NodeID(), DefaultNodeID)
	}
	if Port() != DefaultPort {
		t.Errorf("Port() = %q, want %q", Port(), DefaultPort)
	}
	t.Setenv(EnvNodeID, "swe-pro-2")
	if NodeID() != "swe-pro-2" {
		t.Errorf("NodeID() override = %q, want swe-pro-2", NodeID())
	}
}

// TestChildEnv asserts the SWE-AF → engine env translation, including that the
// appended entries win over inherited duplicates (os/exec last-wins) and that
// provider keys pass through untouched.
func TestChildEnv(t *testing.T) {
	t.Setenv(EnvNodeID, "")
	t.Setenv(EnvPort, "9911")
	t.Setenv(EnvPublicURL, "http://pro.example:9911")
	base := []string{"OPENROUTER_API_KEY=sk-or-test", "AGENTFIELD_URL=http://stale:1"}
	env := childEnv(base, Options{Server: "http://cp:8080", Token: "tok"})

	want := map[string]string{
		"AGENTFIELD_URL":     "http://cp:8080",
		"AGENT_NODE_ID":      DefaultNodeID,
		"AGENT_LISTEN_ADDR":  ":9911",
		"AGENTFIELD_TOKEN":   "tok",
		"AGENT_PUBLIC_URL":   "http://pro.example:9911",
		"OPENROUTER_API_KEY": "sk-or-test",
	}
	got := map[string]string{}
	for _, kv := range env { // later entries overwrite: mirror os/exec last-wins
		k, v, _ := strings.Cut(kv, "=")
		got[k] = v
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("childEnv[%s] = %q, want %q", k, got[k], v)
		}
	}
}

func TestChildEnvNoTokenNoPublicURL(t *testing.T) {
	t.Setenv(EnvPublicURL, "")
	env := childEnv(nil, Options{Server: "http://cp:8080"})
	for _, kv := range env {
		if strings.HasPrefix(kv, "AGENTFIELD_TOKEN=") || strings.HasPrefix(kv, "AGENT_PUBLIC_URL=") {
			t.Errorf("childEnv unexpectedly set %q", kv)
		}
	}
}

func TestStartMissingBinary(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := Start(ctx, Options{Server: "http://cp:8080", Bin: filepath.Join(t.TempDir(), "nope")})
	if s != nil {
		t.Fatal("Start with a missing binary should return nil")
	}
	s.Wait(time.Millisecond) // nil-safe
}

// fakeBin writes an executable shell script and returns its path.
func fakeBin(t *testing.T, script string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("shell-script fake binary; supervisor behavior is POSIX-tested")
	}
	p := filepath.Join(t.TempDir(), "swe-pro")
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestSupervisorStopsOnCancel: a long-running sidecar is interrupted by ctx
// cancellation and the loop winds down promptly.
func TestSupervisorStopsOnCancel(t *testing.T) {
	bin := fakeBin(t, `echo up; trap 'exit 0' INT TERM; while true; do sleep 0.1; done`)
	ctx, cancel := context.WithCancel(context.Background())
	var out strings.Builder
	s := Start(ctx, Options{Server: "http://cp:8080", Bin: bin, Stdout: &out, Stderr: &out})
	if s == nil {
		t.Fatal("Start returned nil for an existing binary")
	}
	time.Sleep(300 * time.Millisecond) // let it spawn and print
	cancel()
	done := make(chan struct{})
	go func() { s.Wait(10 * time.Second); close(done) }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("supervisor did not stop after cancel")
	}
	if !strings.Contains(out.String(), "[pro-engine] up") {
		t.Errorf("sidecar stdout not prefixed/captured: %q", out.String())
	}
}

// TestSupervisorGivesUpOnFastCrashes: a binary that always exits immediately
// stops being restarted after maxFastCrashes.
func TestSupervisorGivesUpOnFastCrashes(t *testing.T) {
	bin := fakeBin(t, `exit 3`)
	origInitial, origMax, origCrashes := backoffInitial, backoffMax, maxFastCrashes
	backoffInitial, backoffMax, maxFastCrashes = time.Millisecond, 2*time.Millisecond, 3
	defer func() { backoffInitial, backoffMax, maxFastCrashes = origInitial, origMax, origCrashes }()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := Start(ctx, Options{Server: "http://cp:8080", Bin: bin, Stdout: &strings.Builder{}, Stderr: &strings.Builder{}})
	if s == nil {
		t.Fatal("Start returned nil")
	}
	done := make(chan struct{})
	go func() { s.Wait(10 * time.Second); close(done) }()
	select {
	case <-done: // gave up without ctx cancellation — expected
	case <-time.After(15 * time.Second):
		t.Fatal("supervisor kept restarting a fast-crashing binary")
	}
}

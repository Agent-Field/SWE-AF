package pro

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
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

// TestEnabledOptOutContract pins the two halves of the default-on rollout. The
// manifest declares SWE_PRO_ENGINE with default "1", and the installer's env
// resolver injects a declared default unconditionally — so unlike before, a
// value is always present on an installed node and "turning it off" can only
// mean writing a falsy one. "0" and "false" must therefore read as disabled,
// and a genuinely absent variable (a bare binary, no manifest) must still read
// as disabled so this package keeps its own default-off behaviour.
func TestEnabledOptOutContract(t *testing.T) {
	t.Setenv(EnvEnabled, "1")
	if !Enabled() {
		t.Errorf("%s=1 must enable the engine — this is what the manifest default injects", EnvEnabled)
	}
	for _, off := range []string{"0", "false", "FALSE"} {
		t.Setenv(EnvEnabled, off)
		if Enabled() {
			t.Errorf("%s=%q must disable the engine — it is the documented opt-out", EnvEnabled, off)
		}
	}
	// Genuinely unset, not merely empty: t.Setenv registers the restore, then
	// Unsetenv removes the variable for the rest of this test.
	t.Setenv(EnvEnabled, "")
	if err := os.Unsetenv(EnvEnabled); err != nil {
		t.Fatal(err)
	}
	if Enabled() {
		t.Errorf("unset %s must leave the engine off (bare binary, no manifest)", EnvEnabled)
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

// TestResolveBin covers the three-step search: explicit SWE_PRO_BIN is
// authoritative (found or not — no fall-through), and Available() is the
// flag AND binary-presence conjunction.
func TestResolveBin(t *testing.T) {
	dir := t.TempDir()
	present := filepath.Join(dir, "engine")
	if err := os.WriteFile(present, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(dir, "missing")

	t.Setenv(EnvBin, present)
	if path, ok := ResolveBin(); !ok || path != present {
		t.Errorf("ResolveBin() with existing override = (%q, %v), want (%q, true)", path, ok, present)
	}

	t.Setenv(EnvBin, missing)
	if path, ok := ResolveBin(); ok || path != missing {
		t.Errorf("ResolveBin() with missing override = (%q, %v), want (%q, false) — no fall-through", path, ok, missing)
	}

	// A present-but-not-executable binary must be treated as unavailable: it
	// would fail at exec time, and routing coding to an engine that can never
	// start is worse than staying on the classic loop. (Some install paths
	// copy files without preserving the source's execute bit.)
	nonExec := filepath.Join(dir, "not-executable")
	if err := os.WriteFile(nonExec, []byte("#!/bin/sh\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvBin, nonExec)
	if _, ok := ResolveBin(); ok {
		t.Error("ResolveBin() reported a non-executable file as usable")
	}
	t.Setenv(EnvEnabled, "1")
	if Available() {
		t.Error("Available() = true for a non-executable binary — must degrade to the classic loop")
	}

	// A directory at the binary path is likewise not runnable (os.Stat alone
	// succeeds on directories).
	t.Setenv(EnvBin, dir)
	if _, ok := ResolveBin(); ok {
		t.Error("ResolveBin() reported a directory as usable")
	}
	t.Setenv(EnvEnabled, "")

	t.Setenv(EnvEnabled, "1")
	t.Setenv(EnvBin, present)
	if !Available() {
		t.Error("Available() = false with flag on and binary present")
	}
	t.Setenv(EnvBin, missing)
	if Available() {
		t.Error("Available() = true with flag on but binary missing")
	}
	t.Setenv(EnvEnabled, "")
	t.Setenv(EnvBin, present)
	if Available() {
		t.Error("Available() = true with flag off")
	}
}

// TestResolveBinSiblings covers the `af install` layout, where the vendored
// engines sit in the same bin/ dir the installer builds the node into. One
// checkout carries a build per platform, so the swe-pro-<GOOS>-<GOARCH>
// sibling must be preferred over a plain swe-pro — running the wrong one is an
// "exec format error", not a fallback — while a lone plain swe-pro (an
// unpacked image, a local engine build) still resolves.
func TestResolveBinSiblings(t *testing.T) {
	if runnable(DefaultBin) {
		t.Skipf("%s exists on this host and short-circuits the sibling search", DefaultBin)
	}
	suffixed := "swe-pro-" + runtime.GOOS + "-" + runtime.GOARCH

	cases := []struct {
		name string
		// present maps sibling file name to its mode; 0o644 is the
		// present-but-unusable case the availability gate must reject.
		present map[string]os.FileMode
		want    string // sibling name, or "" for "no usable engine"
		wantOK  bool
	}{
		{"suffixed preferred over plain", map[string]os.FileMode{suffixed: 0o755, "swe-pro": 0o755}, suffixed, true},
		{"suffixed alone", map[string]os.FileMode{suffixed: 0o755}, suffixed, true},
		{"plain alone is the fallback", map[string]os.FileMode{"swe-pro": 0o755}, "swe-pro", true},
		{"neither present", nil, "", false},
		{"plain usable, suffixed not", map[string]os.FileMode{suffixed: 0o644, "swe-pro": 0o755}, "swe-pro", true},
		// Both unusable: the warning must name the suffixed candidate, the one
		// this platform was meant to run.
		{"both unusable names the best candidate", map[string]os.FileMode{suffixed: 0o644, "swe-pro": 0o644}, suffixed, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvBin, "")
			dir := t.TempDir()
			for name, mode := range tc.present {
				if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\n"), mode); err != nil {
					t.Fatal(err)
				}
			}
			orig := osExecutable
			osExecutable = func() (string, error) { return filepath.Join(dir, "swe-planner"), nil }
			defer func() { osExecutable = orig }()

			want := DefaultBin
			if tc.want != "" {
				want = filepath.Join(dir, tc.want)
			}
			path, ok := ResolveBin()
			if path != want || ok != tc.wantOK {
				t.Errorf("ResolveBin() = (%q, %v), want (%q, %v)", path, ok, want, tc.wantOK)
			}
		})
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

// syncBuffer is a concurrency-safe io.Writer for capturing sidecar output: the
// pipeLines goroutines write while the test goroutine reads.
type syncBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// waitFor polls until want appears in b, or the deadline passes. Returns
// whether it appeared. Polling rather than sleeping a fixed interval keeps the
// test honest on a loaded machine, where spawning a shell can take far longer
// than any hardcoded guess.
func waitFor(b *syncBuffer, want string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(b.String(), want) {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return strings.Contains(b.String(), want)
}

// TestSupervisorStopsOnCancel: a long-running sidecar is interrupted by ctx
// cancellation and the loop winds down promptly.
func TestSupervisorStopsOnCancel(t *testing.T) {
	bin := fakeBin(t, `echo up; trap 'exit 0' INT TERM; while true; do sleep 0.1; done`)
	ctx, cancel := context.WithCancel(context.Background())
	out := &syncBuffer{}
	s := Start(ctx, Options{Server: "http://cp:8080", Bin: bin, Stdout: out, Stderr: out})
	if s == nil {
		t.Fatal("Start returned nil for an existing binary")
	}
	// Cancel only once the sidecar has demonstrably started and its output has
	// been captured — cancelling before it prints is what the old fixed sleep
	// raced against under parallel package load.
	if !waitFor(out, "[pro-engine] up", 15*time.Second) {
		cancel()
		t.Fatalf("sidecar stdout not prefixed/captured: %q", out.String())
	}
	cancel()
	done := make(chan struct{})
	go func() { s.Wait(10 * time.Second); close(done) }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("supervisor did not stop after cancel")
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

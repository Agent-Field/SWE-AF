package furrow

import (
	"bytes"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

const testKey = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

type fakeExec struct {
	mu       sync.Mutex
	commands [][]string
	errFor   map[string]error
	// onCommand runs outside the recording lock so a test can park inside a
	// call and observe whether another one proceeds alongside it.
	onCommand func(args []string)
}

func (f *fakeExec) run(cmd *exec.Cmd) ([]byte, error) {
	f.mu.Lock()
	f.commands = append(f.commands, append([]string(nil), cmd.Args...))
	f.mu.Unlock()
	if f.onCommand != nil {
		f.onCommand(cmd.Args)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !containsEnv(cmd.Env, "FURROW_DATA_DIR=") {
		return nil, errors.New("FURROW_DATA_DIR missing")
	}
	operation := strings.Join(cmd.Args[4:], " ")
	if err := f.errFor[operation]; err != nil {
		return nil, err
	}
	if len(cmd.Args) > 4 && cmd.Args[4] == "remote" {
		return []byte(`{"remote":"local","namespace":"ns","key_hex":"` + testKey + `","machine_id":"m"}`), nil
	}
	return []byte(`{}`), nil
}

func (f *fakeExec) snapshot() [][]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	result := make([][]string, len(f.commands))
	for i := range f.commands {
		result[i] = append([]string(nil), f.commands[i]...)
	}
	return result
}

func containsEnv(env []string, prefix string) bool {
	for _, value := range env {
		if strings.HasPrefix(value, prefix) {
			return true
		}
	}
	return false
}

func testManager(t *testing.T, fake *fakeExec, now func() time.Time) (*Manager, string, string) {
	t.Helper()
	t.Setenv(EnvEnabled, "1")
	root := t.TempDir()
	bin := filepath.Join(root, "furrow")
	if err := os.WriteFile(bin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	repo := filepath.Join(root, "repo")
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	remotes := filepath.Join(root, "remotes")
	m := New(Options{Bin: bin, StoreRoot: filepath.Join(root, "store"), RemotesRoot: remotes, Now: now, Exec: fake.run,
		Logger: log.New(&bytes.Buffer{}, "", 0)})
	return m, repo, remotes
}

func TestNilManagerNoOps(t *testing.T) {
	var m *Manager
	if m.Enabled() || m.Handle("run") != nil {
		t.Fatal("nil manager reported enabled or returned a handle")
	}
	if handle, err := m.Attach("run", "build", t.TempDir()); handle != nil || err != nil {
		t.Fatalf("Attach() = (%v, %v), want (nil, nil)", handle, err)
	}
	if err := m.Publish("run", "label"); err != nil {
		t.Fatal(err)
	}
	if err := m.Detach("run"); err != nil {
		t.Fatal(err)
	}
	if count, err := m.Sweep(time.Hour, 1); count != 0 || err != nil {
		t.Fatalf("Sweep() = (%d, %v), want (0, nil)", count, err)
	}
}

func TestManagerDisabled(t *testing.T) {
	for _, tc := range []struct {
		name string
		env  string
		bin  string
	}{
		{"environment opt-out", "0", "unused"},
		{"unset is opt-out", "", "unused"},
		{"missing binary", "1", filepath.Join(t.TempDir(), "missing")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvEnabled, tc.env)
			t.Setenv(EnvPublicAddr, "")
			m := New(Options{Bin: tc.bin, Logger: log.New(&bytes.Buffer{}, "", 0)})
			if m.Enabled() {
				t.Fatal("manager is enabled")
			}
		})
	}
}

// Mirroring is opt-in and the flag is written by hand into a manifest, a
// compose file or a shell, so it has to survive the spellings people actually
// use — and, more importantly, an unrecognised value must never be what turns
// a workspace-copying feature ON.
func TestManagerEnableFlagSpellings(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "furrow")
	if err := os.WriteFile(bin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		value string
		want  bool
	}{
		{"1", true}, {"true", true}, {"TRUE", true}, {"yes", true}, {"On", true}, {" 1 ", true},
		{"0", false}, {"false", false}, {"NO", false}, {"off", false}, {"", false},
		{"maybe", false}, {"2", false}, {"disabled", false},
	} {
		t.Run(fmt.Sprintf("%s=%q", EnvEnabled, tc.value), func(t *testing.T) {
			t.Setenv(EnvEnabled, tc.value)
			t.Setenv(EnvPublicAddr, "")
			m := New(Options{Bin: bin, StoreRoot: filepath.Join(t.TempDir(), "store"),
				RemotesRoot: filepath.Join(t.TempDir(), "remotes"),
				Exec:        func(*exec.Cmd) ([]byte, error) { return []byte(`{}`), nil },
				Logger:      log.New(&bytes.Buffer{}, "", 0)})
			if got := m.Enabled(); got != tc.want {
				t.Fatalf("Enabled() with %s=%q = %v, want %v", EnvEnabled, tc.value, got, tc.want)
			}
		})
	}
}

// With SWE_FURROW_ENABLED unconfigured, mirroring follows FURROW_PUBLIC_ADDR:
// the desktop cloud deploy sets it exactly when it provisioned a public sync
// port, so a cloud control plane mirrors out of the box and a local install
// that set neither variable stays off. An explicit SWE_FURROW_ENABLED beats the
// address in both directions, and an unrecognised spelling still means OFF even
// with the address present — a typo must never be what copies a workspace.
func TestManagerAutoEnableFollowsPublicAddr(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "furrow")
	if err := os.WriteFile(bin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name    string
		enabled string
		addr    string
		want    bool
	}{
		{"unset follows present addr", "", "mirror.example:31427", true},
		{"blank follows present addr", "   ", "mirror.example:31427", true},
		{"unset with no addr stays off", "", "", false},
		{"unset with blank addr stays off", "", "  ", false},
		{"explicit off beats addr", "0", "mirror.example:31427", false},
		{"explicit on needs no addr", "1", "", true},
		{"typo means off even with addr", "maybe", "mirror.example:31427", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvEnabled, tc.enabled)
			t.Setenv(EnvPublicAddr, tc.addr)
			m := New(Options{Bin: bin, StoreRoot: filepath.Join(t.TempDir(), "store"),
				RemotesRoot: filepath.Join(t.TempDir(), "remotes"),
				Exec:        func(*exec.Cmd) ([]byte, error) { return []byte(`{}`), nil },
				Logger:      log.New(&bytes.Buffer{}, "", 0)})
			if got := m.Enabled(); got != tc.want {
				t.Fatalf("Enabled() with %s=%q %s=%q = %v, want %v",
					EnvEnabled, tc.enabled, EnvPublicAddr, tc.addr, got, tc.want)
			}
		})
	}
}

// An empty run ID is a key two builds SHARE, not a label one build is missing.
// Sanitization used to turn it into the namespace "run", so a second build
// attaching without an ID got the first build's registry row back — its
// workspace path, its recovery key and its transport token. Refusing is the
// only outcome that cannot leak one build's mirror to another.
func TestAttachRefusesEmptyRunID(t *testing.T) {
	fake := &fakeExec{}
	m, repoA, _ := testManager(t, fake, time.Now)
	repoB := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoB, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	before := len(fake.snapshot())

	first, err := m.Attach("", "build-a", repoA)
	if first != nil || err != nil {
		t.Fatalf("Attach(\"\") = (%v, %v), want (nil, nil)", first, err)
	}
	second, err := m.Attach("", "build-b", repoB)
	if second != nil || err != nil {
		t.Fatalf("second Attach(\"\") = (%v, %v), want (nil, nil)", second, err)
	}
	if len(m.entries) != 0 {
		t.Fatalf("registry entries = %v, want none", m.entries)
	}
	if got := fake.snapshot()[before:]; len(got) != 0 {
		t.Fatalf("furrow was invoked for a run with no ID: %v", got)
	}
	if handle := m.Handle(""); handle != nil {
		t.Fatalf("Handle(\"\") = %v, want nil", handle)
	}
}

func TestAttachMissingGit(t *testing.T) {
	fake := &fakeExec{}
	m, _, _ := testManager(t, fake, time.Now)
	// Construction sets the store budget; what matters here is that ATTACH
	// itself runs nothing when the path is not a git repository.
	before := len(fake.snapshot())
	handle, err := m.Attach("run", "build", t.TempDir())
	if err != nil || handle != nil || len(fake.snapshot()) != before {
		t.Fatalf("Attach() = (%v, %v), commands=%v", handle, err, fake.snapshot()[before:])
	}
}

func TestCommandTimesOut(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell script test")
	}
	t.Setenv(EnvEnabled, "1")
	root := t.TempDir()
	bin := filepath.Join(root, "furrow")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\nsleep 60\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := New(Options{Bin: bin, StoreRoot: filepath.Join(root, "store"), CmdTimeout: 200 * time.Millisecond,
		Logger: log.New(&bytes.Buffer{}, "", 0)})
	started := time.Now()
	_, err := m.command(root, "snap")
	if err == nil || !strings.Contains(err.Error(), "timeout") {
		t.Fatalf("command error = %v, want timeout", err)
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Fatalf("timed-out command returned after %s", elapsed)
	}
}

func TestAttachRefusesWhenAggregateStoreExceedsBudget(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses /bin/false")
	}
	t.Setenv(EnvEnabled, "1")
	root := t.TempDir()
	store := filepath.Join(root, "store")
	repo := filepath.Join(root, "repo")
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(store, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(store, "client-data"), make([]byte, 100), 0o600); err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	m := New(Options{Bin: "/bin/false", StoreRoot: store, RemotesRoot: filepath.Join(root, "remotes"),
		MaxBytes: 10, Logger: log.New(&logs, "", 0)})
	handle, err := m.Attach("run", "build", repo)
	if err == nil || !strings.Contains(err.Error(), "disk budget") || handle != nil {
		t.Fatalf("Attach() = (%v, %v), want disk budget error", handle, err)
	}
	if len(m.entries) != 0 {
		t.Fatalf("registered entries = %v, want none", m.entries)
	}
	if got := strings.Count(logs.String(), "WARN "); got != 1 {
		t.Fatalf("warning count = %d, logs = %q", got, logs.String())
	}
}

// The budget only protects the volume when the volume is bigger than the
// budget: a cloud deploy's mirrors share one disk with the control plane's
// database, so Attach also enforces an absolute free-space floor. A probe that
// cannot answer (ok=false) must NOT refuse — "no answer" is not "no space".
func TestAttachRefusesWhenDiskNearlyFull(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses /bin/false")
	}
	t.Setenv(EnvEnabled, "1")
	for _, tc := range []struct {
		name    string
		probe   func(string) (int64, bool)
		refused bool
	}{
		{"below floor refuses", func(string) (int64, bool) { return minFreeBytes - 1, true }, true},
		{"at floor proceeds", func(string) (int64, bool) { return minFreeBytes, true }, false},
		{"probe unavailable proceeds", func(string) (int64, bool) { return 0, false }, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			repo := filepath.Join(root, "repo")
			if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
				t.Fatal(err)
			}
			var logs bytes.Buffer
			m := New(Options{Bin: "/bin/false", StoreRoot: filepath.Join(root, "store"),
				RemotesRoot: filepath.Join(root, "remotes"), Logger: log.New(&logs, "", 0)})
			m.freeBytes = tc.probe
			handle, err := m.Attach("run", "build", repo)
			if tc.refused {
				if err == nil || !strings.Contains(err.Error(), "floor") || handle != nil {
					t.Fatalf("Attach() = (%v, %v), want free-space floor error", handle, err)
				}
				if len(m.entries) != 0 {
					t.Fatalf("registered entries = %v, want none", m.entries)
				}
				return
			}
			// Past the floor the attach proceeds into the furrow invocations,
			// where /bin/false fails the watch — proof the gate did not trip.
			if err != nil || handle != nil {
				t.Fatalf("Attach() = (%v, %v), want (nil, nil) degradation past the gate", handle, err)
			}
			if strings.Contains(logs.String(), "-byte floor") {
				t.Fatalf("free-space floor tripped unexpectedly: %q", logs.String())
			}
		})
	}
}

func TestAttachPublishExactArgvAndIdempotence(t *testing.T) {
	fake := &fakeExec{}
	m, repo, remotes := testManager(t, fake, time.Now)
	handle, err := m.Attach("run/one", "build-1", repo)
	if err != nil || handle == nil {
		t.Fatalf("Attach() = (%v, %v)", handle, err)
	}
	if handle.Key != testKey {
		t.Fatalf("Handle.Key = %q, want key_hex value", handle.Key)
	}
	// The run's own store, not the root. Pairing with the root would find no
	// workspace there, so a handle pointing at it is unusable.
	if handle.Remote != "dir:"+filepath.Join(remotes, "run-one") || len(handle.Token) != 64 {
		t.Fatalf("unexpected handle: %+v", handle)
	}
	second, err := m.Attach("run/one", "different", repo)
	if err != nil || !reflect.DeepEqual(handle, second) {
		t.Fatalf("idempotent Attach() = (%v, %v), want %v", second, err, handle)
	}
	if err := m.Publish("run/one", "checkpoint"); err != nil {
		t.Fatal(err)
	}
	bin := m.bin
	store := filepath.Join(filepath.Dir(remotes), "store")
	want := [][]string{
		// Capping furrow's own store at half the allowance is what keeps the
		// sweeper — which can only reclaim remotes — from ever being left with
		// nothing to free while the budget stays exceeded.
		{bin, "--repo", store, "--json", "budget", "--max", "10737418240"},
		{bin, "--repo", repo, "--json", "watch", "--no-daemon"},
		{bin, "--repo", repo, "--json", "remote", "add", filepath.Join(remotes, "run-one"), "--name", "run-one"},
		{bin, "--repo", repo, "--json", "snap", "-m", "attached"},
		{bin, "--repo", repo, "--json", "sync", "--push"},
		{bin, "--repo", repo, "--json", "snap", "-m", "checkpoint"},
		{bin, "--repo", repo, "--json", "sync", "--push"},
	}
	if got := fake.snapshot(); !reflect.DeepEqual(got, want) {
		t.Fatalf("argv mismatch\n got: %#v\nwant: %#v", got, want)
	}
	// furrow rejects a policy file whose lines are not `exclude <subtree>`, and a
	// rejected file fails `watch`, which switches the mirror off for every build
	// with nothing but a debug line to show for it. Pin the exact bytes.
	policy, err := os.ReadFile(filepath.Join(repo, ".furrowpolicy"))
	if err != nil || string(policy) != "exclude .obs\nexclude node_modules\n" {
		t.Fatalf("policy = %q, %v", policy, err)
	}
	if err := m.Publish("unknown", "label"); err == nil {
		t.Fatal("Publish accepted unknown run ID")
	}
}

func TestPublicHandleRequiresHealthyTransport(t *testing.T) {
	fake := &fakeExec{}
	m, repo, remotes := testManager(t, fake, time.Now)
	m.publicAddr = "mirror.example:8802"

	m.SetTransportHealth(func() bool { return false })
	handle, err := m.Attach("run", "build", repo)
	if err != nil || handle == nil {
		t.Fatalf("Attach() = (%v, %v)", handle, err)
	}
	if want := "dir:" + filepath.Join(remotes, "run"); handle.Remote != want {
		t.Fatalf("unhealthy Remote = %q, want %q", handle.Remote, want)
	}

	m.SetTransportHealth(func() bool { return true })
	if got := m.Handle("run").Remote; got != "ssh://mirror.example:8802" {
		t.Fatalf("healthy Remote = %q", got)
	}
}

// SWE_FURROW_MAX_GB=0 is the operator saying "do not cap my disk". Every other
// consumer of the budget already read it that way; the sweeper read `>= 0` and
// so treated the no-cap setting as a zero-byte cap, retiring every mirror on
// the node — live builds included — on every hourly tick.
func TestSweepTreatsNonPositiveBudgetAsUnlimited(t *testing.T) {
	for _, maxBytes := range []int64{0, -1} {
		t.Run(fmt.Sprintf("maxBytes=%d", maxBytes), func(t *testing.T) {
			fake := &fakeExec{}
			m, repo, _ := testManager(t, fake, time.Now)
			if _, err := m.Attach("run/live", "build", repo); err != nil {
				t.Fatalf("Attach: %v", err)
			}
			entry := m.entries["run/live"]

			removed, err := m.Sweep(0, maxBytes)
			if err != nil {
				t.Fatalf("Sweep: %v", err)
			}
			if removed != 0 {
				t.Fatalf("Sweep removed %d entries under an unlimited budget, want 0", removed)
			}
			if _, ok := m.entries["run/live"]; !ok {
				t.Fatal("unlimited budget retired the run's registry row")
			}
			if _, err := os.Stat(entry.StoreDir); err != nil {
				t.Fatalf("unlimited budget deleted the run's remote store: %v", err)
			}
		})
	}
}

// retire promises that "a run that became active in the meantime is left
// alone". Budget eviction called it with maxAge 0, which turned that re-check
// off, so being over budget deleted the workspace of whichever run happened to
// have published least recently — including one that was mid-build. Reclaiming
// disk is worth less than a live mirror: refusing NEW mirrors is recoverable,
// deleting a running build's is not.
func TestSweepBudgetSparesRunsThatAreStillPublishing(t *testing.T) {
	fake := &fakeExec{}
	clock := time.Now()
	m, abandonedRepo, _ := testManager(t, fake, func() time.Time { return clock })
	liveRepo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(liveRepo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Attach("run/abandoned", "build", abandonedRepo); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	// Well past the grace window for the first run; the second attaches (and so
	// publishes) right now, which is exactly what a mid-build run looks like.
	clock = clock.Add(3 * time.Hour)
	if _, err := m.Attach("run/live", "build", liveRepo); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	liveStore := m.entries["run/live"].StoreDir

	// A one-byte budget: the store is over it no matter what, so eviction runs
	// until it either frees enough or runs out of candidates it may touch.
	removed, err := m.Sweep(0, 1)
	if err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if removed != 1 {
		t.Fatalf("Sweep removed %d entries, want 1 (the abandoned run only)", removed)
	}
	if _, ok := m.entries["run/abandoned"]; ok {
		t.Error("abandoned run survived budget eviction")
	}
	if _, ok := m.entries["run/live"]; !ok {
		t.Error("budget eviction retired a run that published moments ago")
	}
	if _, err := os.Stat(liveStore); err != nil {
		t.Errorf("live run's remote store was deleted: %v", err)
	}
}

// The other half of the same promise: a run that republishes between being
// chosen as the victim and the retirement taking its lock must survive.
func TestRetireSkipsAnEntryThatMovedSinceItWasChosen(t *testing.T) {
	fake := &fakeExec{}
	clock := time.Now()
	m, repo, _ := testManager(t, fake, func() time.Time { return clock })
	if _, err := m.Attach("run/one", "build", repo); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	observed := m.entries["run/one"].UpdatedAt
	clock = clock.Add(3 * time.Hour)

	// The run publishes after the sweeper measured it: same run, newer stamp.
	if err := m.Publish("run/one", "checkpoint"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	dropped, err := m.retire("run/one", m.abandonedSince(observed))
	if err != nil {
		t.Fatalf("retire: %v", err)
	}
	if dropped {
		t.Fatal("retire deleted a run that republished after it was chosen")
	}
	if _, ok := m.entries["run/one"]; !ok {
		t.Fatal("registry row removed")
	}
}

func TestAttachSanitizesRemoteStorePath(t *testing.T) {
	for _, runID := range []string{"../escape", "/absolute", "..", "."} {
		t.Run(runID, func(t *testing.T) {
			fake := &fakeExec{}
			clock := time.Now()
			m, repo, remotes := testManager(t, fake, func() time.Time { return clock })
			outside := filepath.Join(filepath.Dir(remotes), "escape")
			if err := os.WriteFile(outside, []byte("keep"), 0o600); err != nil {
				t.Fatal(err)
			}
			handle, err := m.Attach(runID, "build", repo)
			if err != nil || handle == nil {
				t.Fatalf("Attach() = (%v, %v)", handle, err)
			}
			entry := m.entries[runID]
			if filepath.Dir(entry.StoreDir) != remotes || filepath.Base(entry.StoreDir) != sanitizeNamespace(runID) {
				t.Fatalf("StoreDir %q escaped remotes root %q", entry.StoreDir, remotes)
			}
			// Age the entry past the TTL and sweep by age alone: a zero budget
			// means unlimited disk, so it would no longer retire anything and
			// the deletion this test is about would never run.
			clock = clock.Add(2 * time.Hour)
			if _, err := m.Sweep(time.Hour, 0); err != nil {
				t.Fatal(err)
			}
			if _, ok := m.entries[runID]; ok {
				t.Fatalf("entry %q survived the age sweep, so nothing was deleted", runID)
			}
			if got, err := os.ReadFile(outside); err != nil || string(got) != "keep" {
				t.Fatalf("outside sentinel = %q, %v", got, err)
			}
		})
	}
}

// StoreDir is read back out of a JSON file on disk and handed straight to
// os.RemoveAll. Attach only ever writes remotesRoot/<sanitized namespace>, so
// anything else is a corrupted or hand-edited row — and honouring it would let
// that file choose what the node deletes.
func TestRetireNeverDeletesOutsideTheRemotesRoot(t *testing.T) {
	for _, tc := range []struct{ name, storeDir string }{
		{name: "sibling directory", storeDir: "sibling"},
		{name: "the remotes root itself", storeDir: "root"},
		{name: "empty", storeDir: ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeExec{}
			clock := time.Now()
			m, repo, remotes := testManager(t, fake, func() time.Time { return clock })
			if _, err := m.Attach("run/one", "build", repo); err != nil {
				t.Fatalf("Attach: %v", err)
			}
			sibling := filepath.Join(filepath.Dir(remotes), "not-ours")
			if err := os.MkdirAll(sibling, 0o700); err != nil {
				t.Fatal(err)
			}
			sentinel := filepath.Join(sibling, "keep")
			if err := os.WriteFile(sentinel, []byte("keep"), 0o600); err != nil {
				t.Fatal(err)
			}
			rootSentinel := filepath.Join(remotes, "keep")
			if err := os.WriteFile(rootSentinel, []byte("keep"), 0o600); err != nil {
				t.Fatal(err)
			}

			// Corrupt the row the way an edited registry.json would.
			entry := m.entries["run/one"]
			switch tc.storeDir {
			case "sibling":
				entry.StoreDir = sibling
			case "root":
				entry.StoreDir = remotes
			default:
				entry.StoreDir = ""
			}
			m.entries["run/one"] = entry

			clock = clock.Add(2 * time.Hour)
			if _, err := m.Sweep(time.Hour, -1); err != nil {
				t.Fatalf("Sweep: %v", err)
			}
			if _, ok := m.entries["run/one"]; ok {
				t.Error("bogus row survived the sweep and will be retried forever")
			}
			for _, path := range []string{sentinel, rootSentinel} {
				if got, err := os.ReadFile(path); err != nil || string(got) != "keep" {
					t.Errorf("sweep deleted %q: %q, %v", path, got, err)
				}
			}
		})
	}
}

// A node serves several builds at once and an initial capture of a large
// repository is slow, so work on one run must not block another. This fails if
// the manager ever goes back to holding one lock across furrow invocations:
// each publish parks inside the fake exec until both have arrived, which can
// only happen if they run concurrently.
func TestPublishesForDifferentRunsDoNotSerialize(t *testing.T) {
	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	block := false
	blocking := &fakeExec{onCommand: func(args []string) {
		if block && len(args) > 4 && args[4] == "sync" {
			arrived <- struct{}{}
			<-release
		}
	}}
	m, repoA, _ := testManager(t, blocking, time.Now)
	repoB := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoB, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ run, repo string }{{"run/a", repoA}, {"run/b", repoB}} {
		if _, err := m.Attach(tc.run, "build", tc.repo); err != nil {
			t.Fatalf("Attach(%s): %v", tc.run, err)
		}
	}
	block = true

	done := make(chan struct{}, 2)
	for _, run := range []string{"run/a", "run/b"} {
		go func(runID string) {
			_ = m.Publish(runID, "checkpoint")
			done <- struct{}{}
		}(run)
	}
	for i := 0; i < 2; i++ {
		select {
		case <-arrived:
		case <-time.After(5 * time.Second):
			t.Fatal("publishes serialized: the second never reached furrow while the first was in flight")
		}
	}
	close(release)
	for i := 0; i < 2; i++ {
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("publish did not return")
		}
	}
}

func TestPublishTransportFailuresAreNonFatal(t *testing.T) {
	for _, operation := range []string{"snap -m attached", "snap -m label", "sync --push"} {
		t.Run(operation, func(t *testing.T) {
			fake := &fakeExec{errFor: map[string]error{operation: errors.New("transport down")}}
			m, repo, _ := testManager(t, fake, time.Now)
			handle, err := m.Attach("run", "build", repo)
			if err != nil || handle == nil {
				t.Fatalf("Attach() = (%v, %v), want a handle despite publish failure", handle, err)
			}
			if err := m.Publish("run", "label"); err != nil {
				t.Fatalf("Publish returned transport error: %v", err)
			}
		})
	}
}

func TestNamespaceSanitizationAndTruncation(t *testing.T) {
	cases := []struct{ input, want string }{
		{"abc.DEF_123-xy", "abc.DEF_123-xy"},
		{"run/with spaces/☃", "run-with-spaces--"},
		{"", "run"},
		{strings.Repeat("a", 100), strings.Repeat("a", 96)},
	}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("%q", tc.input), func(t *testing.T) {
			if got := sanitizeNamespace(tc.input); got != tc.want {
				t.Fatalf("sanitizeNamespace(%q) = %q, want %q", tc.input, got, tc.want)
			}
		})
	}
}

func TestConcurrentAttachPublish(t *testing.T) {
	fake := &fakeExec{}
	m, repo, _ := testManager(t, fake, time.Now)
	const workers = 24
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := m.Attach("run", "build", repo); err != nil {
				t.Errorf("Attach: %v", err)
			}
			if err := m.Publish("run", "live"); err != nil {
				t.Errorf("Publish: %v", err)
			}
		}()
	}
	wg.Wait()
	watchCount := 0
	remoteCount := 0
	for _, argv := range fake.snapshot() {
		if len(argv) > 4 && argv[4] == "watch" {
			watchCount++
		}
		if len(argv) > 4 && argv[4] == "remote" {
			remoteCount++
		}
	}
	if watchCount != 1 || remoteCount != 1 {
		t.Fatalf("pairing commands = watch:%d remote:%d, want one each", watchCount, remoteCount)
	}
}

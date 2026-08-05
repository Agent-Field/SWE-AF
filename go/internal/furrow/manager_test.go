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
		{"missing binary", "1", filepath.Join(t.TempDir(), "missing")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvEnabled, tc.env)
			m := New(Options{Bin: tc.bin, Logger: log.New(&bytes.Buffer{}, "", 0)})
			if m.Enabled() {
				t.Fatal("manager is enabled")
			}
		})
	}
}

func TestAttachMissingGit(t *testing.T) {
	fake := &fakeExec{}
	m, _, _ := testManager(t, fake, time.Now)
	handle, err := m.Attach("run", "build", t.TempDir())
	if err != nil || handle != nil || len(fake.snapshot()) != 0 {
		t.Fatalf("Attach() = (%v, %v), commands=%v", handle, err, fake.snapshot())
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
	if handle.Remote != "dir:"+filepath.Join(remotes, "run/one") || len(handle.Token) != 64 {
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
	want := [][]string{
		{bin, "--repo", repo, "--json", "watch", "--no-daemon"},
		{bin, "--repo", repo, "--json", "remote", "add", filepath.Join(remotes, "run/one"), "--name", "run-one"},
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

// A node serves several builds at once and an initial capture of a large
// repository is slow, so work on one run must not block another. This fails if
// the manager ever goes back to holding one lock across furrow invocations:
// each publish parks inside the fake exec until both have arrived, which can
// only happen if they run concurrently.
func TestPublishesForDifferentRunsDoNotSerialize(t *testing.T) {
	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	blocking := &fakeExec{onCommand: func(args []string) {
		if len(args) > 4 && args[4] == "sync" {
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
	for _, operation := range []string{"snap -m label", "sync --push"} {
		t.Run(operation, func(t *testing.T) {
			fake := &fakeExec{errFor: map[string]error{operation: errors.New("transport down")}}
			m, repo, _ := testManager(t, fake, time.Now)
			if _, err := m.Attach("run", "build", repo); err != nil {
				t.Fatal(err)
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

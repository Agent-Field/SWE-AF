package furrow

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRegistryRoundTripAndMode(t *testing.T) {
	fake := &fakeExec{}
	m, repo, remotes := testManager(t, fake, time.Now)
	want, err := m.Attach("run", "build", repo)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(remotes, "registry.json"))
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("registry mode = %o, want 600", got)
	}
	reloaded := New(Options{Bin: m.bin, StoreRoot: m.storeRoot, RemotesRoot: remotes, Exec: fake.run,
		Logger: log.New(&bytes.Buffer{}, "", 0)})
	if got := reloaded.Handle("run"); got == nil || got.Key != want.Key || got.Namespace != want.Namespace {
		t.Fatalf("reloaded handle = %+v, want %+v", got, want)
	}
}

func TestCorruptRegistryRecovery(t *testing.T) {
	t.Setenv(EnvEnabled, "1")
	root := t.TempDir()
	bin := filepath.Join(root, "furrow")
	remotes := filepath.Join(root, "remotes")
	if err := os.WriteFile(bin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(remotes, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(remotes, "registry.json"), []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	m := New(Options{Bin: bin, StoreRoot: filepath.Join(root, "store"), RemotesRoot: remotes,
		Exec: (&fakeExec{}).run, Logger: log.New(&logs, "", 0)})
	if m.Handle("run") != nil || !bytes.Contains(logs.Bytes(), []byte("corrupt")) {
		t.Fatalf("corrupt registry did not recover empty with log: %q", logs.String())
	}
}

func TestSweepByAgeAndSize(t *testing.T) {
	t.Run("age", func(t *testing.T) {
		fake := &fakeExec{}
		now := time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)
		current := now.Add(-2 * time.Hour)
		m, repo, _ := testManager(t, fake, func() time.Time { return current })
		if _, err := m.Attach("old", "build", repo); err != nil {
			t.Fatal(err)
		}
		current = now
		if _, err := m.Attach("new", "build", repo); err != nil {
			t.Fatal(err)
		}
		removed, err := m.Sweep(time.Hour, 1<<30)
		if err != nil || removed != 1 || m.Handle("old") != nil || m.Handle("new") == nil {
			t.Fatalf("Sweep() = (%d, %v), old=%v new=%v", removed, err, m.Handle("old"), m.Handle("new"))
		}
	})

	t.Run("size oldest first", func(t *testing.T) {
		fake := &fakeExec{}
		current := time.Date(2026, 8, 5, 10, 0, 0, 0, time.UTC)
		m, repo, remotes := testManager(t, fake, func() time.Time { return current })
		if _, err := m.Attach("old", "build", repo); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(remotes, "old", "data"), make([]byte, 100), 0o600); err != nil {
			t.Fatal(err)
		}
		current = current.Add(time.Hour)
		if _, err := m.Attach("new", "build", repo); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(remotes, "new", "data"), make([]byte, 10), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(m.storeRoot, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(m.storeRoot, "client-data"), make([]byte, 100), 0o600); err != nil {
			t.Fatal(err)
		}
		// The client store and registry both count toward the total. Pick a
		// ceiling just below the aggregate so exactly the oldest remote goes.
		total, err := m.aggregateSize()
		if err != nil {
			t.Fatal(err)
		}
		removed, err := m.Sweep(0, total-50)
		if err != nil || removed != 1 || m.Handle("old") != nil || m.Handle("new") == nil {
			t.Fatalf("Sweep() = (%d, %v), old=%v new=%v", removed, err, m.Handle("old"), m.Handle("new"))
		}
	})
}

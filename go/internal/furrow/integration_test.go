package furrow_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/furrow"
)

// The unit tests inject a fake exec and assert argv, which pins what we *mean*
// to run. This one runs the real binary, because the two ways that contract has
// actually broken — a flag that does not exist (`snap --label`) and a JSON field
// spelled differently than assumed (`key_hex`) — are both invisible to a fake.
// Skips when no furrow binary is installed, so it is free for everyone else.
func TestRealBinaryAttachPublishAndClone(t *testing.T) {
	bin, err := furrow.ResolveBin()
	if err != nil {
		t.Skipf("no furrow binary: %v", err)
	}

	root := t.TempDir()
	repo := filepath.Join(root, "myrepo-b33f")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	git(t, "", "init", "--quiet", "--", repo)
	git(t, repo, "config", "user.email", "swe@af.local")
	git(t, repo, "config", "user.name", "SWE AF")
	write(t, filepath.Join(repo, "solver.py"), "def solve(): return 41\n")
	git(t, repo, "add", "-A")
	git(t, repo, "commit", "--quiet", "-m", "init")

	// The state git would never carry: an untracked secret and a dirty edit.
	// Carrying these is the entire reason for mirroring rather than pushing.
	write(t, filepath.Join(repo, ".env"), "TOKEN=shhh\n")
	write(t, filepath.Join(repo, "solver.py"), "def solve(): return 42\n")

	m := furrow.New(furrow.Options{
		Bin:         bin,
		StoreRoot:   filepath.Join(root, "store"),
		RemotesRoot: filepath.Join(root, "remotes"),
	})
	if !m.Enabled() {
		t.Fatal("manager disabled with a resolvable binary")
	}

	handle, err := m.Attach("run-int-0001", "b33f", repo)
	if err != nil {
		t.Fatalf("Attach: %v", err)
	}
	if handle == nil {
		t.Fatal("Attach returned no handle for a real git repo")
	}
	if len(handle.Key) != 64 {
		t.Fatalf("recovery key = %q, want 64 hex chars (is the JSON field still key_hex?)", handle.Key)
	}
	if handle.Token == "" || handle.Namespace == "" {
		t.Fatalf("incomplete handle: %+v", handle)
	}

	// Attach itself must publish a HEAD. A caller receives the handle before any
	// build milestone, so it must be able to pair and materialize immediately.
	dest := filepath.Join(root, "clone")
	if err := os.MkdirAll(dest, 0o755); err != nil {
		t.Fatal(err)
	}
	git(t, "", "init", "--quiet", "--", dest)
	cloneStore := filepath.Join(root, "store-clone")
	remoteDir, ok := strings.CutPrefix(handle.Remote, "dir:")
	if !ok {
		t.Fatalf("remote = %q, want a dir: handle when no public address is set", handle.Remote)
	}
	furrowCmd(t, bin, cloneStore, dest, "watch", "--no-daemon")
	furrowCmd(t, bin, cloneStore, dest, "pair", remoteDir, "--name", handle.Namespace, "--key", handle.Key)
	furrowCmd(t, bin, cloneStore, dest, "sync", "--pull", "--bootstrap")
	assertFileContains(t, dest, "solver.py", "return 42")
	assertFileContains(t, dest, ".env", "TOKEN=shhh")

	// Work lands after the attach, exactly as a coding agent produces it.
	write(t, filepath.Join(repo, "feature.py"), "print('agent wrote this')\n")
	if err := m.Publish("run-int-0001", "issue-01 complete"); err != nil {
		t.Fatalf("Publish: %v", err)
	}

	// Materialize on a fresh store, standing in for another machine. `furrow
	// clone` only accepts ssh:// and s3:// URLs, so a directory remote is
	// reproduced by the sequence clone performs internally.
	// Everything below comes from the handle alone — no path assembled by the
	// test. A consumer only ever has the handle, so if it is not sufficient on
	// its own, the mirror is unreachable however correct the rest is.
	furrowCmd(t, bin, cloneStore, dest, "sync", "--pull", "--bootstrap")

	for _, tc := range []struct{ path, want string }{
		{"solver.py", "return 42"},    // the uncommitted edit
		{".env", "TOKEN=shhh"},        // untracked, git-invisible
		{"feature.py", "agent wrote"}, // written after attach, carried by Publish
	} {
		got, err := os.ReadFile(filepath.Join(dest, tc.path))
		if err != nil {
			t.Errorf("%s missing from mirror: %v", tc.path, err)
			continue
		}
		if !strings.Contains(string(got), tc.want) {
			t.Errorf("%s = %q, want it to contain %q", tc.path, got, tc.want)
		}
	}
	if _, err := os.Stat(filepath.Join(dest, ".git")); err != nil {
		t.Errorf("mirror has no .git, so the caller cannot diff or commit: %v", err)
	}
}

func assertFileContains(t *testing.T, root, path, want string) {
	t.Helper()
	got, err := os.ReadFile(filepath.Join(root, path))
	if err != nil {
		t.Fatalf("%s missing from mirror: %v", path, err)
	}
	if !strings.Contains(string(got), want) {
		t.Fatalf("%s = %q, want it to contain %q", path, got, want)
	}
}

func furrowCmd(t *testing.T, bin, store, repo string, args ...string) {
	t.Helper()
	cmd := exec.Command(bin, append([]string{"--repo", repo, "--json"}, args...)...)
	cmd.Env = append(os.Environ(), "FURROW_DATA_DIR="+store)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("furrow %s: %v\n%s", strings.Join(args, " "), err, out)
	}
}

func git(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

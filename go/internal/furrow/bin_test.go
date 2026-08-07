package furrow

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestResolveBin(t *testing.T) {
	dir := t.TempDir()
	runnableBin := filepath.Join(dir, "runnable")
	if err := os.WriteFile(runnableBin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	nonExecutable := filepath.Join(dir, "non-executable")
	if err := os.WriteFile(nonExecutable, []byte("binary"), 0o644); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name     string
		override string
		want     string
		wantErr  bool
	}{
		{"authoritative runnable override", runnableBin, runnableBin, false},
		{"authoritative missing override", filepath.Join(dir, "missing"), "", true},
		// An operator-chosen path is never rewritten: an explicit override that
		// is not executable fails loudly rather than being silently chmod'd.
		{"authoritative non-executable override", nonExecutable, "", true},
		{"directory is not runnable", dir, "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvBin, tc.override)
			got, err := ResolveBin()
			if got != tc.want || (err != nil) != tc.wantErr {
				t.Fatalf("ResolveBin() = (%q, %v), want (%q, error=%v)", got, err, tc.want, tc.wantErr)
			}
		})
	}
}

// The real install shape that loses the bit: a vendored sibling binary next
// to the node executable, delivered rw-r--r-- by the installer.
func TestResolveBinRepairsStrippedSibling(t *testing.T) {
	dir := t.TempDir()
	self := filepath.Join(dir, "swe-planner")
	if err := os.WriteFile(self, []byte("self"), 0o755); err != nil {
		t.Fatal(err)
	}
	vendored := filepath.Join(dir, "furrow-"+runtime.GOOS+"-"+runtime.GOARCH)
	if err := os.WriteFile(vendored, []byte("binary"), 0o644); err != nil {
		t.Fatal(err)
	}
	orig := osExecutable
	t.Cleanup(func() { osExecutable = orig })
	osExecutable = func() (string, error) { return self, nil }
	t.Setenv(EnvBin, "")

	got, err := ResolveBin()
	if err != nil || got != vendored {
		t.Fatalf("ResolveBin() = (%q, %v), want repaired %q", got, err, vendored)
	}
	info, err := os.Stat(vendored)
	if err != nil || info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("execute bit not repaired: mode=%v err=%v", info.Mode(), err)
	}
}

func TestResolveBinSiblingOrder(t *testing.T) {
	if runnable(DefaultBin) {
		t.Skipf("%s exists and precedes sibling binaries", DefaultBin)
	}
	t.Setenv(EnvBin, "")
	dir := t.TempDir()
	plain := filepath.Join(dir, "furrow")
	suffixed := filepath.Join(dir, "furrow-"+runtime.GOOS+"-"+runtime.GOARCH)
	for _, path := range []string{plain, suffixed} {
		if err := os.WriteFile(path, []byte("binary"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	original := osExecutable
	osExecutable = func() (string, error) { return filepath.Join(dir, "swe-af"), nil }
	t.Cleanup(func() { osExecutable = original })
	got, err := ResolveBin()
	if err != nil || got != suffixed {
		t.Fatalf("ResolveBin() = (%q, %v), want (%q, nil)", got, err, suffixed)
	}
}

func TestResolveDaemonBin(t *testing.T) {
	dir := t.TempDir()
	runnableDaemon := filepath.Join(dir, "furrowd")
	if err := os.WriteFile(runnableDaemon, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvDaemonBin, runnableDaemon)
	if got, err := ResolveDaemonBin(); err != nil || got != runnableDaemon {
		t.Fatalf("ResolveDaemonBin() = (%q, %v), want (%q, nil)", got, err, runnableDaemon)
	}

	missing := filepath.Join(dir, "missing")
	t.Setenv(EnvDaemonBin, missing)
	if got, err := ResolveDaemonBin(); err == nil || got != "" {
		t.Fatalf("ResolveDaemonBin() with authoritative missing override = (%q, %v)", got, err)
	}
}

func TestResolveDaemonBinSiblingOrder(t *testing.T) {
	if runnable(DefaultDaemonBin) {
		t.Skipf("%s exists and precedes sibling binaries", DefaultDaemonBin)
	}
	t.Setenv(EnvDaemonBin, "")
	dir := t.TempDir()
	plain := filepath.Join(dir, "furrowd")
	suffixed := filepath.Join(dir, "furrowd-"+runtime.GOOS+"-"+runtime.GOARCH)
	for _, path := range []string{plain, suffixed} {
		if err := os.WriteFile(path, []byte("binary"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	original := osExecutable
	osExecutable = func() (string, error) { return filepath.Join(dir, "swe-af"), nil }
	t.Cleanup(func() { osExecutable = original })
	got, err := ResolveDaemonBin()
	if err != nil || got != suffixed {
		t.Fatalf("ResolveDaemonBin() = (%q, %v), want (%q, nil)", got, err, suffixed)
	}
}

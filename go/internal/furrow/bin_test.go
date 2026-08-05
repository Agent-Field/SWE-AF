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

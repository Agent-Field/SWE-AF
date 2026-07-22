package workspace

import (
	"path/filepath"
	"testing"
)

// fakeEnv builds a getenv closure from a fixed map so each table case drives an
// isolated environment without mutating the real process env.
func fakeEnv(vars map[string]string) func(string) string {
	return func(k string) string { return vars[k] }
}

// TestRootFor covers the full resolution matrix from issue #107: the override
// wins everywhere, Windows never yields a drive-relative "/workspaces", and
// non-Windows stays byte-identical to the historical default.
func TestRootFor(t *testing.T) {
	const fakeTemp = `C:\FakeTemp`
	orig := tempDir
	tempDir = func() string { return fakeTemp }
	t.Cleanup(func() { tempDir = orig })

	cases := []struct {
		name string
		goos string
		env  map[string]string
		want string
	}{
		{
			name: "windows override wins over LOCALAPPDATA",
			goos: "windows",
			env: map[string]string{
				"SWE_WORKSPACE_ROOT": `E:\builds`,
				"LOCALAPPDATA":       `C:\Users\me\AppData\Local`,
			},
			want: `E:\builds`,
		},
		{
			name: "windows LOCALAPPDATA default",
			goos: "windows",
			env:  map[string]string{"LOCALAPPDATA": `C:\Users\me\AppData\Local`},
			want: filepath.Join(`C:\Users\me\AppData\Local`, "agentfield", "workspaces"),
		},
		{
			name: "windows neither falls back to tempdir",
			goos: "windows",
			env:  map[string]string{},
			want: filepath.Join(fakeTemp, "agentfield", "workspaces"),
		},
		{
			name: "linux default is exactly /workspaces",
			goos: "linux",
			env:  map[string]string{},
			want: "/workspaces",
		},
		{
			name: "linux override wins",
			goos: "linux",
			env:  map[string]string{"SWE_WORKSPACE_ROOT": "/mnt/scratch/ws"},
			want: "/mnt/scratch/ws",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := rootFor(tc.goos, fakeEnv(tc.env))
			if got != tc.want {
				t.Fatalf("rootFor(%q, ...) = %q, want %q", tc.goos, got, tc.want)
			}
		})
	}
}

// Package workspace resolves the base directory into which SWE-AF clones build
// repositories when the caller supplies only a repo_url (no explicit
// repo_path). It exists so that default never resolves to a drive-relative
// path on Windows.
//
// A hardcoded "/workspaces" base is drive-relative on Windows: it carries no
// drive letter, so the Windows Go node's spawn context resolves it
// unpredictably. os.MkdirAll appears to succeed, but the following `git clone`
// fails with "destination path ... already exists and is not an empty
// directory" — git's is_empty_dir reports "not empty" when it cannot open the
// directory it was handed from that context. Rooting the default under an
// absolute drive-letter base (%LOCALAPPDATA%) removes the ambiguity while
// keeping byte-identical "/workspaces" behavior on every other platform
// (Docker parity).
//
// Ref: https://github.com/Agent-Field/SWE-AF/issues/107
package workspace

import (
	"os"
	"path/filepath"
	"runtime"
)

// tempDir is indirected so tests can drive the LOCALAPPDATA-empty Windows
// fallback deterministically. Production always uses os.TempDir.
var tempDir = os.TempDir

// Root returns the base directory under which build repositories are cloned.
// See rootFor for the resolution rules.
func Root() string {
	return rootFor(runtime.GOOS, os.Getenv)
}

// rootFor is the testable core of Root. Resolution order:
//
//  1. SWE_WORKSPACE_ROOT, when non-empty, on every platform — the explicit
//     operator override.
//  2. On Windows: %LOCALAPPDATA%\agentfield\workspaces. When LOCALAPPDATA is
//     empty, fall back to <os.TempDir()>\agentfield\workspaces so the base is
//     still an absolute drive-letter path (never drive-relative "/workspaces").
//  3. Everywhere else: exactly "/workspaces" — byte-identical to the historical
//     default (Docker parity).
//
// goos and getenv are injected so tests can exercise every branch without
// touching the real runtime environment.
func rootFor(goos string, getenv func(string) string) string {
	if root := getenv("SWE_WORKSPACE_ROOT"); root != "" {
		return root
	}
	if goos == "windows" {
		base := getenv("LOCALAPPDATA")
		if base == "" {
			base = tempDir()
		}
		return filepath.Join(base, "agentfield", "workspaces")
	}
	return "/workspaces"
}

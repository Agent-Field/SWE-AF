package furrow

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	EnvBin       = "SWE_FURROW_BIN"
	EnvDaemonBin = "SWE_FURROWD_BIN"
	// EnvEnabled gates the whole feature. Mirroring is OPT-IN: it copies every
	// byte of a build's workspace — including the untracked files and secrets
	// git never sees — into a second on-disk store, so an operator has to ask
	// for it. Unset means off.
	EnvEnabled = "SWE_FURROW_ENABLED"
	// EnvExposeSecrets opts a node into returning a handle's recovery key and
	// transport token from get_workspace_handle. That reasoner authorizes
	// nobody, so the secrets are withheld unless an operator states that every
	// caller which can reach this node is already trusted with the workspace.
	EnvExposeSecrets = "SWE_FURROW_EXPOSE_SECRETS"
	DefaultBin       = "/usr/local/bin/furrow"
	DefaultDaemonBin = "/usr/local/bin/furrowd"
)

// EnvTruthy reports whether an environment variable opts a feature in.
// "1", "true", "yes" and "on" (any case, surrounding space ignored) enable it;
// "0", "false", "no", "off", empty, unset and anything unrecognised disable it.
// Deliberately the same rule as pro.Enabled — one spelling for every SWE-AF
// feature gate — and deliberately closed by default, so a typo in a flag can
// never be what switches a feature ON.
func EnvTruthy(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// runnable rejects copies that exist but lost their execute bit during install.
// It is a pure probe: an operator-supplied path (SWE_FURROW_BIN, /usr/local/bin)
// is never modified, so an explicit override that is not executable still fails
// loudly instead of being silently rewritten.
func runnable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0
}

// vendoredRunnable is runnable for the binaries WE ship, beside our own
// executable. `af` only started preserving file modes in v0.1.121
// (agentfield#865); every older CLI copies these into the package as 0644.
// Verified live on af 0.1.119: a checkout that is rwxr-xr-x installs as
// rw-r--r--, so the node logged "no runnable furrow binary found" and the
// feature was silently off on the one platform it ships for. Repairing is safe
// precisely here — the file is one we vendored, inside our own install tree,
// and never a path anyone else chose. When the repair fails (read-only fs,
// foreign owner) the candidate stays rejected.
func vendoredRunnable(path string) bool {
	if runnable(path) {
		return true
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		return false
	}
	return os.Chmod(path, info.Mode().Perm()|0o755) == nil
}

// ResolveDaemonBin returns the first runnable furrowd binary. An explicit
// SWE_FURROWD_BIN is authoritative; installed and sibling layouts otherwise
// follow the same order as ResolveBin.
func ResolveDaemonBin() (string, error) {
	if path := os.Getenv(EnvDaemonBin); path != "" {
		if runnable(path) {
			return path, nil
		}
		return "", fmt.Errorf("furrowd binary %q is missing or not executable", path)
	}
	if runnable(DefaultDaemonBin) {
		return DefaultDaemonBin, nil
	}
	if executable, err := osExecutable(); err == nil {
		dir := filepath.Dir(executable)
		for _, name := range []string{"furrowd-" + runtime.GOOS + "-" + runtime.GOARCH, "furrowd"} {
			path := filepath.Join(dir, name)
			if vendoredRunnable(path) {
				return path, nil
			}
		}
	}
	return "", fmt.Errorf("no runnable furrowd binary found")
}

var osExecutable = os.Executable

// ResolveBin returns the first runnable furrow binary in the supported install
// layouts. An explicit override is authoritative and never falls through.
func ResolveBin() (string, error) {
	if path := os.Getenv(EnvBin); path != "" {
		if runnable(path) {
			return path, nil
		}
		return "", fmt.Errorf("furrow binary %q is missing or not executable", path)
	}
	if runnable(DefaultBin) {
		return DefaultBin, nil
	}
	if executable, err := osExecutable(); err == nil {
		dir := filepath.Dir(executable)
		for _, name := range []string{"furrow-" + runtime.GOOS + "-" + runtime.GOARCH, "furrow"} {
			path := filepath.Join(dir, name)
			if vendoredRunnable(path) {
				return path, nil
			}
		}
	}
	return "", fmt.Errorf("no runnable furrow binary found")
}

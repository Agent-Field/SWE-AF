package furrow

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

const (
	EnvBin           = "SWE_FURROW_BIN"
	EnvDaemonBin     = "SWE_FURROWD_BIN"
	EnvEnabled       = "SWE_FURROW_ENABLED"
	DefaultBin       = "/usr/local/bin/furrow"
	DefaultDaemonBin = "/usr/local/bin/furrowd"
)

// runnable reports whether path is an executable regular file — repairing a
// copy that lost its execute bit on the way in, which is exactly what `af`
// does to vendored binaries (verified live: an install delivers
// bin/furrow-linux-amd64 as rw-r--r--, so every install logged "no runnable
// furrow binary found" and the feature was silently off on the one platform
// it ships for). When the repair fails (read-only fs, foreign owner) the
// candidate stays rejected, as before.
func runnable(path string) bool {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		return false
	}
	if info.Mode().Perm()&0o111 != 0 {
		return true
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
			if runnable(path) {
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
			if runnable(path) {
				return path, nil
			}
		}
	}
	return "", fmt.Errorf("no runnable furrow binary found")
}

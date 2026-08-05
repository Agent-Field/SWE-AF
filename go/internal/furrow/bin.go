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

// runnable rejects copies that exist but lost their execute bit during install.
func runnable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0
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

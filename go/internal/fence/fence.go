// Package fence keeps every codeaf harness run inside its own build's folder.
//
// Builds on one node sit side by side under one workspace root
// (…/workspaces/<repo>-<build>/), and nothing in codeaf keeps an unattended
// run from reading past its own: at codeaf 9b0b7d189 a chat run is host-bound
// (no jail), the jail fences writes only, and the file tools take any
// absolute or ../ path. Live on 2026-11-07 a build for issue #184 planned
// around a plan for issue #83 that it read in the next directory. With two
// customers' builds on one node that is one customer's plan in the other's
// pull request.
//
// So the WHOLE codeaf process is started inside an operating-system fence —
// seatbelt (sandbox-exec) on macOS, bubblewrap on Linux — that hides the
// workspace root except the build's own folder. The fence covers codeaf's
// in-process file tools and every command it runs alike, because both are
// that process or its children. A FENCE THAT CANNOT BE APPLIED REFUSES THE
// RUN; it never quietly runs unfenced. SWE_FENCE=off is the operator's
// explicit opt-out.
//
// The node binary is its own fence launcher: harnessx points the codeaf
// harness at os.Executable() with EnvExec set, and main hands control to Main
// before anything else starts.
package fence

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
)

// Environment the launcher reads. They ride on the harness call's own
// environment, so every call carries its own build's fence.
const (
	EnvExec = "SWE_FENCE_EXEC"   // "1": this process is the fence launcher
	EnvReal = "SWE_FENCE_CODEAF" // the real codeaf binary
	EnvRoot = "SWE_FENCE_ROOT"   // the shared workspace root to hide
	EnvDir  = "SWE_FENCE_DIR"    // the build's own folder to keep visible
	EnvOff  = "SWE_FENCE"        // "off": run without a fence (operator opt-out)
)

// Off reports the operator's explicit opt-out.
func Off() bool { return strings.EqualFold(strings.TrimSpace(os.Getenv(EnvOff)), "off") }

// Scope is what one call may see of the workspace root: Deny is hidden,
// except Allow inside it.
type Scope struct{ Deny, Allow string }

// ScopeFor is the fence for a run whose working directory is cwd under the
// shared workspace root: the root hidden, the build's own folder (the first
// directory below the root on the way to cwd) visible. A cwd outside the root
// keeps its own directory visible and still hides the whole root. ok is false
// when there is no root to hide.
func ScopeFor(root, cwd string) (Scope, bool) {
	if strings.TrimSpace(root) == "" || strings.TrimSpace(cwd) == "" {
		return Scope{}, false
	}
	r, c := canonical(root), canonical(cwd)
	rel, err := filepath.Rel(r, c)
	if err != nil || rel == "." {
		return Scope{}, false // the root itself is no build's folder
	}
	if strings.HasPrefix(rel, ".."+string(filepath.Separator)) || rel == ".." {
		return Scope{Deny: r, Allow: c}, true
	}
	first := strings.SplitN(rel, string(filepath.Separator), 2)[0]
	return Scope{Deny: r, Allow: filepath.Join(r, first)}, true
}

// Stamp returns env with the launcher's variables for one call: run through
// the fence, start real, and see only scope.
func Stamp(env map[string]string, real string, scope Scope) map[string]string {
	out := make(map[string]string, len(env)+4)
	for k, v := range env {
		out[k] = v
	}
	out[EnvExec] = "1"
	out[EnvReal] = real
	out[EnvRoot] = scope.Deny
	out[EnvDir] = scope.Allow
	return out
}

// SeatbeltProfile hides deny except allow. Seatbelt applies the LAST rule
// that matches, so the allow below the deny wins inside the build's folder.
func SeatbeltProfile(s Scope) string {
	return fmt.Sprintf(`(version 1)
(allow default)
(deny file-read* file-write* (subpath %q))
(allow file-read* file-write* (subpath %q))
`, s.Deny, s.Allow)
}

// Command is the argv that starts real with args inside the fence on goos.
func Command(goos, real string, args []string, s Scope) ([]string, error) {
	switch goos {
	case "darwin":
		return append([]string{"/usr/bin/sandbox-exec", "-p", SeatbeltProfile(s), real}, args...), nil
	case "linux":
		bwrap, err := exec.LookPath("bwrap")
		if err != nil {
			return nil, errors.New("bubblewrap (bwrap) is not installed, so the run cannot be fenced to its build's folder")
		}
		// The host stays as it is; an empty tmpfs covers the workspace root,
		// and only the build's own folder is bound back onto it.
		return append([]string{bwrap, "--dev-bind", "/", "/", "--tmpfs", s.Deny,
			"--bind", s.Allow, s.Allow, "--die-with-parent", "--", real}, args...), nil
	}
	return nil, fmt.Errorf("no fence is known on %s, so the run cannot be fenced to its build's folder", goos)
}

// Main is the launcher: it replaces this process with the real codeaf inside
// the fence that the call's environment names. It returns only on failure.
func Main(args []string) int {
	real := os.Getenv(EnvReal)
	if real == "" {
		fmt.Fprintln(os.Stderr, "fence: no codeaf binary named ("+EnvReal+")")
		return 126
	}
	s := Scope{Deny: os.Getenv(EnvRoot), Allow: os.Getenv(EnvDir)}
	env := withoutLauncherVars(os.Environ())
	argv := append([]string{real}, args...)
	if !Off() && s.Deny != "" && s.Allow != "" {
		if err := os.MkdirAll(s.Allow, 0o700); err != nil {
			fmt.Fprintln(os.Stderr, "fence:", err)
			return 126
		}
		fenced, err := Command(runtime.GOOS, real, args, s)
		if err != nil {
			fmt.Fprintln(os.Stderr, "fence: refusing to run unfenced:", err)
			return 126
		}
		argv = fenced
	}
	if err := syscall.Exec(argv[0], argv, env); err != nil {
		fmt.Fprintln(os.Stderr, "fence:", err)
		return 126
	}
	return 0
}

// withoutLauncherVars keeps the fence's own variables out of codeaf: its
// children are not launchers.
func withoutLauncherVars(env []string) []string {
	out := env[:0:0]
	for _, kv := range env {
		if strings.HasPrefix(kv, EnvExec+"=") || strings.HasPrefix(kv, EnvReal+"=") {
			continue
		}
		out = append(out, kv)
	}
	return out
}

// canonical is an absolute path with links resolved where it exists: the
// fence matches real paths, and /tmp and /var are links on macOS.
func canonical(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		return filepath.Clean(p)
	}
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		return real
	}
	// A folder not made yet: resolve its nearest existing parent.
	dir, base := filepath.Split(abs)
	if dir != "" && dir != abs {
		return filepath.Join(canonical(filepath.Clean(dir)), base)
	}
	return abs
}

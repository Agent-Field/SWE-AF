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
// that process or its children. SWE_FENCE is opt-in (off by default); when
// enabled, A FENCE THAT CANNOT BE APPLIED REFUSES THE RUN rather than quietly
// running unfenced.
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
	EnvExec  = "SWE_FENCE_EXEC"   // "1": this process is the fence launcher
	EnvReal  = "SWE_FENCE_CODEAF" // the real codeaf binary
	EnvRoot  = "SWE_FENCE_ROOT"   // the shared workspace root to hide
	EnvDir   = "SWE_FENCE_DIR"    // the build's own folder to keep visible
	EnvFence = "SWE_FENCE"        // truthy: run inside the fence (opt-in)

	// A linked git worktree's own metadata, when it lives outside the
	// build's folder (see Scope.GitDir).
	EnvGitDir    = "SWE_FENCE_GITDIR"    // the worktree's private gitdir
	EnvGitCommon = "SWE_FENCE_GITCOMMON" // the repository's shared git dir
)

// Enabled reports whether EnvFence is truthy. Off by default so nodes without
// an operating-system fence (Linux without bwrap, unprivileged containers,
// Windows) run codeaf directly as on main; when enabled, a fence that cannot
// be applied refuses the run.
func Enabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvFence))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// Scope is what one call may see of the workspace root: Deny is hidden,
// except Allow inside it.
//
// GitDir and GitCommon are set only when the run's working directory is a
// linked git worktree whose metadata lives in the hidden root OUTSIDE Allow.
// A worktree's `.git` is a file naming <common>/worktrees/<name>, and git
// cannot commit without that folder and the shared objects, refs and config
// in <common>. So the fence opens GitCommon, closes <common>/worktrees again,
// and reopens only GitDir: the run gets the repository's shared history and
// its own worktree's index and HEAD, never another worktree's.
type Scope struct{ Deny, Allow, GitDir, GitCommon string }

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
	s := Scope{Deny: r, Allow: c}
	if !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != ".." {
		first := strings.SplitN(rel, string(filepath.Separator), 2)[0]
		s.Allow = filepath.Join(r, first)
	}
	s.GitDir, s.GitCommon = linkedGitDirs(c, s)
	return s, true
}

// linkedGitDirs finds the linked worktree that cwd sits in, walking up no
// further than the folder the fence already opens, and returns its private
// gitdir and the repository's shared git dir when the fence would otherwise
// hide them. It returns nothing for an ordinary repository, for metadata the
// fence already shows, and for a `.git` file that does not check out.
//
// THE `.git` FILE IS NOT TRUSTED ON ITS OWN WORD. It sits in a folder the
// previous run could write, so a run could point it at another build's
// repository and be handed that repository by the next fence. git's own
// back-link is required too: <common>/worktrees/<name>/gitdir must name this
// very `.git` file, and that file lives where the run cannot write.
func linkedGitDirs(cwd string, s Scope) (gitDir, common string) {
	for dir := cwd; ; dir = filepath.Dir(dir) {
		dotgit := filepath.Join(dir, ".git")
		if fi, err := os.Lstat(dotgit); err == nil {
			if !fi.Mode().IsRegular() {
				return "", "" // an ordinary repository: its .git is a folder
			}
			return checkedGitDirs(dir, dotgit, s)
		}
		if dir == s.Allow || !within(s.Allow, dir) {
			return "", ""
		}
	}
}

func checkedGitDirs(dir, dotgit string, s Scope) (string, string) {
	gd, ok := pointer(dotgit, "gitdir:")
	if !ok {
		return "", ""
	}
	gd = canonical(resolve(dir, gd))
	common := gd
	if c, ok := pointer(filepath.Join(gd, "commondir"), ""); ok {
		common = canonical(resolve(gd, c))
	}
	if filepath.Dir(gd) != filepath.Join(common, "worktrees") {
		return "", "" // not git's own layout for a linked worktree
	}
	back, ok := pointer(filepath.Join(gd, "gitdir"), "")
	if !ok || canonical(resolve(gd, back)) != canonical(dotgit) {
		return "", ""
	}
	if within(s.Allow, gd) || !within(s.Deny, gd) {
		return "", "" // already visible to the run; nothing to open
	}
	return gd, common
}

// pointer reads a one-line git pointer file, without its prefix.
func pointer(path, prefix string) (string, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	line := strings.TrimSpace(strings.SplitN(string(b), "\n", 2)[0])
	if !strings.HasPrefix(line, prefix) {
		return "", false
	}
	v := strings.TrimSpace(strings.TrimPrefix(line, prefix))
	return v, v != ""
}

func resolve(base, p string) string {
	if filepath.IsAbs(p) {
		return filepath.Clean(p)
	}
	return filepath.Join(base, p)
}

// within reports whether p is parent or below it.
func within(parent, p string) bool {
	rel, err := filepath.Rel(parent, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
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
	out[EnvGitDir] = scope.GitDir
	out[EnvGitCommon] = scope.GitCommon
	return out
}

// SeatbeltProfile hides deny except allow. Seatbelt applies the LAST rule
// that matches, so the allow below the deny wins inside the build's folder.
//
// THE ROOT ITSELF CAN BE STAT'D, NEVER LISTED. A `subpath` deny covers the
// root directory too, and git canonicalises every path it is handed by
// lstat'ing each component on the way down: with the root unstattable every
// git command in the build's own worktree died with "stat …/workspaces:
// Operation not permitted", and the coder for issue #221 could not commit at
// all (2026-11-08). file-read-metadata on the literal root (and on each folder
// between it and an opened git dir) answers stat and readlink; listing a
// directory is file-read-data, which stays denied, so the neighbours' names
// stay hidden.
func SeatbeltProfile(s Scope) string {
	var b strings.Builder
	fmt.Fprintf(&b, "(version 1)\n(allow default)\n")
	fmt.Fprintf(&b, "(deny file-read* file-write* (subpath %q))\n", s.Deny)
	fmt.Fprintf(&b, "(allow file-read-metadata (literal %q))\n", s.Deny)
	fmt.Fprintf(&b, "(allow file-read* file-write* (subpath %q))\n", s.Allow)
	if s.GitDir != "" && s.GitCommon != "" {
		for _, d := range between(s.Deny, s.GitCommon) {
			fmt.Fprintf(&b, "(allow file-read-metadata (literal %q))\n", d)
		}
		wt := filepath.Join(s.GitCommon, "worktrees")
		fmt.Fprintf(&b, "(allow file-read* file-write* (subpath %q))\n", s.GitCommon)
		fmt.Fprintf(&b, "(deny file-read* file-write* (subpath %q))\n", wt)
		fmt.Fprintf(&b, "(allow file-read-metadata (literal %q))\n", wt)
		fmt.Fprintf(&b, "(allow file-read* file-write* (subpath %q))\n", s.GitDir)
	}
	return b.String()
}

// between is every folder strictly below root on the way to p.
func between(root, p string) []string {
	var out []string
	for d := filepath.Dir(p); d != root && within(root, d); d = filepath.Dir(d) {
		out = append([]string{d}, out...)
	}
	return out
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
		// and only the build's own folder is bound back onto it. A linked
		// worktree's shared git dir is bound back too, its worktrees/ folder
		// covered again, and only this worktree's own gitdir bound into that.
		argv := []string{bwrap, "--dev-bind", "/", "/", "--tmpfs", s.Deny, "--bind", s.Allow, s.Allow}
		if s.GitDir != "" && s.GitCommon != "" {
			wt := filepath.Join(s.GitCommon, "worktrees")
			argv = append(argv, "--bind", s.GitCommon, s.GitCommon, "--tmpfs", wt, "--bind", s.GitDir, s.GitDir)
		}
		argv = append(argv, "--die-with-parent", "--", real)
		return append(argv, args...), nil
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
	s := Scope{Deny: os.Getenv(EnvRoot), Allow: os.Getenv(EnvDir),
		GitDir: os.Getenv(EnvGitDir), GitCommon: os.Getenv(EnvGitCommon)}
	env := withoutLauncherVars(os.Environ())
	argv := append([]string{real}, args...)
	if Enabled() && s.Deny != "" && s.Allow != "" {
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

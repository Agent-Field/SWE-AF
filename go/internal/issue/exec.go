package issue

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"strings"
)

// runGit executes `git -C repoPath args...` and returns (stdout, detail,
// exitCode). detail is stderr when present, else stdout, else the exec error —
// mirroring git_ops._git's error-detail selection.
func runGit(repoPath string, args ...string) (string, string, int) {
	cmd := exec.Command("git", append([]string{"-C", repoPath}, args...)...)
	// Callers classify failures by matching git's stderr phrasing; pin the
	// locale so a translated message can never change what they decide.
	cmd.Env = append(os.Environ(), "LC_ALL=C")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()

	code := 0
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			code = exitErr.ExitCode()
		} else {
			code = -1
		}
	}

	out := strings.TrimSpace(stdout.String())
	detail := strings.TrimSpace(stderr.String())
	if detail == "" {
		detail = out
	}
	if detail == "" && err != nil {
		detail = err.Error()
	}
	return out, detail, code
}

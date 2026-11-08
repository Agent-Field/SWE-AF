// Package projectcheck runs the target project's own build and test commands
// after a run's work has merged, as a check no plan can remove.
//
// The verifier is a model grading the PRD's acceptance criteria, and the
// criteria are whatever the planner wrote. Live on 2026-11-08 (issue #269)
// the person answered the planning question, the planner rewrote every
// criterion as a text match ("ledger.go imports …", "go.mod lists …"), the
// verifier passed them all, and the run opened PR #278 on a module that does
// not compile: nothing in the run ever executed `go build`. The same request
// worded "checked by running the commands" (issue #279) failed correctly. So
// whether the build ran depended on how the issue was phrased.
//
// THE PROJECT'S OWN BUILD AND TESTS ARE RUN BY CODE, NOT BY A CRITERION. The
// commands are detected from the repository itself (go.mod, Cargo.toml,
// package.json scripts), so the check is the same for every issue and every
// plan, and a failure fails the run and holds its pull request.
//
// A failure is blamed on the run only when the run's starting commit passed
// the same command: a repository whose build was already red before the run
// touched it is reported, not held against the run. A command whose tool is
// not installed is skipped and said so — a check that cannot run is absent,
// never a failure.
package projectcheck

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Command is one detected build or test step.
type Command struct {
	// Argv is the command line, Argv[0] the program.
	Argv []string
}

// String is the command as a person would type it.
func (c Command) String() string { return strings.Join(c.Argv, " ") }

// Outcome is what Run found.
type Outcome struct {
	// Ran is true when at least one detected command executed.
	Ran bool
	// Passed is false only when a command failed on the run's work and the
	// same command passed at the run's base (or the base could not be checked).
	Passed bool
	// Commands are the detected commands, in order.
	Commands []string
	// FailedCommand is the first command that failed on the run's work.
	FailedCommand string
	// ExitCode of FailedCommand; -1 for a timeout or a start failure.
	ExitCode int
	// Output is the tail of FailedCommand's combined output.
	Output string
	// PreExisting is true when FailedCommand also fails at the base commit,
	// so the failure is not the run's doing; Passed stays true.
	PreExisting bool
	// Skipped names commands that could not run (tool not installed).
	Skipped []string
	// Reason says why nothing ran, when Ran is false.
	Reason string
}

// CommandTimeout bounds one build or test command.
var CommandTimeout = 15 * time.Minute

// outputTail is how much of a failing command's output is kept.
const outputTail = 4000

// runCommand executes one command. A package variable so tests can count
// calls; the default runs the real process.
var runCommand = func(ctx context.Context, dir string, argv []string) (int, string, error) {
	cctx, cancel := context.WithTimeout(ctx, CommandTimeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, argv[0], argv[1:]...)
	cmd.Dir = dir
	// CI=1 keeps test runners (jest, vitest) out of watch mode.
	cmd.Env = append(os.Environ(), "CI=1")
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	err := cmd.Run()
	if err == nil {
		return 0, out.String(), nil
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if cctx.Err() != nil {
			return -1, out.String() + fmt.Sprintf("\n(stopped after %s)", CommandTimeout), nil
		}
		return ee.ExitCode(), out.String(), nil
	}
	return -1, out.String(), err
}

// Detect returns the repository's own build and test commands, read from the
// files at its root. An empty result means no ecosystem was recognised.
func Detect(repo string) []Command {
	var cmds []Command
	if fileExists(filepath.Join(repo, "go.mod")) {
		cmds = append(cmds,
			Command{Argv: []string{"go", "build", "./..."}},
			Command{Argv: []string{"go", "test", "./..."}})
	}
	if fileExists(filepath.Join(repo, "Cargo.toml")) {
		cmds = append(cmds,
			Command{Argv: []string{"cargo", "build"}},
			Command{Argv: []string{"cargo", "test"}})
	}
	if scripts, ok := packageScripts(repo); ok && dirExists(filepath.Join(repo, "node_modules")) {
		// Only with dependencies installed: without node_modules every script
		// fails for a reason that is not the run's.
		if strings.TrimSpace(scripts["build"]) != "" {
			cmds = append(cmds, Command{Argv: []string{"npm", "run", "build"}})
		}
		if t := strings.TrimSpace(scripts["test"]); t != "" && !strings.Contains(t, "no test specified") {
			cmds = append(cmds, Command{Argv: []string{"npm", "test"}})
		}
	}
	return cmds
}

// Run checks repo's work against base. base is the commit the run started
// from; with no base there is nothing to compare the work to and nothing runs.
func Run(ctx context.Context, repo, base string) Outcome {
	out := Outcome{Passed: true}
	if strings.TrimSpace(repo) == "" || strings.TrimSpace(base) == "" {
		out.Reason = "no starting commit to compare the work to"
		return out
	}
	if !changedSince(ctx, repo, base) {
		out.Reason = "the run changed nothing"
		return out
	}
	cmds := Detect(repo)
	for _, c := range cmds {
		out.Commands = append(out.Commands, c.String())
	}
	if len(cmds) == 0 {
		out.Reason = "no build or test command found in the repository"
		return out
	}
	for _, c := range cmds {
		if _, err := exec.LookPath(c.Argv[0]); err != nil {
			out.Skipped = append(out.Skipped, c.String())
			continue
		}
		code, text, err := runCommand(ctx, repo, c.Argv)
		if err != nil {
			// The program exists but would not start; say so, do not fail on it.
			out.Skipped = append(out.Skipped, c.String())
			continue
		}
		out.Ran = true
		if code == 0 {
			continue
		}
		out.FailedCommand = c.String()
		out.ExitCode = code
		out.Output = tail(text, outputTail)
		out.PreExisting = failsAtBase(ctx, repo, base, c)
		out.Passed = out.PreExisting
		return out
	}
	if !out.Ran {
		out.Reason = "the build tools are not installed here"
	}
	return out
}

// Criterion is the line a failed Outcome adds to the verification's criteria.
func (o Outcome) Criterion() string {
	return "The project builds and its tests pass (" + strings.Join(o.Commands, " && ") + ")"
}

// Evidence is the failed command, its exit code and the tail of its output.
func (o Outcome) Evidence() string {
	return fmt.Sprintf("`%s` exited %d:\n%s", o.FailedCommand, o.ExitCode, strings.TrimSpace(o.Output))
}

// Fold writes a failed Outcome into a verification result: passed becomes
// false, the failing command joins criteria_results (so a fix cycle sees it),
// and the summary leads with it. where names the repository in a multi-repo
// build ("" otherwise). An Outcome that passed leaves verification untouched.
func (o Outcome) Fold(verification map[string]any, where string) {
	if verification == nil || o.Passed {
		return
	}
	verification["passed"] = false
	crit := map[string]any{
		"criterion":  o.Criterion() + where,
		"passed":     false,
		"evidence":   o.Evidence(),
		"issue_name": "",
		// Marks the gate's own criterion so no later pass reconsiders it.
		"project_check": true,
	}
	switch list := verification["criteria_results"].(type) {
	case []any:
		verification["criteria_results"] = append(list, crit)
	case []map[string]any:
		verification["criteria_results"] = append(list, crit)
	default:
		verification["criteria_results"] = []any{crit}
	}
	summary, _ := verification["summary"].(string)
	verification["summary"] = strings.TrimSpace(fmt.Sprintf(
		"OVERALL: FAIL. The project does not build or its tests fail: `%s` exited %d%s. %s",
		o.FailedCommand, o.ExitCode, where, summary))
}

// changedSince reports whether the working tree differs from base at all,
// committed or not. An unreadable comparison counts as changed, so the check
// runs rather than being skipped on a git error.
func changedSince(ctx context.Context, repo, base string) bool {
	cmd := exec.CommandContext(ctx, "git", "diff", "--quiet", base, "--")
	cmd.Dir = repo
	return cmd.Run() != nil
}

// failsAtBase runs c at base in a throwaway detached worktree. A worktree
// that cannot be made reads as "base passes", so the failure stays the run's.
func failsAtBase(ctx context.Context, repo, base string, c Command) bool {
	dir, err := os.MkdirTemp("", "projectcheck-base-")
	if err != nil {
		return false
	}
	defer os.RemoveAll(dir)
	wt := filepath.Join(dir, "base")
	add := exec.CommandContext(ctx, "git", "worktree", "add", "--detach", "--force", wt, base)
	add.Dir = repo
	if err := add.Run(); err != nil {
		return false
	}
	defer func() {
		rm := exec.Command("git", "worktree", "remove", "--force", wt)
		rm.Dir = repo
		_ = rm.Run()
		prune := exec.Command("git", "worktree", "prune")
		prune.Dir = repo
		_ = prune.Run()
	}()
	// Installed dependencies are not in git; lend them to the base checkout so
	// a Node base is checked with the same packages the work was.
	if nm := filepath.Join(repo, "node_modules"); dirExists(nm) {
		_ = os.Symlink(nm, filepath.Join(wt, "node_modules"))
	}
	code, _, err := runCommand(ctx, wt, c.Argv)
	return err == nil && code != 0
}

func packageScripts(repo string) (map[string]string, bool) {
	b, err := os.ReadFile(filepath.Join(repo, "package.json"))
	if err != nil {
		return nil, false
	}
	var pkg struct {
		Scripts map[string]string `json:"scripts"`
	}
	if json.Unmarshal(b, &pkg) != nil {
		return nil, false
	}
	return pkg.Scripts, true
}

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

func dirExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

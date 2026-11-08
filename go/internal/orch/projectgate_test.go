package orch

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func gitT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func writeCommit(t *testing.T, dir, msg string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitT(t, dir, "add", "-A")
	gitT(t, dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
}

// buildIssue269 runs Build the way issue #269 ran on 2026-11-08: a Go
// repository that compiles at the start, a coder whose merged work imports a
// module that does not exist, and a verifier that PASSES because every
// criterion the planner wrote after the person's answer was a text match. It
// returns the reasoners called and the final verification.
func buildIssue269(t *testing.T, work string) (calls []string, result map[string]any) {
	t.Helper()
	return buildWithVerifier(t, work, func() map[string]any {
		return map[string]any{"passed": true, "summary": "OVERALL: PASS", "criteria_results": []any{
			map[string]any{"criterion": "ledger.go imports github.com/santoshkumarradha/hashkit-does-not-exist", "passed": true},
		}}
	})
}

// buildWithVerifier runs Build over a Go repository whose merged work is
// work, with a verifier that answers verdict().
func buildWithVerifier(t *testing.T, work string, verdict func() map[string]any) (calls []string, result map[string]any) {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	t.Setenv("GOPROXY", "off")
	t.Setenv("GOFLAGS", "-mod=readonly")

	repo := t.TempDir()
	gitT(t, repo, "init", "-q", "-b", "main")
	writeCommit(t, repo, "base", map[string]string{
		"go.mod":    "module example.com/ledger\n\ngo 1.21\n",
		"ledger.go": "package ledger\n\ntype Ledger struct{}\n\nfunc New() *Ledger { return &Ledger{} }\n",
	})

	prevGit, prevGH := runGit, runGH
	t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
	runGit = func(context.Context, string, ...string) cmdResult { return cmdResult{} }
	runGH = func(context.Context, string, ...string) cmdResult {
		return cmdResult{Stdout: "https://github.com/o/r/pull/278\n"}
	}

	exec := func(map[string]any) map[string]any {
		writeCommit(t, repo, "issue/ledger-digest", map[string]string{"digest.go": work})
		return map[string]any{
			"completed_issues": []any{map[string]any{"name": "ledger-digest"}},
			"merged_branches":  []any{"issue/ledger-digest"},
			"all_issues":       []any{map[string]any{"name": "ledger-digest"}},
			"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
		}
	}
	base := buildHandler(exec, func(map[string]any) map[string]any { return verdict() })
	var mu sync.Mutex
	app := &mockApp{handler: func(ctx context.Context, target string, input map[string]any) (map[string]any, error) {
		mu.Lock()
		calls = append(calls, target[strings.LastIndex(target, ".")+1:])
		mu.Unlock()
		switch {
		case strings.HasSuffix(target, ".run_git_init"):
			return map[string]any{
				"success": true, "integration_branch": "feature/ledger-digest",
				"original_branch": "main", "mode": "fresh",
				"remote_url": "https://github.com/o/r.git", "remote_default_branch": "main",
			}, nil
		case strings.HasSuffix(target, ".run_github_pr"):
			return map[string]any{"pr_url": "https://github.com/o/r/pull/278", "pr_number": 278}, nil
		}
		return base(ctx, target, input)
	}}
	out, err := Build(context.Background(), &Deps{App: app, NodeID: "swe-planner"}, map[string]any{
		"goal":      "Resolve GitHub issue #269: Add a Digest method to Ledger using hashkit",
		"repo_path": repo,
		"config":    map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0},
	})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	m, _ := out.(map[string]any)
	v, _ := m["verification"].(map[string]any)
	return calls, v
}

// Issue #269 → PR #278: a verifier that passed text-match criteria cannot
// ship code that does not compile. The project's own `go build ./...` runs,
// fails, fails the verification, and no pull request is opened.
func TestARedBuildFailsTheRunWhateverTheVerifierSaid(t *testing.T) {
	defer withExecCtx("run-269", "exec-269")()
	calls, v := buildIssue269(t, "package ledger\n\nimport \"github.com/santoshkumarradha/hashkit-does-not-exist\"\n\nfunc (l *Ledger) Digest() string { return hashkit.Sum256Hex(nil) }\n")
	for _, c := range calls {
		if c == "run_github_pr" {
			t.Fatalf("a build that does not compile called the PR role: %v", calls)
		}
	}
	if v["passed"] != false {
		t.Fatalf("verification still passed: %v", v)
	}
	found := false
	for _, c := range asMapList(v["criteria_results"]) {
		if strings.Contains(mapStr(c, "criterion", ""), "go build ./...") && c["passed"] == false {
			found = strings.Contains(mapStr(c, "evidence", ""), "hashkit-does-not-exist")
		}
	}
	if !found {
		t.Fatalf("no failed build criterion naming the missing module: %v", v["criteria_results"])
	}
}

// The control: the same run with work that compiles still opens its PR, so
// the gate above is holding the build and not every publish.
func TestABuildThatCompilesStillOpensItsPullRequest(t *testing.T) {
	defer withExecCtx("run-270", "exec-270")()
	calls, v := buildIssue269(t, "package ledger\n\nfunc (l *Ledger) Digest() string { return \"\" }\n")
	if v["passed"] != true {
		t.Fatalf("verification failed: %v", v)
	}
	if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
		t.Fatalf("a passing build did not call the PR role: %v", calls)
	}
}

// issue291Verdict is what the verifier said on issue #291 (WorkAF r8): every
// criterion passed except the one the planner spelled
// `go doc ./ ledger.Ledger.Checksum`, which the Go tool refuses ("too many
// periods") for every implementation.
func issue291Verdict(extra ...map[string]any) func() map[string]any {
	return func() map[string]any {
		list := []any{
			map[string]any{"criterion": "1. `go test ./...` exits 0", "passed": true},
			map[string]any{"criterion": "3. go doc ./ ledger.Ledger.Checksum (run from the repo root) prints func (l *Ledger) Checksum() uint32.",
				"passed": false, "evidence": "doc: too many periods in symbol specification (exit 2)"},
		}
		for _, e := range extra {
			list = append(list, e)
		}
		return map[string]any{"passed": false, "summary": "Overall verdict: FAIL", "criteria_results": list}
	}
}

const checksumWork = "package ledger\n\n// Checksum is the CRC of the entries.\nfunc (l *Ledger) Checksum() uint32 { return 0 }\n"

// Issue #291: code that builds, passes its tests and has the method failed on
// a check the Go tool cannot run as written. The check is repaired, passes,
// and the pull request opens.
func TestAMalformedAcceptanceCheckDoesNotFailWorkThatBuilds(t *testing.T) {
	defer withExecCtx("run-291", "exec-291")()
	calls, v := buildWithVerifier(t, checksumWork, issue291Verdict())
	if v["passed"] != true {
		t.Fatalf("a malformed check failed the run: %v", v)
	}
	if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
		t.Fatalf("no pull request: %v", calls)
	}
	if !strings.Contains(mapStr(v, "summary", ""), "go doc ./ Ledger.Checksum") {
		t.Fatalf("summary does not name the repair: %q", v["summary"])
	}
}

// A real assertion beside the malformed check still fails the run.
func TestATrueFailingCheckStillFailsBesideAMalformedOne(t *testing.T) {
	defer withExecCtx("run-291b", "exec-291b")()
	calls, v := buildWithVerifier(t, checksumWork, issue291Verdict(map[string]any{
		"criterion": "`grep -q 'func (l \\*Ledger) Digest' ledger.go` exits 0", "passed": false,
	}))
	if v["passed"] != false {
		t.Fatalf("a real failure was lifted: %v", v)
	}
	for _, c := range calls {
		if c == "run_github_pr" {
			t.Fatalf("a failing build called the PR role: %v", calls)
		}
	}
}

// THE PROJECT GATE STAYS: a malformed check is repaired, but work that does
// not compile still fails the run and opens no pull request.
func TestARepairedCheckCannotLiftARedBuild(t *testing.T) {
	defer withExecCtx("run-291c", "exec-291c")()
	calls, v := buildWithVerifier(t, checksumWork+"\nfunc broken() { undefined() }\n", issue291Verdict())
	if v["passed"] != false {
		t.Fatalf("a red build passed: %v", v)
	}
	for _, c := range calls {
		if c == "run_github_pr" {
			t.Fatalf("a build that does not compile called the PR role: %v", calls)
		}
	}
}

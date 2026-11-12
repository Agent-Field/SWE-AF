package orch

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/projectcheck"
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
	// build_issue always runs the project gate even with SWE_STRICT_BUILD unset.
	t.Setenv(config.EnvStrictBuild, "1")
	_ = os.Unsetenv(config.EnvStrictBuild)

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
	out, err := BuildIssue(context.Background(), &Deps{App: app, NodeID: "swe-planner"}, map[string]any{
		"title":        "Add a Digest method to Ledger using hashkit",
		"issue_number": 269,
		"repo_path":    repo,
		"plan_review":  false,
		"config":       map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0},
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

// N2: With SWE_STRICT_BUILD unset, a plain build runs no project build or test
// command, and a repository whose own tests fail does not fail the build on
// that account. With SWE_STRICT_BUILD set, or via build_issue without the
// switch, the project gate runs and fails the build.
func TestProjectGateSkippedOnPlainBuildUnlessStrict(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	t.Setenv("GOPROXY", "off")
	t.Setenv("GOFLAGS", "-mod=readonly")

	runCase := func(t *testing.T, viaBuildIssue, strictEnv bool) (
		gateCalls int, calls []string, finalizeInput map[string]any, result map[string]any,
	) {
		t.Helper()
		defer withExecCtx("run-n2", "exec-n2")()
		t.Setenv(config.EnvStrictBuild, "1")
		if !strictEnv {
			if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
				t.Fatal(err)
			}
		}

		repo := t.TempDir()
		gitT(t, repo, "init", "-q", "-b", "main")
		writeCommit(t, repo, "base", map[string]string{
			"go.mod":    "module example.com/ledger\n\ngo 1.21\n",
			"ledger.go": "package ledger\n\nfunc Sum(a, b int) int { return a + b }\n",
		})

		prevCheck := runProjectCheck
		t.Cleanup(func() { runProjectCheck = prevCheck })
		runProjectCheck = func(ctx context.Context, path, base string) projectcheck.Outcome {
			gateCalls++
			return prevCheck(ctx, path, base)
		}

		prevGit, prevGH := runGit, runGH
		t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
		runGit = func(context.Context, string, ...string) cmdResult { return cmdResult{} }
		runGH = func(context.Context, string, ...string) cmdResult {
			return cmdResult{Stdout: "https://github.com/o/r/pull/300\n"}
		}

		execFn := func(map[string]any) map[string]any {
			writeCommit(t, repo, "issue/failing-test", map[string]string{
				"ledger_test.go": "package ledger\n\nimport \"testing\"\n\nfunc TestSumFails(t *testing.T) { t.Fatal(\"repo test fails\") }\n",
			})
			return map[string]any{
				"completed_issues": []any{map[string]any{"name": "add-test"}},
				"merged_branches":  []any{"issue/add-test"},
				"all_issues":       []any{map[string]any{"name": "add-test"}},
				"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
			}
		}
		base := buildHandler(execFn, func(map[string]any) map[string]any {
			return map[string]any{"passed": true, "summary": "OVERALL: PASS", "criteria_results": []any{}}
		})
		var mu sync.Mutex
		app := &mockApp{handler: func(ctx context.Context, target string, input map[string]any) (map[string]any, error) {
			mu.Lock()
			calls = append(calls, target[strings.LastIndex(target, ".")+1:])
			if strings.HasSuffix(target, ".run_repo_finalize") {
				finalizeInput = input
			}
			mu.Unlock()
			switch {
			case strings.HasSuffix(target, ".run_git_init"):
				return map[string]any{
					"success": true, "integration_branch": "feature/add-test",
					"original_branch": "main", "mode": "fresh",
					"remote_url": "https://github.com/o/r.git", "remote_default_branch": "main",
				}, nil
			case strings.HasSuffix(target, ".run_github_pr"):
				return map[string]any{"pr_url": "https://github.com/o/r/pull/300", "pr_number": 300}, nil
			}
			return base(ctx, target, input)
		}}

		deps := &Deps{App: app, NodeID: "swe-planner"}
		cfg := map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0}
		var (
			out any
			err error
		)
		if viaBuildIssue {
			out, err = BuildIssue(context.Background(), deps, map[string]any{
				"title":       "Add Sum test",
				"repo_path":   repo,
				"plan_review": false,
				"config":      cfg,
			})
		} else {
			out, err = Build(context.Background(), deps, map[string]any{
				"goal":      "Add Sum test",
				"repo_path": repo,
				"config":    cfg,
			})
		}
		if err != nil {
			t.Fatalf("build: %v", err)
		}
		result, _ = out.(map[string]any)
		return gateCalls, calls, finalizeInput, result
	}

	t.Run("plain build switch unset runs no project command and succeeds", func(t *testing.T) {
		gateCalls, calls, finalizeInput, result := runCase(t, false, false)
		if gateCalls != 0 {
			t.Fatalf("runProjectCheck called %d times with switch unset, want 0", gateCalls)
		}
		if !asBool(result["success"]) {
			t.Fatalf("plain build failed on repository's own tests when switch unset: %v", result["verification"])
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("plain passing build did not reach run_github_pr: %v", calls)
		}
		if _, hasStrict := finalizeInput["strict"]; hasStrict {
			t.Fatalf("plain build with switch unset passed strict to finalize: %v", finalizeInput)
		}
	})

	t.Run("plain build switch set runs project gate and fails", func(t *testing.T) {
		gateCalls, _, finalizeInput, result := runCase(t, false, true)
		if gateCalls != 1 {
			t.Fatalf("runProjectCheck called %d times with switch set, want 1", gateCalls)
		}
		if asBool(result["success"]) {
			t.Fatalf("strict build passed despite failing repo test: %v", result["verification"])
		}
		verif, _ := result["verification"].(map[string]any)
		if !strings.Contains(mapStr(verif, "summary", ""), "go test ./...") {
			t.Fatalf("verification summary missing failing test command: %v", verif)
		}
		if finalizeInput["strict"] != true {
			t.Fatalf("strict build did not pass strict=true to finalize: %v", finalizeInput)
		}
	})

	t.Run("build_issue without switch runs project gate and fails", func(t *testing.T) {
		gateCalls, calls, finalizeInput, result := runCase(t, true, false)
		if gateCalls != 1 {
			t.Fatalf("runProjectCheck called %d times for build_issue, want 1", gateCalls)
		}
		if asBool(result["success"]) {
			t.Fatalf("build_issue passed despite failing repo test: %v", result["verification"])
		}
		for _, c := range calls {
			if c == "run_github_pr" {
				t.Fatalf("failed build_issue called run_github_pr: %v", calls)
			}
		}
		if finalizeInput["strict"] != true {
			t.Fatalf("build_issue did not thread strict=true to finalize: %v", finalizeInput)
		}
	})
}

// Issue #183 (rework R1–R4): when verification passes on a two-file diff and
// run_repo_finalize subsequently tries to add or modify .gitignore (or commit
// an untracked scratch file as in #162), a strict build must repair the tree
// back to the verified commit (moving ignore rules to .git/info/exclude),
// report the repair in the result, and still open its pull request whose diff
// satisfies the exact file-scope criterion the verifier passed.
func TestFinalizationPreservesVerifiedTreeAndOpensPullRequest(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	t.Setenv("GOPROXY", "off")
	t.Setenv("GOFLAGS", "-mod=readonly")

	strictTwoFile := "test \"$(git diff --name-only main...HEAD | sort)\" = \"$(printf 'textutil/textutil.go\\ntextutil/textutil_test.go' | sort)\""

	type caseOpts struct {
		viaBuildIssue          bool
		strictEnv              bool
		hasBaseGitignore       bool
		finalizeAddsGitignore  bool
		finalizeStagesScratch  bool
		coderCommitsArtifact   bool
		extraNotCountedGitCrit string
		verifierPasses         bool
	}

	runCase := func(t *testing.T, opts caseOpts) (
		repo string, calls, git []string, result map[string]any,
	) {
		t.Helper()
		defer withExecCtx("run-183", "exec-183")()

		t.Setenv(config.EnvStrictBuild, "1")
		if !opts.strictEnv {
			if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
				t.Fatal(err)
			}
		}

		repo = t.TempDir()
		if err := os.MkdirAll(filepath.Join(repo, "textutil"), 0o755); err != nil {
			t.Fatal(err)
		}
		gitT(t, repo, "init", "-q", "-b", "main")
		baseFiles := map[string]string{
			"go.mod":                    "module example.com/sandbox\n\ngo 1.21\n",
			"textutil/textutil.go":      "package textutil\n\nfunc Reverse(s string) string { return s }\n",
			"textutil/textutil_test.go": "package textutil\n\nimport \"testing\"\n\nfunc TestReverse(t *testing.T) {}\n",
		}
		if opts.hasBaseGitignore {
			baseFiles[".gitignore"] = "*.orig\n"
		}
		writeCommit(t, repo, "base", baseFiles)

		prevGit, prevGH := runGit, runGH
		t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
		var mu sync.Mutex
		runGit = func(_ context.Context, _ string, args ...string) cmdResult {
			mu.Lock()
			defer mu.Unlock()
			git = append(git, strings.Join(args, " "))
			return cmdResult{}
		}
		runGH = func(context.Context, string, ...string) cmdResult {
			return cmdResult{Stdout: "https://github.com/o/r/pull/183\n"}
		}

		var app *mockApp
		app = &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
			mu.Lock()
			calls = append(calls, target[strings.LastIndex(target, ".")+1:])
			mu.Unlock()
			switch {
			case strings.HasSuffix(target, ".plan"):
				acs := []any{strictTwoFile}
				if opts.extraNotCountedGitCrit != "" {
					acs = append(acs, opts.extraNotCountedGitCrit)
				}
				return map[string]any{
					"prd": map[string]any{
						"acceptance_criteria": acs,
					},
					"issues":        []any{},
					"artifacts_dir": input["artifacts_dir"],
				}, nil
			case strings.HasSuffix(target, ".run_git_init"):
				return map[string]any{
					"success": true, "integration_branch": "feature/runecount",
					"original_branch": "main", "mode": "existing",
					"remote_url": "https://github.com/o/r.git", "remote_default_branch": "main",
				}, nil
			case strings.HasSuffix(target, ".execute"):
				gitT(t, repo, "checkout", "-q", "-b", "feature/runecount")
				execFiles := map[string]string{
					"textutil/textutil.go":      "package textutil\n\nimport \"unicode/utf8\"\n\nfunc Reverse(s string) string { return s }\nfunc RuneCount(s string) int { return utf8.RuneCountInString(s) }\n",
					"textutil/textutil_test.go": "package textutil\n\nimport \"testing\"\n\nfunc TestReverse(t *testing.T) {}\nfunc TestRuneCount(t *testing.T) { if RuneCount(\"a\") != 1 { t.Fatal() } }\n",
				}
				if opts.coderCommitsArtifact {
					if err := os.MkdirAll(filepath.Join(repo, "bin"), 0o755); err != nil {
						t.Fatal(err)
					}
					execFiles["bin/runecount"] = "\x7fELF"
				}
				writeCommit(t, repo, "issue/runecount", execFiles)
				if opts.finalizeStagesScratch {
					// Leave an untracked scratch file in the working tree that
					// the verifier excluded from `git diff main...HEAD` (#162).
					if err := os.WriteFile(filepath.Join(repo, "textutil", "scratch_repro_test.go"),
						[]byte("package textutil\n"), 0o644); err != nil {
						t.Fatal(err)
					}
				}
				return map[string]any{
					"completed_issues": []any{map[string]any{"name": "runecount"}},
					"merged_branches":  []any{"issue/runecount"},
					"all_issues":       []any{map[string]any{"name": "runecount"}},
					"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
				}, nil
			case strings.HasSuffix(target, ".run_verifier"):
				if !opts.verifierPasses {
					return map[string]any{
						"passed":  false,
						"summary": "OVERALL: FAIL",
						"criteria_results": []any{
							map[string]any{
								"criterion": strictTwoFile,
								"passed":    false,
								"evidence":  "work failed verification",
							},
						},
					}, nil
				}
				return map[string]any{
					"passed":  true,
					"summary": "OVERALL: PASS",
					"criteria_results": []any{
						map[string]any{
							"criterion": strictTwoFile,
							"passed":    true,
							"evidence":  "two-file scope checked",
						},
					},
				}, nil
			case strings.HasSuffix(target, ".run_repo_finalize"):
				if opts.coderCommitsArtifact {
					gitT(t, repo, "rm", "-q", "-f", "bin/runecount")
				}
				if opts.finalizeAddsGitignore || opts.finalizeStagesScratch || opts.coderCommitsArtifact {
					files := map[string]string{}
					if opts.finalizeAddsGitignore {
						prefix := ""
						if opts.hasBaseGitignore {
							prefix = "*.orig\n"
						}
						files[".gitignore"] = prefix + "/bin/\n*.test\n.artifacts/\n.worktrees/\n"
					}
					writeCommit(t, repo, "chore: finalize repo for handoff", files)
					return map[string]any{
						"success":           true,
						"gitignore_updated": opts.finalizeAddsGitignore,
						"summary":           "Extended .gitignore",
					}, nil
				}
				return map[string]any{"success": true, "summary": "Clean"}, nil
			case strings.HasSuffix(target, ".run_github_pr"):
				return map[string]any{"pr_url": "https://github.com/o/r/pull/183", "pr_number": 183}, nil
			default:
				return map[string]any{}, nil
			}
		}}

		deps := &Deps{App: app, NodeID: "swe-planner"}
		cfg := map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0}
		var (
			out any
			err error
		)
		if opts.viaBuildIssue {
			out, err = BuildIssue(context.Background(), deps, map[string]any{
				"title":        "Add textutil.RuneCount",
				"issue_number": 183,
				"repo_path":    repo,
				"plan_review":  false,
				"config":       cfg,
			})
		} else {
			out, err = Build(context.Background(), deps, map[string]any{
				"goal":      "Add textutil.RuneCount",
				"repo_path": repo,
				"config":    cfg,
			})
		}
		if err != nil {
			t.Fatalf("build: %v", err)
		}
		result, _ = out.(map[string]any)
		return repo, calls, git, result
	}

	t.Run("R1_R2_R3_repo_without_gitignore_repairs_finalize_commit_and_opens_PR", func(t *testing.T) {
		repo, calls, _, result := runCase(t, caseOpts{
			viaBuildIssue:         true,
			hasBaseGitignore:      false,
			finalizeAddsGitignore: true,
			verifierPasses:        true,
		})
		if !asBool(result["success"]) {
			t.Fatalf("strict build failed when finalize added .gitignore: %v", result["verification"])
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("strict build withheld pull request after finalize added .gitignore: %v", calls)
		}
		if prs := asMapList(result["pr_results"]); len(prs) != 1 || mapStr(prs[0], "pr_url", "") != "https://github.com/o/r/pull/183" {
			t.Fatalf("pr_results = %v, want PR #183 opened", prs)
		}
		if diff := gitT(t, repo, "diff", "--name-only", "main...HEAD"); diff != "textutil/textutil.go\ntextutil/textutil_test.go" {
			t.Fatalf("handed-off diff = %q, want only textutil/textutil.go and textutil/textutil_test.go", diff)
		}
		if _, err := os.Stat(filepath.Join(repo, ".gitignore")); !os.IsNotExist(err) {
			t.Fatalf(".gitignore still exists on disk in repo that had none: err=%v", err)
		}
		excludeBytes, err := os.ReadFile(filepath.Join(repo, ".git", "info", "exclude"))
		if err != nil || !strings.Contains(string(excludeBytes), "/bin/") {
			t.Fatalf(".git/info/exclude = %q (%v), want finalize patterns migrated to local exclude", string(excludeBytes), err)
		}
		if !strings.Contains(mapStr(result, "summary", ""), ".gitignore") {
			t.Fatalf("result summary = %q, want repair note naming .gitignore", result["summary"])
		}
	})

	t.Run("R1_R2_R3_repo_with_existing_gitignore_repairs_finalize_edit_and_opens_PR", func(t *testing.T) {
		repo, calls, _, result := runCase(t, caseOpts{
			viaBuildIssue:         true,
			hasBaseGitignore:      true,
			finalizeAddsGitignore: true,
			verifierPasses:        true,
		})
		if !asBool(result["success"]) {
			t.Fatalf("strict build failed when finalize modified existing .gitignore: %v", result["verification"])
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("strict build withheld pull request after finalize modified .gitignore: %v", calls)
		}
		if diff := gitT(t, repo, "diff", "--name-only", "main...HEAD"); diff != "textutil/textutil.go\ntextutil/textutil_test.go" {
			t.Fatalf("handed-off diff = %q, want only textutil/textutil.go and textutil/textutil_test.go", diff)
		}
		gi, err := os.ReadFile(filepath.Join(repo, ".gitignore"))
		if err != nil || string(gi) != "*.orig\n" {
			t.Fatalf(".gitignore = %q (%v), want restored to base *.orig", string(gi), err)
		}
		excludeBytes, err := os.ReadFile(filepath.Join(repo, ".git", "info", "exclude"))
		if err != nil || !strings.Contains(string(excludeBytes), "/bin/") {
			t.Fatalf(".git/info/exclude = %q (%v), want finalize patterns migrated to local exclude", string(excludeBytes), err)
		}
		if !strings.Contains(mapStr(result, "summary", ""), ".gitignore") {
			t.Fatalf("result summary = %q, want repair note naming .gitignore", result["summary"])
		}
	})

	t.Run("R4_issue_162_finalize_committing_untracked_scratch_file_is_repaired_and_opens_PR", func(t *testing.T) {
		repo, calls, _, result := runCase(t, caseOpts{
			viaBuildIssue:         true,
			hasBaseGitignore:      false,
			finalizeAddsGitignore: true,
			finalizeStagesScratch: true,
			verifierPasses:        true,
		})
		if !asBool(result["success"]) {
			t.Fatalf("strict build failed when finalize committed untracked scratch file: %v", result["verification"])
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("strict build withheld pull request: %v", calls)
		}
		if diff := gitT(t, repo, "diff", "--name-only", "main...HEAD"); diff != "textutil/textutil.go\ntextutil/textutil_test.go" {
			t.Fatalf("handed-off diff = %q, want scratch file and .gitignore dropped", diff)
		}
		if !strings.Contains(mapStr(result, "summary", ""), "scratch_repro_test.go") {
			t.Fatalf("result summary = %q, want repair note naming scratch_repro_test.go", result["summary"])
		}
	})

	t.Run("non_strict_plain_build_opens_PR_as_on_main", func(t *testing.T) {
		_, calls, _, result := runCase(t, caseOpts{
			viaBuildIssue:         false,
			strictEnv:             false,
			hasBaseGitignore:      false,
			finalizeAddsGitignore: true,
			verifierPasses:        true,
		})
		if !asBool(result["success"]) || !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("non-strict build failed or skipped PR: success=%v calls=%v", result["success"], calls)
		}
	})

	t.Run("work_failing_verification_still_withholds_PR", func(t *testing.T) {
		_, calls, git, result := runCase(t, caseOpts{
			viaBuildIssue:         true,
			hasBaseGitignore:      false,
			finalizeAddsGitignore: false,
			verifierPasses:        false,
		})
		if asBool(result["success"]) {
			t.Fatalf("result[success] = true, want false when work fails verification")
		}
		for _, c := range calls {
			if c == "run_github_pr" {
				t.Fatalf("build_issue called run_github_pr despite failed work verification: %v", calls)
			}
		}
		for _, g := range git {
			if strings.HasPrefix(g, "push") {
				t.Fatalf("build_issue pushed branch despite failed work verification: %v", git)
			}
		}
	})

	t.Run("no_new_post_finalize_failure_path_and_legitimate_created_artifact_removal_preserved", func(t *testing.T) {
		repo, calls, _, result := runCase(t, caseOpts{
			viaBuildIssue:          true,
			hasBaseGitignore:       false,
			finalizeAddsGitignore:  true,
			coderCommitsArtifact:   true,
			extraNotCountedGitCrit: "`git diff --name-only main...HEAD | grep -qx nonexistent`",
			verifierPasses:         true,
		})
		if !asBool(result["success"]) {
			t.Fatalf("strict build failed after finalize despite passing verification: %v", result["verification"])
		}
		if !strings.Contains(strings.Join(calls, " "), "run_github_pr") {
			t.Fatalf("strict build withheld pull request: %v", calls)
		}
		if diff := gitT(t, repo, "diff", "--name-only", "main...HEAD"); diff != "textutil/textutil.go\ntextutil/textutil_test.go" {
			t.Fatalf("handed-off diff = %q, want bin/runecount kept removed and .gitignore dropped", diff)
		}
	})
}

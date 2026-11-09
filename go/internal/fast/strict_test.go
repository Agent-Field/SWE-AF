package fast

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
	"github.com/Agent-Field/SWE-AF/go/internal/projectcheck"
)

func gitFastT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func commitFastFiles(t *testing.T, dir, msg string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitFastT(t, dir, "add", "-A")
	gitFastT(t, dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
}

// N1: On swe-fast.build, a zero-change run with SWE_STRICT_BUILD unset reaches
// run_github_pr and records no nochange.Note; with SWE_STRICT_BUILD set, it
// skips run_github_pr and records nochange.Note.
func TestFastBuildZeroChangeGatedByStrictSwitch(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}

	runCase := func(t *testing.T, strictEnv bool) (targets []string, notes []recordedNote, finalizeKw, prKw, result map[string]any) {
		t.Helper()
		t.Setenv(config.EnvStrictBuild, "1")
		if !strictEnv {
			if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
				t.Fatal(err)
			}
		}

		repo := t.TempDir()
		gitFastT(t, repo, "init", "-q", "-b", "main")
		commitFastFiles(t, repo, "seed", map[string]string{"ledger.go": "package ledger\n"})
		gitFastT(t, repo, "checkout", "-q", "-b", "feature/fast-zero")
		gitFastT(t, repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "noop")

		rec := &noteRecorder{}
		scripter := &callScripter{fn: func(_ context.Context, target string, kwargs map[string]any) (map[string]any, error) {
			switch {
			case contains(target, "run_git_init"):
				return map[string]any{
					"success":               true,
					"integration_branch":    "feature/fast-zero",
					"original_branch":       "main",
					"mode":                  "branch",
					"remote_url":            "https://github.com/o/r.git",
					"remote_default_branch": "main",
				}, nil
			case contains(target, "fast_plan_tasks"):
				return planResultFixture, nil
			case contains(target, "fast_execute_tasks"):
				return execResultFixture, nil
			case contains(target, "fast_verify"):
				return verifyFixture(true), nil
			case contains(target, "run_repo_finalize"):
				finalizeKw = kwargs
				return finalizeFixture, nil
			case contains(target, "run_github_pr"):
				prKw = kwargs
				return map[string]any{"pr_url": "https://github.com/o/r/pull/99", "pr_number": 99}, nil
			default:
				return map[string]any{}, nil
			}
		}}
		deps := &Deps{Call: scripter.call, Note: rec, NodeID: "swe-fast"}
		out, err := Build(context.Background(), deps, map[string]any{
			"goal":      "No-op change",
			"repo_path": repo,
		})
		if err != nil {
			t.Fatalf("Build error: %v", err)
		}
		return scripter.targets(), rec.notes, finalizeKw, prKw, asMap(t, out)
	}

	t.Run("switch unset reaches PR step and records no nochange note", func(t *testing.T) {
		targets, notes, finalizeKw, prKw, res := runCase(t, false)
		foundPR := false
		for _, tgt := range targets {
			if tgt == "swe-fast.run_github_pr" {
				foundPR = true
			}
		}
		if !foundPR || res["pr_url"] != "https://github.com/o/r/pull/99" {
			t.Fatalf("targets=%v pr_url=%v, want swe-fast.run_github_pr reached", targets, res["pr_url"])
		}
		if _, ok := finalizeKw["strict"]; ok {
			t.Fatalf("switch unset passed strict to finalize: %v", finalizeKw)
		}
		if _, ok := prKw["strict"]; ok {
			t.Fatalf("switch unset passed strict to run_github_pr: %v", prKw)
		}
		for _, n := range notes {
			if n.message == nochange.Note {
				t.Fatalf("switch unset recorded %q: %+v", nochange.Note, notes)
			}
		}
	})

	t.Run("switch set skips PR step and records nochange note", func(t *testing.T) {
		targets, notes, finalizeKw, _, res := runCase(t, true)
		for _, tgt := range targets {
			if tgt == "swe-fast.run_github_pr" {
				t.Fatalf("strict fast build called run_github_pr on zero-change repo: %v", targets)
			}
		}
		if res["pr_url"] != "" {
			t.Fatalf("pr_url = %v, want empty", res["pr_url"])
		}
		if finalizeKw["strict"] != true {
			t.Fatalf("strict fast build did not pass strict=true to finalize: %v", finalizeKw)
		}
		foundNote := false
		for _, n := range notes {
			foundNote = foundNote || n.message == nochange.Note
		}
		if !foundNote {
			t.Fatalf("missing %q in notes: %+v", nochange.Note, notes)
		}
	})
}

// N2: On swe-fast.build, with SWE_STRICT_BUILD unset no project build or test
// command runs, and a repository whose own tests fail does not fail the build;
// with SWE_STRICT_BUILD set, the project gate runs and fails the build.
func TestFastBuildProjectGateGatedByStrictSwitch(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	t.Setenv("GOPROXY", "off")
	t.Setenv("GOFLAGS", "-mod=readonly")

	runCase := func(t *testing.T, strictEnv bool) (gateCalls int, result map[string]any) {
		t.Helper()
		t.Setenv(config.EnvStrictBuild, "1")
		if !strictEnv {
			if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
				t.Fatal(err)
			}
		}

		repo := t.TempDir()
		gitFastT(t, repo, "init", "-q", "-b", "main")
		commitFastFiles(t, repo, "base", map[string]string{
			"go.mod":    "module example.com/fastledger\n\ngo 1.21\n",
			"ledger.go": "package fastledger\n\nfunc Add(a, b int) int { return a + b }\n",
		})

		prevCheck := runProjectCheck
		t.Cleanup(func() { runProjectCheck = prevCheck })
		runProjectCheck = func(ctx context.Context, path, base string) projectcheck.Outcome {
			gateCalls++
			return prevCheck(ctx, path, base)
		}

		deps, _ := buildDeps(func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
			switch {
			case contains(target, "run_git_init"):
				return gitInitFixture, nil
			case contains(target, "fast_plan_tasks"):
				return planResultFixture, nil
			case contains(target, "fast_execute_tasks"):
				commitFastFiles(t, repo, "failing-test", map[string]string{
					"ledger_test.go": "package fastledger\n\nimport \"testing\"\n\nfunc TestBroken(t *testing.T) { t.Fatal(\"broken\") }\n",
				})
				return execResultFixture, nil
			case contains(target, "fast_verify"):
				return verifyFixture(true), nil
			case contains(target, "run_repo_finalize"):
				return finalizeFixture, nil
			default:
				return map[string]any{}, nil
			}
		})

		out, err := Build(context.Background(), deps, map[string]any{
			"goal":      "Add tests",
			"repo_path": repo,
		})
		if err != nil {
			t.Fatalf("Build error: %v", err)
		}
		return gateCalls, asMap(t, out)
	}

	t.Run("switch unset runs no project command and succeeds", func(t *testing.T) {
		gateCalls, res := runCase(t, false)
		if gateCalls != 0 {
			t.Fatalf("runProjectCheck called %d times with switch unset, want 0", gateCalls)
		}
		if res["success"] != true {
			t.Fatalf("fast build failed on repo's own failing test with switch unset: %v", res["verification"])
		}
	})

	t.Run("switch set runs project gate and fails", func(t *testing.T) {
		gateCalls, res := runCase(t, true)
		if gateCalls != 1 {
			t.Fatalf("runProjectCheck called %d times with switch set, want 1", gateCalls)
		}
		if res["success"] != false {
			t.Fatalf("strict fast build passed despite failing repo test: %v", res["verification"])
		}
	})
}

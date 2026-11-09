package orch

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
)

type derivedRunOpts struct {
	viaBuildIssue bool
	changeFiles   bool
	passed        bool
	emptyExec     bool
	prURL         string
	pushFail      bool
	execErr       error
	cancelInExec  bool
}

func runDerivedWorkspaceBuild(t *testing.T, opts derivedRunOpts) (derived string, notes []string, err error) {
	t.Helper()
	defer withExecCtx("run-ws", "exec-ws")()

	wsRoot := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", wsRoot)

	prevGit, prevGH := runGit, runGH
	t.Cleanup(func() { runGit, runGH = prevGit, prevGH })

	runGit = func(_ context.Context, _ string, args ...string) cmdResult {
		if len(args) >= 3 && args[0] == "clone" {
			derived = args[2]
			if mkErr := os.MkdirAll(derived, 0o755); mkErr != nil {
				t.Fatal(mkErr)
			}
			gitT(t, derived, "init", "-q", "-b", "main")
			writeCommit(t, derived, "seed", map[string]string{"README.md": "# demo\n"})
			return cmdResult{}
		}
		if len(args) > 0 && args[0] == "push" && opts.pushFail {
			return cmdResult{ExitCode: 1, Stderr: "push refused"}
		}
		return cmdResult{}
	}
	runGH = func(context.Context, string, ...string) cmdResult {
		if opts.prURL == "" {
			return cmdResult{ExitCode: 1, Stderr: "gh failed"}
		}
		return cmdResult{Stdout: opts.prURL + "\n"}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	writeCheckpoint := func() {
		cpPath := filepath.Join(derived, ".artifacts", "execution", "checkpoint.json")
		if mkErr := os.MkdirAll(filepath.Dir(cpPath), 0o755); mkErr != nil {
			t.Fatal(mkErr)
		}
		if wErr := os.WriteFile(cpPath, []byte(`{"all_issues":[{"name":"i1"}],"levels":[["i1"]]}`), 0o644); wErr != nil {
			t.Fatal(wErr)
		}
	}

	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".plan"):
			return map[string]any{
				"prd": map[string]any{}, "issues": []any{map[string]any{"name": "i1"}},
				"artifacts_dir": input["artifacts_dir"],
			}, nil
		case strings.HasSuffix(target, ".run_git_init"):
			gitT(t, derived, "checkout", "-q", "-b", "feature/demo")
			return map[string]any{
				"success": true, "integration_branch": "feature/demo",
				"original_branch": "main", "mode": "fresh",
				"remote_url": "https://github.com/example/demo.git", "remote_default_branch": "main",
			}, nil
		case strings.HasSuffix(target, ".execute"):
			writeCheckpoint()
			if opts.cancelInExec {
				cancel()
				return nil, ctx.Err()
			}
			if opts.execErr != nil {
				return nil, opts.execErr
			}
			if opts.changeFiles {
				writeCommit(t, derived, "issue/i1: add feature", map[string]string{"feature.txt": "new\n"})
			}
			if opts.emptyExec {
				return emptyExec(input), nil
			}
			return map[string]any{
				"completed_issues": []any{map[string]any{"name": "i1"}},
				"merged_branches":  []any{"issue/i1"},
				"all_issues":       []any{map[string]any{"name": "i1"}},
				"failed_issues":    []any{}, "skipped_issues": []any{}, "accumulated_debt": []any{},
			}, nil
		case strings.HasSuffix(target, ".run_verifier"):
			return map[string]any{"passed": opts.passed, "criteria_results": []any{}, "summary": "verdict"}, nil
		case strings.HasSuffix(target, ".run_repo_finalize"):
			return map[string]any{"success": true, "summary": ""}, nil
		case strings.HasSuffix(target, ".run_github_pr"):
			if opts.prURL == "" {
				return map[string]any{"success": false, "error_message": "no pr"}, nil
			}
			return map[string]any{"success": true, "pr_url": opts.prURL, "pr_number": 1}, nil
		default:
			return map[string]any{}, nil
		}
	}}

	deps := &Deps{App: app, NodeID: "swe-planner"}
	cfg := map[string]any{"git_init_max_retries": 1, "max_verify_fix_cycles": 0}
	if opts.viaBuildIssue {
		_, err = BuildIssue(ctx, deps, map[string]any{
			"title":       "Ship feature",
			"repo_url":    "https://github.com/example/demo.git",
			"plan_review": false,
			"config":      cfg,
		})
	} else {
		_, err = Build(ctx, deps, map[string]any{
			"goal":     "Ship feature",
			"repo_url": "https://github.com/example/demo.git",
			"config":   cfg,
		})
	}
	return derived, app.notes, err
}

// V5: The derived workspace is removed after a successful, published build and
// after a no-change build. It is kept after an error, a cancel, or unpushed
// work. SWE_KEEP_WORKSPACES=1 keeps it in every case.
func TestDerivedWorkspaceCleanup(t *testing.T) {
	t.Run("removed after successful published build", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		derived, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			changeFiles: true,
			passed:      true,
			prURL:       "https://github.com/example/demo/pull/1",
		})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if _, statErr := os.Stat(derived); !os.IsNotExist(statErr) {
			t.Fatalf("derived workspace %s still exists after published build: %v", derived, statErr)
		}
	})

	t.Run("removed after no-change build", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		t.Setenv(config.EnvStrictBuild, "1")
		derived, notes, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			changeFiles: false,
			passed:      true,
			prURL:       "https://github.com/example/demo/pull/1",
		})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !strings.Contains(strings.Join(notes, "\n"), nochange.Note) {
			t.Fatalf("expected no-change note, got %v", notes)
		}
		if _, statErr := os.Stat(derived); !os.IsNotExist(statErr) {
			t.Fatalf("derived workspace %s still exists after no-change build: %v", derived, statErr)
		}
	})

	t.Run("kept after error so resume_build finds checkpoint", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		derived, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			execErr: errors.New("executor crashed"),
		})
		if err == nil {
			t.Fatal("expected build error")
		}
		cp := filepath.Join(derived, ".artifacts", "execution", "checkpoint.json")
		if _, statErr := os.Stat(cp); statErr != nil {
			t.Fatalf("checkpoint missing after errored build (%s): %v", cp, statErr)
		}
		resumeApp := &mockApp{handler: func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
			return map[string]any{"status": "succeeded"}, nil
		}}
		if _, rErr := ResumeBuildHandler(context.Background(), &Deps{App: resumeApp, NodeID: "swe-planner"}, map[string]any{
			"repo_path": derived,
		}); rErr != nil {
			t.Fatalf("resume_build failed on kept workspace: %v", rErr)
		}
	})

	t.Run("kept after empty build ReasonerFailed error", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		derived, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			emptyExec: true,
			passed:    false,
		})
		var rf *agent.ReasonerFailed
		if !errors.As(err, &rf) {
			t.Fatalf("expected *agent.ReasonerFailed, got %v", err)
		}
		if _, statErr := os.Stat(derived); statErr != nil {
			t.Fatalf("derived workspace removed after ReasonerFailed: %v", statErr)
		}
	})

	t.Run("kept after cancel", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		derived, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			cancelInExec: true,
		})
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("expected context.Canceled, got %v", err)
		}
		cp := filepath.Join(derived, ".artifacts", "execution", "checkpoint.json")
		if _, statErr := os.Stat(cp); statErr != nil {
			t.Fatalf("checkpoint missing after cancelled build: %v", statErr)
		}
	})

	t.Run("kept after unpushed work", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "")
		// build_issue withholds push/PR on failed verification -> unpushed work kept.
		derivedIssue, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			viaBuildIssue: true,
			changeFiles:   true,
			passed:        false,
			prURL:         "https://github.com/example/demo/pull/1",
		})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if _, statErr := os.Stat(filepath.Join(derivedIssue, "feature.txt")); statErr != nil {
			t.Fatalf("unpushed build_issue workspace removed: %v", statErr)
		}

		// Plain build where push and PR both fail -> unpushed work kept.
		derivedPushFail, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
			changeFiles: true,
			passed:      true,
			pushFail:    true,
			prURL:       "",
		})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if _, statErr := os.Stat(filepath.Join(derivedPushFail, "feature.txt")); statErr != nil {
			t.Fatalf("unpushed failed-PR workspace removed: %v", statErr)
		}
	})

	t.Run("SWE_KEEP_WORKSPACES=1 keeps in every case", func(t *testing.T) {
		t.Setenv("SWE_KEEP_WORKSPACES", "1")
		for _, change := range []bool{true, false} {
			derived, _, err := runDerivedWorkspaceBuild(t, derivedRunOpts{
				changeFiles: change,
				passed:      true,
				prURL:       "https://github.com/example/demo/pull/1",
			})
			if err != nil {
				t.Fatalf("change=%v error: %v", change, err)
			}
			if _, statErr := os.Stat(derived); statErr != nil {
				t.Fatalf("SWE_KEEP_WORKSPACES=1 did not keep workspace (change=%v): %v", change, statErr)
			}
		}
	})
}

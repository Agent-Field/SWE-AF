package gitops

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

func emptyCommitRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	repo := t.TempDir()
	git := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@t"}, args...)...)
		cmd.Dir = repo
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	git("init", "-q", "-b", "main")
	git("commit", "-q", "--allow-empty", "-m", "base")
	git("checkout", "-q", "-b", "feature/haszero")
	git("commit", "-q", "--allow-empty", "-m", "issue/ledger-haszero: verify HasZero")
	return repo
}

// Issue #309 (WorkAF r9): with SWE_STRICT_BUILD enabled, the PR role itself
// refuses a branch that changes no file, whatever empty commits it carries, and
// never starts the PR agent.
func TestThePRRoleOpensNothingForABranchThatChangesNothing(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
	repo := emptyCommitRepo(t)

	ran := false
	deps, app := newDeps(func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
		ran = true
		return &harness.Result{}, nil
	})
	out, err := RunGitHubPR(context.Background(), deps, map[string]any{
		"repo_path": repo, "integration_branch": "feature/haszero", "base_branch": "main", "goal": "g",
	})
	if err != nil {
		t.Fatal(err)
	}
	res, _ := out.(schemas.GitHubPRResult)
	if ran || res.PRURL != "" || res.ErrorMessage != nochange.Note {
		t.Fatalf("ran agent %v, result %+v", ran, res)
	}
	found := false
	for _, n := range app.notes {
		found = found || strings.HasPrefix(n.message, "No pull request:")
	}
	if !found {
		t.Fatalf("notes = %+v", app.notes)
	}
}

// N1: With SWE_STRICT_BUILD unset and no strict input flag, RunGitHubPR reaches
// the PR agent on a zero-change branch and records no nochange.Note; with
// SWE_STRICT_BUILD set or strict:true threaded from build_issue, it refuses.
func TestGitHubPRZeroChangeGatedByStrictSwitch(t *testing.T) {
	t.Run("switch unset reaches PR agent and records no nochange note", func(t *testing.T) {
		t.Setenv(config.EnvStrictBuild, "1")
		if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
			t.Fatal(err)
		}
		repo := emptyCommitRepo(t)
		ran := false
		deps, app := newDeps(func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
			ran = true
			if err := json.Unmarshal([]byte(`{"success":true,"pr_url":"https://github.com/o/r/pull/12","pr_number":12}`), dest); err != nil {
				return nil, err
			}
			return &harness.Result{Parsed: dest}, nil
		})
		out, err := RunGitHubPR(context.Background(), deps, map[string]any{
			"repo_path": repo, "integration_branch": "feature/haszero", "base_branch": "main", "goal": "g",
		})
		if err != nil {
			t.Fatal(err)
		}
		res, _ := out.(schemas.GitHubPRResult)
		if !ran || !res.Success || res.PRURL != "https://github.com/o/r/pull/12" {
			t.Fatalf("ran=%v res=%+v, want PR agent reached", ran, res)
		}
		for _, n := range app.notes {
			if n.message == nochange.Note {
				t.Fatalf("switch unset recorded %q: %+v", nochange.Note, app.notes)
			}
		}
	})

	t.Run("strict input flag without switch refuses zero-change PR", func(t *testing.T) {
		t.Setenv(config.EnvStrictBuild, "1")
		if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
			t.Fatal(err)
		}
		repo := emptyCommitRepo(t)
		ran := false
		deps, app := newDeps(func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
			ran = true
			return &harness.Result{}, nil
		})
		out, err := RunGitHubPR(context.Background(), deps, map[string]any{
			"repo_path":          repo,
			"integration_branch": "feature/haszero",
			"base_branch":        "main",
			"goal":               "g",
			"strict":             true,
		})
		if err != nil {
			t.Fatal(err)
		}
		res, _ := out.(schemas.GitHubPRResult)
		if ran || res.PRURL != "" || res.ErrorMessage != nochange.Note {
			t.Fatalf("ran=%v res=%+v, want zero-change refusal", ran, res)
		}
		if !hasNote(app, nochange.Note, "github_pr", "skipped", "no_changes") {
			t.Fatalf("missing nochange note: %+v", app.notes)
		}
	})
}

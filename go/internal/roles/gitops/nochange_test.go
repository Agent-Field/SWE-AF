package gitops

import (
	"context"
	"os/exec"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/nochange"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"github.com/Agent-Field/agentfield/sdk/go/harness"
)

// Issue #309 (WorkAF r9): the PR role itself refuses a branch that changes no
// file, whatever empty commits it carries, and never starts the PR agent.
func TestThePRRoleOpensNothingForABranchThatChangesNothing(t *testing.T) {
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

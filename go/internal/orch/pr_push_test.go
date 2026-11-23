package orch

import (
	"context"
	"strings"
	"testing"
)

// The orchestrator pushes the branch itself and, when the PR role reports no
// PR, opens one with gh whose body closes the issue.
func TestPushAndOpenPRWhenTheRoleCannot(t *testing.T) {
	prevGit, prevGH := runGit, runGH
	t.Cleanup(func() { runGit, runGH = prevGit, prevGH })
	var gitCalls, ghCalls [][]string
	runGit = func(_ context.Context, _ string, args ...string) cmdResult {
		gitCalls = append(gitCalls, args)
		return cmdResult{}
	}
	runGH = func(_ context.Context, _ string, args ...string) cmdResult {
		ghCalls = append(ghCalls, args)
		return cmdResult{Stdout: "https://github.com/o/r/pull/42\n"}
	}
	deps := &Deps{App: &mockApp{}}
	if !pushIntegrationBranch(context.Background(), deps, "/repo", "feature/x") {
		t.Fatal("push must succeed")
	}
	if strings.Join(gitCalls[0], " ") != "push -u origin feature/x" {
		t.Fatalf("git %v", gitCalls[0])
	}
	goal := "Resolve GitHub issue #83: Add a Len() method\n\nbody\n\nThe pull request description must include \"Closes #83\"."
	url, n := openPRWithGH(context.Background(), deps, "/repo", "main", "feature/x", goal, "Done.")
	if url != "https://github.com/o/r/pull/42" || n != 42 {
		t.Fatalf("pr = %q %d", url, n)
	}
	args := strings.Join(ghCalls[0], "|")
	for _, want := range []string{"--base|main", "--head|feature/x", "--title|Add a Len() method", "Closes #83"} {
		if !strings.Contains(args, want) {
			t.Errorf("gh args missing %q: %s", want, args)
		}
	}
}

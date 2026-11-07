// This file (pr_push.go) makes the push-and-PR phase work when the coding
// harness may not push. codeaf refuses an unattended run's `git push` of a
// branch it did not cut (its session git guard: "git push is not yours to run
// here"), and run_github_pr is exactly that — an unattended run on the
// integration branch the build cut. So THE ORCHESTRATOR PUSHES, in Go, before
// the PR role runs; the role only writes the description and opens the PR. If
// the role still reports no PR, the orchestrator opens one with gh itself, so
// a finished change is never stranded on the node's disk.
package orch

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// pushIntegrationBranch pushes branch to origin and reports whether it landed.
func pushIntegrationBranch(ctx context.Context, deps *Deps, repoPath, branch string) bool {
	if strings.TrimSpace(branch) == "" {
		return false
	}
	r := runGit(ctx, repoPath, "push", "-u", "origin", branch)
	if r.ExitCode != 0 {
		deps.Note(ctx, fmt.Sprintf("Push of %s failed: %s", branch, strings.TrimSpace(r.Stderr)),
			"build", "github_pr", "push", "error")
		return false
	}
	deps.Note(ctx, fmt.Sprintf("Pushed %s to origin", branch), "build", "github_pr", "push")
	return true
}

var prURLPattern = regexp.MustCompile(`https://github\.com/[^\s/]+/[^\s/]+/pull/(\d+)`)

// openPRWithGH opens the PR directly when the PR role produced none. The body
// carries the build summary and the goal's own closing line ("Closes #N").
func openPRWithGH(ctx context.Context, deps *Deps, repoPath, base, branch, goal, summary string) (string, int) {
	title := prTitleFromGoal(goal)
	body := strings.TrimSpace(summary)
	if m := regexp.MustCompile(`Closes #\d+`).FindString(goal); m != "" && !strings.Contains(body, m) {
		body += "\n\n" + m
	}
	r := runGH(ctx, repoPath, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body)
	out := r.Stdout + "\n" + r.Stderr
	m := prURLPattern.FindStringSubmatch(out)
	if m == nil {
		deps.Note(ctx, "Opening the PR with gh failed: "+strings.TrimSpace(r.Stderr), "build", "github_pr", "error")
		return "", 0
	}
	n, _ := strconv.Atoi(m[1])
	deps.Note(ctx, "PR opened by the orchestrator: "+m[0], "build", "github_pr", "fallback")
	return m[0], n
}

// prTitleFromGoal is the goal's first line without build_issue's
// "Resolve GitHub issue #N: " lead.
func prTitleFromGoal(goal string) string {
	t := firstLine(goal)
	t = regexp.MustCompile(`^Resolve (GitHub issue #\d+|this GitHub issue): `).ReplaceAllString(t, "")
	if len(t) > 120 {
		t = t[:120]
	}
	if t == "" {
		t = "SWE-AF change"
	}
	return t
}

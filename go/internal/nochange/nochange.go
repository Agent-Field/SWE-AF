// Package nochange tells a run that changed files from a run that only made
// commits.
//
// Live on 2026-11-08 (issue #309, WorkAF r9) the issue asked for HasZero,
// which was already on main. The coder recorded an empty "verify" commit, the
// level merge recorded an empty --no-ff merge commit, and so the integration
// branch was two commits ahead of main with the very same tree. GitHub's "No
// commits between" refusal never fired, PR #315 opened with 0 files, and its
// description claimed it added HasZero. The factory read it as shipped.
//
// COMMITS ARE NOT CHANGES; THE TREE IS. So before anything is pushed or a pull
// request is opened, the branch is compared with the base branch by what it
// changes (the diff from their merge base to the branch tip), and a branch
// that changes no file opens no pull request. The run ends with the plain note
// Note, which the factory reads like every "No pull request:" note.
package nochange

import (
	"context"
	"os/exec"
	"strings"
)

// Note is the outcome a run records when it made no changes. The factory
// reads a note starting "No pull request:" as the run's outcome, so the
// wording is a contract: change it only with the factory.
const Note = "No pull request: the run made no changes"

// Verdict is what Check found.
type Verdict struct {
	// Known is false when the branch or every candidate base could not be
	// resolved; the caller then proceeds as it did before this check.
	Known bool
	// Empty is true when the branch changes no file against Base.
	Empty bool
	// Base is the ref the branch was compared with.
	Base string
	// Files are the paths the branch changes, at most a handful.
	Files []string
}

// git runs one git command in repo. A package variable so tests can replace it.
var git = func(ctx context.Context, repo string, args ...string) (string, bool) {
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = repo
	out, err := cmd.Output()
	return strings.TrimSpace(string(out)), err == nil
}

// Check compares branch (HEAD when "") with the first base that resolves:
// origin/<baseBranch>, <baseBranch>, then each fallback (the commit the run
// started from). The comparison is a three-dot diff, so a base that moved on
// after the run started does not count as the run's change.
func Check(ctx context.Context, repo, branch, baseBranch string, fallbacks ...string) Verdict {
	if strings.TrimSpace(repo) == "" {
		return Verdict{}
	}
	if strings.TrimSpace(branch) == "" {
		branch = "HEAD"
	}
	if _, ok := git(ctx, repo, "rev-parse", "--verify", "--quiet", branch+"^{commit}"); !ok {
		return Verdict{}
	}
	var candidates []string
	if b := strings.TrimSpace(baseBranch); b != "" {
		candidates = append(candidates, "origin/"+b, b)
	}
	candidates = append(candidates, fallbacks...)
	for _, base := range candidates {
		base = strings.TrimSpace(base)
		if base == "" {
			continue
		}
		if _, ok := git(ctx, repo, "rev-parse", "--verify", "--quiet", base+"^{commit}"); !ok {
			continue
		}
		out, ok := git(ctx, repo, "diff", "--name-only", base+"..."+branch)
		if !ok {
			continue
		}
		v := Verdict{Known: true, Base: base}
		for _, f := range strings.Split(out, "\n") {
			if f = strings.TrimSpace(f); f != "" {
				v.Files = append(v.Files, f)
			}
		}
		v.Empty = len(v.Files) == 0
		return v
	}
	return Verdict{}
}

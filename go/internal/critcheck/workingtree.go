package critcheck

// This file (workingtree.go) generalises the malformed-check repair to the
// second class of check that fails for every implementation: one that reads
// uncommitted working-tree state after the work has been committed.
//
// Live on 2026-11-08 (issue #306, WorkAF r9) the planner wrote AC-6 as
// "`git status --porcelain` lists ledger.go and ledger_test.go (nothing under
// auth/ or cmd/)". The coder commits its work, so by the time the verifier ran
// the working tree was clean and `git status --porcelain` printed nothing. The
// verifier itself saw the change was confined to exactly those two files
// (`git diff --name-only 858b2f0 HEAD`), passed 6 of 7 criteria and the
// project build, and still failed the run; no pull request opened.
//
// A CHECK THAT READS THE WORKING TREE AFTER THE WORK IS COMMITTED SAYS NOTHING
// ABOUT THE WORK. When the tracked tree is clean and the work is committed on
// top of the run's base, such a check is rewritten to compare with the base
// (`git status …` becomes `git diff --name-only <base>...HEAD`; a bare
// `git diff` gains `<base>...HEAD`) and judged by the rewritten command: by its
// exit status when the check asserts with one (a pipe into grep, `--quiet`,
// `!`), otherwise by the file names the criterion spells, required before a
// negation ("nothing", "no", "not") and forbidden after one. A criterion
// whose words name no file is not counted, with the committed file list as its
// evidence. A named file the work did not change, or a forbidden one it did,
// still fails.

import (
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
)

// gitQuiet runs one git command in repo for this file's own questions about
// the tree. A package variable so tests can replace it.
var gitQuiet = func(ctx context.Context, repo string, args ...string) (int, string) {
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = repo
	out, err := cmd.Output()
	if err == nil {
		return 0, string(out)
	}
	if ee, ok := err.(*exec.ExitError); ok {
		return ee.ExitCode(), string(out)
	}
	return -1, string(out)
}

// workIsCommitted is true when the tracked working tree matches HEAD and HEAD
// changes files against base: the state in which a working-tree check is
// empty for every implementation.
func workIsCommitted(ctx context.Context, repo, base string) bool {
	code, out := gitQuiet(ctx, repo, "status", "--porcelain", "--untracked-files=no")
	if code != 0 || strings.TrimSpace(out) != "" {
		return false
	}
	code, _ = gitQuiet(ctx, repo, "diff", "--quiet", base+"...HEAD")
	return code == 1
}

// segmentWithSeparators finds each part of a compound command line.
var segmentWithSeparators = regexp.MustCompile(`\s*(?:&&|\|\||\||;)\s*`)

// WorkingTreeRewrite returns line with every working-tree git part rewritten
// to compare with base, or "" when no part reads the working tree. isRev says
// whether a bare argument to `git diff` names a commit (so the diff already
// has a base and is left alone).
func WorkingTreeRewrite(line, base string, isRev func(string) bool) string {
	seps := segmentWithSeparators.FindAllStringIndex(line, -1)
	var parts []string
	prev := 0
	for _, loc := range seps {
		parts = append(parts, line[prev:loc[0]], line[loc[0]:loc[1]])
		prev = loc[1]
	}
	parts = append(parts, line[prev:])
	changed := false
	for i := 0; i < len(parts); i += 2 {
		if r, ok := rewriteGitPart(parts[i], base, isRev); ok {
			parts[i] = r
			changed = true
		}
	}
	if !changed {
		return ""
	}
	return strings.Join(parts, "")
}

func rewriteGitPart(part, base string, isRev func(string) bool) (string, bool) {
	trimmed := strings.TrimSpace(part)
	neg := ""
	if strings.HasPrefix(trimmed, "! ") {
		neg = "! "
		trimmed = strings.TrimSpace(trimmed[2:])
	}
	f := strings.Fields(trimmed)
	if len(f) < 2 || f[0] != "git" {
		return "", false
	}
	rng := base + "...HEAD"
	switch f[1] {
	case "status":
		return neg + "git diff --name-only " + rng, true
	case "diff":
		var flags, paths []string
		afterDash := false
		for _, a := range f[2:] {
			switch {
			case afterDash:
				paths = append(paths, a)
			case a == "--":
				afterDash = true
			case a == "--cached" || a == "--staged":
				// The index is working-tree state too; the base range replaces it.
			case strings.HasPrefix(a, "-"):
				flags = append(flags, a)
			case strings.Contains(a, "..") || (isRev != nil && isRev(a)):
				return "", false // the diff already names a base
			default:
				paths = append(paths, a)
			}
		}
		out := append([]string{"git", "diff"}, flags...)
		out = append(out, rng)
		if len(paths) > 0 {
			out = append(append(out, "--"), paths...)
		}
		return neg + strings.Join(out, " "), true
	}
	return "", false
}

// exitAsserting is true when a command line's exit status is its assertion.
var exitAsserting = regexp.MustCompile(`\||&&|;|(?:^|\s)!\s|--quiet\b|--exit-code\b`)

// pathToken is a file (with an extension) or a directory (with a trailing
// slash) spelled in a criterion.
var pathToken = regexp.MustCompile(`(?:^|[\s(\x60'"/,])((?:[A-Za-z0-9_.\-]+/)*[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9]+|/))`)

// negation splits a criterion's expected paths from its forbidden ones.
var negation = regexp.MustCompile(`(?i)\b(?:nothing|no|not|never|none|without)\b`)

// namedPaths returns the paths a criterion requires and forbids, read from its
// words with its commands removed.
func namedPaths(crit string, cmds []string) (required, forbidden []string) {
	text := crit
	for _, c := range cmds {
		text = strings.ReplaceAll(text, c, " ")
	}
	cut := len(text)
	if loc := negation.FindStringIndex(text); loc != nil {
		cut = loc[0]
	}
	for _, m := range pathToken.FindAllStringSubmatchIndex(text, -1) {
		p := text[m[2]:m[3]]
		if m[2] < cut {
			required = append(required, p)
		} else {
			forbidden = append(forbidden, p)
		}
	}
	return required, forbidden
}

// judgeFiles checks a changed-file list against what the criterion names. It
// returns whether the criterion names anything, and whether the list holds.
func judgeFiles(out string, required, forbidden []string) (named, ok bool, why string) {
	if len(required) == 0 && len(forbidden) == 0 {
		return false, false, ""
	}
	var files []string
	for _, l := range strings.Split(out, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			files = append(files, strings.Fields(l)[0])
		}
	}
	has := func(p string) bool {
		for _, f := range files {
			if strings.HasSuffix(p, "/") {
				if strings.HasPrefix(f, p) || strings.Contains(f, "/"+p) {
					return true
				}
			} else if f == p || strings.HasSuffix(f, "/"+p) {
				return true
			}
		}
		return false
	}
	for _, p := range required {
		if !has(p) {
			return true, false, p + " is not among the changed files"
		}
	}
	for _, p := range forbidden {
		if has(p) {
			return true, false, p + " is among the changed files"
		}
	}
	return true, true, ""
}

// reconcileWorkingTree rewrites the first working-tree command of a failed
// criterion to compare with base and judges the criterion by it.
func reconcileWorkingTree(ctx context.Context, repo, base string, c map[string]any, crit string, cmds []string) (Finding, bool) {
	isRev := func(a string) bool {
		code, _ := gitQuiet(ctx, repo, "rev-parse", "--verify", "--quiet", a+"^{commit}")
		return code == 0
	}
	shown := base
	if len(shown) == 40 {
		shown = shown[:12]
	}
	for i, line := range cmds {
		if i >= maxCommands {
			break
		}
		fixed := WorkingTreeRewrite(line, shown, isRev)
		if fixed == "" {
			continue
		}
		code, out, err := runShell(ctx, repo, fixed)
		if err != nil || Malformed(out) != "" {
			continue
		}
		f := Finding{Criterion: crit, Command: line, Problem: workingTreeProblem, Repair: fixed, WorkingTree: true}
		original, _ := c["evidence"].(string)
		var why string
		switch {
		case exitAsserting.MatchString(line):
			f.Action = RewrittenFailed
			if code == 0 {
				f.Action = Rewritten
			}
		default:
			required, forbidden := namedPaths(crit, cmds)
			named, ok, reason := judgeFiles(out, required, forbidden)
			switch {
			case !named:
				f.Action = NotCounted
				f.Repair = ""
			case ok:
				f.Action = Rewritten
			default:
				f.Action = RewrittenFailed
				why = " (" + reason + ")"
			}
		}
		switch f.Action {
		case Rewritten:
			c["passed"] = true
			c["repaired_command"] = fixed
		case RewrittenFailed:
			c["repaired_command"] = fixed
		case NotCounted:
			c["passed"] = true
			c["not_counted"] = true
		}
		c["evidence"] = strings.TrimSpace(fmt.Sprintf("%s%s.\n`%s` exited %d:\n%s\nThe verifier said: %s",
			f.Note(), why, fixed, code, tail(out), original))
		return f, true
	}
	return Finding{}, false
}

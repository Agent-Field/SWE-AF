package gitops

import (
	"bytes"
	"context"
	"os/exec"
	"path"
	"sort"
	"strings"
)

// The finalize guard: what the cleanup agent may and may not do to a
// repository, enforced in code after it has run.
//
// Live on 2026-11-08 (job 77, issue #265) the cleanup agent was told to
// remove "pipeline leftovers", read the repository's own `.codeaf/factory.yaml`
// as one, and deleted it in "chore: finalize repo for handoff". The pull
// request carried the deletion, the person pressed Merge, and the
// repository's factory configuration was gone from main.
//
// THE CLEANUP MAY ONLY REMOVE WHAT THE RUN ITSELF CREATED, AND MAY ONLY EDIT
// .gitignore. Every other path it deleted or changed is put back to what it
// was before the cleanup ran, and each one is reported. The prompt says the
// same thing, but a prompt is a request; this is the rule.

// gitOut runs git in dir and returns trimmed stdout and whether it exited 0.
// A package variable so a test can see the argv; the default runs git.
var gitOut = func(ctx context.Context, dir string, args ...string) (string, bool) {
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = dir
	var out bytes.Buffer
	cmd.Stdout = &out
	err := cmd.Run()
	return strings.TrimSpace(out.String()), err == nil
}

// finalizeScope is what the guard needs to know before the agent runs.
type finalizeScope struct {
	repo    string
	preHead string // HEAD before the cleanup ran
	base    string // the commit the run started from
	// created are the tracked paths that exist at preHead but not at base:
	// the only tracked files the cleanup may remove.
	created []string
}

// openFinalizeScope records the repository's state before the cleanup runs.
// ok is false when repo is not a git repository with a commit, in which case
// there is nothing to guard and the cleanup runs unguarded as before.
//
// base is the run's starting commit when the caller knows it. Without it the
// guard tries the merge base with the remote's default branch, and failing
// that uses preHead itself — which protects every tracked file, the safe
// reading when nothing says which files are the run's.
func openFinalizeScope(ctx context.Context, repo, base string) (finalizeScope, bool) {
	preHead, ok := gitOut(ctx, repo, "rev-parse", "--verify", "--quiet", "HEAD")
	if !ok || preHead == "" {
		return finalizeScope{}, false
	}
	s := finalizeScope{repo: repo, preHead: preHead}
	if b := strings.TrimSpace(base); b != "" {
		if sha, ok := gitOut(ctx, repo, "rev-parse", "--verify", "--quiet", b+"^{commit}"); ok && sha != "" {
			s.base = sha
		}
	}
	if s.base == "" {
		for _, ref := range []string{"origin/HEAD", "origin/main", "origin/master"} {
			if mb, ok := gitOut(ctx, repo, "merge-base", "HEAD", ref); ok && mb != "" {
				s.base = mb
				break
			}
		}
	}
	if s.base == "" {
		s.base = preHead
	}
	if s.base != preHead {
		if out, ok := gitOut(ctx, repo, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", s.base, preHead); ok {
			s.created = splitZ(out)
		}
	}
	return s, true
}

// restore puts back every path the cleanup was not allowed to touch, commits
// the restoration, and returns the restored paths (sorted). A path the
// cleanup deleted is allowed only if the run created it; a path it modified is
// allowed only if it is a .gitignore. Additions are allowed.
func (s finalizeScope) restore(ctx context.Context) []string {
	out, ok := gitOut(ctx, s.repo, "diff", "--name-status", "--no-renames", "-z", s.preHead, "--")
	if !ok {
		return nil
	}
	created := make(map[string]bool, len(s.created))
	for _, p := range s.created {
		created[p] = true
	}
	fields := splitZ(out)
	var restore []string
	for i := 0; i+1 < len(fields); i += 2 {
		status, p := fields[i], fields[i+1]
		switch status[0] {
		case 'D':
			if !created[p] {
				restore = append(restore, p)
			}
		case 'M', 'T':
			if path.Base(p) != ".gitignore" {
				restore = append(restore, p)
			}
		}
	}
	if len(restore) == 0 {
		return nil
	}
	sort.Strings(restore)
	args := append([]string{"checkout", s.preHead, "--"}, restore...)
	if _, ok := gitOut(ctx, s.repo, args...); !ok {
		return nil
	}
	if _, clean := gitOut(ctx, s.repo, "diff", "--cached", "--quiet"); !clean {
		msg := "chore: restore files the cleanup removed or changed\n\n" +
			"The repo cleanup may only remove files this run created. Restored:\n  " +
			strings.Join(restore, "\n  ")
		if _, ok := gitOut(ctx, s.repo, "commit", "--no-verify", "-q", "-m", msg); !ok {
			// No committer identity in this clone: commit as the pipeline.
			gitOut(ctx, s.repo, "-c", "user.name=SWE-AF", "-c", "user.email=swe-af@agentfield.ai",
				"commit", "--no-verify", "-q", "-m", msg)
		}
	}
	return restore
}

func splitZ(s string) []string {
	var out []string
	for _, f := range strings.Split(s, "\x00") {
		if f != "" {
			out = append(out, f)
		}
	}
	return out
}

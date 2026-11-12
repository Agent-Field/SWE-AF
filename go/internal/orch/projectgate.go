package orch

import (
	"context"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/critcheck"
	"github.com/Agent-Field/SWE-AF/go/internal/projectcheck"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// projectTarget is one repository the project gate checks, with the commit
// its run started from.
type projectTarget struct {
	path, base, name string
}

// projectTargets lists the repositories a build changed: every repo of a
// multi-repo manifest (each against the starting commit its git_init
// recorded), or the single repo against the commit the build started on.
func projectTargets(manifest *schemas.WorkspaceManifest, repoPath, buildBaseSHA string) []projectTarget {
	if manifest != nil && len(manifest.Repos) > 1 {
		out := make([]projectTarget, 0, len(manifest.Repos))
		for _, r := range manifest.Repos {
			out = append(out, projectTarget{
				path: r.AbsolutePath,
				base: mapStr(r.GitInitResult, "initial_commit_sha", ""),
				name: r.RepoName,
			})
		}
		return out
	}
	return []projectTarget{{path: repoPath, base: buildBaseSHA}}
}

// reconcileCriteria is the seam tests replace; the default is the real pass.
var reconcileCriteria = critcheck.ReconcileFrom

// applyCriterionAudit re-runs the commands of the verifier's failed criteria
// and repairs, or stops counting, the ones the tool itself refused to run
// (issue #291: `go doc ./ ledger.Ledger.Checksum`, "too many periods"). It
// runs BEFORE applyProjectGate, so a red project build fails the run whatever
// this pass lifted.
func applyCriterionAudit(ctx context.Context, deps *Deps, verification map[string]any, targets []projectTarget) {
	if verification == nil || asBool(verification["passed"]) {
		return
	}
	for _, t := range targets {
		for _, f := range reconcileCriteria(ctx, t.path, t.base, verification) {
			tag := "not_counted"
			if f.Action != critcheck.NotCounted {
				tag = "repaired"
			}
			deps.Note(ctx, f.Note(), "build", "verify", "criterion_check", tag)
		}
	}
}

// runProjectCheck is the seam tests replace; the default is the real check.
var runProjectCheck = projectcheck.Run

// applyProjectGate runs each target's own build and test commands after the
// verifier has answered, and folds a failure into the verification: passed
// becomes false and the failing command joins criteria_results, so the fix
// cycle sees it and the publish gate holds the pull request.
//
// THE VERIFIER'S VERDICT CANNOT OUTVOTE A RED BUILD. Issue #269 passed every
// criterion the planner wrote and opened PR #278 on code that did not compile
// (see package projectcheck), so the check lives here, in code, outside the
// plan. Gated on strict (SWE_STRICT_BUILD or build_issue) so a plain build
// behaves as on main when the switch is unset.
func applyProjectGate(ctx context.Context, deps *Deps, verification map[string]any, targets []projectTarget, strict bool) {
	if !strict || verification == nil {
		return
	}
	for _, t := range targets {
		o := runProjectCheck(ctx, t.path, t.base)
		where := ""
		if t.name != "" {
			where = " (" + t.name + ")"
		}
		switch {
		case !o.Ran:
			reason := o.Reason
			if len(o.Skipped) > 0 {
				reason = "not installed here: " + strings.Join(o.Skipped, ", ")
			}
			deps.Note(ctx, fmt.Sprintf("Project build check%s did not run: %s", where, reason),
				"build", "verify", "project_check", "skipped")
		case o.PreExisting:
			deps.Note(ctx, fmt.Sprintf(
				"Project build check%s: `%s` fails, and it failed the same way before this run started",
				where, o.FailedCommand), "build", "verify", "project_check", "preexisting")
		case o.Passed:
			deps.Note(ctx, fmt.Sprintf("Project build check%s passed: %s",
				where, strings.Join(o.Commands, " && ")), "build", "verify", "project_check", "passed")
		default:
			deps.Note(ctx, fmt.Sprintf("Project build check%s failed: `%s` exited %d",
				where, o.FailedCommand, o.ExitCode), "build", "verify", "project_check", "failed")
			o.Fold(verification, where)
		}
	}
}

type targetSnapshot struct {
	head            string
	status          string
	gitignoreExists bool
	gitignore       string
}

func snapshotTargets(ctx context.Context, targets []projectTarget) map[string]targetSnapshot {
	out := make(map[string]targetSnapshot, len(targets))
	for _, t := range targets {
		p := strings.TrimSpace(t.path)
		if p == "" {
			continue
		}
		var snap targetSnapshot
		snap.head = headSHA(ctx, p)
		if st := runProc(ctx, p, "git", "status", "--porcelain"); st.ExitCode == 0 {
			snap.status = strings.TrimSpace(st.Stdout)
		}
		if data, err := os.ReadFile(filepath.Join(p, ".gitignore")); err == nil {
			snap.gitignoreExists = true
			snap.gitignore = string(data)
		}
		out[p] = snap
	}
	return out
}

func targetsChanged(ctx context.Context, targets []projectTarget, before map[string]targetSnapshot) bool {
	after := snapshotTargets(ctx, targets)
	if len(after) != len(before) {
		return true
	}
	for p, now := range after {
		prev, ok := before[p]
		if !ok || now != prev {
			return true
		}
	}
	return false
}

// repairPostFinalizeTargets guarantees that the tree handed off to PR creation
// carries only the verified work (and any legitimate removal of a tracked build
// artifact created during this run): if finalization still changed tracked
// content (e.g. committing .gitignore or staging an untracked scratch file),
// any newly added .gitignore patterns are migrated into .git/info/exclude,
// unauthorized tracked changes are dropped, and the repair is recorded in
// verification["summary"] and build notes without failing the build.
func repairPostFinalizeTargets(
	ctx context.Context,
	deps *Deps,
	verification map[string]any,
	targets []projectTarget,
	before map[string]targetSnapshot,
	strict bool,
) {
	if !strict || verification == nil {
		return
	}
	after := snapshotTargets(ctx, targets)
	var notes []string
	for _, t := range targets {
		p := strings.TrimSpace(t.path)
		if p == "" {
			continue
		}
		prev, ok := before[p]
		if !ok || prev.head == "" {
			continue
		}
		now := after[p]
		diffRes := runProc(ctx, p, "git", "diff", "--name-status", "--no-renames", "-z", prev.head, "--")
		var fields []string
		if diffRes.ExitCode == 0 {
			for _, f := range strings.Split(diffRes.Stdout, "\x00") {
				if f != "" {
					fields = append(fields, f)
				}
			}
		}
		if now == prev && len(fields) == 0 {
			continue
		}
		created := make(map[string]bool)
		if base := strings.TrimSpace(t.base); base != "" && base != prev.head {
			if cRes := runProc(ctx, p, "git", "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", base, prev.head); cRes.ExitCode == 0 {
				for _, f := range strings.Split(cRes.Stdout, "\x00") {
					if f != "" {
						created[f] = true
					}
				}
			}
		}
		legitimateDeleted := 0
		seenPath := make(map[string]bool)
		var (
			repairedPaths []string
			checkoutPaths []string
			addedPaths    []string
		)
		for i := 0; i+1 < len(fields); i += 2 {
			status, rel := fields[i], fields[i+1]
			if len(status) > 0 && status[0] == 'D' && created[rel] {
				legitimateDeleted++
				continue
			}
			if !seenPath[rel] {
				seenPath[rel] = true
				repairedPaths = append(repairedPaths, rel)
			}
			if path.Base(rel) == ".gitignore" {
				migrateTargetGitignoreToExclude(ctx, p, prev.head, rel)
			}
			if len(status) > 0 && status[0] == 'A' {
				addedPaths = append(addedPaths, rel)
			} else {
				checkoutPaths = append(checkoutPaths, rel)
			}
		}
		if now.gitignoreExists && (!prev.gitignoreExists || now.gitignore != prev.gitignore) && !seenPath[".gitignore"] {
			seenPath[".gitignore"] = true
			repairedPaths = append(repairedPaths, ".gitignore")
			migrateTargetGitignoreToExclude(ctx, p, prev.head, ".gitignore")
			if !prev.gitignoreExists {
				addedPaths = append(addedPaths, ".gitignore")
			} else {
				checkoutPaths = append(checkoutPaths, ".gitignore")
			}
		}
		if legitimateDeleted > 0 && len(repairedPaths) == 0 {
			excludeHarnessMetadata(ctx, p)
			continue
		}
		if legitimateDeleted == 0 {
			_ = runProc(ctx, p, "git", "reset", "--hard", prev.head)
			for _, rel := range addedPaths {
				_ = os.RemoveAll(filepath.Join(p, filepath.FromSlash(rel)))
			}
			if !prev.gitignoreExists {
				_ = os.Remove(filepath.Join(p, ".gitignore"))
			}
		} else {
			if len(checkoutPaths) > 0 {
				args := append([]string{"checkout", prev.head, "--"}, checkoutPaths...)
				_ = runProc(ctx, p, "git", args...)
			}
			if len(addedPaths) > 0 {
				args := append([]string{"rm", "-f", "-q", "--ignore-unmatch", "--"}, addedPaths...)
				_ = runProc(ctx, p, "git", args...)
				for _, rel := range addedPaths {
					_ = os.RemoveAll(filepath.Join(p, filepath.FromSlash(rel)))
				}
			}
			if !prev.gitignoreExists {
				_ = os.Remove(filepath.Join(p, ".gitignore"))
			}
			if cached := runProc(ctx, p, "git", "diff", "--cached", "--quiet"); cached.ExitCode != 0 {
				commitRes := runProc(ctx, p, "git", "commit", "--no-verify", "-q", "-m", "chore: repair post-finalize tracked changes")
				if commitRes.ExitCode != 0 {
					_ = runProc(ctx, p, "git", "-c", "user.name=SWE-AF", "-c", "user.email=swe-af@agentfield.ai",
						"commit", "--no-verify", "-q", "-m", "chore: repair post-finalize tracked changes")
				}
			}
		}
		excludeHarnessMetadata(ctx, p)

		sort.Strings(repairedPaths)
		where := ""
		if t.name != "" {
			where = " (" + t.name + ")"
		}
		short := prev.head
		if len(short) > 7 {
			short = short[:7]
		}
		msg := fmt.Sprintf("Repaired post-finalize commit%s: restored verified commit %s", where, short)
		if len(repairedPaths) > 0 {
			msg = fmt.Sprintf(
				"Repaired post-finalize tracked changes%s: restored verified commit %s (dropped %s; local ignore patterns kept in .git/info/exclude)",
				where, short, strings.Join(repairedPaths, ", "),
			)
		}
		deps.Note(ctx, msg, "build", "finalize", "repaired")
		notes = append(notes, msg)
	}
	if len(notes) > 0 {
		repairText := strings.Join(notes, "; ")
		prev := strings.TrimSpace(mapStr(verification, "summary", ""))
		if prev != "" {
			verification["summary"] = prev + " (" + repairText + ")"
		} else {
			verification["summary"] = repairText
		}
	}
}

func migrateTargetGitignoreToExclude(ctx context.Context, repo, preHead, relPath string) {
	curBytes, err := os.ReadFile(filepath.Join(repo, filepath.FromSlash(relPath)))
	if err != nil {
		return
	}
	prevLines := make(map[string]bool)
	if show := runProc(ctx, repo, "git", "show", preHead+":"+relPath); show.ExitCode == 0 {
		for _, line := range strings.Split(show.Stdout, "\n") {
			prevLines[strings.TrimSpace(line)] = true
		}
	}
	var added []string
	for _, raw := range strings.Split(string(curBytes), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") || prevLines[line] {
			continue
		}
		prevLines[line] = true
		added = append(added, line)
	}
	if len(added) == 0 {
		return
	}
	excludePath := filepath.Join(repo, ".git", "info", "exclude")
	if gp := runProc(ctx, repo, "git", "rev-parse", "--git-path", "info/exclude"); gp.ExitCode == 0 {
		if trimmed := strings.TrimSpace(gp.Stdout); trimmed != "" {
			if filepath.IsAbs(trimmed) {
				excludePath = trimmed
			} else {
				excludePath = filepath.Join(repo, trimmed)
			}
		}
	}
	if err := os.MkdirAll(filepath.Dir(excludePath), 0o755); err != nil {
		return
	}
	existing, _ := os.ReadFile(excludePath)
	existingLines := make(map[string]bool)
	for _, raw := range strings.Split(string(existing), "\n") {
		existingLines[strings.TrimSpace(raw)] = true
	}
	var toAppend []string
	for _, line := range added {
		if !existingLines[line] {
			existingLines[line] = true
			toAppend = append(toAppend, line)
		}
	}
	if len(toAppend) == 0 {
		return
	}
	var b strings.Builder
	b.Write(existing)
	if len(existing) > 0 && existing[len(existing)-1] != '\n' {
		b.WriteByte('\n')
	}
	for _, line := range toAppend {
		b.WriteString(line)
		b.WriteByte('\n')
	}
	_ = os.WriteFile(excludePath, []byte(b.String()), 0o644)
}

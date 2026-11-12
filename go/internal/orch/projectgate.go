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
// is the tree that passed verification: if finalization still changed tracked
// content (e.g. committing .gitignore or staging an untracked scratch file),
// any newly added .gitignore patterns are migrated into .git/info/exclude,
// HEAD and the working tree are restored to the verified commit, and the
// repair is recorded in verification["summary"] and build notes.
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
		seenPath := make(map[string]bool)
		var (
			repairedPaths []string
			addedPaths    []string
		)
		for i := 0; i+1 < len(fields); i += 2 {
			status, rel := fields[i], fields[i+1]
			if !seenPath[rel] {
				seenPath[rel] = true
				repairedPaths = append(repairedPaths, rel)
			}
			if path.Base(rel) == ".gitignore" {
				migrateTargetGitignoreToExclude(ctx, p, prev.head, rel)
			}
			if len(status) > 0 && status[0] == 'A' {
				addedPaths = append(addedPaths, rel)
			}
		}
		if now.gitignoreExists && (!prev.gitignoreExists || now.gitignore != prev.gitignore) && !seenPath[".gitignore"] {
			seenPath[".gitignore"] = true
			repairedPaths = append(repairedPaths, ".gitignore")
			migrateTargetGitignoreToExclude(ctx, p, prev.head, ".gitignore")
			if !prev.gitignoreExists {
				addedPaths = append(addedPaths, ".gitignore")
			}
		}
		_ = runProc(ctx, p, "git", "reset", "--hard", prev.head)
		for _, rel := range addedPaths {
			_ = os.RemoveAll(filepath.Join(p, filepath.FromSlash(rel)))
		}
		if !prev.gitignoreExists {
			_ = os.Remove(filepath.Join(p, ".gitignore"))
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

// applyFinalTreeScopeCheck re-evaluates runnable git-diff / git-status scope
// criteria against the finalized repository tree so a post-verification
// finalization commit (e.g. adding .gitignore) cannot bypass a file-scope
// criterion even if the verifier's own verdict remained green.
func applyFinalTreeScopeCheck(
	ctx context.Context,
	deps *Deps,
	verification map[string]any,
	planResult map[string]any,
	targets []projectTarget,
	strict bool,
) {
	if !strict || verification == nil {
		return
	}
	results := asMapList(verification["criteria_results"])
	seen := make(map[string]bool, len(results))
	for _, r := range results {
		if crit := strings.TrimSpace(mapStr(r, "criterion", "")); crit != "" {
			seen[crit] = true
		}
	}
	if prd, _ := planResult["prd"].(map[string]any); prd != nil {
		for _, raw := range coerceAnyList(prd["acceptance_criteria"]) {
			crit, _ := raw.(string)
			crit = strings.TrimSpace(crit)
			if crit != "" && !seen[crit] {
				seen[crit] = true
				results = append(results, map[string]any{
					"criterion": crit,
					"passed":    true,
				})
			}
		}
	}

	var failed []string
	for _, t := range targets {
		p := strings.TrimSpace(t.path)
		if p == "" {
			continue
		}
		for _, c := range results {
			crit := strings.TrimSpace(mapStr(c, "criterion", ""))
			if crit == "" {
				continue
			}
			for _, cmd := range scopeCheckCommands(crit) {
				res := runProc(ctx, p, "sh", "-c", cmd)
				if res.ExitCode != 0 {
					c["passed"] = false
					detail := strings.TrimSpace(res.Stdout + "\n" + res.Stderr)
					ev := fmt.Sprintf("Post-finalize scope check `%s` exited %d", cmd, res.ExitCode)
					if detail != "" {
						ev += ": " + detail
					}
					c["evidence"] = ev
					failed = append(failed, fmt.Sprintf("`%s` (exit %d)", cmd, res.ExitCode))
					deps.Note(ctx, fmt.Sprintf("Post-finalize scope check failed: `%s` exited %d", cmd, res.ExitCode),
						"build", "verify", "finalize", "scope_failed")
					break
				}
			}
		}
	}
	if len(failed) > 0 {
		verification["passed"] = false
		raw := make([]any, 0, len(results))
		for _, r := range results {
			raw = append(raw, r)
		}
		verification["criteria_results"] = raw
		msg := "Post-finalize scope check failed: " + strings.Join(failed, "; ")
		prev := strings.TrimSpace(mapStr(verification, "summary", ""))
		if prev != "" {
			verification["summary"] = msg + ". " + prev
		} else {
			verification["summary"] = msg
		}
	}
}

func scopeCheckCommands(criterion string) []string {
	candidates := critcheck.Commands(criterion)
	if len(candidates) == 0 {
		candidates = []string{strings.TrimSpace(criterion)}
	}
	var out []string
	for _, raw := range candidates {
		cmd := strings.TrimSpace(raw)
		if cmd == "" {
			continue
		}
		if !strings.Contains(cmd, "git diff") && !strings.Contains(cmd, "git status") {
			continue
		}
		isAssertion := strings.ContainsAny(cmd, "|;&") ||
			strings.HasPrefix(cmd, "test ") ||
			strings.HasPrefix(cmd, "[ ") ||
			strings.HasPrefix(cmd, "! ") ||
			strings.Contains(cmd, "--exit-code") ||
			strings.Contains(cmd, "--quiet")
		if isAssertion {
			out = append(out, cmd)
		}
	}
	return out
}

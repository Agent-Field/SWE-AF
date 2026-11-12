package orch

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
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

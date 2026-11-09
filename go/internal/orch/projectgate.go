package orch

import (
	"context"
	"fmt"
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

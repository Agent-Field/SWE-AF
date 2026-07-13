package orch

import (
	"context"
	"fmt"
)

// RunCIGate ports app.py:_run_ci_gate (:224-352): watch CI on the freshly-pushed
// PR and fix-repush on failure, up to cfg.max_ci_fix_cycles fix cycles. It
// satisfies the CIGateRunner seam in common.go; the node-wiring wave sets
// deps.CIGate = orch.RunCIGate so build() (and resolve()) drive the gate through
// it.
//
// The loop is bounded by cfg.MaxCIFixCycles+1 watch cycles (range(max+1) in
// Python): each cycle watches once; on a "failed" verdict it invokes the fixer
// and, if the fixer pushed, loops back to watch again. Terminal statuses are
// returned verbatim: passed | no_checks | timed_out | error | failed_exhausted |
// fixer_gave_up | loop_exhausted.
//
// The startup-grace sleep is NOT here: Python sleeps it in resolve() before
// calling _run_ci_gate (app.py:1887), and the Go resolve.go ports that sleep
// at its call site. The gate anchors every watch to req.HeadSHA verbatim —
// including after a fixer push — because Python passes the original head_sha
// on every cycle and never re-anchors to the fixer's commit_sha.
func RunCIGate(ctx context.Context, req CIGateRequest) (map[string]any, error) {
	deps := req.Deps
	cfg := req.Cfg
	headSHA := req.HeadSHA

	attempts := []map[string]any{}
	var lastWatch map[string]any

	for cycle := 0; cycle <= cfg.MaxCIFixCycles; cycle++ {
		deps.Note(ctx, fmt.Sprintf("CI gate: watch cycle %d for PR #%d", cycle+1, req.PRNumber),
			"ci_gate", "watch")
		watch, err := deps.Call(ctx, "run_ci_watcher", map[string]any{
			"repo_path":    req.RepoPath,
			"pr_number":    req.PRNumber,
			"wait_seconds": cfg.CIWaitSeconds,
			"poll_seconds": cfg.CIPollSeconds,
			"head_sha":     headSHA,
		}, "run_ci_watcher")
		if err != nil {
			return nil, err
		}
		lastWatch = watch
		status := mapStr(watch, "status", "error")

		if status == "passed" || status == "no_checks" {
			deps.Note(ctx, fmt.Sprintf("CI gate: %s — PR ready for review", status),
				"ci_gate", "ready")
			final := "no_checks"
			if status == "passed" {
				final = "passed"
			}
			return map[string]any{
				"final_status": final,
				"fix_attempts": attempts,
				"watch":        watch,
			}, nil
		}

		if status == "timed_out" || status == "error" {
			deps.Note(ctx, fmt.Sprintf(
				"CI gate: %s — PR stays open with failing checks. %s",
				status, mapStr(watch, "summary", "")),
				"ci_gate", status)
			return map[string]any{
				"final_status": status,
				"fix_attempts": attempts,
				"watch":        watch,
			}, nil
		}

		// status == "failed"
		if cycle >= cfg.MaxCIFixCycles {
			deps.Note(ctx, fmt.Sprintf(
				"CI gate: exhausted %d fix cycle(s) — PR stays open with failing checks",
				cfg.MaxCIFixCycles),
				"ci_gate", "exhausted")
			return map[string]any{
				"final_status": "failed_exhausted",
				"fix_attempts": attempts,
				"watch":        watch,
			}, nil
		}

		failedChecks := maps0(watch["failed_checks"])
		deps.Note(ctx, fmt.Sprintf(
			"CI gate: fix attempt %d/%d — %d failing check(s)",
			cycle+1, cfg.MaxCIFixCycles, len(failedChecks)),
			"ci_gate", "fix")

		// Model resolution ports app.py:326 exactly:
		// resolved.get("ci_fixer_model", resolved.get("coder_model", "")).
		// dict.get returns the value when the key is present (even ""), so we
		// fall back to coder_model only when ci_fixer_model is ABSENT.
		model, ok := req.Resolved["ci_fixer_model"]
		if !ok {
			model = req.Resolved["coder_model"]
		}

		fix, err := deps.Call(ctx, "run_ci_fixer", map[string]any{
			"repo_path":          req.RepoPath,
			"pr_number":          req.PRNumber,
			"pr_url":             req.PRURL,
			"integration_branch": req.IntegrationBranch,
			"base_branch":        req.BaseBranch,
			"failed_checks":      failedChecks,
			"iteration":          cycle + 1,
			"max_iterations":     cfg.MaxCIFixCycles,
			"goal":               req.Goal,
			"completed_issues":   req.CompletedIssues,
			"previous_attempts":  attempts,
			"model":              model,
			"permission_mode":    cfg.PermissionMode,
			"ai_provider":        cfg.AIProvider(),
		}, "run_ci_fixer")
		if err != nil {
			return nil, err
		}
		attempts = append(attempts, fix)

		if !asBool(fix["pushed"]) {
			deps.Note(ctx, fmt.Sprintf(
				"CI gate: fixer did not push (%s) — PR stays open with failing checks",
				mapStr(fix, "summary", "no summary")),
				"ci_gate", "fixer_no_push")
			return map[string]any{
				"final_status": "fixer_gave_up",
				"fix_attempts": attempts,
				"watch":        watch,
			}, nil
		}

		// Pushed — loop back and watch again with the ORIGINAL head_sha anchor
		// (Python passes head_sha=head_sha on every cycle and never re-anchors
		// to the fixer's commit). GitHub may take a moment to register the new
		// run; the watcher's poll covers that.
	}

	// Loop fell through (shouldn't happen because the failed branch returns).
	watch := lastWatch
	if watch == nil {
		watch = map[string]any{}
	}
	return map[string]any{
		"final_status": "loop_exhausted",
		"fix_attempts": attempts,
		"watch":        watch,
	}, nil
}

// Compile-time assertion that RunCIGate satisfies the CIGateRunner seam.
var _ CIGateRunner = RunCIGate
